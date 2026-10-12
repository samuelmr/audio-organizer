import os
import threading
import unicodedata

import pytest
from sqlalchemy import select

from audio_organizer import changes as ch
from audio_organizer import tags
from audio_organizer.models import AudioFile, Changeset, Library
from audio_organizer.provenance import resolve, set_value
from audio_organizer.scanner import scan


@pytest.fixture
def catalog(Session, copy_audio, tmp_path):
    root = tmp_path / "music"
    copy_audio("mp3", root / "Texas/Texas_Halo.mp3")
    session = Session()
    library = Library(name="test", root=str(root))
    session.add(library)
    session.commit()
    scan(session, library)
    file = session.scalar(select(AudioFile))
    yield session, root, file
    session.close()


def test_retag_apply_and_undo(catalog):
    session, root, file = catalog
    set_value(session, file, "manual", "album", "The Greatest Hits")
    changeset = ch.new_changeset(session, "edit", "writing")
    change = ch.propose_retag(changeset, file, {"album": "The Greatest Hits"})
    session.commit()

    assert ch.apply(session, changeset) == []  # nothing approved yet
    ch.set_status([change], "approved")
    dry = ch.apply(session, changeset, dry_run=True)
    assert dry[0].message.startswith("would retag")
    assert tags.read(root / file.path).fields["album"] == "Greatest Hits"

    [outcome] = ch.apply(session, changeset)
    assert outcome.ok, outcome.message
    assert change.status == "applied"
    assert tags.read(root / file.path).fields["album"] == "The Greatest Hits"
    resolved = resolve(file.values)["album"]
    assert resolved.candidates["tag"] == "The Greatest Hits"
    assert scan(session, file.library).unchanged == 1  # catalog is up to date

    [outcome] = ch.undo(session, changeset)
    assert outcome.ok, outcome.message
    assert tags.read(root / file.path).fields["album"] == "Greatest Hits"
    assert change.status == "undone"
    assert resolve(file.values)["album"].value == "The Greatest Hits"  # the edit stays


def test_undo_of_an_earlier_edit_changeset_reverts_the_edit(catalog):
    """Before edits were written with other pending changes, each edit proposed its own
    changeset, remembering the manual values it replaced; undoing one reverts them."""
    session, root, file = catalog
    set_value(session, file, "manual", "genre", "Rock")  # an earlier edit, not yet in the tags
    set_value(session, file, "manual", "album", "The Greatest Hits")
    changeset = ch.new_changeset(session, "edit", "manual")
    change = ch.propose_retag(changeset, file, {"album": "The Greatest Hits", "genre": "Rock"})
    change.payload = {**change.payload, "manual_before": {"album": None}}
    ch.set_status([change], "approved")
    assert all(o.ok for o in ch.apply(session, changeset))

    [outcome] = ch.undo(session, changeset)
    assert outcome.ok, outcome.message
    resolved = resolve(file.values)
    assert resolved["album"].value == "Greatest Hits"  # the manual edit is gone
    assert resolved["genre"].value == "Rock"  # the earlier edit is kept


def test_move_apply_and_undo(catalog):
    session, root, file = catalog
    changeset = ch.new_changeset(session, "reorganize", "manual")
    change = ch.propose_move(changeset, file, "Texas/Greatest Hits/11 Halo.mp3")
    ch.set_status([change], "approved")
    [outcome] = ch.apply(session, changeset)
    assert outcome.ok, outcome.message
    assert (root / "Texas/Greatest Hits/11 Halo.mp3").exists()
    assert ch.describe(change) == "move Texas/Texas_Halo.mp3 -> Texas/Greatest Hits/11 Halo.mp3"
    assert not (root / "Texas/Texas_Halo.mp3").exists()
    assert resolve(file.values)["album"].candidates["path"] == "Greatest Hits"

    [outcome] = ch.undo(session, changeset)
    assert outcome.ok, outcome.message
    assert (root / "Texas/Texas_Halo.mp3").exists()
    assert not (root / "Texas/Greatest Hits").exists()  # emptied folder removed


