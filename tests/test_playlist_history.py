"""Playlists that name tracks where they used to be find them where they are now."""

import pytest
from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified

from audio_organizer import changes as ch
from audio_organizer import playlists as pl
from audio_organizer.models import AudioFile, Change, Library
from audio_organizer.scanner import scan

HALO = "Texas/Greatest Hits/11 Halo.mp3"
HALO_COPY = "Aguilera, Christina/Back to Basics/01 Aguilera, Christina - Halo.mp3"  # same audio
INSANE = "Texas/Texas_Insane.mp3"


@pytest.fixture
def library(Session, copy_audio, tmp_path):
    root = tmp_path / "music"
    copy_audio("mp3", root / HALO)
    copy_audio("mp3", root / HALO_COPY)
    copy_audio("mp3b", root / INSANE)
    session = Session()
    lib = Library(name="music", root=str(root))
    session.add(lib)
    session.commit()
    scan(session, lib)
    yield session, lib, root
    session.close()


def _file(session, path):
    return session.scalar(select(AudioFile).where(AudioFile.path == path))


def _apply(session, propose):
    changeset = ch.new_changeset(session, "Test", "layout")
    propose(changeset)
    session.commit()
    ch.set_status(changeset.changes, "approved")
    assert all(o.ok for o in ch.apply(session, changeset))
    session.commit()
    return changeset


def _import(session, lib, root, *paths):
    """The file ids an imported playlist's entries refer to."""
    return [e.file_id for e in _import_playlist(session, lib, root, *paths).entries]


def _import_playlist(session, lib, root, *paths):
    m3u = root / "lists" / "Old.m3u"
    m3u.parent.mkdir(exist_ok=True)
    m3u.write_text("".join(f"{p}\n" for p in paths), encoding="utf-8")
    name = f"Old {len(pl.library_playlists(session, lib))}"
    playlist, _ = pl.import_m3u(session, lib, m3u, name=name)
    session.commit()
    return playlist


def test_a_path_aorg_moved_a_file_from(library):
    session, lib, root = library
    halo = _file(session, HALO)
    _apply(session, lambda c: ch.propose_move(c, halo, "Texas/Halo/11 Halo.mp3"))
    assert _import(session, lib, root, HALO) == [halo.id]


def test_a_path_of_a_trashed_duplicate(library):
    """The playlist was written before a duplicate copy was trashed."""
    session, lib, root = library
    halo, copy = _file(session, HALO), _file(session, HALO_COPY)
    _apply(session, lambda c: ch.propose_trash(c, copy, replacement=halo))
    assert _import(session, lib, root, HALO_COPY) == [halo.id]

    # Older trash changes recorded only where the file went in the trash
    [change] = session.scalars(select(Change).where(Change.kind == "trash"))
    del change.undo["from"]
    flag_modified(change, "undo")
    session.commit()
    assert _import(session, lib, root, HALO_COPY) == [halo.id]


def test_a_path_of_a_moved_file_that_was_trashed_later(library):
    session, lib, root = library
    halo, copy = _file(session, HALO), _file(session, HALO_COPY)
    _apply(session, lambda c: ch.propose_move(c, copy, "Christina Aguilera/Halo.mp3"))
    _apply(session, lambda c: ch.propose_trash(c, copy, replacement=halo))
    assert _import(session, lib, root, HALO_COPY) == [halo.id]


def test_a_path_of_a_copy_deleted_outside_aorg(library):
    session, lib, root = library
    (root / HALO_COPY).unlink()
    scan(session, lib)
    assert _import(session, lib, root, HALO_COPY) == [_file(session, HALO).id]


def test_a_path_that_leads_to_two_files_is_not_followed(library):
    session, lib, root = library
    halo, insane = _file(session, HALO), _file(session, INSANE)
    _apply(session, lambda c: ch.propose_move(c, insane, "Texas/Insane.mp3"))
    _apply(session, lambda c: ch.propose_move(c, halo, INSANE))
    _apply(session, lambda c: ch.propose_move(c, halo, "Texas/Halo.mp3"))
    assert _import(session, lib, root, INSANE, HALO) == [None, halo.id]


def test_locating_follows_the_history(library):
    session, lib, root = library
    halo, copy = _file(session, HALO), _file(session, HALO_COPY)
    playlist = _import_playlist(session, lib, root, "Nobody/Nothing.mp3", HALO_COPY)
    assert [e.file_id for e in playlist.entries] == [None, copy.id]
    _apply(session, lambda c: ch.propose_trash(c, copy, replacement=halo))
    # As if the playlist had been imported after the trash, with the entry unmatched
    playlist.entries[1].file_id = None
    session.commit()
    [found] = pl.locate_missing(session, lib, pl.missing_entries(session, playlist))
    assert (found.entry.position, found.file_id, found.how) == (1, halo.id, "where its file went")
