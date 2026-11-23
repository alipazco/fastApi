"""
آمار و تحلیل فروش برای داشبورد ادمین.

همه‌ی محاسبه‌ها در پایتون روی ردیف‌های بازه انجام می‌شه (حجم فروشگاه کوچکه و
به این شکل روی MySQL و sqlite یکسان کار می‌کنه). مبلغ‌ها همه تومان‌ان.

«فروش واقعی» = سفارشی که پرداخت شده (payment_status=1) و لغو/مرجوع نشده (۶ و ۷).
زمان‌ها در دیتابیس UTC هستن و برای گروه‌بندی روزانه/ساعتی به وقت ایران (UTC+3:30) تبدیل می‌شن.
"""
import json
import logging
import time
from collections import defaultdict
from datetime import datetime, timedelta

import httpx
import jdatetime
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import Coupon, Order, OrderItems, Product, ProductColor, ProductSize, Transaction, User
from app.models.tryon import TryOnUsage
from app.services.storage import image_url
from app.utils.jalali import _MONTHS

logger = logging.getLogger(__name__)

IRAN_OFFSET = timedelta(hours=3, minutes=30)
CANCELLED, RETURNED = 6, 7
STATUS_LABELS = {
    0: "در انتظار پرداخت", 1: "پرداخت‌شده", 2: "در حال پردازش", 3: "آماده ارسال",
    4: "ارسال‌شده", 5: "تحویل‌شده", 6: "لغو‌شده", 7: "مرجوعی",
}
WEEKDAYS = ["شنبه", "یکشنبه", "دوشنبه", "سه‌شنبه", "چهارشنبه", "پنجشنبه", "جمعه"]
LOW_STOCK_THRESHOLD = 5


def _iran(dt: datetime) -> datetime:
    return dt + IRAN_OFFSET


def _jlabel(d) -> str:
    j = jdatetime.date.fromgregorian(date=d)
    return f"{j.day} {_MONTHS[j.month - 1]}"


def _is_sale(o: Order) -> bool:
    return o.payment_status == 1 and o.order_status not in (CANCELLED, RETURNED)


def _pct_change(cur: float, prev: float) -> float | None:
    if not prev:
        return None
    return round((cur - prev) / prev * 100, 1)


def _kpi(cur: float, prev: float) -> dict:
    return {"value": cur, "previous": prev, "change": _pct_change(cur, prev)}


def _product_stock(p: Product, sizes_by_product: dict[int, int]) -> int:
    # اگه سایزبندی دارن، موجودی واقعی جمع سایزهاست؛ وگرنه فیلد quantity خود محصول
    return sizes_by_product[p.id] if p.id in sizes_by_product else (p.quantity or 0)


