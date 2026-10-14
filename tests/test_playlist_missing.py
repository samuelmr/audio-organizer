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
SEXY_BOY = "Air/Moon Safari/02 Sexy Boy.mp3"


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


def _id(session, path):
    return session.scalar(select(AudioFile.id).where(AudioFile.path == path))


def _playlist(session, lib, entries):
    playlist = Playlist(library=lib, name="Mix", sort="manual")
    pl.set_entries(playlist, entries)
    session.add(playlist)
    session.commit()
    return playlist


def test_entries_without_a_readable_file_are_missing(library):
    session, lib, root = library
    playlist = _playlist(
        session, lib, [(INSANE, _id(session, INSANE)), ("Nobody/Nothing.mp3", None)]
    )
    (root / INSANE).unlink()
    scan(session, lib)
    assert [e.path for e in pl.missing_entries(session, playlist)] == [
        INSANE,
        "Nobody/Nothing.mp3",
    ]


def test_locate_by_path_audio_and_name(library, copy_audio):
    session, lib, root = library
    playlist = _playlist(
        session,
        lib,
        [
            (HALO, _id(session, HALO)),  # trashed alone; a copy has the same audio
            (SEXY_BOY, None),  # imported before the file was there
            ("D:\\Old\\Texas\\Greatest Hits\\11 halo.flac", None),  # another computer
            ("C:\\Music\\Mixes\\Texas_Insane.mp3", None),  # only the name fits
            ("Nobody/Nothing.mp3", None),
        ],
    )
    changeset = ch.new_changeset(session, "Trash", "duplicates")
    ch.propose_trash(changeset, session.get(AudioFile, _id(session, HALO)))
    session.commit()
    ch.set_status(changeset.changes, "approved")
    assert all(o.ok for o in ch.apply(session, changeset))
    copy_audio("mp3b", root / SEXY_BOY)
    copy_audio("m4a", root / "Texas/Best Of/11 Halo.m4a")  # same name, another folder
    assert scan(session, lib).matched == 1  # SEXY_BOY; the others need choosing
    playlist.entries[1].file_id = None  # as if no scan had run since the file came
    session.commit()

    located = pl.locate_missing(session, lib, pl.missing_entries(session, playlist))
    assert [(i.entry.position, i.path, i.how, i.sure) for i in located] == [
        (0, HALO_COPY, "same audio", True),
        (1, SEXY_BOY, "path", True),
        (2, "Texas/Best Of/11 Halo.m4a", "file name", False),  # its folder differs
        (3, INSANE, "file name", False),
    ]

    # The folders above a file decide between files of the same name
    copy_audio("mp3", root / "Texas/Greatest Hits/11 Halo.mp3")
    scan(session, lib)
    [entry] = [e for e in pl.missing_entries(session, playlist) if e.position == 2]
    [found] = pl.locate_missing(session, lib, [entry])
    assert (found.path, found.how, found.sure) == (HALO, "file name and 2 folders", True)


def test_equally_good_matches_are_not_proposed(library, copy_audio):
    session, lib, root = library
    copy_audio("m4a", root / "Other/Texas_Insane.m4a")
    copy_audio("m4a", root / "Else/Texas_Insane.m4a")
    scan(session, lib)
    playlist = _playlist(session, lib, [("Mixes/Texas_Insane.ogg", None)])
    assert pl.locate_missing(session, lib, pl.missing_entries(session, playlist)) == []


def test_relink_and_drop_missing(library):
    session, lib, _ = library
    playlist = _playlist(
        session,
        lib,
        [("Nobody/Nothing.mp3", None), ("old/Texas_Insane.mp3", None), (HALO, _id(session, HALO))],
    )
    located = pl.locate_missing(session, lib, pl.missing_entries(session, playlist))
    assert pl.relink(session, located) == 1
    assert pl.drop_missing(session, playlist) == 1
    session.commit()
    assert [(e.position, e.path) for e in playlist.entries] == [(0, INSANE), (1, HALO)]
    [generated] = pl.generate(session, lib)
    assert [t.path for t in generated.tracks] == [INSANE, HALO]


def test_the_web_pages(library, Session):
    session, lib, _ = library
    playlist = _playlist(
        session,
        lib,
        [
            ("Nobody/Nothing.mp3", None),
            ("old/Texas_Insane.mp3", None),
            ("elsewhere/Texas_Halo.mp3", None),
        ],
    )
    ids = [e.id for e in playlist.entries]
    client = TestClient(create_app(Session))

    page = client.get(f"/playlists/{playlist.id}").text
    assert "Not found: 3 entries" in page and "Look for them" in page

    page = client.get(f"/playlists/{playlist.id}/locate").text
    assert f'value="{ids[1]}:{_id(session, INSANE)}"' in page
    assert "Still not found: 1" in page

    page = client.post(
        f"/playlists/{playlist.id}/locate",
        data={"found": [f"{ids[1]}:{_id(session, INSANE)}", f"{ids[0]}:{_id(session, HALO)}"]},
    ).text
    assert "Found the tracks of 1 entries" in page  # the entry nothing was proposed for stays
    assert "Not found: 2 entries" in page

    page = client.post(f"/playlists/{playlist.id}/drop-missing").text
    assert "Removed 2 entries" in page and "Not found" not in page
    session.expire_all()
    assert [e.path for e in session.get(Playlist, playlist.id).entries] == [INSANE]


def test_the_missing_command(library, tmp_path):
    session, lib, _ = library
    _playlist(
        session,
        lib,
        [("Nobody/Nothing.mp3", None), ("old/Texas_Insane.mp3", None), ("x/Halo.mp3", None)],
    )
    db = ["--db", str(tmp_path / "catalog.db")]
    result = CliRunner().invoke(app, [*db, "playlists", "missing"])
    assert result.exit_code == 0, result.output
    assert "Mix: 3 not found, 1 located" in result.output
    assert f"2. old/Texas_Insane.mp3 -> {INSANE} (file name)" in result.output

    result = CliRunner().invoke(app, [*db, "playlists", "missing", "--apply", "--remove"])
    assert "Relinked 0 entries, removed 3." in result.output  # a guess needs --guesses


def test_the_missing_command_uses_guesses_when_asked(library, tmp_path):
    session, lib, _ = library
    _playlist(session, lib, [("old/Texas_Insane.mp3", None)])
    db = ["--db", str(tmp_path / "catalog.db")]
    result = CliRunner().invoke(app, [*db, "playlists", "missing", "--apply", "--guesses"])
    assert "Relinked 1 entries, removed 0." in result.output
