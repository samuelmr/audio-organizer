import pytest

from audio_organizer import changes as ch
from audio_organizer import leftovers as lo
from audio_organizer.models import Library


@pytest.fixture
def library(Session, tmp_path):
    root = tmp_path / "music"
    files = {
        "Texas/The Greatest Hits/01 Halo.mp3": b"audio",
        "Texas/The Greatest Hits/Scans/folder.jpg": b"booklet",  # belongs to the album
        "Texas/Greatest Hits/Folder.jpg": b"jpg",  # emptied by a merge
        "Texas/Greatest Hits/desktop.ini": b"ini",
        "Texas/Greatest Hits/Booklet.pdf": b"pdf",
        "Texas/Greatest Hits/._Folder.jpg": b"apple",
        "Texas/Greatest Hits/halo.m3u": b"#EXTM3U\n../The Greatest Hits/01 Halo.mp3\n",
        "Texas/The Greatest Hits/AlbumArtSmall.jpg": b"small",
        "Texas/The Greatest Hits/Folder.jpg": b"bigger image",
        "Playlists/Old/best.m3u": b"#EXTM3U",
        "Playlists/Old/best.html": b"<html>",
        ".aorg-trash/1/Gone/Folder.jpg": b"trashed",
    }
    for path, data in files.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_bytes(data)
    session = Session()
    lib = Library(root=str(root), name="test")
    session.add(lib)
    session.commit()
    yield session, lib, root
    session.close()


def _approve_all(changeset):
    for change in changeset.changes:
        change.status = "approved"


def test_survey_finds_folders_without_music(library):
    _, lib, _ = library

    found = lo.survey(lib)

    assert {f.path: (len(f.litter), len(f.other)) for f in found.folders} == {
        "Texas/Greatest Hits": (3, 2),  # a playlist naming tracks is not litter
        "Playlists/Old": (2, 0),
    }
    assert (found.litter, found.other) == (5, 2)
    assert found.renames == [
        ("Texas/The Greatest Hits/Folder.jpg", "Texas/The Greatest Hits/cover.jpg")
    ]


def test_survey_limited_to_a_path(library):
    _, lib, _ = library

    assert [f.path for f in lo.survey(lib, "Playlists").folders] == ["Playlists/Old"]


def test_propose_leaves_other_files_unless_asked(library):
    session, lib, root = library

    changeset = lo.propose(session, lib, lo.survey(lib), "x")
    _approve_all(changeset)
    outcomes = ch.apply(session, changeset)

    assert all(o.ok for o in outcomes), [o.message for o in outcomes]
    assert changeset.source == "leftovers"
    assert not (root / "Playlists").exists()  # emptied folders go
    assert sorted(p.name for p in (root / "Texas/Greatest Hits").iterdir()) == [
        "Booklet.pdf",
        "halo.m3u",
    ]
    assert (root / "Texas/The Greatest Hits/cover.jpg").read_bytes() == b"bigger image"
    assert not (root / "Texas/The Greatest Hits/Folder.jpg").exists()
    assert (root / ".aorg-trash" / str(changeset.id) / "Playlists/Old/best.m3u").is_file()


def test_everything_takes_other_files_too(library):
    session, lib, root = library

    changeset = lo.propose(session, lib, lo.survey(lib), "x", everything=True)
    _approve_all(changeset)
    ch.apply(session, changeset)

    assert not (root / "Texas/Greatest Hits").exists()  # even the playlist, when asked
    assert (root / "Texas/The Greatest Hits/Scans/folder.jpg").is_file()


def test_undo_restores_files_and_folders(library):
    session, lib, root = library
    changeset = lo.propose(session, lib, lo.survey(lib), "x")
    _approve_all(changeset)
    ch.apply(session, changeset)

    outcomes = ch.undo(session, changeset)

    assert all(o.ok for o in outcomes), [o.message for o in outcomes]
    assert (root / "Playlists/Old/best.m3u").read_bytes() == b"#EXTM3U"
    assert (root / "Texas/Greatest Hits/Folder.jpg").is_file()
    assert (root / "Texas/The Greatest Hits/Folder.jpg").is_file()
    assert not (root / "Texas/The Greatest Hits/cover.jpg").exists()
    assert {c.status for c in changeset.changes} == {"undone"}


def test_a_changed_file_is_left_alone(library):
    session, lib, root = library
    changeset = lo.propose(session, lib, lo.survey(lib, "Playlists"), "x")
    _approve_all(changeset)
    (root / "Playlists/Old/best.m3u").write_bytes(b"#EXTM3U\nedited")

    outcomes = ch.apply(session, changeset)

    assert sorted(o.ok for o in outcomes) == [False, True]
    assert (root / "Playlists/Old/best.m3u").is_file()


def test_nothing_to_propose(library):
    session, lib, _ = library

    found = lo.survey(lib, "Texas/The Greatest Hits")

    assert lo.propose(session, lib, found, "x", covers=False) is None


def test_a_folder_with_a_cover_is_left_alone(tmp_path):
    album = tmp_path / "Album"
    album.mkdir()
    for name in ("Cover.JPG", "Folder.png"):
        (album / name).write_bytes(b"x")

    assert lo.cover_rename(album, ["Cover.JPG", "Folder.png"]) is None


def test_the_cover_keeps_the_images_format(tmp_path):
    (tmp_path / "AlbumArtSmall.png").write_bytes(b"x")

    assert lo.cover_rename(tmp_path, ["AlbumArtSmall.png"]) == ("AlbumArtSmall.png", "cover.png")
