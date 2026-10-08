"""Database engine and session setup. SQLite locally; any SQLAlchemy URL works."""

import os
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, event, inspect
from sqlalchemy.orm import Session, sessionmaker

from .models import Base, EffectiveMetadata
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
# recreated and filled again instead of being migrated.
DERIVED_TABLES = [EffectiveMetadata.__table__]


def _drop_outdated_derived_tables(engine: Engine) -> None:
    inspector = inspect(engine)
    for table in DERIVED_TABLES:
        if not inspector.has_table(table.name):
            continue
        existing = {
            c["name"]: str(c["type"].compile(engine.dialect)).upper()
            for c in inspector.get_columns(table.name)
        }
        wanted = {c.name: str(c.type.compile(engine.dialect)).upper() for c in table.columns}
        if existing != wanted:
            table.drop(engine)


def make_sessionmaker(url: str | None = None) -> sessionmaker[Session]:
    factory = sessionmaker(make_engine(url), expire_on_commit=False)
    with factory() as session:
        backfill_effective(session)  # older catalogs, or after the table was rebuilt
    return factory