def build_overview(db: Session, days: int = 30) -> dict:
    days = max(1, min(days, 365))
    now = datetime.utcnow()
    start = now - timedelta(days=days)
    prev_start = start - timedelta(days=days)

    orders_all = (
        db.query(Order)
        .filter(Order.deleted_at.is_(None), Order.created_at >= prev_start)
        .all()
    )
    cur_orders = [o for o in orders_all if o.created_at >= start]
    prev_orders = [o for o in orders_all if o.created_at < start]
    cur_sales = [o for o in cur_orders if _is_sale(o)]
    prev_sales = [o for o in prev_orders if _is_sale(o)]

    sale_ids = {o.id for o in cur_sales}
    prev_sale_ids = {o.id for o in prev_sales}
    items = (
        db.query(OrderItems)
        .filter(OrderItems.deleted_at.is_(None), OrderItems.order_id.in_(sale_ids | prev_sale_ids))
        .all()
        if (sale_ids or prev_sale_ids) else []
    )
    cur_items = [i for i in items if i.order_id in sale_ids]
    prev_items = [i for i in items if i.order_id in prev_sale_ids]

    revenue = sum(o.paying_amount for o in cur_sales)
    prev_revenue = sum(o.paying_amount for o in prev_sales)
    n_orders, prev_n = len(cur_sales), len(prev_sales)

    new_users = db.query(User).filter(
        User.deleted_at.is_(None), User.is_admin == 0, User.created_at >= start,
        User.email.isnot(None) | User.cellphone.isnot(None),
    ).count()
    prev_new_users = db.query(User).filter(
        User.deleted_at.is_(None), User.is_admin == 0,
        User.created_at >= prev_start, User.created_at < start,
        User.email.isnot(None) | User.cellphone.isnot(None),
    ).count()

    buyers = {o.user_id for o in cur_sales}
    # مشتری تکراری = کسی که بازه‌ی قبل از این هم خرید موفق داشته
    all_before = {
        r[0] for r in db.query(Order.user_id).filter(
            Order.deleted_at.is_(None), Order.payment_status == 1, Order.created_at < start
        ).distinct().all()
    }
    returning = len(buyers & all_before)

    kpis = {
        "revenue": _kpi(revenue, prev_revenue),
        "orders": _kpi(n_orders, prev_n),
        "avg_order": _kpi(round(revenue / n_orders) if n_orders else 0,
                          round(prev_revenue / prev_n) if prev_n else 0),
        "units": _kpi(sum(i.quantity for i in cur_items), sum(i.quantity for i in prev_items)),
        "new_users": _kpi(new_users, prev_new_users),
        "buyers": {"value": len(buyers), "returning": returning,
                   "returning_rate": round(returning / len(buyers) * 100, 1) if buyers else None},
    }

    # ----- روند روزانه (به وقت ایران، صفر پر شده) -----
    today = _iran(now).date()
    day_list = [today - timedelta(days=i) for i in range(days - 1, -1, -1)]
    by_day = {d: {"online": 0, "pos": 0, "orders": 0} for d in day_list}
    for o in cur_sales:
        d = _iran(o.created_at).date()
        if d in by_day:
            ch = "pos" if o.sales_channel == "pos" else "online"
            by_day[d][ch] += o.paying_amount
            by_day[d]["orders"] += 1
    series = [{"date": d.isoformat(), "label": _jlabel(d), **v, "total": v["online"] + v["pos"]}
              for d, v in by_day.items()]

    # ----- کانال فروش -----
    channels = {"online": {"revenue": 0, "orders": 0}, "pos": {"revenue": 0, "orders": 0}}
    for o in cur_sales:
        ch = channels["pos" if o.sales_channel == "pos" else "online"]
        ch["revenue"] += o.paying_amount
        ch["orders"] += 1

    # ----- وضعیت سفارش‌ها (همه‌ی سفارش‌های بازه) -----
    status_count: dict[int, int] = defaultdict(int)
    for o in cur_orders:
        status_count[o.order_status] += 1
    funnel = [{"status": s, "label": STATUS_LABELS.get(s, str(s)), "count": c}
              for s, c in sorted(status_count.items())]

    # ----- پرداخت نیمه‌کاره: ثبت شده ولی بعد از ۳۰ دقیقه هنوز پرداخت نشده -----
    cutoff = now - timedelta(minutes=30)
    abandoned = [o for o in cur_orders
                 if o.payment_status == 0 and o.order_status == 0 and o.created_at <= cutoff
                 and o.sales_channel != "pos"]
    abandoned_info = {"count": len(abandoned), "value": sum(o.paying_amount for o in abandoned)}

    # ----- محصولات -----
    products = {p.id: p for p in db.query(Product).filter(Product.deleted_at.is_(None)).all()}
    agg: dict[int, dict] = defaultdict(lambda: {"units": 0, "revenue": 0})
    for i in cur_items:
        agg[i.product_id]["units"] += i.quantity
        agg[i.product_id]["revenue"] += i.subtotal

    size_rows = (
        db.query(ProductColor.product_id, ProductSize.quantity)
        .join(ProductSize, ProductSize.product_color_id == ProductColor.id)
        .filter(ProductColor.deleted_at.is_(None), ProductSize.deleted_at.is_(None))
        .all()
    )
    sizes_by_product: dict[int, int] = defaultdict(int)
    for pid, q in size_rows:
        sizes_by_product[pid] += q or 0
    sizes_by_product = dict(sizes_by_product)

    def prod_row(pid: int, v: dict) -> dict:
        p = products.get(pid)
        return {
            "id": pid, "name": p.name if p else f"#{pid}",
            "image": image_url(p.primary_image, "products") if p else None,
            "units": v["units"], "revenue": v["revenue"],
            "stock": _product_stock(p, sizes_by_product) if p else None,
        }

    top_products = [prod_row(pid, v) for pid, v in
                    sorted(agg.items(), key=lambda kv: (-kv[1]["revenue"], -kv[1]["units"]))[:8]]

    stock_rows = []
    for p in products.values():
        if p.status != 1:
            continue
        stock_rows.append((p, _product_stock(p, sizes_by_product)))
    low_stock = [
        {"id": p.id, "name": p.name, "image": image_url(p.primary_image, "products"), "stock": s,
         "units_sold": agg.get(p.id, {}).get("units", 0)}
        for p, s in sorted(stock_rows, key=lambda t: t[1]) if s <= LOW_STOCK_THRESHOLD
    ][:8]
    # پرفروش‌های رو‌به‌اتمام اول بیان
    low_stock.sort(key=lambda r: (r["stock"], -r["units_sold"]))
    slow_movers = [
        {"id": p.id, "name": p.name, "image": image_url(p.primary_image, "products"), "stock": s}
        for p, s in sorted(stock_rows, key=lambda t: -t[1])
        if s > LOW_STOCK_THRESHOLD and p.id not in agg
    ][:8]

    # ----- دسته‌بندی -----
    cat_rev: dict[int, int] = defaultdict(int)
    for pid, v in agg.items():
        p = products.get(pid)
        if p:
            cat_rev[p.category_id] += v["revenue"]
    cat_names = {}
    if cat_rev:
        from app.models import Category
        cat_names = {c.id: c.name for c in db.query(Category).filter(Category.id.in_(cat_rev)).all()}
    categories = [{"name": cat_names.get(cid, f"#{cid}"), "revenue": r}
                  for cid, r in sorted(cat_rev.items(), key=lambda kv: -kv[1])[:6]]

    # ----- مشتری‌های برتر -----
    spend: dict[int, dict] = defaultdict(lambda: {"revenue": 0, "orders": 0})
    for o in cur_sales:
        spend[o.user_id]["revenue"] += o.paying_amount
        spend[o.user_id]["orders"] += 1
    top_ids = [uid for uid, _ in sorted(spend.items(), key=lambda kv: -kv[1]["revenue"])[:6]]
    users = {u.id: u for u in db.query(User).filter(User.id.in_(top_ids)).all()} if top_ids else {}
    top_customers = []
    for uid in top_ids:
        u = users.get(uid)
        cell = (u.cellphone or "") if u else ""
        walkin = bool(u and u.email == "walkin@instore.local")
        top_customers.append({
            "id": uid, "name": ("مشتری حضوری (بدون ثبت‌نام)" if walkin else (u.name if u and u.name else "بدون نام")),
            "cellphone": cell if not walkin else "", **spend[uid],
        })

    # ----- زمان‌های پرفروش (روز هفته × ساعت) -----
    heat = [[0] * 24 for _ in range(7)]
    for o in cur_sales:
        t = _iran(o.created_at)
        heat[(t.weekday() + 2) % 7][t.hour] += 1  # شنبه=۰ (Python: دوشنبه=۰)
    best_dow = max(range(7), key=lambda d: sum(heat[d])) if n_orders else None
    hours = [sum(heat[d][h] for d in range(7)) for h in range(24)]
    best_hour = max(range(24), key=lambda h: hours[h]) if n_orders else None

    # ----- کوپن -----
    coupon_orders = [o for o in cur_sales if o.coupon_id]
    coupon_info = {"orders": len(coupon_orders), "discount": sum(o.coupon_amount or 0 for o in coupon_orders)}

    # ----- پروب مجازی و اثرش روی خرید -----
    tryon_rows = db.query(TryOnUsage).filter(TryOnUsage.created_at >= start).all()
    tryon_users = {r.user_id for r in tryon_rows}
    tryon_info = {"tries": len(tryon_rows), "users": len(tryon_users),
                  "bought": len(tryon_users & buyers)}

    return {
        "days": days,
        "generated_at": now.isoformat(),
        "kpis": kpis,
        "series": series,
        "channels": channels,
        "funnel": funnel,
        "abandoned": abandoned_info,
        "top_products": top_products,
        "low_stock": low_stock,
        "slow_movers": slow_movers,
        "categories": categories,
        "top_customers": top_customers,
        "heatmap": {"weekdays": WEEKDAYS, "data": heat},
        "best": {"weekday": WEEKDAYS[best_dow] if best_dow is not None else None, "hour": best_hour},
        "coupons": coupon_info,
        "tryon": tryon_info,
    }


