"""
دستیار اینستاگرام: جواب دایرکت (همان مغز دستیار سایت، با جستجوی واقعی محصولات)
و جواب کامنت‌ها (پاسخ عمومی کوتاه + دایرکت خصوصی وقتی کامنت سؤاله).

وب‌هوک‌های Meta همیشه با X-Hub-Signature-256 امضا می‌شن؛ بدون INSTAGRAM_APP_SECRET هیچ‌چیز پذیرفته نمی‌شه.
توکن اینستاگرام (Meta) با GAPGPT_API_KEY هیچ ربطی نداره؛ GapGPT فقط متن جواب‌ها رو می‌نویسه.
"""
import hashlib
import hmac
import logging
import re
from datetime import datetime, timedelta

import httpx
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import InstagramEvent, InstagramMessage
from app.services.ai_assistant import run_assistant

logger = logging.getLogger(__name__)

HISTORY_LIMIT = 10
MAX_USER_MSGS_PER_HOUR = 30
CAROUSEL_MAX = 5
TIMEOUT = 15.0

DM_EXTRA_SYSTEM = """\
الان داری توی دایرکت اینستاگرام جواب می‌دی (نه توی سایت).
- متن ساده بنویس: بدون مارک‌داون، بدون ستاره و عنوان؛ کوتاه (حداکثر ۴-۵ خط) و با چند اموجی ملایم.
- کارت محصول‌ها خودکار با دکمه‌ی «مشاهده و خرید» زیر پیامت فرستاده می‌شه؛ مشخصات رو تکرار نکن.
- ثبت سفارش و پرداخت توی دایرکت انجام نمی‌شه: وقتی مشتری تصمیم گرفت، بگو روی دکمه‌ی «مشاهده و خرید» بزنه، رنگ و سایز رو انتخاب کنه و از سایت سفارش بده. هیچ‌وقت نگو سفارشش ثبت شد.
- به ابزار add_to_cart نیازی نیست (مشتری اینستاگرام وارد حساب سایت نیست).
"""

QUESTION_RE = re.compile(r"[?؟]|قیمت|چنده|چند|سایز|موجود|رنگ|دارید|دارین|سفارش|ارسال|خرید|پیام|دایرکت|تخفیف|کجا")
HANDOFF_RE = re.compile(r"اپراتور|پشتیبان|انسان|مدیر|شکایت")


# ---------------------------------------------------------------- امنیت وب‌هوک
def verify_signature(raw_body: bytes, header: str | None) -> bool:
    secret = settings.INSTAGRAM_APP_SECRET
    if not secret or not header:
        return False
    expected = "sha256=" + hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.strip())


# ---------------------------------------------------------------- Graph API
def _graph(path: str, payload: dict) -> bool:
    if not settings.INSTAGRAM_ACCESS_TOKEN:
        logger.warning("instagram: no access token configured")
        return False
    url = f"{settings.INSTAGRAM_GRAPH_URL.rstrip('/')}/{path.lstrip('/')}"
    try:
        r = httpx.post(url, json=payload, headers={"Authorization": f"Bearer {settings.INSTAGRAM_ACCESS_TOKEN}"}, timeout=TIMEOUT)
        if r.status_code >= 400:
            logger.error("instagram graph %s -> %s %s", path, r.status_code, r.text[:400])
            return False
        return True
    except httpx.HTTPError as exc:
        logger.error("instagram graph %s failed: %r", path, exc)
        return False


def send_text(recipient: dict, text: str) -> bool:
    """recipient: {"id": igsid} or {"comment_id": id} (private reply)."""
    return _graph("me/messages", {"recipient": recipient, "message": {"text": text[:1000]}})


def _product_url(p: dict) -> str:
    return f"{settings.SITE_URL.rstrip('/')}/products/{p['slug']}"


def _price_line(p: dict) -> str:
    price = f"{p['final_price']:,} تومان"
    if p.get("on_sale") and p.get("discount_percent"):
        return f"{price} ({p['discount_percent']}٪ تخفیف)"
    return price


