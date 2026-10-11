import os

import pytest
from sqlalchemy import select

from audio_organizer import changes as ch
from audio_organizer import duplicates as du
from audio_organizer import playlists as pl
from audio_organizer import tags
from audio_organizer import writing as wr
from audio_organizer.models import AudioFile, Library, Playlist
from audio_organizer.scanner import scan


@pytest.fixture
def library(Session, copy_audio, tmp_path):
    root = tmp_path / "music"
    # One track: album copy, iTunes copy next to it, flat copy with an extra tag
    copy_audio("mp3", root / "Texas/Greatest Hits/11 Halo.mp3")
    copy_audio("mp3", root / "Texas/Greatest Hits/11 Halo 1.mp3")
    flat = copy_audio("mp3", root / "Texas/Texas_Halo.mp3")
    tags.write(root / "Texas/Greatest Hits/11 Halo.mp3", {"genre": None})
    tags.write(root / "Texas/Greatest Hits/11 Halo 1.mp3", {"genre": None})
    tags.write(flat, {"mood": "Upbeat"})
    # Another: only on a compilation, besides the artist's folder
    copy_audio("mp3b", root / "Various Artists/Hits 2000/04 Insane.mp3")
    copy_audio("mp3b", root / "Texas/Texas_Insane.mp3")
    # Another: in two album folders: a decision for the user
    copy_audio("m4a", root / "P!nk/Funhouse/01 So What.m4a")
    copy_audio("m4a", root / "P!nk/Funhouse (Deluxe)/01 So What.m4a")
    session = Session()
    lib = Library(name="music", root=str(root))
    session.add(lib)
    session.commit()
    scan(session, lib)
    yield session, lib, root
    session.close()


def test_survey(library):
    session, lib, root = library
    found = du.survey(session, lib)
    assert (found.groups, found.several_albums, found.compilations) == (3, 1, 1)
    [proposal] = found.proposals
    assert proposal.keep.file.path == "Texas/Greatest Hits/11 Halo.mp3"
    assert sorted(c.file.path for c in proposal.trash) == [
        "Texas/Greatest Hits/11 Halo 1.mp3",
        "Texas/Texas_Halo.mp3",
    ]
    assert proposal.fill == {"genre": "Pop", "mood": "Upbeat"}

    found = du.survey(session, lib, include_compilations=True)
    assert [p.keep.file.path for p in found.proposals] == [
        "Texas/Greatest Hits/11 Halo.mp3",
        "Various Artists/Hits 2000/04 Insane.mp3",
    ]
    assert du.survey(session, lib, path_prefix="P!nk").proposals == []


def test_propose_apply_and_undo(library):
    session, lib, root = library
    flat = session.scalar(select(AudioFile).where(AudioFile.path == "Texas/Texas_Halo.mp3"))
    playlist = Playlist(library=lib, name="Mine", sort="manual")
    pl.set_entries(playlist, [(flat.path, flat.id)])
    session.add(playlist)
    session.commit()

    found = du.survey(session, lib)
    operation = du.decide(session, lib, found.proposals, "Trash loose copies")
    assert operation.kind == "duplicates" and len(operation.values) == 4  # 2 tags, 2 copies
    assert du.survey(session, lib).pending == 1  # not decided twice
    assert (root / "Texas/Texas_Halo.mp3").exists()  # the files are untouched
    changeset, outcomes = wr.write(session, wr.survey(session, lib, wr.Selection()).files, "x")
    assert sorted(c.kind for c in changeset.changes) == ["retag", "trash", "trash"]
    assert all(o.ok for o in outcomes), [o.message for o in outcomes]
    assert not (root / "Texas/Texas_Halo.mp3").exists()
    assert not (root / "Texas/Greatest Hits/11 Halo 1.mp3").exists()
    trashed = root / ch.TRASH_DIR / str(changeset.id) / "Texas/Texas_Halo.mp3"
    assert trashed.exists()
    kept = tags.read(root / "Texas/Greatest Hits/11 Halo.mp3").fields
    assert (kept["mood"], kept["genre"]) == ("Upbeat", "Pop")
    keeper_id = found.proposals[0].keep.file.id
    [generated] = pl.generate(session, lib, [playlist])
    assert [t.file_id for t in generated.tracks] == [keeper_id]

    # The trash folder is not scanned; trashed files stay missing
    stats = scan(session, lib)
    assert stats.new == 0
    assert du.survey(session, lib).groups == 2
    assert wr.survey(session, lib, wr.Selection()).total == 0  # written

    outcomes = ch.undo(session, changeset)
    assert all(o.ok for o in outcomes), [o.message for o in outcomes]
    assert (root / "Texas/Texas_Halo.mp3").exists()
    assert not trashed.exists()
    assert "mood" not in tags.read(root / "Texas/Greatest Hits/11 Halo.mp3").fields
    session.expire_all()
    assert not session.get(AudioFile, flat.id).missing
    assert session.get(Playlist, playlist.id).entries[0].file_id == flat.id
    assert scan(session, lib).unchanged == 7
    # The decision stays: the copies are to be trashed again
    assert wr.survey(session, lib, wr.Selection(only=["trash"])).total == 2