# ------------------------------------------------------------------
# تحلیل هوشمند
# ------------------------------------------------------------------
def _fmt(n: float) -> str:
    return f"{int(round(n)):,}"


def rule_based_insights(ov: dict) -> list[dict]:
    """تحلیل بدون هوش مصنوعی؛ همیشه کار می‌کنه و جایگزین وقتی AI در دسترس نیست."""
    out: list[dict] = []
    k = ov["kpis"]
    rev = k["revenue"]
    if rev["change"] is not None:
        if rev["change"] >= 10:
            out.append({"type": "good", "title": f"فروش {rev['change']:g}٪ رشد کرده",
                        "body": f"فروش {ov['days']} روز اخیر {_fmt(rev['value'])} تومانه؛ نسبت به دوره‌ی قبل بهتر شده. کانال یا محصولی که باعثش شده رو تقویت کن."})
        elif rev["change"] <= -10:
            out.append({"type": "warn", "title": f"فروش {abs(rev['change']):g}٪ افت کرده",
                        "body": "نسبت به دوره‌ی قبل کمتر فروختی. یک کوپن کوتاه‌مدت یا استوری محصولات پرفروش می‌تونه جبران کنه."})
    if ov["abandoned"]["count"]:
        a = ov["abandoned"]
        out.append({"type": "warn", "title": f"{a['count']} پرداخت نیمه‌کاره ({_fmt(a['value'])} تومان)",
                    "body": "این مشتری‌ها تا مرحله‌ی پرداخت اومدن ولی تکمیل نکردن. یک پیامک یادآوری یا کد تخفیف کوچک بفرست."})
    if ov["low_stock"]:
        names = "، ".join(p["name"] for p in ov["low_stock"][:3])
        hot = [p for p in ov["low_stock"] if p["units_sold"] > 0]
        out.append({"type": "warn", "title": f"{len(ov['low_stock'])} محصول در حال تمام شدن",
                    "body": f"{names}" + (" — بعضی‌هاشون در همین بازه فروش داشتن، اولویت تامین با این‌هاست." if hot else "")})
    if ov["slow_movers"]:
        names = "، ".join(p["name"] for p in ov["slow_movers"][:3])
        out.append({"type": "info", "title": "محصولات موجود ولی بدون فروش",
                    "body": f"{names} در این بازه فروش نداشتن. در استوری/اسلایدر معرفی‌شون کن یا تخفیف بده."})
    b = ov["kpis"]["buyers"]
    if b["returning_rate"] is not None:
        out.append({"type": "info", "title": f"{b['returning_rate']}٪ خریدارها مشتری قبلی هستن",
                    "body": "برای بالا بردن این عدد، باشگاه مشتریان یا پیامک پیشنهاد بعد از خرید بزن."})
    if ov["best"]["weekday"] is not None:
        out.append({"type": "info", "title": f"پرفروش‌ترین زمان: {ov['best']['weekday']}، حوالی ساعت {ov['best']['hour']}",
                    "body": "استوری و پست‌های تبلیغاتی رو کمی قبل از این زمان منتشر کن."})
    t = ov["tryon"]
    if t["tries"]:
        out.append({"type": "info", "title": f"{t['users']} نفر پروب مجازی رو امتحان کردن",
                    "body": f"{t['bought']} نفر از اون‌ها در همین بازه خرید کردن."})
    ch = ov["channels"]
    if ch["pos"]["orders"] and ch["online"]["orders"]:
        share = round(ch["pos"]["revenue"] / max(1, ch["pos"]["revenue"] + ch["online"]["revenue"]) * 100)
        out.append({"type": "info", "title": f"سهم فروش حضوری: {share}٪",
                    "body": "فروش حضوری و آنلاین رو با هم ببین؛ محصول پرفروش هر کانال رو توی کانال دیگه هم جلو بنداز."})
    if not out:
        out.append({"type": "info", "title": "هنوز داده‌ی کافی نیست",
                    "body": "بعد از چند سفارش، اینجا پیشنهادهای عملی نمایش داده می‌شه."})
    return out[:7]


