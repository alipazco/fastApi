import json
import logging

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request, Response
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.api.deps import get_current_admin
from app.core.config import settings
from app.db.session import SessionLocal, get_db
from app.models import InstagramMessage, User
from app.services import instagram
from app.utils.response import success_response

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/instagram", tags=["instagram"])
admin_router = APIRouter(prefix="/admin-panel/instagram", tags=["admin-instagram"])


@router.get("/webhook")
def verify(
    mode: str = Query("", alias="hub.mode"),
    token: str = Query("", alias="hub.verify_token"),
    challenge: str = Query("", alias="hub.challenge"),
):
    """Meta calls this once when the webhook is saved in the app dashboard."""
    if settings.INSTAGRAM_VERIFY_TOKEN and mode == "subscribe" and token == settings.INSTAGRAM_VERIFY_TOKEN:
        return Response(content=challenge, media_type="text/plain")
    return Response(status_code=403)


def _process(payload: dict) -> None:
    db = SessionLocal()
    try:
        instagram.process_payload(db, payload)
    finally:
        db.close()


@router.post("/webhook")
async def receive(request: Request, background: BackgroundTasks):
    """Must answer 200 fast; the real work (AI call + replies) runs in the background."""
    if not settings.INSTAGRAM_ENABLED:
        return Response(status_code=200)
    raw = await request.body()
    if not instagram.verify_signature(raw, request.headers.get("x-hub-signature-256")):
        return Response(status_code=403)
    try:
        payload = json.loads(raw)
    except ValueError:
        return Response(status_code=400)
    background.add_task(_process, payload)
    return Response(status_code=200)


@admin_router.get("/status")
def status(db: Session = Depends(get_db), _: User = Depends(get_current_admin)):
    """Setup checklist for the admin panel (never returns secrets)."""
    from datetime import datetime, timedelta
    since = datetime.utcnow() - timedelta(days=1)
    msgs = db.query(func.count(InstagramMessage.id)).filter(
        InstagramMessage.role == "user", InstagramMessage.created_at >= since).scalar() or 0
    return success_response({
        "enabled": settings.INSTAGRAM_ENABLED,
        "has_token": bool(settings.INSTAGRAM_ACCESS_TOKEN),
        "has_account_id": bool(settings.INSTAGRAM_ACCOUNT_ID),
        "has_app_secret": bool(settings.INSTAGRAM_APP_SECRET),
        "has_verify_token": bool(settings.INSTAGRAM_VERIFY_TOKEN),
        "comment_replies": settings.INSTAGRAM_COMMENT_REPLIES,
        "ai_configured": bool(settings.GAPGPT_API_KEY),
        "messages_24h": msgs,
        "webhook_path": "/api/v1/instagram/webhook",
    })
