import os

from sqlalchemy import select

from audio_organizer import changes as ch
from audio_organizer import playlists as pl
from audio_organizer.models import AudioFile, Library, Playlist, TagSnapshot
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


def test_a_move_two_scans_saw_in_parts(Session, copy_audio, tmp_path):
    """A scan ran while the folder was being moved: the old files had gone, the new ones
    were not there yet. The next scan finds them and keeps the old record."""
    root = tmp_path / "music"
    old = copy_audio("mp3", root / "Aguilera, Christina/Back to Basics/01 Intro.mp3")
    with Session() as session:
        library = _library(session, root)
        scan(session, library)
        file = _files(session)["Aguilera, Christina/Back to Basics/01 Intro.mp3"]
        file_id = file.id
        set_value(session, file, "manual", "genre", "Pop")
        playlist = Playlist(library=library, name="Mix", sort="manual")
        pl.set_entries(playlist, [(file.path, file.id)])
        session.add(playlist)
        session.commit()

        moving = old.rename(tmp_path / "in transit.mp3")
        assert scan(session, library).missing == 1
        new = root / "Christina Aguilera/Back to Basics/01 Intro.mp3"
        new.parent.mkdir(parents=True)
        moving.rename(new)
        stats = scan(session, library)
        assert (stats.moved, stats.new) == (1, 0)
        session.expire_all()
        file = session.get(AudioFile, file_id)
        assert (file.path, file.missing) == (
            "Christina Aguilera/Back to Basics/01 Intro.mp3",
            False,
        )
        assert resolve(file.values)["genre"].value == "Pop"
        assert [e.file_id for e in session.get(Playlist, playlist.id).entries] == [file_id]


def test_a_trashed_file_is_not_taken_over(Session, copy_audio, tmp_path):
    root = tmp_path / "music"
    copy_audio("mp3", root / "a/01 x.mp3")
    copy_audio("mp3", root / "b/01 x.mp3")  # the copy that is kept
    with Session() as session:
        library = _library(session, root)
        scan(session, library)
        files = _files(session)
        changeset = ch.new_changeset(session, "Trash", "duplicates")
        ch.propose_trash(changeset, files["a/01 x.mp3"], replacement=files["b/01 x.mp3"])
        session.commit()
        ch.set_status(changeset.changes, "approved")
        assert all(o.ok for o in ch.apply(session, changeset))
        session.commit()

        copy_audio("mp3", root / "c/01 x.mp3")  # yet another copy
        stats = scan(session, library)
        assert (stats.moved, stats.new) == (0, 1)
        assert all(o.ok for o in ch.undo(session, changeset))  # and undo still works


def test_entries_of_a_deleted_copy_switch_to_the_other(Session, copy_audio, tmp_path):
    """Deleting a duplicate outside aorg does to playlists what trashing it does."""
    root = tmp_path / "music"
    copy_audio("mp3", root / "a/01 x.mp3")
    copy_audio("mp3", root / "b/01 x.mp3")
    copy_audio("mp3b", root / "c/02 y.mp3")  # no other copy
    with Session() as session:
        library = _library(session, root)
        scan(session, library)
        files = _files(session)
        playlist = Playlist(library=library, name="Mix", sort="manual")
        pl.set_entries(playlist, [(p, files[p].id) for p in ("a/01 x.mp3", "c/02 y.mp3")])
        session.add(playlist)
        session.commit()

        (root / "a/01 x.mp3").unlink()
        (root / "c/02 y.mp3").unlink()
        stats = scan(session, library)
        assert (stats.missing, stats.relinked) == (2, 1)
        session.expire_all()
        entries = session.get(Playlist, playlist.id).entries
        assert [e.file_id for e in entries] == [files["b/01 x.mp3"].id, files["c/02 y.mp3"].id]


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
    # Names written composed or decomposed ("a" + combining mark), in either case, are one
    for composed, decomposed in [("Ä", "ä"), ("Ö", "ö"), ("Å", "å"),
                                 ("É", "é"), ("ñ", "Ñ")]:  # fmt: skip
        assert case_aliases([_Entry(composed, tmp_path / "Album"), _Entry(decomposed, alias.path)])
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