_AI_CACHE: dict[int, tuple[float, dict]] = {}
AI_CACHE_SECONDS = 600
AI_PROMPT = """\
تو تحلیل‌گر فروش فروشگاه اسلیپر پاز (دمپایی، کفش، کیف) هستی. خلاصه‌ی آمار فروش رو می‌گیری و برای مدیر فروشگاه
۴ تا ۶ بینش کوتاه و عملی به فارسی روان می‌نویسی. فقط بر اساس اعداد داده‌شده حرف بزن و عدد از خودت نساز.
هر بینش باید یک اقدام مشخص پیشنهاد بده (مثلاً کدوم محصول رو تبلیغ کنه، چی رو تامین کنه، چه کوپنی بده).
خروجی فقط JSON معتبر به این شکل، بدون متن اضافه:
{"insights":[{"type":"good|warn|info","title":"حداکثر ۶۰ حرف","body":"حداکثر ۲۲۰ حرف"}]}
"""


def _compact(ov: dict) -> dict:
    k = ov["kpis"]
    return {
        "days": ov["days"],
        "revenue": k["revenue"], "orders": k["orders"], "avg_order": k["avg_order"],
        "new_users": k["new_users"], "buyers": k["buyers"],
        "channels": ov["channels"], "abandoned": ov["abandoned"],
        "top_products": [{"name": p["name"], "units": p["units"], "revenue": p["revenue"], "stock": p["stock"]}
                         for p in ov["top_products"][:5]],
        "low_stock": [{"name": p["name"], "stock": p["stock"], "sold": p["units_sold"]} for p in ov["low_stock"][:5]],
        "slow_movers": [p["name"] for p in ov["slow_movers"][:5]],
        "categories": ov["categories"], "best": ov["best"], "coupons": ov["coupons"], "tryon": ov["tryon"],
        "last_days": [{"d": s["label"], "t": s["total"]} for s in ov["series"][-14:]],
    }