def test_refuses_to_touch_file_changed_since_scan(catalog):
    session, root, file = catalog
    path = root / file.path
    tags.write(path, {"title": "Changed"})
    changeset = ch.new_changeset(session, "edit", "manual")
    change = ch.propose_retag(changeset, file, {"genre": "Rock"})
    ch.set_status([change], "approved")
    [outcome] = ch.apply(session, changeset)
    assert not outcome.ok
    assert "changed since it was scanned" in change.error
    assert tags.read(path).fields["genre"] == "Pop"


def test_move_refuses_to_overwrite(catalog, copy_audio):
    session, root, file = catalog
    copy_audio("mp3b", root / "Texas/other.mp3")
    changeset = ch.new_changeset(session, "reorganize", "manual")
    change = ch.propose_move(changeset, file, "Texas/other.mp3")
    ch.set_status([change], "approved")
    [outcome] = ch.apply(session, changeset)
    assert not outcome.ok and "already exists" in outcome.message


def test_move_to_another_unicode_form_of_its_own_name(Session, copy_audio, tmp_path):
    """On volumes that treat NFC and NFD names as one (macOS, NAS shares) the target
    "exists": it is the file itself, so it is renamed and not refused."""
    nfd, nfc = (
        unicodedata.normalize("NFD", "Hittejä.mp3"),
        unicodedata.normalize("NFC", "Hittejä.mp3"),
    )
    root = tmp_path / "music"
    copy_audio("mp3", root / "Aknestik" / nfd)
    session = Session()
    library = Library(name="test", root=str(root))
    session.add(library)
    session.commit()
    scan(session, library)
    file = session.scalar(select(AudioFile))
    assert file.path == f"Aknestik/{nfd}"

    changeset = ch.new_changeset(session, "reorganize", "manual")
    change = ch.propose_move(changeset, file, f"Aknestik/{nfc}")
    ch.set_status([change], "approved")
    [outcome] = ch.apply(session, changeset)
    assert outcome.ok, outcome.message
    assert file.path == f"Aknestik/{nfc}"
    assert [p.name for p in (root / "Aknestik").iterdir()] == [nfc]  # no temporary left

    [outcome] = ch.undo(session, changeset)
    assert outcome.ok, outcome.message
    assert file.path == f"Aknestik/{nfd}"
    session.close()


def test_retry_failed_change_after_cause_is_gone(catalog, copy_audio):
    session, root, file = catalog
    blocker = root / "Texas/other.mp3"
    copy_audio("mp3b", blocker)
    changeset = ch.new_changeset(session, "reorganize", "manual")
    change = ch.propose_move(changeset, file, "Texas/other.mp3")
    ch.set_status([change], "approved")
    [outcome] = ch.apply(session, changeset)
    assert not outcome.ok and change.status == "failed"

    blocker.unlink()
    assert ch.retry_failed(changeset) == 1
    assert (change.status, change.error) == ("approved", None)
    [outcome] = ch.apply(session, changeset)
    assert outcome.ok, outcome.message
    assert (root / "Texas/other.mp3").exists()


def test_drop_failed_keeps_applied_changes_and_their_undo(catalog, copy_audio):
    session, root, file = catalog
    copy_audio("mp3b", root / "Texas/other.mp3")
    changeset = ch.new_changeset(session, "reorganize", "manual")
    bad = ch.propose_move(changeset, file, "Texas/other.mp3")
    ch.set_status([bad], "approved")
    ch.apply(session, changeset)
    assert bad.status == "failed"
    good = ch.propose_move(changeset, file, "Texas/Halo.mp3")
    ch.set_status([good], "approved")
    ch.apply(session, changeset)

    assert ch.drop_failed(session, changeset) == 1
    assert [c.status for c in changeset.changes] == ["applied"]
    assert ch.drop_failed(session, changeset) == 0
    [outcome] = ch.undo(session, changeset)
    assert outcome.ok, outcome.message


def test_delete_changeset_only_without_applied_changes(catalog):
    session, root, file = catalog
    changeset = ch.new_changeset(session, "reorganize", "manual")
    change = ch.propose_move(changeset, file, "Texas/Halo.mp3")
    ch.set_status([change], "approved")
    ch.apply(session, changeset)
    with pytest.raises(ch.ChangeError, match="undo"):
        ch.delete_changeset(session, changeset)

    ch.undo(session, changeset)
    ch.delete_changeset(session, changeset)
    assert session.scalars(select(Changeset)).all() == []


