import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from typer.testing import CliRunner

from audio_organizer import changes as ch
from audio_organizer import playlists as pl
from audio_organizer.cli import app
from audio_organizer.models import AudioFile, Library, Playlist
from audio_organizer.scanner import scan
from audio_organizer.web import create_app

HALO = "Texas/Greatest Hits/11 Halo.mp3"
HALO_COPY = "Texas/Texas_Halo.mp3"  # identical audio
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


def _playlist(session, lib, name, entries):
    playlist = Playlist(library=lib, name=name, sort="manual")
    pl.set_entries(playlist, entries)
    session.add(playlist)
    session.commit()
    return playlist


def _entries(session, lib, paths_and_files):
    return [(path, _file(session, path).id if file else None) for path, file in paths_and_files]


def test_find_and_collapse_repeats(library):
    session, lib, _ = library
    playlist = _playlist(
        session,
        lib,
        "Mix",
        _entries(
            session,
            lib,
            [
                (HALO, True),
                (INSANE, True),
                (HALO_COPY, True),  # identical audio to the first
                (HALO, True),  # the same file again
                ("old/gone.mp3", False),
                ("old/gone.mp3", False),  # unmatched, the same path
                ("old/other.mp3", False),
            ],
        ),
    )
    session.add(Playlist(library=lib, name="Smart", rule="artist=texas", sort="shuffle"))
    _playlist(session, lib, "Clean", _entries(session, lib, [(HALO, True), (INSANE, True)]))
    keep = {e.position: e.id for e in playlist.entries}

    found = pl.find_repeats(session, pl.library_playlists(session, lib))

    assert [r.playlist.name for r in found] == ["Mix"]  # not Clean, and not the smart one
    assert [(d.position, earlier.position) for d, earlier in found[0].dropped] == [
        (2, 0),
        (3, 0),
        (5, 4),
    ]
    assert pl.collapse(session, found) == 3
    session.commit()
    session.expire_all()
    playlist = session.get(Playlist, playlist.id)
    assert [(e.position, e.path) for e in playlist.entries] == [
        (0, HALO),
        (1, INSANE),
        (2, "old/gone.mp3"),
        (3, "old/other.mp3"),
    ]
    assert [e.id for e in playlist.entries] == [keep[0], keep[1], keep[4], keep[6]]
    assert pl.find_repeats(session, [playlist]) == []


def test_trashing_a_copy_leaves_a_repeat_that_collapses(library):
    """Both copies were in the playlist; trashing one points its entry at the kept file."""
    session, lib, _ = library
    playlist = _playlist(
        session, lib, "Mix", _entries(session, lib, [(HALO_COPY, True), (HALO, True)])
    )
    changeset = ch.new_changeset(session, "Trash", "duplicates")
    ch.propose_trash(changeset, _file(session, HALO_COPY), replacement=_file(session, HALO))
    session.commit()
    ch.set_status(changeset.changes, "approved")
    assert all(o.ok for o in ch.apply(session, changeset))
    assert [e.file_id for e in playlist.entries] == [_file(session, HALO).id] * 2

    [found] = pl.find_repeats(session, [playlist])
    pl.collapse(session, [found])
    session.commit()

    assert [e.file_id for e in playlist.entries] == [_file(session, HALO).id]


def test_the_dedupe_command(library, tmp_path):
    session, lib, _ = library
    playlist = _playlist(
        session, lib, "Mix", _entries(session, lib, [(HALO, True), (HALO_COPY, True)])
    )
    db = ["--db", str(tmp_path / "catalog.db")]

    result = CliRunner().invoke(app, [*db, "playlists", "dedupe"])
    assert result.exit_code == 0, result.output
    assert "1 repeated entries in 1 static playlists" in result.output
    assert "Run with --apply" in result.output
    session.expire_all()
    assert len(session.get(Playlist, playlist.id).entries) == 2  # only listed

    result = CliRunner().invoke(app, [*db, "playlists", "dedupe", "--name", "Mix", "--apply"])
    assert result.exit_code == 0, result.output
    assert "Removed 1 entries." in result.output
    session.expire_all()
    assert len(session.get(Playlist, playlist.id).entries) == 1

    result = CliRunner().invoke(app, [*db, "playlists", "dedupe"])
    assert "0 repeated entries" in result.output


def test_the_playlist_page_offers_to_remove_repeats(library, Session):
    session, lib, _ = library
    playlist = _playlist(
        session, lib, "Mix", _entries(session, lib, [(HALO, True), (HALO_COPY, True)])
    )
    client = TestClient(create_app(Session))

    page = client.get(f"/playlists/{playlist.id}").text
    assert "Remove the repeats" in page and "1 entry name a track" in page

    page = client.post(f"/playlists/{playlist.id}/dedupe").text
    assert "Removed 1 repeated entries" in page
    assert "Remove the repeats" not in page