def _ask_ai(ov: dict) -> list[dict]:
    if not settings.GAPGPT_API_KEY:
        raise RuntimeError("no GAPGPT_API_KEY")
    resp = httpx.post(
        "https://api.gapgpt.app/v1/chat/completions",
        headers={"Authorization": f"Bearer {settings.GAPGPT_API_KEY}"},
        json={
            "model": settings.ANALYTICS_AI_MODEL or settings.GAPGPT_MODEL,
            "temperature": 0.3,
            "max_tokens": 700,
            "messages": [
                {"role": "system", "content": AI_PROMPT},
                {"role": "user", "content": json.dumps(_compact(ov), ensure_ascii=False)},
            ],
        },
        timeout=25.0,
    )
    resp.raise_for_status()
    text = resp.json()["choices"][0]["message"]["content"].strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("{"):]
    data = json.loads(text[text.find("{"): text.rfind("}") + 1])
    out = []
    for it in data.get("insights", [])[:6]:
        title, body = str(it.get("title", "")).strip(), str(it.get("body", "")).strip()
        if title and body:
            out.append({"type": it.get("type") if it.get("type") in ("good", "warn", "info") else "info",
                        "title": title[:90], "body": body[:320]})
    if not out:
        raise ValueError("empty insights")
    return out


def build_insights(db: Session, days: int = 30, refresh: bool = False) -> dict:
    days = max(1, min(days, 365))
    cached = _AI_CACHE.get(days)
    if cached and not refresh and time.time() - cached[0] < AI_CACHE_SECONDS:
        return cached[1]
    ov = build_overview(db, days)
    source = "rules"
    try:
        insights = _ask_ai(ov)
        source = "ai"
    except Exception as exc:  # noqa: BLE001 - هر خطای AI نباید داشبورد رو بشکنه
        logger.warning("analytics AI unavailable, using rules: %r", exc)
        insights = rule_based_insights(ov)
    result = {"source": source, "days": days, "insights": insights,
              "generated_at": datetime.utcnow().isoformat()}
    if source == "ai":  # جواب قاعده‌محور رو کش نمی‌کنیم تا با برگشتن AI زود جایگزین بشه
        _AI_CACHE[days] = (time.time(), result)
    return result
