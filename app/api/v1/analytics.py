from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.deps import get_current_admin
from app.db.session import get_db
from app.models import User
from app.services import analytics
from app.utils.response import success_response

admin_router = APIRouter(prefix="/admin-panel/analytics", tags=["admin-analytics"])


@admin_router.get("/overview")
def overview(days: int = Query(30, ge=1, le=365), db: Session = Depends(get_db), _: User = Depends(get_current_admin)):
    return success_response(analytics.build_overview(db, days))


@admin_router.get("/insights")
def insights(
    days: int = Query(30, ge=1, le=365),
    refresh: bool = False,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_admin),
):
    return success_response(analytics.build_insights(db, days, refresh))
