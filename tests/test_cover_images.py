"""Saving covers found online as the folder's cover.jpg."""

from test_coverart import _image, library  # noqa: F401 (a fixture)

from audio_organizer import changes as ch
from audio_organizer import covers, sync, tags
from audio_organizer.models import CoverLookup
from audio_organizer.scanner import scan


def _found_online(session, artist, album, data):
    stored = covers.store_image(session, data, "coverartarchive")
    session.add(CoverLookup(
        key=covers.album_key(artist, album), artist=artist, album=album, status="found",
        image_sha1=stored.sha1, min_pixels=200,
    ))  # fmt: skip
    session.commit()
    return stored


def _found_texas(session, tmp_path):
    jpeg = _image(tmp_path / "found.jpg", "500x500").read_bytes()
    _found_online(session, "Texas", "Greatest Hits", jpeg)
    return jpeg


def _apply(session, changeset):
    ch.set_status(changeset.changes, "approved")
    outcomes = ch.apply(session, changeset)
    assert all(o.ok for o in outcomes), [o.message for o in outcomes]


def test_found_cover_saved_in_folder(library):  # noqa: F811
    session, lib, root, tmp_path = library
    jpeg = _found_texas(session, tmp_path)
    _image(root / "Texas/Greatest Hits/AlbumArtSmall.jpg", "75x75")  # outranked by cover.jpg

    [folder] = covers.survey(session, lib, path_prefix="Texas/").folders
    assert (folder.save_as, folder.overwrite) == ("Texas/Greatest Hits/cover.jpg", False)
    changeset = covers.propose(session, lib, [folder], "Covers")
    assert [c.kind for c in changeset.changes] == ["cover", "cover", "image"]
    assert ch.describe(changeset.changes[-1]) == (
        "save the Cover Art Archive's image of Texas – Greatest Hits (500x500) as "
        "Texas/Greatest Hits/cover.jpg"
    )
    _apply(session, changeset)
    assert (root / "Texas/Greatest Hits/cover.jpg").read_bytes() == jpeg
    assert sync.find_cover(root / "Texas/Greatest Hits").name == "cover.jpg"
    assert covers.survey(session, lib, path_prefix="Texas/").folders == []

    assert all(o.ok for o in ch.undo(session, changeset))
    assert not (root / "Texas/Greatest Hits/cover.jpg").exists()
    assert (root / "Texas/Greatest Hits/AlbumArtSmall.jpg").exists()


def test_save_found_cover_for_tracks_that_have_it_embedded(library):  # noqa: F811
    """Covers embedded without saving them still get their folder image later."""
    session, lib, root, tmp_path = library
    jpeg = _found_texas(session, tmp_path)
    embedding = covers.survey(session, lib, path_prefix="Texas/", save_images=False).folders
    _apply(session, covers.propose(session, lib, embedding, "Embed"))
    assert not (root / "Texas/Greatest Hits/cover.jpg").exists()

    [folder] = covers.survey(session, lib, path_prefix="Texas/").folders
    assert folder.files == [] and folder.save_as == "Texas/Greatest Hits/cover.jpg"
    changeset = covers.propose(session, lib, [folder], "Save")
    assert [c.kind for c in changeset.changes] == ["image"]
    _apply(session, changeset)
    assert (root / "Texas/Greatest Hits/cover.jpg").read_bytes() == jpeg
    assert covers.survey(session, lib, path_prefix="Texas/").folders == []


def test_saving_replaces_a_small_cover_jpg(library):  # noqa: F811
    session, lib, root, tmp_path = library
    jpeg = _found_texas(session, tmp_path)
    thumbnail = _image(root / "Texas/Greatest Hits/Cover.JPG", "90x90").read_bytes()

    [folder] = covers.survey(session, lib, path_prefix="Texas/").folders
    assert (folder.save_as, folder.overwrite) == ("Texas/Greatest Hits/Cover.JPG", True)
    changeset = covers.propose(session, lib, [folder], "Covers")
    assert ch.describe(changeset.changes[-1]).startswith("replace Texas/Greatest Hits/Cover.JPG")
    _apply(session, changeset)
    assert (root / "Texas/Greatest Hits/Cover.JPG").read_bytes() == jpeg
    assert all(o.ok for o in ch.undo(session, changeset))
    assert (root / "Texas/Greatest Hits/Cover.JPG").read_bytes() == thumbnail


