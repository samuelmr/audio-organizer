import os

import pytest
from sqlalchemy import select

from audio_organizer import changes as ch
from audio_organizer import tags
from audio_organizer.models import AudioFile, Library
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


def test_manual_edit_retag_apply_and_undo(catalog):
    session, root, file = catalog
    set_value(session, file, "manual", "genre", "Rock")  # an earlier edit, not yet in the tags
    changeset = ch.new_changeset(session, "edit")
    change = ch.propose_manual_edit(session, changeset, file, {"album": "The Greatest Hits"})
    session.commit()
    assert change.payload == {
        "fields": {"album": "The Greatest Hits", "genre": "Rock"},
        "manual_before": {"album": None},
    }

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
    resolved = resolve(file.values)
    assert resolved["album"].value == "Greatest Hits"  # the manual edit is gone
    assert resolved["genre"].value == "Rock"  # the earlier edit is kept


def test_move_apply_and_undo(catalog):
    session, root, file = catalog
    changeset = ch.new_changeset(session, "reorganize")
    change = ch.propose_move(changeset, file, "Texas/Greatest Hits/11 Halo.mp3")
    ch.set_status([change], "approved")
    [outcome] = ch.apply(session, changeset)
    assert outcome.ok, outcome.message
    assert (root / "Texas/Greatest Hits/11 Halo.mp3").exists()
    assert not (root / "Texas/Texas_Halo.mp3").exists()
    assert resolve(file.values)["album"].candidates["path"] == "Greatest Hits"

    [outcome] = ch.undo(session, changeset)
    assert outcome.ok, outcome.message
    assert (root / "Texas/Texas_Halo.mp3").exists()
    assert not (root / "Texas/Greatest Hits").exists()  # emptied folder removed


def test_refuses_to_touch_file_changed_since_scan(catalog):
    session, root, file = catalog
    path = root / file.path
    os.utime(path, ns=(0, path.stat().st_mtime_ns + 10**9))
    changeset = ch.new_changeset(session, "edit")
    change = ch.propose_retag(changeset, file, {"genre": "Rock"})
    ch.set_status([change], "approved")
    [outcome] = ch.apply(session, changeset)
    assert not outcome.ok
    assert "changed since it was scanned" in change.error
    assert tags.read(path).fields["genre"] == "Pop"


def test_move_refuses_to_overwrite(catalog, copy_audio):
    session, root, file = catalog
    copy_audio("mp3b", root / "Texas/other.mp3")
    changeset = ch.new_changeset(session, "reorganize")
    change = ch.propose_move(changeset, file, "Texas/other.mp3")
    ch.set_status([change], "approved")
    [outcome] = ch.apply(session, changeset)
    assert not outcome.ok and "already exists" in outcome.message
