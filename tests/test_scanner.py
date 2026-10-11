import os

from sqlalchemy import select

from audio_organizer.models import AudioFile, Library, TagSnapshot
from audio_organizer.provenance import resolve, set_value
from audio_organizer.scanner import case_aliases, scan


def _library(session, root):
    library = Library(name="test", root=str(root))
    session.add(library)
    session.commit()
    return library


def _files(session):
    return {f.path: f for f in session.scalars(select(AudioFile))}


def test_scan_new_unchanged_changed(Session, copy_audio, tmp_path):
    root = tmp_path / "music"
    copy_audio("mp3", root / "Texas/Greatest Hits/11 Halo.mp3")
    copy_audio("m4a", root / "P!nk/Funhouse/01 So What.m4a")
    (root / "Texas/desktop.ini").write_text("x")
    copy_audio("mp3", root / "Texas/._11 Halo.mp3")
    copy_audio("mp3", root / "@Recycle/old.mp3")

    with Session() as session:
        library = _library(session, root)
        stats = scan(session, library)
        assert (stats.seen, stats.new, stats.unchanged) == (2, 2, 0)
        files = _files(session)
        halo = files["Texas/Greatest Hits/11 Halo.mp3"]
        resolved = resolve(halo.values)
        assert resolved["album"].value == "Greatest Hits"
        assert resolved["album"].source == "tag"
        assert resolved["tracknumber"].candidates == {"tag": "11", "path": "11"}
        assert session.scalar(select(TagSnapshot).where(TagSnapshot.file_id == halo.id))

        stats = scan(session, library)
        assert (stats.new, stats.changed, stats.unchanged) == (0, 0, 2)

        path = root / "Texas/Greatest Hits/11 Halo.mp3"
        os.utime(path, ns=(0, path.stat().st_mtime_ns + 10**9))
        stats = scan(session, library)
        assert (stats.new, stats.changed, stats.unchanged) == (0, 1, 1)


def test_move_keeps_identity_and_manual_edits(Session, copy_audio, tmp_path):
    root = tmp_path / "music"
    old = copy_audio("mp3", root / "Texas/Texas_Halo.mp3")
    with Session() as session:
        library = _library(session, root)
        scan(session, library)
        file = _files(session)["Texas/Texas_Halo.mp3"]
        file_id = file.id
        set_value(session, file, "manual", "genre", "Rock")
        session.commit()

        new = root / "Texas/Greatest Hits/11 Halo.mp3"
        new.parent.mkdir(parents=True)
        old.rename(new)
        stats = scan(session, library)
        assert (stats.moved, stats.new, stats.missing) == (1, 0, 0)
        session.expire_all()
        file = session.get(AudioFile, file_id)
        assert file.path == "Texas/Greatest Hits/11 Halo.mp3"
        resolved = resolve(file.values)
        assert resolved["genre"].value == "Rock"
        assert resolved["album"].candidates["path"] == "Greatest Hits"
        assert len(_files(session)) == 1


def test_missing_and_reappearing(Session, copy_audio, tmp_path):
    root = tmp_path / "music"
    path = copy_audio("mp3", root / "a/b/01 x.mp3")
    with Session() as session:
        library = _library(session, root)
        scan(session, library)
        hidden = path.rename(tmp_path / "elsewhere.mp3")
        assert scan(session, library).missing == 1
        assert _files(session)["a/b/01 x.mp3"].missing
        hidden.rename(path)
        stats = scan(session, library)
        assert (stats.missing, stats.unchanged) == (0, 1)
        session.expire_all()
        assert not _files(session)["a/b/01 x.mp3"].missing


def test_unreadable_file_is_recorded(Session, tmp_path):
    root = tmp_path / "music"
    (root / "x").mkdir(parents=True)
    (root / "x/broken.mp3").write_bytes(b"not really audio" * 10)
    with Session() as session:
        library = _library(session, root)
        stats = scan(session, library)
        assert len(stats.errors) == 1
        assert _files(session)["x/broken.mp3"].error
        assert scan(session, library).unchanged == 1  # not retried until it changes
        stats = scan(session, library, retry_errors=True)
        assert (stats.unchanged, stats.changed, len(stats.errors)) == (0, 1, 1)


