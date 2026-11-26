from datetime import datetime

from sqlalchemy import Boolean, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base
from app.models.mixins import TimestampMixin, SoftDeleteMixin


class Occasion(Base, TimestampMixin, SoftDeleteMixin):
    """
    A temporary storefront occasion (Black Friday, Yalda, Nowruz...). While
    enabled and between starts_at/ends_at (UTC) the storefront switches to the
    occasion's theme and shows its deals section; afterwards it reverts by itself.
    """
    __tablename__ = "occasions"

    id: Mapped[int] = mapped_column(primary_key=True)
    theme: Mapped[str] = mapped_column(String(32), default="black-friday")
    title: Mapped[str] = mapped_column(String(120))
    subtitle: Mapped[str | None] = mapped_column(String(255), nullable=True)
    starts_at: Mapped[datetime] = mapped_column(DateTime)
    ends_at: Mapped[datetime] = mapped_column(DateTime)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
