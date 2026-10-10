import pytest
from sqlalchemy import func, select
from typer.testing import CliRunner

from audio_organizer import album_edit as ae
from audio_organizer import operations as ops
from audio_organizer import writing as wr
from audio_organizer.cli import app
from audio_organizer.models import AudioFile, Library, Operation, OperationValue
from audio_organizer.provenance import resolve
from audio_organizer.scanner import scan


@pytest.fixture
def library(Session, copy_audio, tmp_path):
    """An album of two tracks by Texas and one by P!nk."""
    root = tmp_path / "music"
    copy_audio("mp3", root / "Texas/Greatest Hits/11 Halo.mp3")
    copy_audio("mp3b", root / "Texas/Greatest Hits/12 Insane.mp3")
    copy_audio("m4a", root / "P!nk/Funhouse/01 So What.m4a")
    session = Session()
    lib = Library(name="music", root=str(root))
    session.add(lib)
    session.commit()
    scan(session, lib)
    yield session, lib
    session.close()


def _file(session, part):
    return session.scalar(select(AudioFile).where(AudioFile.path.contains(part)))


def _value(file, name):
    found = resolve(file.values).get(name)
    return (found.value, found.source) if found else None


def _edit(session, files, values, description="edit"):
    wr.store_edit(session, files, values, description)
    return session.scalars(select(Operation).order_by(Operation.id.desc())).first()


def test_an_edit_records_what_it_changed(library):
    session, lib = library
    halo = _file(session, "Halo")
    operation = _edit(session, [halo], {"genre": "Rock", "title": None}, "Edit Halo")
    assert (operation.kind, operation.description, operation.library_id) == (
        "edit", "Edit Halo", lib.id,
    )  # fmt: skip
    # Removing a manual title that was not there changes nothing, so it is not recorded
    assert [(v.file_id, v.field, v.source, v.before, v.after) for v in operation.values] == [
        (halo.id, "genre", "manual", None, "Rock")
    ]
    assert _value(halo, "genre") == ("Rock", "manual")


def test_an_edit_that_changes_nothing_is_not_recorded(library):
    session, lib = library
    halo = _file(session, "Halo")
    _edit(session, [halo], {"genre": "Rock"})
    _edit(session, [halo], {"genre": "Rock"})
    assert session.scalar(select(func.count()).select_from(Operation)) == 1


def test_undo_sets_values_back_and_can_be_undone(library):
    session, lib = library
    halo = _file(session, "Halo")
    first = _edit(session, [halo], {"genre": "Rock"})
    second = _edit(session, [halo], {"genre": "Pop-rock"})
    undone = ops.undo(session, second)
    session.commit()
    assert (undone.reverted, undone.kept) == (1, 0)
    assert undone.operation.kind == "undo" and undone.operation.reverts == second.id
    assert second.reverted_by == undone.operation.id
    assert undone.operation.description == "Undo: edit"
    assert _value(halo, "genre") == ("Rock", "manual")  # the first edit's, not the tag's
    assert [p.fields for p in wr.survey(session, lib, wr.Selection()).files] == [{"genre": "Rock"}]
    with pytest.raises(ValueError, match="already been undone"):
        ops.undo(session, second)

    ops.undo(session, undone.operation)  # redo
    session.commit()
    assert _value(halo, "genre") == ("Pop-rock", "manual")
    ops.undo(session, first)  # the second edit changed the genre since: kept
    assert _value(halo, "genre") == ("Pop-rock", "manual")


def test_undo_leaves_values_changed_since(library):
    session, lib = library
    halo, insane = _file(session, "Halo"), _file(session, "Insane")
    album = _edit(session, [halo, insane], {"album": "The Greatest Hits"})
    _edit(session, [insane], {"album": "Greatest Hits 2"})
    undone = ops.undo(session, album)
    session.commit()
    assert (undone.reverted, undone.kept) == (1, 1)
    assert _value(halo, "album")[1] == "tag"
    assert _value(insane, "album") == ("Greatest Hits 2", "manual")


def test_undo_with_nothing_left_records_nothing(library):
    session, lib = library
    halo = _file(session, "Halo")
    edit = _edit(session, [halo], {"genre": "Rock"})
    _edit(session, [halo], {"genre": "Pop-rock"})
    undone = ops.undo(session, edit)
    assert (undone.operation, undone.reverted, undone.kept) == (None, 0, 1)
    assert edit.reverted_by is None


def test_album_edit_is_one_operation(library):
    session, lib = library
    files = [_file(session, "Halo"), _file(session, "Insane")]
    ae.edit(session, lib, files, {"album": "The Greatest Hits"}, ae.description("x", None))
    [entry] = ops.history(session, lib.id)
    assert (entry.values, entry.files) == (2, 2)
    assert entry.operation.description == "Manual edit of album x"
    ops.undo(session, entry.operation)
    session.commit()
    assert all(_value(file, "album")[1] != "manual" for file in files)
    assert wr.survey(session, lib, wr.Selection()).total == 0


def test_discard_is_an_operation(library):
    session, lib = library
    halo = _file(session, "Halo")
    _edit(session, [halo], {"genre": "Rock"})
    wr.discard(session, wr.survey(session, lib, wr.Selection()).files)
    [discard, _] = ops.history(session, lib.id)
    assert discard.operation.kind == "discard"
    assert discard.operation.description == "Discard the edits of 1 file"
    assert _value(halo, "genre")[1] == "tag"
    ops.undo(session, discard.operation)
    session.commit()
    assert _value(halo, "genre") == ("Rock", "manual")


def test_values_of_a_deleted_file_go_with_it(library):
    session, lib = library
    halo = _file(session, "Halo")
    edit = _edit(session, [halo, _file(session, "Insane")], {"genre": "Rock"})
    session.delete(halo)
    session.commit()
    assert session.scalar(select(func.count()).select_from(OperationValue)) == 1
    assert ops.undo(session, edit).reverted == 1


def test_history_pages(library):
    session, lib = library
    halo = _file(session, "Halo")
    for genre in ("A", "B", "C"):
        _edit(session, [halo], {"genre": genre}, genre)
    assert [e.operation.description for e in ops.history(session, lib.id)] == ["C", "B", "A"]
    assert [e.operation.description for e in ops.history(session, lib.id, 1, 1)] == ["B"]


def test_unknown_kind_is_refused(library):
    session, lib = library
    with pytest.raises(ValueError, match="Unknown operation kind"):
        ops.record(session, lib.id, "magic", "", [])


def test_cli_history(library):
    session, lib = library
    db = str(session.get_bind().url)
    run = lambda *args: CliRunner().invoke(app, ["--db", db, *args])  # noqa: E731
    assert "No decisions yet." in run("history").output
    run("edit", "Texas/Greatest Hits/11 Halo.mp3", "--set", "genre=Rock")

    result = run("history")
    assert result.exit_code == 0, result.output
    assert "Edit Texas/Greatest Hits/11 Halo.mp3 (1 values in 1 files)" in result.output
    result = run("history", "show", "1")
    assert "Texas/Greatest Hits/11 Halo.mp3\n      genre [manual]: None -> 'Rock'" in result.output

    result = run("history", "undo", "1")
    assert result.exit_code == 0, result.output
    assert "Set 1 values back (operation 2)." in result.output
    assert "[undone by 2]" in run("history").output
    assert "0 files have values" in run("pending").output
    assert run("history", "undo", "1").exit_code == 2
    assert run("history", "show", "9").exit_code == 2
