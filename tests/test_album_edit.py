import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from typer.testing import CliRunner

from audio_organizer import album_edit as ae
from audio_organizer import changes as ch
from audio_organizer import tags
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


def test_edit_album_apply_and_undo(Session, library):
    with Session() as session:
        lib_id = session.scalar(select(Library.id))
        files = album_files(session, lib_id, album="Best of Foriegner")
        changeset = ch.new_changeset(session, "edit", "manual")
        changes = ch.propose_manual_edits(session, changeset, files, {"album": "Best of Foreigner"})
        session.commit()
        assert len(changes) == 3 and len(changeset.changes) == 3
        ch.set_status(changes, "approved")
        outcomes = ch.apply(session, changeset)
        assert all(o.ok for o in outcomes), outcomes
        for file in files:
            assert tags.read(library / file.path).fields["album"] == "Best of Foreigner"
            assert tags.read(library / file.path).fields["genre"] in ("Rock", "AOR")  # kept
        assert album_files(session, lib_id, album="Best of Foriegner") == []

        outcomes = ch.undo(session, changeset)
        assert all(o.ok for o in outcomes), outcomes
        for file in files:
            assert tags.read(library / file.path).fields["album"] == "Best of Foriegner"
            assert resolve(file.values)["album"].source == "tag"  # the edit is gone too


def test_edit_album_only_changes_differing_files(Session, library):
    with Session() as session:
        lib_id = session.scalar(select(Library.id))
        files = album_files(session, lib_id, folder=ALBUM)
        changeset = ch.new_changeset(session, "edit", "manual")
        [change] = ch.propose_manual_edits(session, changeset, files, {"genre": "Rock"})
        assert change.file.path == f"{ALBUM}/02 Juke Box Hero.mp3"


def test_cli_edit_album(Session, library):
    db = str(Session.kw["bind"].url)
    result = CliRunner().invoke(
        app, ["--db", db, "edit-album", "--album", "Best of Foriegner",
              "--set", "album=Best of Foreigner"],
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    assert "3 of 3 tracks, in 2 folders" in result.output
    assert "'Best of Foriegner' -> 'Best of Foreigner'" in result.output

    result = CliRunner().invoke(
        app, ["--db", db, "edit-album", "--folder", str(library / ALBUM), "--set", "date=1992"]
    )
    assert result.exit_code == 0, result.output
    assert f"2 of 2 tracks, in {ALBUM}/" in result.output
    with Session() as session:
        assert session.scalar(select(Changeset).order_by(Changeset.id.desc())).description == (
            f"Manual edit of folder {ALBUM}/"
        )

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
    assert re.fullmatch(r"/changes/\d+", response.url.path)
    assert "review the changes to 3 files" in response.text
    with Session() as session:
        changeset = session.scalar(select(Changeset))
        assert changeset.description == "Manual edit of album Best of Foriegner"
        assert [c.payload["fields"] for c in changeset.changes] == [
            {"album": "Best of Foreigner"}
        ] * 3  # artist was as before and genre left empty: neither changed
        file = session.scalar(select(AudioFile).where(AudioFile.path.endswith("Urgent.mp3")))
        assert resolve(file.values)["album"].value == "Best of Foreigner"

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
        edit = ae.propose(
            session, lib, files, {"album": "Best of Foreigner", "genre": "Rock"}, "edit",
            move_files=True,
        )  # fmt: skip
        assert len(edit.retags) == 3 and len(edit.moves) == 3
        changes = edit.changeset.changes
        assert [c.kind for c in changes] == ["retag"] * 3 + ["move"] * 3  # tags first
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
        assert tags.read(library / new / "02 Juke Box Hero.mp3").fields["genre"] == "Rock"

        outcomes = ch.undo(session, edit.changeset)
        assert all(o.ok for o in outcomes), outcomes
        assert (library / ALBUM / "folder.jpg").exists()
        assert not (library / new).exists()
        fields = tags.read(library / ALBUM / "02 Juke Box Hero.mp3").fields
        assert (fields["album"], fields["genre"]) == ("Best of Foriegner", "AOR")


def test_edit_album_leaves_taken_paths(Session, library, copy_audio):
    copy_audio("mp3b", library / "Foreigner/Best of Foreigner/01 Urgent.mp3")
    with Session() as session:
        lib = session.scalar(select(Library))
        scan(session, lib)
        files = album_files(session, lib.id, folder=ALBUM)
        edit = ae.propose(
            session, lib, files, {"album": "Best of Foreigner"}, "edit", move_files=True
        )
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
        edit = ae.propose(
            session, lib, files, {"album": "BEST OF FORIEGNER"}, "edit", move_files=True
        )
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
    assert "review the changes to 3 files and 3 moves" in response.text
    assert "move Foreigner/Best of Foriegner/Bonus/03 Cold as Ice.mp3" in response.text
