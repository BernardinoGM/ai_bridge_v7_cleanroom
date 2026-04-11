from __future__ import annotations

from contextlib import contextmanager
from functools import lru_cache
from typing import Iterator

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, declarative_base, sessionmaker

from app.config import get_settings


Base = declarative_base()
SessionLocal = sessionmaker(autoflush=False, autocommit=False, expire_on_commit=False, future=True)


@lru_cache
def get_engine() -> Engine:
    settings = get_settings()
    connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
    engine = create_engine(settings.database_url, future=True, pool_pre_ping=True, connect_args=connect_args)
    SessionLocal.configure(bind=engine)
    return engine


def init_database() -> None:
    engine = get_engine()
    Base.metadata.create_all(bind=engine)
    _apply_safe_additive_columns(engine)


def _apply_safe_additive_columns(engine: Engine) -> None:
    inspector = inspect(engine)
    columns = {column["name"] for column in inspector.get_columns("agent_profiles")}
    additions = {
        "last_known_workspace_fingerprint": "VARCHAR(255)",
        "recent_task_patterns_json": "JSON",
        "surface_preferences_json": "JSON",
        "last_strategy_json": "JSON",
    }
    with engine.begin() as connection:
        for column_name, column_type in additions.items():
            if column_name in columns:
                continue
            connection.execute(text(f"ALTER TABLE agent_profiles ADD COLUMN {column_name} {column_type}"))


def get_db() -> Iterator[Session]:
    get_engine()
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    get_engine()
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