def test_trash_refuses_changed_file(library):
    session, lib, root = library
    found = du.survey(session, lib)
    du.decide(session, lib, found.proposals, "Trash loose copies")
    (root / "Texas/Texas_Halo.mp3").write_bytes(b"changed")
    _, outcomes = wr.write(session, wr.survey(session, lib, wr.Selection()).files, "x")
    outcomes = {o.change.file.path: o for o in outcomes if o.change.kind == "trash"}
    assert not outcomes["Texas/Texas_Halo.mp3"].ok
    assert (root / "Texas/Texas_Halo.mp3").exists()
    # What failed stays pending
    pending = wr.survey(session, lib, wr.Selection(only=["trash"]))
    assert [p.file.path for p in pending.files] == ["Texas/Texas_Halo.mp3"]


def test_trashed_path_is_free_for_another_file(library):
    """A trashed file's catalog entry moves to its path in the trash, so a better copy can
    take its place in the same changeset; undo restores both."""
    session, lib, root = library
    album = session.scalar(
        select(AudioFile).where(AudioFile.path == "Texas/Greatest Hits/11 Halo.mp3")
    )
    flat = session.scalar(select(AudioFile).where(AudioFile.path == "Texas/Texas_Halo.mp3"))
    changeset = ch.new_changeset(session, "Replace", "duplicates")
    ch.propose_trash(changeset, album, replacement=flat, same="recording")
    ch.propose_move(changeset, flat, album.path)
    session.commit()
    assert ch.describe(changeset.changes[0]) == (
        "trash Texas/Greatest Hits/11 Halo.mp3 (same recording as Texas/Texas_Halo.mp3)"
    )
    ch.set_status(changeset.changes, "approved")
    outcomes = ch.apply(session, changeset)
    assert all(o.ok for o in outcomes), [o.message for o in outcomes]
    trash_path = f"{ch.TRASH_DIR}/{changeset.id}/Texas/Greatest Hits/11 Halo.mp3"
    assert (album.path, album.missing) == (trash_path, True)
    assert flat.path == "Texas/Greatest Hits/11 Halo.mp3"
    assert ch.move_source(changeset.changes[0]) == "Texas/Greatest Hits/11 Halo.mp3"
    assert "mood" in tags.read(root / flat.path).fields  # the flat copy's tags

    outcomes = ch.undo(session, changeset)
    assert all(o.ok for o in outcomes), [o.message for o in outcomes]
    assert (album.path, album.missing) == ("Texas/Greatest Hits/11 Halo.mp3", False)
    assert flat.path == "Texas/Texas_Halo.mp3"
    assert "mood" not in tags.read(root / album.path).fields
    assert scan(session, lib).unchanged == 7


def _alias_folder(root):
    """'Texas/greatest hits' as a case-insensitive share shows it: another name of
    'Texas/Greatest Hits' (a symlink here, so that the test runs on any disk)."""
    alias = root / "Texas/Hits alias"
    alias.symlink_to(root / "Texas/Greatest Hits")
    return alias


def _apply(session, changeset):
    session.commit()
    ch.set_status(changeset.changes, "approved")
    return ch.apply(session, changeset)