def test_unreadable_audio_fails_the_change_instead_of_crashing(catalog, monkeypatch):
    from mutagen.mp3 import HeaderNotFoundError

    session, root, file = catalog
    (root / "cover.jpg").write_bytes(b"\xff\xd8\xff\xe0" + b"\0" * 100)
    changeset = ch.new_changeset(session, "cover", "manual")
    change = ch.propose_embed_cover(changeset, file, "cover.jpg")
    session.commit()
    ch.set_status([change], "approved")

    def unreadable(path):
        raise HeaderNotFoundError("can't sync to MPEG frame")

    monkeypatch.setattr(tags, "read", unreadable)
    [outcome] = ch.apply(session, changeset)
    assert not outcome.ok and "can't sync to MPEG frame" in outcome.message
    assert change.status == "failed"


def _recording_threads(monkeypatch) -> set[str]:
    """The names of the threads that retag files from now on."""
    threads, retag_file = set(), ch._retag_file

    def recording(*args):
        threads.add(threading.current_thread().name)
        return retag_file(*args)

    monkeypatch.setattr(ch, "_retag_file", recording)
    return threads


def test_retags_of_many_files_run_side_by_side(catalog, copy_audio, monkeypatch):
    """On a network share each file's first access waits on round trips, so a changeset
    that only retags files writes several at once; the catalog is updated in order, a
    failure stays the failed file's own, and undo works as for any retag."""
    session, root, _ = catalog
    for name in ("One", "Two", "Three"):
        copy_audio("mp3", root / f"Texas/{name}.mp3")
    scan(session, session.scalar(select(Library)))
    files = session.scalars(select(AudioFile).order_by(AudioFile.path)).all()
    changeset = ch.new_changeset(session, "edit", "writing")
    changes = [ch.propose_retag(changeset, f, {"genre": f"Pop {i}"}) for i, f in enumerate(files)]
    ch.set_status(changes, "approved")
    session.commit()
    tags.write(root / files[1].path, {"title": "Changed"})  # changed since the scan
    genre = tags.read(root / files[0].path).fields.get("genre")
    threads = _recording_threads(monkeypatch)
    applied = []
    outcomes = ch.apply(session, changeset, applied=lambda o: applied.append(o.change))
    assert threads and all(name.startswith("retag") for name in threads)
    assert applied == [o.change for o in outcomes] == changes
    assert [o.ok for o in outcomes] == [True, False, True, True]
    assert "has changed since it was scanned" in outcomes[1].message
    for i in (0, 2, 3):
        assert tags.read(root / files[i].path).fields["genre"] == f"Pop {i}"
        assert resolve(files[i].values)["genre"].candidates["tag"] == f"Pop {i}"

    assert all(o.ok for o in ch.undo(session, changeset))
    assert tags.read(root / files[0].path).fields.get("genre") == genre
    assert scan(session, files[0].library).unchanged == 3  # the catalog is up to date


def _two_files(session, root, copy_audio) -> list[AudioFile]:
    copy_audio("mp3", root / "Texas/Two.mp3")
    scan(session, session.scalar(select(Library)))
    return session.scalars(select(AudioFile).order_by(AudioFile.path)).all()


@pytest.mark.parametrize("count", [1, 2])
def test_a_file_with_only_a_new_mtime_is_compared_with_the_catalog(catalog, copy_audio, count):
    """Backups, sync tools and network shares change modification times: a file whose
    tags and audio are as scanned is written, one change at a time or several at once,
    and the catalog takes its new time."""
    session, root, _ = catalog
    files = _two_files(session, root, copy_audio)[:count]
    changeset = ch.new_changeset(session, "edit", "writing")
    changes = [ch.propose_retag(changeset, f, {"genre": "Pop"}) for f in files]
    ch.set_status(changes, "approved")
    session.commit()
    for file in files:
        os.utime(root / file.path, ns=(0, 0))
    outcomes = ch.apply(session, changeset)
    assert all(o.ok for o in outcomes), [o.message for o in outcomes]
    for file in files:
        assert tags.read(root / file.path).fields["genre"] == "Pop"
        assert file.mtime_ns == (root / file.path).stat().st_mtime_ns
    assert scan(session, files[0].library).unchanged == 2