def send_carousel(igsid: str, products: list[dict]) -> bool:
    elements = []
    for p in products[:CAROUSEL_MAX]:
        url = _product_url(p)
        el = {
            "title": p["name"][:80],
            "subtitle": _price_line(p)[:80],
            "default_action": {"type": "web_url", "url": url},
            "buttons": [{"type": "web_url", "url": url, "title": "مشاهده و خرید"}],
        }
        if p.get("image"):
            el["image_url"] = p["image"]
        elements.append(el)
    if not elements:
        return True
    ok = _graph("me/messages", {"recipient": {"id": igsid}, "message": {"attachment": {
        "type": "template", "payload": {"template_type": "generic", "elements": elements}}}})
    if not ok:  # مثلاً فرمت عکس پذیرفته نشد: لینک متنی بفرست تا مشتری دست‌خالی نمونه
        lines = [f"• {p['name']} — {_price_line(p)}\n{_product_url(p)}" for p in products[:3]]
        ok = send_text({"id": igsid}, "\n\n".join(lines))
    return ok


def reply_to_comment(comment_id: str, text: str) -> bool:
    return _graph(f"{comment_id}/replies", {"message": text[:300]})


# ---------------------------------------------------------------- کمکی‌ها
def clean(text: str) -> str:
    text = re.sub(r"\*\*?|__|`+|^#+\s*", "", text, flags=re.M)
    return text.strip()


def claim_event(db: Session, event_id: str, kind: str) -> bool:
    """True اگه این رویداد قبلاً پردازش نشده؛ Meta گاهی یک رویداد رو چند بار می‌فرسته."""
    try:
        db.add(InstagramEvent(event_id=event_id, kind=kind))
        db.commit()
        return True
    except IntegrityError:
        db.rollback()
        return False


def _history(db: Session, igsid: str) -> list[dict]:
    rows = (db.query(InstagramMessage).filter(InstagramMessage.ig_user_id == igsid)
            .order_by(InstagramMessage.id.desc()).limit(HISTORY_LIMIT).all())
    return [{"role": r.role, "content": r.text} for r in reversed(rows)]


def _remember(db: Session, igsid: str, role: str, text: str) -> None:
    db.add(InstagramMessage(ig_user_id=igsid, role=role, text=text[:2000]))
    db.commit()


def _too_chatty(db: Session, igsid: str) -> bool:
    since = datetime.utcnow() - timedelta(hours=1)
    n = (db.query(func.count(InstagramMessage.id))
         .filter(InstagramMessage.ig_user_id == igsid, InstagramMessage.role == "user",
                 InstagramMessage.created_at >= since).scalar())
    return (n or 0) >= MAX_USER_MSGS_PER_HOUR


def _answer(db: Session, igsid: str, text: str) -> dict:
    messages = _history(db, igsid) + [{"role": "user", "content": text}]
    # پیام‌های پشت‌سرهم یک نقش باید ادغام بشن تا API مدل خطا نده
    merged: list[dict] = []
    for m in messages:
        if merged and merged[-1]["role"] == m["role"]:
            merged[-1]["content"] += "\n" + m["content"]
        else:
            merged.append(dict(m))
    if merged[0]["role"] != "user":
        merged = merged[1:]
    return run_assistant(db, None, merged, extra_system=DM_EXTRA_SYSTEM)


# ---------------------------------------------------------------- دایرکت
def handle_dm(db: Session, sender_id: str, text: str, mid: str | None) -> None:
    if mid and not claim_event(db, f"dm:{mid}", "dm"):
        return
    text = (text or "").strip()
    if not text:
        return
    if HANDOFF_RE.search(text):
        send_text({"id": sender_id}, f"حتماً 🙏 همکارام به‌زودی جواب می‌دن. اگه عجله داری از اینجا هم می‌تونی پیام بدی: {settings.SITE_URL}/contact")
        return
    if _too_chatty(db, sender_id):
        return
    _remember(db, sender_id, "user", text)
    result = _answer(db, sender_id, text)
    reply = clean(result.get("reply") or "") or "یه لحظه بیشتر توضیح می‌دی؟ 🌸"
    _remember(db, sender_id, "assistant", reply)
    send_text({"id": sender_id}, reply)
    products = result.get("products") or []
    if products:
        send_carousel(sender_id, products)


