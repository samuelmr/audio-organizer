import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from typer.testing import CliRunner

from audio_organizer import album_edit as ae
from audio_organizer import changes as ch
from audio_organizer import tags
from audio_organizer import writing as wr
from audio_organizer.cli import app
from audio_organizer.layout import _renamed
from audio_organizer.models import AudioFile, Changeset, Library
from audio_organizer.provenance import resolve
from audio_organizer.scanner import scan
from audio_organizer.tracks import album_files, shared_values
from audio_organizer.web import create_app

ALBUM = "Foreigner/Best of Foriegner"


@pytest.fixture
def library(Session, copy_audio, tmp_path):
    """An album with a typo in its name, a bonus track in a subfolder and another album."""
    root = tmp_path / "music"
    for name, path, title, genre in (
        ("mp3", f"{ALBUM}/01 Urgent.mp3", "Urgent", "Rock"),
        ("mp3b", f"{ALBUM}/02 Juke Box Hero.mp3", "Juke Box Hero", "AOR"),
        ("mp3", f"{ALBUM}/Bonus/03 Cold as Ice.mp3", "Cold as Ice", "Rock"),
    ):
        copy_audio(name, root / path)
        fields = {"artist": "Foreigner", "album": "Best of Foriegner", "title": title}
        tags.write(root / path, {**fields, "genre": genre})
    copy_audio("mp3", root / "Texas/Greatest Hits/11 Halo.mp3")
    with Session() as session:
        lib = Library(name="music", root=str(root))
        session.add(lib)
        session.commit()
        scan(session, lib)
    return root


def _paths(files):
    return [f.path for f in files]


def test_album_files_by_album_or_folder(Session, library):
    with Session() as session:
        lib_id = session.scalar(select(Library.id))
        by_album = album_files(session, lib_id, album="best of FORIEGNER")
        assert _paths(by_album) == [
            f"{ALBUM}/01 Urgent.mp3", f"{ALBUM}/02 Juke Box Hero.mp3",
            f"{ALBUM}/Bonus/03 Cold as Ice.mp3",
        ]  # fmt: skip
        assert album_files(session, lib_id, album="Best of Foriegner", albumartist="Texas") == []
        by_folder = album_files(session, lib_id, folder=ALBUM + "/")
        assert _paths(by_folder) == [f"{ALBUM}/01 Urgent.mp3", f"{ALBUM}/02 Juke Box Hero.mp3"]
        shared = shared_values(session, by_folder, ("album", "genre", "composer"))
        assert shared == {"album": "Best of Foriegner", "genre": None, "composer": ""}


def test_edit_album_write_and_undo(Session, library):
    """The edit only changes the catalog; writing the album's pending changes writes
    the files, and undoing that write leaves the edit, so they are pending again."""
    with Session() as session:
        lib = session.scalar(select(Library))
        files = album_files(session, lib.id, album="Best of Foriegner")
        edit = ae.edit(session, lib, files, {"album": "Best of Foreigner"}, "edit")
        assert len(edit.pending) == 3 and edit.changeset is None
        assert session.scalar(select(Changeset)) is None
        for file in files:
            assert tags.read(library / file.path).fields["album"] == "Best of Foriegner"
        assert album_files(session, lib.id, album="Best of Foriegner") == []

        selection = wr.Selection(artist="foreigner", album="best of foreigner")
        found = wr.survey(session, lib, selection)
        assert found.total == 3
        changeset, outcomes = wr.write(session, found.files, "write")
        assert all(o.ok for o in outcomes), outcomes
        for file in files:
            assert tags.read(library / file.path).fields["album"] == "Best of Foreigner"
            assert tags.read(library / file.path).fields["genre"] in ("Rock", "AOR")  # kept
        assert wr.survey(session, lib, selection).total == 0

        outcomes = ch.undo(session, changeset)
        assert all(o.ok for o in outcomes), outcomes
        for file in files:
            assert tags.read(library / file.path).fields["album"] == "Best of Foriegner"
            assert resolve(file.values)["album"].source == "manual"  # the edit stays
        assert wr.survey(session, lib, selection).total == 3


