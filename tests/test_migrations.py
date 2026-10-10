import pytest
from sqlalchemy import create_engine, inspect, text

from audio_organizer import changes as ch
from audio_organizer.db import make_engine, make_sessionmaker
from audio_organizer.models import Base, Changeset, Library


def _pre_alembic_catalog(url: str) -> None:
    """A catalog as older versions made it: no alembic_version table, no changeset.source."""
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(text("DROP INDEX ix_changeset_source"))
        connection.execute(text("ALTER TABLE changeset DROP COLUMN source"))
        for table in ("library", "sync_target"):
            connection.execute(text(f"ALTER TABLE {table} DROP COLUMN playlist_folder"))
            connection.execute(text(f"ALTER TABLE {table} DROP COLUMN playlist_formats"))
        connection.execute(text("INSERT INTO library (name, root) VALUES ('music', '/music')"))
        connection.execute(
            text("INSERT INTO changeset (description, created_at) VALUES ('old', '2026-01-01')")
        )
    engine.dispose()


def test_old_catalog_gets_source_column_and_keeps_its_rows(tmp_path):
    url = f"sqlite:///{tmp_path / 'old.db'}"
    _pre_alembic_catalog(url)

    factory = make_sessionmaker(url)

    with factory() as session:
        old = session.query(Changeset).one()
        assert (old.description, old.source) == ("old", None)
        new = ch.new_changeset(session, "new", "layout")
        session.commit()
        assert new.source == "layout"


def test_old_catalog_gets_playlist_folder_settings(tmp_path):
    url = f"sqlite:///{tmp_path / 'old.db'}"
    _pre_alembic_catalog(url)

    factory = make_sessionmaker(url)

    with factory() as session:
        library = session.query(Library).one()
        assert (library.playlist_folder, library.playlist_formats) == ("", "m3u")


def test_new_catalog_is_stamped_and_reopens(tmp_path):
    url = f"sqlite:///{tmp_path / 'new.db'}"
    make_engine(url).dispose()

    engine = make_engine(url)  # second open runs the upgrade path on a stamped catalog

    assert inspect(engine).has_table("alembic_version")
    assert "source" in {c["name"] for c in inspect(engine).get_columns("changeset")}


def test_changeset_needs_a_known_source(Session):
    with Session() as session, pytest.raises(ValueError):
        ch.new_changeset(session, "x", "nonsense")