def test_saved_image_changed_since_is_not_undone(library):  # noqa: F811
    session, lib, root, tmp_path = library
    _found_texas(session, tmp_path)
    folders = covers.survey(session, lib, path_prefix="Texas/").folders
    changeset = covers.propose(session, lib, folders, "Covers")
    _apply(session, changeset)
    (root / "Texas/Greatest Hits/cover.jpg").write_bytes(b"edited elsewhere")
    outcomes = {o.change.kind: o for o in ch.undo(session, changeset)}
    assert not outcomes["image"].ok
    assert "has changed since it was saved" in outcomes["image"].message
    assert (root / "Texas/Greatest Hits/cover.jpg").read_bytes() == b"edited elsewhere"


def test_no_saving_when_not_wanted_or_ambiguous(library, copy_audio):  # noqa: F811
    session, lib, root, tmp_path = library
    _found_texas(session, tmp_path)
    [folder] = covers.survey(session, lib, path_prefix="Texas/", save_images=False).folders
    assert folder.save_as is None

    # Another album in the same folder: one cover.jpg would be wrong for one of them
    other = copy_audio("m4a", root / "Texas/Greatest Hits/99 Other.m4a")
    tags.write(other, {"album": "Another Album"})
    scan(session, lib)
    [folder] = [f for f in covers.survey(session, lib, path_prefix="Texas/").folders if f.fetched]
    assert folder.save_as is None and len(folder.files) == 2


def test_save_target():
    assert covers._save_target([], "image/jpeg") == ("cover.jpg", False)
    assert covers._save_target([], "image/png") == ("cover.png", False)
    assert covers._save_target(["COVER.jpeg"], "image/jpeg") == ("cover.jpg", False)
    assert covers._save_target(["COVER.jpeg"], "image/png") is None  # cover.jpeg would win
    assert covers._save_target(["Cover.JPG"], "image/jpeg") == ("Cover.JPG", True)


def test_cached_survey_lists_folders_only_when_needed(library, monkeypatch):  # noqa: F811
    session, lib, root, tmp_path = library
    folder = root / "Texas/Greatest Hits"
    _image(folder / "cover.jpg", "500x500")
    listed = []
    real = covers.os.listdir
    monkeypatch.setattr(covers.os, "listdir", lambda p: listed.append(p) or real(p))

    [shown] = covers.survey(session, lib, path_prefix="Texas/", cached=True).folders
    assert shown.image.name == "cover.jpg" and len(listed) == 1  # first view lists the folder
    covers.survey(session, lib, path_prefix="Texas/", cached=True)
    assert len(listed) == 1  # later views use the catalog; the unchanged folder is not listed

    (folder / "cover.jpg").rename(folder / "folder.png")  # the folder's mtime changes
    [shown] = covers.survey(session, lib, path_prefix="Texas/", cached=True).folders
    assert shown.image.name == "folder.png" and len(listed) == 2  # a shown folder is checked

    covers.survey(session, lib, path_prefix="Texas/")  # not cached: listed afresh
    assert len(listed) == 3


def test_cached_survey_trusts_the_catalog_for_folders_not_shown(library):  # noqa: F811
    session, lib, root, tmp_path = library
    folder = root / "Texas/Greatest Hits"
    assert covers.survey(session, lib, path_prefix="Texas/", cached=True).without_image == 1
    _image(folder / "cover.jpg", "500x500")
    # Only the counts of folders that are not shown come from the catalog
    assert covers.survey(session, lib, path_prefix="Texas/", cached=True).without_image == 1
    [shown] = covers.survey(session, lib, path_prefix="Texas/").folders  # checking again
    assert shown.image.name == "cover.jpg"
    assert covers.survey(session, lib, path_prefix="Texas/", cached=True).without_image == 0