@pytest.mark.parametrize("count", [1, 2])
@pytest.mark.parametrize("difference", ["tags", "audio"])
def test_a_file_of_the_same_size_with_other_content_is_not_written(
    catalog, copy_audio, count, difference
):
    """A new modification time with tags or audio that differ from the catalog's is a
    change made elsewhere, even when the size stays (as mp3gain changes the audio)."""
    session, root, _ = catalog
    files = _two_files(session, root, copy_audio)[:count]
    changeset = ch.new_changeset(session, "edit", "writing")
    changes = [ch.propose_retag(changeset, f, {"genre": "Jazz"}) for f in files]
    ch.set_status(changes, "approved")
    session.commit()
    changed = files[0]
    os.utime(root / changed.path, ns=(0, 0))
    if difference == "tags":  # as if the file had another title than the catalog knows
        set_value(session, changed, "tag", "title", "Something Else")
    else:
        changed.audio_hash = "0" * 32
    session.commit()
    outcomes = ch.apply(session, changeset)
    assert not outcomes[0].ok
    assert "has changed since it was scanned" in outcomes[0].message
    assert tags.read(root / changed.path).fields["genre"] == "Pop"
    assert all(o.ok for o in outcomes[1:])


def test_the_mtime_stored_after_a_write_is_the_one_the_file_keeps(catalog, monkeypatch):
    """On a network share the server sets the modification time when the written data
    reaches it, so a stat straight after writing gave the old time. Flushing first gives
    the time the file keeps (here, a fake share sets it on fsync)."""
    session, root, file = catalog
    path, kept = root / file.path, 1_700_000_000_000_000_000
    write, fsync = tags.write, os.fsync

    def cached_write(target, *args, **kwargs):
        before = os.stat(target).st_mtime_ns
        write(target, *args, **kwargs)
        os.utime(target, ns=(before, before))  # still in the client's cache

    def flushing(fd):
        fsync(fd)
        os.utime(path, ns=(kept, kept))  # arrived at the server

    monkeypatch.setattr(tags, "write", cached_write)
    monkeypatch.setattr(os, "fsync", flushing)
    changeset = ch.new_changeset(session, "edit", "writing")
    ch.set_status([ch.propose_retag(changeset, file, {"genre": "Pop"})], "approved")
    session.commit()
    assert all(o.ok for o in ch.apply(session, changeset))
    assert file.mtime_ns == kept == path.stat().st_mtime_ns


def test_changesets_that_do_more_than_retag_apply_in_order(catalog, monkeypatch):
    """A move or trash may depend on the changes before it: no change runs ahead."""
    session, root, file = catalog
    changeset = ch.new_changeset(session, "edit", "writing")
    changes = [
        ch.propose_retag(changeset, file, {"genre": "Pop"}),
        ch.propose_move(changeset, file, "Texas/Halo.mp3"),
    ]
    ch.set_status(changes, "approved")
    session.commit()
    threads = _recording_threads(monkeypatch)
    assert all(o.ok for o in ch.apply(session, changeset))
    assert threads == {threading.current_thread().name}
    assert tags.read(root / "Texas/Halo.mp3").fields["genre"] == "Pop"


def test_retag_the_file_does_not_keep_fails(Session, copy_audio, tmp_path):
    """MP4 keeps a track number only as a number: a retag it cannot store must not be
    reported as applied, with the file still pending."""
    root = tmp_path / "music"
    copy_audio("m4a", root / "P!nk/Funhouse/01 So What.m4a")
    session = Session()
    library = Library(name="test", root=str(root))
    session.add(library)
    session.commit()
    scan(session, library)
    file = session.scalar(select(AudioFile))
    changeset = ch.new_changeset(session, "edit", "writing")
    change = ch.propose_retag(changeset, file, {"genre": "Rock", "tracknumber": "Intro"})
    change.status = "approved"
    session.commit()
    [outcome] = ch.apply(session, changeset)
    assert not outcome.ok
    assert "did not keep tracknumber" in outcome.message
    assert change.status == "failed"
    assert resolve(file.values)["genre"].candidates["tag"] == "Rock"  # the catalog has the file's
    session.close()
