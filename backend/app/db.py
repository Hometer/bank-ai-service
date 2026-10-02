import time
from pathlib import Path
from typing import Any

from alembic.config import Config
from sqlalchemy import JSON, Boolean, Float, ForeignKey, Integer, String, create_engine, delete, event
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from alembic import command


class Base(DeclarativeBase):
    pass


class Conversation(Base):
    __tablename__ = "conversations"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    owner_hash: Mapped[str] = mapped_column(String, index=True)
    customer_type: Mapped[str] = mapped_column(String)
    created_at: Mapped[float] = mapped_column(Float)
    expires_at: Mapped[float] = mapped_column(Float, index=True)
    context: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    revision: Mapped[int] = mapped_column(Integer, default=0)


class SafetyNotice(Base):
    __tablename__ = "safety_notices"
    owner_hash: Mapped[str] = mapped_column(String, primary_key=True)
    active: Mapped[bool] = mapped_column(Boolean)
    revision: Mapped[int] = mapped_column(Integer)
    updated_at: Mapped[float] = mapped_column(Float)
    expires_at: Mapped[float] = mapped_column(Float, index=True)


class Message(Base):
    __tablename__ = "messages"
    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True,
    )
    role: Mapped[str] = mapped_column(String)
    text: Mapped[str] = mapped_column(String)
    created_at: Mapped[float] = mapped_column(Float)
    answer: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)


def open_database(url: str) -> Engine:
    parsed = make_url(url)
    if parsed.get_backend_name() != "sqlite" or not parsed.database or parsed.database == ":memory:":
        raise ValueError("This local demo requires a file-backed SQLite database")
    Path(parsed.database).resolve().parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(url, connect_args={"check_same_thread": False, "timeout": 10})

    @event.listens_for(engine, "connect")
    def sqlite_settings(connection: Any, _: Any) -> None:
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()

    cfg = Config()
    cfg.set_main_option("script_location", str(Path(__file__).resolve().parents[1] / "alembic"))
    with engine.begin() as connection:
        cfg.attributes["connection"] = connection
        command.upgrade(cfg, "head")
    return engine


def cleanup_expired(engine: Engine, now: float | None = None) -> int:
    with sessionmaker(engine).begin() as db:
        result = db.execute(delete(Conversation).where(Conversation.expires_at <= (now or time.time())))
        db.execute(delete(SafetyNotice).where(SafetyNotice.expires_at <= (now or time.time())))
        return int(getattr(result, "rowcount", 0))
