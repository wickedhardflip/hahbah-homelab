"""Portal users and their sign-in sessions."""
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


def utcnow() -> datetime:
    """Naive UTC, because SQLite drops time zones."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(50), unique=True, nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, server_default="0")
    google_email: Mapped[str | None] = mapped_column(String(254), unique=True, nullable=True)   # lower-case; the Google allowlist
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class Session(Base):
    __tablename__ = "sessions"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    last_seen: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)   # sliding idle timeout; written at most once a minute

    @property
    def is_expired(self) -> bool:
        return utcnow() > self.expires_at


class Incident(Base):
    """Something that went to Caution or Danger for at least two checks in a row (a minute), until it cleared."""
    __tablename__ = "incidents"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(160), nullable=False, index=True)       # "<kind>:<target>"
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    target: Mapped[str] = mapped_column(String(80), nullable=False)
    severity: Mapped[str] = mapped_column(String(8), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    opened_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False, index=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    emailed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    clear_sent: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)   # the all-clear went out (or was skipped)


class DailyMetric(Base):
    """One number per key per local day (the latest reading of the day wins); kept 30 days for the trend lines."""
    __tablename__ = "daily_metrics"
    day: Mapped[str] = mapped_column(String(10), primary_key=True)                  # YYYY-MM-DD, home time zone
    key: Mapped[str] = mapped_column(String(40), primary_key=True)
    value: Mapped[float] = mapped_column(nullable=False)


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(60), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