def test_scan_remembers_the_images_of_folders(Session, copy_audio, tmp_path):
    from audio_organizer.models import FolderImages

    root = tmp_path / "music"
    copy_audio("mp3", root / "Texas/Greatest Hits/11 Halo.mp3")
    copy_audio("mp3", root / "Texas/Other/01 x.mp3")
    (root / "Texas/Greatest Hits/Cover.JPG").write_bytes(b"x")
    (root / "Texas/Greatest Hits/notes.txt").write_text("x")

    def remembered(session):
        return {f.folder: f for f in session.scalars(select(FolderImages))}

    with Session() as session:
        library = _library(session, root)
        scan(session, library)
        found = remembered(session)
        assert set(found) == {"Texas/Greatest Hits", "Texas/Other"}
        assert found["Texas/Greatest Hits"].images == ["Cover.JPG"]
        assert found["Texas/Greatest Hits"].cover == "Cover.JPG"
        assert found["Texas/Other"].cover is None

        (root / "Texas/Other/folder.png").write_bytes(b"x")
        scan(session, library)
        assert remembered(session)["Texas/Other"].cover == "folder.png"

        (root / "Texas/Other/01 x.mp3").unlink()
        (root / "Texas/Other/folder.png").unlink()
        (root / "Texas/Other").rmdir()
        scan(session, library)
        assert set(remembered(session)) == {"Texas/Greatest Hits"}  # a gone folder is forgotten


class _Entry:
    """A directory entry as a case-insensitive share lists it: its own name, but a path
    that opens another entry."""

    def __init__(self, name, path):
        self.name, self.path = name, str(path)

    def is_dir(self, follow_symlinks=True):
        return os.path.isdir(self.path)

    def stat(self, follow_symlinks=True):
        return os.stat(self.path)


def test_case_aliases_are_one_folder_under_two_names(tmp_path):
    (tmp_path / "Album").mkdir()
    (tmp_path / "Other").mkdir()
    (tmp_path / "links").mkdir()
    (tmp_path / "links/album").symlink_to(tmp_path / "Album")
    first = _Entry("Album", tmp_path / "Album")
    alias = _Entry("album", tmp_path / "links/album")
    assert case_aliases([first, alias]) == {"album": first}
    # Names written with a composed or a decomposed "ä" are one name too
    assert case_aliases([_Entry("Ä", tmp_path / "Album"), _Entry("ä", alias.path)])
    # Folders on a case-sensitive disk whose names differ in case are different folders
    assert case_aliases([first, _Entry("album", tmp_path / "Other")]) == {}


def test_scan_skips_and_reports_case_aliases(Session, copy_audio, tmp_path, monkeypatch):
    """A case-insensitive share lists "Greatest Hits" and "greatest hits", but opens one
    folder under both names: its files are cataloged once, and rows that an earlier scan
    made under the other name become missing."""
    root = tmp_path / "music"
    copy_audio("mp3", root / "Texas/Greatest Hits/11 Halo.mp3")
    (tmp_path / "links").mkdir()
    link = tmp_path / "links/greatest hits"
    link.symlink_to(root / "Texas/Greatest Hits")
    real_scandir = os.scandir

    class Listing(list):  # what scandir gives: an iterable that is also a context manager
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def scandir(path="."):
        if os.fspath(path) != str(root / "Texas"):
            return real_scandir(path)
        with real_scandir(path) as entries:
            return Listing([*entries, _Entry("greatest hits", link)])

    with Session() as session:
        library = _library(session, root)
        scan(session, library)
        halo = _files(session)["Texas/Greatest Hits/11 Halo.mp3"]
        twin = AudioFile(
            library=library, path="Texas/greatest hits/11 Halo.mp3", size=halo.size,
            mtime_ns=halo.mtime_ns, audio_hash=halo.audio_hash,
        )  # fmt: skip
        session.add(twin)
        session.commit()

        monkeypatch.setattr(os, "scandir", scandir)
        stats = scan(session, library)
        assert stats.aliases == [("Texas/greatest hits", "Texas/Greatest Hits")]
        assert (stats.seen, stats.unchanged, stats.missing) == (1, 1, 1)
        session.expire_all()
        files = _files(session)
        assert files["Texas/greatest hits/11 Halo.mp3"].missing
        assert not files["Texas/Greatest Hits/11 Halo.mp3"].missing
