from sqlalchemy import delete, select, text

from audio_organizer import tags
from audio_organizer.db import make_sessionmaker
from audio_organizer.models import AudioFile, EffectiveMetadata, Library
from audio_organizer.provenance import set_value
from audio_organizer.scanner import scan


def _scanned(Session, copy_audio, tmp_path):
    root = tmp_path / "music"
    copy_audio("mp3", root / "Texas/Singles/11 Halo.mp3")  # album tag: Greatest Hits
    copy_audio("mp3b", root / "Texas/Greatest Hits/07 Insane.mp3")
    session = Session()
    library = Library(name="music", root=str(root))
    session.add(library)
    session.commit()
    scan(session, library)
    return session


def _effective(session, path_like):
    return session.scalar(
        select(EffectiveMetadata).join(AudioFile).where(AudioFile.path.like(path_like))
    )


def test_effective_metadata_follows_sources(Session, copy_audio, tmp_path):
    session = _scanned(Session, copy_audio, tmp_path)
    halo = _effective(session, "%Halo%")
    assert (halo.artist, halo.album, halo.title, halo.tracknumber, halo.year) == (
        "Texas", "Greatest Hits", "Halo", 11, 2000,
    )  # fmt: skip
    assert halo.disputed  # the folder says "Singles"
    insane = _effective(session, "%Insane%")
    assert insane.album == "Greatest Hits"  # from the folder: the file has no album tag
    assert not insane.disputed

    set_value(session, halo.file, "manual", "album", "The Greatest Hits")
    session.commit()
    session.expire_all()
    halo = _effective(session, "%Halo%")
    assert halo.album == "The Greatest Hits"
    assert not halo.disputed  # decided by the user


def test_missing_rows_are_backfilled(Session, copy_audio, tmp_path):
    session = _scanned(Session, copy_audio, tmp_path)
    session.execute(delete(EffectiveMetadata))
    session.commit()
    session.close()
    with make_sessionmaker(f"sqlite:///{tmp_path / 'catalog.db'}")() as fresh:
        assert len(fresh.scalars(select(EffectiveMetadata)).all()) == 2
        assert _effective(fresh, "%Halo%").title == "Halo"


def test_numbers_and_years_disagree_only_by_value(Session, copy_audio, tmp_path):
    root = tmp_path / "music"
    path = copy_audio("mp3", root / "Abel/Onderweg/02 Onderweg.mp3")
    tags.write(path, {"tracknumber": "02.", "date": "2017-03-03T08:00:00Z", "album": "Onderweg",
                      "artist": "Abel", "title": "Onderweg"})  # fmt: skip
    session = Session()
    library = Library(name="music", root=str(root))
    session.add(library)
    session.commit()
    scan(session, library)
    effective = _effective(session, "%Onderweg%")
    assert (effective.tracknumber, effective.year) == (2, 2017)
    assert not effective.disputed  # tag "02." and folder "02" are the same number

    set_value(session, effective.file, "gnudb", "date", "2016")
    session.commit()
    session.expire_all()
    effective = _effective(session, "%Onderweg%")
    assert effective.disputed  # 2016 is not 2017
    assert effective.year == 2017  # but a lookup does not outrank the tag


def test_outdated_derived_table_is_rebuilt(Session, copy_audio, tmp_path):
    session = _scanned(Session, copy_audio, tmp_path)
    engine = session.get_bind()
    session.close()
    with engine.begin() as connection:
        connection.execute(text("drop table effective_metadata"))
        connection.execute(
            text("create table effective_metadata (file_id integer primary key, artist text)")
        )
    with make_sessionmaker(f"sqlite:///{tmp_path / 'catalog.db'}")() as fresh:
        assert _effective(fresh, "%Halo%").tracknumber == 11
