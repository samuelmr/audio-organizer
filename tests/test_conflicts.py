import shutil

import pytest
from sqlalchemy import select
from typer.testing import CliRunner

from audio_organizer import conflicts as cf
from audio_organizer import operations as ops
from audio_organizer import tags
from audio_organizer import writing as wr
from audio_organizer.cli import app
from audio_organizer.models import AudioFile, EffectiveMetadata, FieldConflict, Library
from audio_organizer.provenance import resolve
from audio_organizer.scanner import scan

HALO = "Texas/Greatest Hits/11 Halo.mp3"


@pytest.fixture
def library(Session, copy_audio, tmp_path):
    """Halo (genre Pop in its tag) and So What by P!nk."""
    root = tmp_path / "music"
    copy_audio("mp3", root / HALO)
    copy_audio("m4a", root / "P!nk/Funhouse/01 So What.m4a")
    session = Session()
    lib = Library(name="music", root=str(root))
    session.add(lib)
    session.commit()
    scan(session, lib)
    yield session, lib, root
    session.close()


def _halo(session):
    return session.scalar(select(AudioFile).where(AudioFile.path == HALO))


def _edit(session, values):
    wr.store_edit(session, [_halo(session)], values, "edit")


def _retag_elsewhere(session, lib, root, values, path=HALO):
    """Another program changes the file's tags; then a rescan."""
    tags.write(root / path, values)
    stats = scan(session, lib)
    session.expire_all()
    return stats


def _conflicts(session):
    return [(c.field, c.decided, c.before, c.after) for c in session.scalars(select(FieldConflict))]


def _pending(session, lib):
    return {p.file.path: p.fields for p in wr.survey(session, lib, wr.Selection()).files}


def test_a_tag_changed_elsewhere_after_an_edit_is_held_back(library):
    session, lib, root = library
    _edit(session, {"genre": "Rock", "title": "Halo (edit)"})
    stats = _retag_elsewhere(session, lib, root, {"genre": "Jazz"})
    assert stats.conflicts == 1
    assert _conflicts(session) == [("genre", "Rock", "Pop", "Jazz")]
    # The title was not retagged, so it is still pending; the genre waits for the user
    assert _pending(session, lib) == {HALO: {"title": "Halo (edit)"}}
    found = wr.survey(session, lib, wr.Selection())
    assert found.conflicts == 1 and found.by_field == {"title": 1}
    assert session.get(EffectiveMetadata, _halo(session).id).disputed
    assert resolve(_halo(session).values)["genre"].value == "Rock"  # the edit still wins


def test_a_written_edit_retagged_elsewhere_is_a_conflict(library):
    session, lib, root = library
    _edit(session, {"genre": "Rock"})
    wr.write(session, wr.survey(session, lib, wr.Selection()).files, "write")
    assert _retag_elsewhere(session, lib, root, {"genre": "Jazz"}).conflicts == 1
    assert _conflicts(session) == [("genre", "Rock", "Rock", "Jazz")]
    assert _pending(session, lib) == {}


def test_other_retags_are_no_conflict(library):
    session, lib, root = library
    _edit(session, {"genre": "Rock"})
    assert _retag_elsewhere(session, lib, root, {"title": "Halo!"}).conflicts == 0
    assert _retag_elsewhere(session, lib, root, {"genre": "rock"}).conflicts == 0  # agrees
    assert _conflicts(session) == []
    assert _pending(session, lib) == {HALO: {"genre": "Rock"}}  # only the spelling differs
    # Without an edit, a retag is simply the new evidence
    tags.write(root / "P!nk/Funhouse/01 So What.m4a", {"genre": "Rock"})
    assert scan(session, lib).conflicts == 0


def test_keep_edits_makes_them_pending_again(library):
    session, lib, root = library
    _edit(session, {"genre": "Rock"})
    _retag_elsewhere(session, lib, root, {"genre": "Jazz"})
    assert cf.keep_edits(session, cf.find(session, lib.id).rows) == 1
    session.commit()
    assert _conflicts(session) == []
    assert _pending(session, lib) == {HALO: {"genre": "Rock"}}
    assert not session.get(EffectiveMetadata, _halo(session).id).disputed


def test_take_files_drops_the_edit_and_can_be_undone(library):
    session, lib, root = library
    _edit(session, {"genre": "Rock"})
    _retag_elsewhere(session, lib, root, {"genre": "Jazz"})
    assert cf.take_files(session, lib.id, cf.find(session, lib.id).rows) == 1
    session.commit()
    assert _conflicts(session) == []
    assert resolve(_halo(session).values)["genre"].value == "Jazz"
    assert _pending(session, lib) == {}
    [entry, _] = ops.history(session, lib.id)
    assert entry.operation.description == "Take the tags of 1 file retagged elsewhere"
    ops.undo(session, entry.operation)
    session.commit()
    assert _pending(session, lib) == {HALO: {"genre": "Rock"}}


def test_a_new_decision_or_an_agreeing_tag_settles_a_conflict(library):
    session, lib, root = library
    _edit(session, {"genre": "Rock"})
    _retag_elsewhere(session, lib, root, {"genre": "Jazz"})
    _edit(session, {"genre": "Blues"})
    assert _conflicts(session) == []
    assert _pending(session, lib) == {HALO: {"genre": "Blues"}}

    _retag_elsewhere(session, lib, root, {"genre": "Soul"})
    assert _conflicts(session) == [("genre", "Blues", "Jazz", "Soul")]
    _retag_elsewhere(session, lib, root, {"genre": "Blues"})
    assert _conflicts(session) == [] and _pending(session, lib) == {}


def test_a_moved_and_retagged_file_is_a_conflict(library):
    session, lib, root = library
    _edit(session, {"genre": "Rock"})
    shutil.move(root / HALO, root / "Texas/Halo.mp3")
    tags.write(root / "Texas/Halo.mp3", {"genre": "Jazz"})
    stats = scan(session, lib)
    assert (stats.moved, stats.conflicts) == (1, 1)
    assert _conflicts(session) == [("genre", "Rock", "Pop", "Jazz")]


def test_cli_conflicts(library):
    session, lib, root = library
    db = str(session.get_bind().url)
    run = lambda *args: CliRunner().invoke(app, ["--db", db, *args])  # noqa: E731
    run("edit", HALO, "--set", "genre=Rock")
    tags.write(root / HALO, {"genre": "Jazz"})

    result = run("scan")
    assert "1 fields were retagged elsewhere since you edited them" in result.output
    assert "1 fields retagged elsewhere since you edited them are held back" in (
        run("pending").output
    )
    result = run("conflicts")
    assert f"{HALO}\n  genre: yours 'Rock'; the tag was 'Pop', is now 'Jazz'" in result.output
    assert run("conflicts", "--keep-edits", "--take-files").exit_code == 2
    assert "No conflicts." in run("conflicts", "--path", "P!nk/").output

    result = run("conflicts", "--keep-edits")
    assert "Kept 1 edits" in result.output
    assert "genre: 'Jazz' -> 'Rock'" in run("pending", "--path", "Texas/").output
    assert "No conflicts." in run("conflicts").output
