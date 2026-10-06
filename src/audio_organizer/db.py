"""Database engine and session setup. SQLite locally; any SQLAlchemy URL works."""

import os
from pathlib import Path

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from .models import Base
from .provenance import backfill_effective


def default_db_url() -> str:
    """AORG_DB (a path or SQLAlchemy URL), else $XDG_DATA_HOME/audio-organizer/catalog.db."""
    value = os.environ.get("AORG_DB")
    if value and "://" in value:
        return value
    if value:
        path = Path(value).expanduser()
    else:
        data_home = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
        path = data_home / "audio-organizer" / "catalog.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{path}"


def make_engine(url: str | None = None) -> Engine:
    engine = create_engine(url or default_db_url())
    if engine.dialect.name == "sqlite":

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _record):
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()

    Base.metadata.create_all(engine)
    return engine


def make_sessionmaker(url: str | None = None) -> sessionmaker[Session]:
    factory = sessionmaker(make_engine(url), expire_on_commit=False)
    with factory() as session:
        backfill_effective(session)  # catalogs made before the table existed
    return factory
