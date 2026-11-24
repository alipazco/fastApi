from sqlalchemy import Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base
from app.models.mixins import TimestampMixin


class InstagramMessage(Base, TimestampMixin):
    """Short per-customer chat history for the Instagram DM assistant."""
    __tablename__ = "instagram_messages"
    __table_args__ = (Index("ix_igmsg_user_id_id", "ig_user_id", "id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    ig_user_id: Mapped[str] = mapped_column(String(64))
    role: Mapped[str] = mapped_column(String(16))  # user | assistant
    text: Mapped[str] = mapped_column(Text)


class InstagramEvent(Base, TimestampMixin):
    """Webhook idempotency: Meta retries deliveries, each message/comment id is handled once."""
    __tablename__ = "instagram_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[str] = mapped_column(String(191), unique=True)
    kind: Mapped[str] = mapped_column(String(16))  # dm | comment