# ---------------------------------------------------------------- کامنت
def _llm_one_liner(system: str, user: str) -> str | None:
    if not settings.GAPGPT_API_KEY:
        return None
    try:
        r = httpx.post(
            settings.gapgpt_chat_url,
            headers={"Authorization": f"Bearer {settings.GAPGPT_API_KEY}"},
            json={"model": settings.ANALYTICS_AI_MODEL or settings.GAPGPT_MODEL, "temperature": 0.7, "max_tokens": 120,
                  "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]},
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        out = clean(r.json()["choices"][0]["message"]["content"])
        return out[:200] or None
    except Exception as exc:  # noqa: BLE001
        logger.warning("instagram: comment AI unavailable: %r", exc)
        return None


COMMENT_SYSTEM = (
    "تو مدیر شبکه‌های اجتماعی فروشگاه اسلیپر پاز (دمپایی، کفش، کیف) هستی. به کامنت زیر یک جواب خیلی کوتاه، "
    "گرم و خودمونی فارسی بنویس (حداکثر ۱۵ کلمه، حداکثر یک اموجی). قیمت و موجودی از خودت نساز. "
    "{dm_hint} فقط متن جواب رو بنویس."
)


def handle_comment(db: Session, value: dict) -> None:
    cid = str(value.get("id") or "")
    text = (value.get("text") or "").strip()
    author = value.get("from") or {}
    author_id = str(author.get("id") or "")
    if not cid or not text or len(text) < 2:
        return
    if author_id and author_id == settings.INSTAGRAM_ACCOUNT_ID:
        return  # کامنت خودمون
    if value.get("parent_id"):
        return  # ریپلای زیر کامنت؛ زنجیره‌ی بی‌پایان درست نکن
    if not claim_event(db, f"comment:{cid}", "comment"):
        return

    is_question = bool(QUESTION_RE.search(text))
    dm_hint = "به مشتری بگو جواب کاملش رو توی دایرکت فرستادی." if is_question else "فقط تشکر یا واکنش دوستانه."
    public = _llm_one_liner(COMMENT_SYSTEM.format(dm_hint=dm_hint), text) or (
        "سلام! جواب رو برات دایرکت فرستادم 🌸" if is_question else "ممنون از لطفت 🌸")
    reply_to_comment(cid, public)

    if is_question:
        key = author_id or f"c{cid}"
        _remember(db, key, "user", text)
        result = _answer(db, key, text)
        reply = clean(result.get("reply") or "")
        products = result.get("products") or []
        if reply:
            if products:
                reply += f"\n\nمشاهده و خرید: {_product_url(products[0])}"
            _remember(db, key, "assistant", reply)
            send_text({"comment_id": cid}, reply)


# ---------------------------------------------------------------- ورودی وب‌هوک
def process_payload(db: Session, payload: dict) -> None:
    own = settings.INSTAGRAM_ACCOUNT_ID
    for entry in payload.get("entry", []):
        for ev in entry.get("messaging", []) or []:
            msg = ev.get("message") or {}
            sender = str((ev.get("sender") or {}).get("id") or "")
            if not sender or sender == own or msg.get("is_echo"):
                continue
            if msg.get("text"):
                try:
                    handle_dm(db, sender, msg["text"], msg.get("mid"))
                except Exception:  # noqa: BLE001 - یک پیام خراب نباید بقیه رو بخوابونه
                    logger.exception("instagram: dm handling failed")
                    db.rollback()
        if not settings.INSTAGRAM_COMMENT_REPLIES:
            continue
        for ch in entry.get("changes", []) or []:
            if ch.get("field") == "comments":
                try:
                    handle_comment(db, ch.get("value") or {})
                except Exception:  # noqa: BLE001
                    logger.exception("instagram: comment handling failed")
                    db.rollback()