def test_trash_refuses_a_copy_that_is_the_kept_file(library):
    """Two catalog rows that open one file are not copies: trashing either loses it."""
    session, lib, root = library
    _alias_folder(root)
    album = session.scalar(
        select(AudioFile).where(AudioFile.path == "Texas/Greatest Hits/11 Halo.mp3")
    )
    twin = AudioFile(
        library=lib, path="Texas/Hits alias/11 Halo.mp3", size=album.size,
        mtime_ns=album.mtime_ns, audio_hash=album.audio_hash,
    )  # fmt: skip
    session.add(twin)
    session.flush()
    changeset = ch.new_changeset(session, "Merge", "album_merge")
    ch.propose_trash(changeset, album, replacement=twin)
    [outcome] = _apply(session, changeset)
    assert not outcome.ok and "one file under two names" in outcome.message
    assert (root / "Texas/Greatest Hits/11 Halo.mp3").exists()
    assert not album.missing


def test_trash_refuses_a_leftover_that_is_the_kept_file(library):
    session, lib, root = library
    _alias_folder(root)
    (root / "Texas/Greatest Hits/cover.jpg").write_bytes(b"image")
    changeset = ch.new_changeset(session, "Merge", "album_merge")
    ch.propose_trash_leftover(
        changeset, lib, "Texas/Hits alias/cover.jpg", same_as="Texas/Greatest Hits/cover.jpg"
    )
    [outcome] = _apply(session, changeset)
    assert not outcome.ok and "one file under two names" in outcome.message
    assert (root / "Texas/Greatest Hits/cover.jpg").exists()


def test_trash_takes_a_hard_link_of_the_kept_file(library):
    """A hard link is a second link of the file: trashing it leaves the kept one."""
    session, lib, root = library
    album_path = root / "Texas/Greatest Hits/11 Halo.mp3"
    flat_path = root / "Texas/Texas_Halo.mp3"
    flat_path.unlink()
    os.link(album_path, flat_path)
    scan(session, lib)
    album, flat = (
        session.scalar(select(AudioFile).where(AudioFile.path == path))
        for path in ("Texas/Greatest Hits/11 Halo.mp3", "Texas/Texas_Halo.mp3")
    )
    changeset = ch.new_changeset(session, "Trash", "duplicates")
    ch.propose_trash(changeset, flat, replacement=album)
    [outcome] = _apply(session, changeset)
    assert outcome.ok, outcome.message
    assert album_path.exists() and not flat_path.exists()


def test_trash_refuses_when_the_kept_copy_is_gone(library):
    """The kept copy can disappear between the decision and the write (deleted outside
    aorg): the copy to trash is then the only one."""
    session, lib, root = library
    found = du.survey(session, lib)
    du.decide(session, lib, found.proposals, "Trash loose copies")
    (root / "Texas/Greatest Hits/11 Halo.mp3").unlink()

    _, outcomes = wr.write(session, wr.survey(session, lib, wr.Selection()).files, "x")

    trashes = [o for o in outcomes if o.change.kind == "trash"]
    assert trashes and not any(o.ok for o in trashes)
    assert all("is not there" in o.message for o in trashes)
    assert (root / "Texas/Texas_Halo.mp3").exists()
    assert (root / "Texas/Greatest Hits/11 Halo 1.mp3").exists()


def test_trash_refuses_a_leftover_whose_identical_copy_is_gone(library):
    session, lib, root = library
    (root / "Texas/Greatest Hits/cover.jpg").write_bytes(b"image")
    (root / "Texas/Hits/cover.jpg").parent.mkdir()
    (root / "Texas/Hits/cover.jpg").write_bytes(b"image")
    changeset = ch.new_changeset(session, "Merge", "album_merge")
    ch.propose_trash_leftover(
        changeset, lib, "Texas/Hits/cover.jpg", same_as="Texas/Greatest Hits/cover.jpg"
    )
    (root / "Texas/Greatest Hits/cover.jpg").unlink()

    [outcome] = _apply(session, changeset)

    assert not outcome.ok and "is not there" in outcome.message
    assert (root / "Texas/Hits/cover.jpg").exists()
