"""Database engine and session setup. SQLite locally; any SQLAlchemy URL works."""

import os
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, event, inspect
from sqlalchemy.orm import Session, sessionmaker

from .models import Base, EffectiveMetadata, PendingField
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


# Seconds a SQLite connection waits for another writer before giving up
BUSY_TIMEOUT = 30


def make_engine(url: str | None = None) -> Engine:
    url = url or default_db_url()
    # SQLite allows one writer at a time; wait for a long scan or sync to release its lock
    # instead of failing after the default 5 seconds
    connect_args = {"timeout": BUSY_TIMEOUT} if url.startswith("sqlite") else {}
    engine = create_engine(url, connect_args=connect_args)
    if engine.dialect.name == "sqlite":

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _record):
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()

    _drop_outdated_derived_tables(engine)
    migrate(engine)
    Base.metadata.create_all(engine)
    return engine


# The revision that catalogs from before migrations existed are taken to be at.
BASELINE_REVISION = "0001"


def migrate(engine: Engine) -> None:
    """Bring the catalog's existing tables up to date with Alembic migrations.

    A new catalog is created from the models and stamped as current. A catalog from before
    migrations existed is stamped with the baseline first. New tables are not migrations:
    create_all adds them.
    """
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
    tables = set(inspect(engine).get_table_names())
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        if "changeset" not in tables:
            Base.metadata.create_all(connection)
            command.stamp(config, "head")
            return
        if "alembic_version" not in tables:
            command.stamp(config, BASELINE_REVISION)
        command.upgrade(config, "head")


# Tables computed entirely from other data: when their columns change, they are dropped,
# recreated and filled again instead of being migrated. They are filled together, file by
# file (provenance.refresh_effective), so when one is outdated or missing, all are rebuilt.
DERIVED_TABLES = [EffectiveMetadata.__table__, PendingField.__table__]


def _drop_outdated_derived_tables(engine: Engine) -> None:
    inspector = inspect(engine)
    present = [table for table in DERIVED_TABLES if inspector.has_table(table.name)]
    if not present:
        return  # a new catalog
    outdated = len(present) < len(DERIVED_TABLES)
    for table in present:
        existing = {
            c["name"]: str(c["type"].compile(engine.dialect)).upper()
            for c in inspector.get_columns(table.name)
        }
        wanted = {c.name: str(c.type.compile(engine.dialect)).upper() for c in table.columns}
        outdated = outdated or existing != wanted
    if outdated:
        for table in present:
            table.drop(engine)


def make_sessionmaker(url: str | None = None) -> sessionmaker[Session]:
    factory = sessionmaker(make_engine(url), expire_on_commit=False)
    with factory() as session:
        backfill_effective(session)  # older catalogs, or after the table was rebuilt
    return factory
