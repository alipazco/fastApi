from datetime import datetime, timedelta

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from app.api.deps import get_current_admin
from app.db.session import get_db
from app.models import Occasion, User
from app.utils.response import error_response, success_response

router = APIRouter(tags=["occasions"])
admin_router = APIRouter(prefix="/admin-panel", tags=["admin-occasions"])

IRAN_OFFSET = timedelta(hours=3, minutes=30)
THEMES = {"black-friday": "بلک فرایدی"}


def _parse_local(value: str) -> datetime:
    """Admin forms send Iran local time ('2026-11-27T00:00'); the DB keeps UTC."""
    return datetime.fromisoformat(value.replace("Z", "")) - IRAN_OFFSET


def _local(dt: datetime) -> str:
    return (dt + IRAN_OFFSET).strftime("%Y-%m-%dT%H:%M")


def _public(o: Occasion, now: datetime) -> dict:
    return {
        "id": o.id,
        "theme": o.theme,
        "title": o.title,
        "subtitle": o.subtitle,
        "starts_at": o.starts_at.isoformat() + "Z",
        "ends_at": o.ends_at.isoformat() + "Z",
        "seconds_left": max(0, int((o.ends_at - now).total_seconds())),
    }


def _admin(o: Occasion, now: datetime) -> dict:
    live = o.enabled and o.starts_at <= now <= o.ends_at
    status = "live" if live else ("scheduled" if o.enabled and now < o.starts_at else ("ended" if now > o.ends_at else "off"))
    return {
        "id": o.id, "theme": o.theme, "title": o.title, "subtitle": o.subtitle, "enabled": o.enabled,
        "starts_at_local": _local(o.starts_at), "ends_at_local": _local(o.ends_at), "status": status,
    }


@router.get("/occasions/active")
def active(db: Session = Depends(get_db)):
    """The occasion running right now (or null). The storefront themes itself from this."""
    now = datetime.utcnow()
    o = (
        db.query(Occasion)
        .filter(Occasion.deleted_at.is_(None), Occasion.enabled.is_(True), Occasion.starts_at <= now, Occasion.ends_at >= now)
        .order_by(Occasion.starts_at.desc())
        .first()
    )
    return success_response(_public(o, now) if o else None)


class OccasionIn(BaseModel):
    theme: str = "black-friday"
    title: str = Field(min_length=2, max_length=120)
    subtitle: str | None = Field(default=None, max_length=255)
    starts_at: str   # Iran local time, e.g. 2026-11-27T00:00
    ends_at: str
    enabled: bool = True

    @field_validator("theme")
    @classmethod
    def _theme(cls, v: str) -> str:
        if v not in THEMES:
            raise ValueError("theme not supported")
        return v


def _apply(o: Occasion, p: OccasionIn) -> str | None:
    try:
        start, end = _parse_local(p.starts_at), _parse_local(p.ends_at)
    except ValueError:
        return "فرمت تاریخ درست نیست"
    if end <= start:
        return "زمان پایان باید بعد از شروع باشه"
    o.theme, o.title, o.subtitle, o.starts_at, o.ends_at, o.enabled = p.theme, p.title.strip(), (p.subtitle or None), start, end, p.enabled
    return None


@admin_router.get("/occasions")
def admin_index(db: Session = Depends(get_db), _: User = Depends(get_current_admin)):
    now = datetime.utcnow()
    rows = db.query(Occasion).filter(Occasion.deleted_at.is_(None)).order_by(Occasion.starts_at.desc()).all()
    return success_response({"occasions": [_admin(o, now) for o in rows], "themes": [{"value": k, "label": v} for k, v in THEMES.items()]})


@admin_router.post("/occasions")
def admin_store(payload: OccasionIn, db: Session = Depends(get_db), _: User = Depends(get_current_admin)):
    o = Occasion()
    if (err := _apply(o, payload)):
        return error_response({"error": [err]}, 422)
    db.add(o); db.commit(); db.refresh(o)
    return success_response(_admin(o, datetime.utcnow()), 201)


@admin_router.put("/occasions/{occasion_id}")
def admin_update(occasion_id: int, payload: OccasionIn, db: Session = Depends(get_db), _: User = Depends(get_current_admin)):
    o = db.query(Occasion).filter(Occasion.id == occasion_id, Occasion.deleted_at.is_(None)).first()
    if o is None:
        return error_response("پیدا نشد", 404)
    if (err := _apply(o, payload)):
        return error_response({"error": [err]}, 422)
    db.commit()
    return success_response(_admin(o, datetime.utcnow()))


@admin_router.patch("/occasions/{occasion_id}/toggle")
def admin_toggle(occasion_id: int, db: Session = Depends(get_db), _: User = Depends(get_current_admin)):
    o = db.query(Occasion).filter(Occasion.id == occasion_id, Occasion.deleted_at.is_(None)).first()
    if o is None:
        return error_response("پیدا نشد", 404)
    o.enabled = not o.enabled
    db.commit()
    return success_response(_admin(o, datetime.utcnow()))


@admin_router.delete("/occasions/{occasion_id}")
def admin_delete(occasion_id: int, db: Session = Depends(get_db), _: User = Depends(get_current_admin)):
    o = db.query(Occasion).filter(Occasion.id == occasion_id, Occasion.deleted_at.is_(None)).first()
    if o is None:
        return error_response("پیدا نشد", 404)
    o.deleted_at = datetime.utcnow()
    db.commit()
    return success_response(None)