def test_edit_album_only_differing_files_are_pending(Session, library):
    with Session() as session:
        lib = session.scalar(select(Library))
        files = album_files(session, lib.id, folder=ALBUM)
        edit = ae.edit(session, lib, files, {"genre": "Rock"}, "edit")
        assert _paths(edit.pending) == [f"{ALBUM}/02 Juke Box Hero.mp3"]


def test_cli_edit_album(Session, library):
    db = str(Session.kw["bind"].url)
    result = CliRunner().invoke(
        app, ["--db", db, "edit-album", "--album", "Best of Foriegner",
              "--set", "album=Best of Foreigner"],
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    assert "Saved for 3 tracks in 2 folders; 3 of them are not yet written" in result.output
    assert "aorg pending --artist Foreigner --album 'Best of Foreigner'" in result.output

    result = CliRunner().invoke(
        app, ["--db", db, "edit-album", "--folder", str(library / ALBUM), "--set", "date=1992"]
    )
    assert result.exit_code == 0, result.output
    assert f"Saved for 2 tracks in {ALBUM}/; 2 of them" in result.output
    with Session() as session:
        assert session.scalar(select(Changeset)) is None  # nothing is written yet

    result = CliRunner().invoke(
        app, ["--db", db, "pending", "--artist", "Foreigner", "--album", "Best of Foreigner"]
    )
    assert result.exit_code == 0, result.output
    assert "Foreigner – Best of Foreigner" in result.output
    assert "album: 'Best of Foriegner' -> 'Best of Foreigner'" in result.output
    assert "date: None -> '1992'" in result.output  # both edits, in one write

    result = CliRunner().invoke(app, ["--db", db, "edit-album", "--set", "date=1992"])
    assert result.exit_code != 0 and "--album or --folder" in result.output
    result = CliRunner().invoke(
        app, ["--db", db, "edit-album", "--album", "Nothing", "--set", "date=1992"]
    )
    assert result.exit_code == 1 and "No tracks found" in result.output


def test_web_edit_album(Session, library):
    web = TestClient(create_app(Session))
    page = web.get("/libraries/music", params={"folder": ALBUM}).text
    assert "Edit all 2 tracks" in page
    assert 'name="album" value="Best of Foriegner"' in page
    assert 'name="genre" value="" placeholder="(differs between tracks)"' in page
    assert "Edit all" not in web.get("/libraries/music").text  # not for the whole library
    assert "Edit all" not in web.get("/libraries/music", params={"artist": "Foreigner"}).text

    response = web.post(
        "/libraries/music/edit-album",
        data={"of_album": "Best of Foriegner", "album": "Best of Foreigner",
              "artist": "Foreigner", "genre": ""},
    )  # fmt: skip
    assert response.url.path == "/libraries/music/pending"
    assert response.url.params["album"] == "Best of Foreigner"
    assert "Saved; 3 files are not yet written" in response.text
    assert response.text.count('name="file_ids"') == 3
    with Session() as session:
        assert session.scalar(select(Changeset)) is None
        file = session.scalar(select(AudioFile).where(AudioFile.path.endswith("Urgent.mp3")))
        resolved = resolve(file.values)
        assert resolved["album"].value == "Best of Foreigner"
        # artist was as before and genre left empty: neither changed
        assert resolved["artist"].source == resolved["genre"].source == "tag"

    response = web.post(
        "/libraries/music/edit-album", data={"of_folder": ALBUM, "album": "Best of Foreigner"}
    )
    assert "Nothing changed" in response.text


@pytest.mark.parametrize(
    ("path", "changed", "expected"),
    [
        (f"{ALBUM}/01 Urgent.mp3", {"album": ("Best of Foriegner", "Best of Foreigner")},
         "Foreigner/Best of Foreigner/01 Urgent.mp3"),
        ("Foreigner/1992 - Best of Foriegner [CD1]/01.mp3",
         {"album": ("best of foriegner", "Best of Foreigner")},
         "Foreigner/1992 - Best of Foreigner [CD1]/01.mp3"),
        # Only whole words: "Hits" is not renamed in "Greatest Hitsville"
        ("Texas/Greatest Hitsville/01.mp3", {"album": ("Hits", "Songs")},
         "Texas/Greatest Hitsville/01.mp3"),
        # An artist only where a folder's whole name is theirs
        ("Yes/Yesterday/01.mp3", {"artist": ("Yes", "YES")}, "YES/Yesterday/01.mp3"),
        ("Yes/Yesterday/01.mp3", {"albumartist": ("Yes", "The Yes Band")},
         "The Yes Band/Yesterday/01.mp3"),
        # Characters not allowed in file names are replaced, as in the layout
        ("AC_DC/Back in Blak/01.mp3",
         {"albumartist": ("AC/DC", "AC/DC"), "album": ("Back in Blak", "Back in Black: Live")},
         "AC_DC/Back in Black_ Live/01.mp3"),
        ("01 Loose.mp3", {"album": ("Loose", "Tight")}, "01 Loose.mp3"),  # file names stay
    ],
)  # fmt: skip
def test_renamed(path, changed, expected):
    assert _renamed(path, changed) == expected


def test_edit_album_moving_files(Session, library):
    (library / ALBUM / "folder.jpg").write_bytes(b"jpeg")
    with Session() as session:
        lib = session.scalar(select(Library))
        files = album_files(session, lib.id, album="Best of Foriegner")
        edit = ae.edit(
            session, lib, files, {"album": "Best of Foreigner", "genre": "Rock"}, "edit",
            move_files=True,
        )  # fmt: skip
        assert len(edit.pending) == 3 and len(edit.moves) == 3
        changes = edit.changeset.changes
        assert [c.kind for c in changes] == ["move"] * 3  # the tags are written separately
        ch.set_status(changes, "approved")
        outcomes = ch.apply(session, edit.changeset)
        assert all(o.ok for o in outcomes), outcomes
        new = "Foreigner/Best of Foreigner"
        assert _paths(album_files(session, lib.id, album="Best of Foreigner")) == [
            f"{new}/01 Urgent.mp3", f"{new}/02 Juke Box Hero.mp3",
            f"{new}/Bonus/03 Cold as Ice.mp3",
        ]  # fmt: skip
        assert (library / new / "folder.jpg").exists()  # the cover went along
        assert not (library / ALBUM).exists()

        # Writing after the move writes the files where they are now
        found = wr.survey(session, lib, wr.Selection(path=new + "/"))
        assert found.total == 3
        _, outcomes = wr.write(session, found.files, "write")
        assert all(o.ok for o in outcomes), outcomes
        assert tags.read(library / new / "02 Juke Box Hero.mp3").fields["genre"] == "Rock"

        outcomes = ch.undo(session, edit.changeset)
        assert all(o.ok for o in outcomes), outcomes
        assert (library / ALBUM / "folder.jpg").exists()
        assert not (library / new).exists()


def test_edit_album_takes_the_folders_other_files_along(Session, library):
    """The .cue, .nfo, booklet and a Scans folder go with the last track, so the old
    folder disappears; undo brings them back."""
    others = ["folder.jpg", "Best of Foriegner.cue", "info.nfo", "booklet.pdf",
              "Scans/back.jpg", "Scans/inlay/1.png", "Bonus/notes.txt"]  # fmt: skip
    for name in others:
        (library / ALBUM / name).parent.mkdir(parents=True, exist_ok=True)
        (library / ALBUM / name).write_bytes(name.encode())
    with Session() as session:
        lib = session.scalar(select(Library))
        files = album_files(session, lib.id, album="Best of Foriegner")
        edit = ae.edit(session, lib, files, {"album": "Best of Foreigner"}, "edit", move_files=True)
        described = [ch.describe(c) for c in edit.moves]
        assert described[-2:] == [
            f"move {ALBUM}/02 Juke Box Hero.mp3 -> Foreigner/Best of Foreigner/02 Juke Box "
            "Hero.mp3 with 6 other files: Best of Foriegner.cue, Scans/back.jpg, "
            "Scans/inlay/1.png, booklet.pdf, folder.jpg, info.nfo",
            f"move {ALBUM}/Bonus/03 Cold as Ice.mp3 -> Foreigner/Best of Foreigner/Bonus/03 "
            "Cold as Ice.mp3 with 1 other file: notes.txt",
        ]
        page = TestClient(create_app(Session)).get(f"/changes/{edit.changeset.id}").text
        assert "with 6 other files:" in page and "Scans/inlay/1.png" in page
        ch.set_status(edit.changeset.changes, "approved")
        outcomes = ch.apply(session, edit.changeset)
        assert all(o.ok for o in outcomes), outcomes
        new = library / "Foreigner/Best of Foreigner"
        assert all((new / name).read_bytes() == name.encode() for name in others)
        assert not (library / ALBUM).exists()

        outcomes = ch.undo(session, edit.changeset)
        assert all(o.ok for o in outcomes), outcomes
        assert all((library / ALBUM / name).read_bytes() == name.encode() for name in others)
        assert not new.exists()


def test_edit_album_leaves_taken_paths(Session, library, copy_audio):
    copy_audio("mp3b", library / "Foreigner/Best of Foreigner/01 Urgent.mp3")
    with Session() as session:
        lib = session.scalar(select(Library))
        scan(session, lib)
        files = album_files(session, lib.id, folder=ALBUM)
        edit = ae.edit(session, lib, files, {"album": "Best of Foreigner"}, "edit", move_files=True)
        assert [c.payload["to"] for c in edit.moves] == [
            "Foreigner/Best of Foreigner/02 Juke Box Hero.mp3"
        ]
        [(file, _, why)] = edit.plan.conflicts
        assert file.path == f"{ALBUM}/01 Urgent.mp3" and why.startswith("taken by")


def test_edit_album_case_only(Session, library):
    """Changing only the letter case moves nothing: the folder is the same one on
    case-insensitive file systems."""
    with Session() as session:
        lib = session.scalar(select(Library))
        files = album_files(session, lib.id, folder=ALBUM)
        edit = ae.edit(session, lib, files, {"album": "BEST OF FORIEGNER"}, "edit", move_files=True)
        assert edit.moves == [] and edit.plan.case_only == 2


def test_cli_edit_album_moving_files(Session, library):
    db = str(Session.kw["bind"].url)
    result = CliRunner().invoke(
        app, ["--db", db, "edit-album", "--folder", ALBUM, "--set", "album=Best of Foreigner",
              "--move-files"],
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    assert (
        f"move {ALBUM}/01 Urgent.mp3 -> Foreigner/Best of Foreigner/01 Urgent.mp3" in result.output
    )


def test_web_rename_album_link_and_moves(Session, library):
    web = TestClient(create_app(Session))
    page = web.get("/libraries/music/albums").text
    link = re.search(r'href="([^"]*edit=true[^"]*)">Rename album', page).group(1)
    page = web.get(link.replace("&amp;", "&")).text
    assert '<details class="album-edit" id="edit-album" open>' in page
    assert 'value="Best of Foriegner"' in page

    response = web.post(
        "/libraries/music/edit-album",
        data={"of_album": "Best of Foriegner", "album": "Best of Foreigner",
              "move_files": "true"},
    )  # fmt: skip
    assert re.fullmatch(r"/changes/\d+", response.url.path)
    assert "Review the 3 moves renaming the folders" in response.text
    assert "The tags of 3 files are not yet written" in response.text
    assert "Review and write them" in response.text
    assert "move Foreigner/Best of Foriegner/Bonus/03 Cold as Ice.mp3" in response.text
