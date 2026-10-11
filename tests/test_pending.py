import pytest
from sqlalchemy import select, text
from typer.testing import CliRunner

from audio_organizer import changes as ch
from audio_organizer import tags
from audio_organizer import writing as wr
from audio_organizer.cli import app
from audio_organizer.db import make_sessionmaker
from audio_organizer.models import AudioFile, Changeset, Library, PendingField
from audio_organizer.provenance import resolve, set_value
from audio_organizer.scanner import scan


@pytest.fixture
def library(Session, copy_audio, tmp_path):
    """Two albums by Texas, a loose track without an album and one by P!nk."""
    root = tmp_path / "music"
    copy_audio("mp3", root / "Texas/Greatest Hits/11 Halo.mp3")
    copy_audio("mp3b", root / "Texas/White on Blonde/01 Insane.mp3")
    tags.write(root / "Texas/White on Blonde/01 Insane.mp3", {"album": "White on Blonde"})
    copy_audio("mp3b", root / "Texas/Loose.mp3")
    copy_audio("m4a", root / "P!nk/Funhouse/01 So What.m4a")
    session = Session()
    lib = Library(name="music", root=str(root))
    session.add(lib)
    session.commit()
    scan(session, lib)
    yield session, lib, root
    session.close()


def _file(session, part):
    return session.scalar(select(AudioFile).where(AudioFile.path.contains(part)))


def _pending(session):
    return {
        (_file.path, row.field): (row.value, row.source, row.tag)
        for row, _file in session.execute(select(PendingField, AudioFile).join(AudioFile))
    }


def test_pending_fields_follow_the_values(library):
    session, lib, root = library
    assert _pending(session) == {}  # the tags are all there is
    halo = _file(session, "Halo")
    set_value(session, halo, "manual", "genre", "Rock")
    set_value(session, halo, "manual", "title", "Halo")  # what the tag says already
    set_value(session, halo, "analysis", "replaygain_track_gain", "-3.00 dB")
    set_value(session, halo, "neighbours", "composer", "Sharleen Spiteri")  # evidence only
    session.commit()
    assert _pending(session) == {
        ("Texas/Greatest Hits/11 Halo.mp3", "genre"): ("Rock", "manual", "Pop"),
        ("Texas/Greatest Hits/11 Halo.mp3", "replaygain_track_gain"): (
            "-3.00 dB", "analysis", None,
        ),
    }  # fmt: skip

    _, outcomes = wr.write(session, wr.survey(session, lib, wr.Selection()).files, "write")
    assert all(o.ok for o in outcomes)
    assert _pending(session) == {}  # the rescan after writing sees the new tags


def test_survey_groups_and_selections(library):
    session, lib, root = library
    for part in ("Halo", "Insane", "Loose", "So What"):
        set_value(session, _file(session, part), "manual", "genre", "Rock")
    session.commit()
    groups = [(g.artist, g.album, g.files) for g in wr.groups(session, lib, wr.Selection())]
    assert groups == [
        ("P!nk", "Funhouse", 1),
        ("Texas", "", 1),  # the loose track: no album
        ("Texas", "Greatest Hits", 1),
        ("Texas", "White on Blonde", 1),
    ]
    found = wr.survey(session, lib, wr.Selection(artist="texas"))
    assert found.total == 3 and found.by_field == {"genre": 3}
    assert [(p.album, p.file.path) for p in found.files] == [
        ("", "Texas/Loose.mp3"),
        ("Greatest Hits", "Texas/Greatest Hits/11 Halo.mp3"),
        ("White on Blonde", "Texas/White on Blonde/01 Insane.mp3"),
    ]
    assert wr.survey(session, lib, wr.Selection(artist="Texas", album="")).total == 1
    assert wr.survey(session, lib, wr.Selection(path="P!nk/")).total == 1
    assert wr.survey(session, lib, wr.Selection(only=["replaygain"])).total == 0
    found = wr.survey(session, lib, wr.Selection(), limit=2)
    assert found.total == 4 and len(found.files) == 2  # P!nk first, then Texas


def test_every_change_to_a_file_is_written_at_once(library):
    """Edits made one after another (an album rename, a rating, a measurement) are written
    to the file in one change."""
    session, lib, root = library
    halo = _file(session, "Halo")
    wr.store_edit(session, [halo], {"album": "The Greatest Hits"}, "edit")
    wr.store_edit(session, [halo], {"rating": "5"}, "edit")
    set_value(session, halo, "analysis", "replaygain_track_gain", "-3.00 dB")
    session.commit()
    assert session.scalar(select(Changeset)) is None
    [pending] = wr.survey(
        session, lib, wr.Selection(artist="Texas", album="the greatest hits")
    ).files
    assert pending.fields == {
        "album": "The Greatest Hits", "rating": "5", "replaygain_track_gain": "-3.00 dB",
    }  # fmt: skip
    changeset, [outcome] = wr.write(session, [pending], wr.description(wr.Selection(), 1))
    assert outcome.ok, outcome.message
    assert changeset.description == "Write the tags of 1 file" and changeset.source == "writing"
    fields = tags.read(root / halo.path).fields
    assert (fields["album"], fields["rating"]) == ("The Greatest Hits", "5")


def test_waiting_files_are_left_to_their_changeset(library):
    session, lib, root = library
    halo = _file(session, "Halo")
    wr.store_edit(session, [halo], {"genre": "Rock"}, "edit")
    ch.propose_retag(ch.new_changeset(session, "waiting", "manual"), halo, {"genre": "Rock"})
    session.commit()
    found = wr.survey(session, lib, wr.Selection())
    assert (found.total, found.waiting, found.files) == (0, 1, [])
    assert wr.groups(session, lib, wr.Selection()) == []


def test_discard_drops_edits_and_keeps_measurements(library):
    session, lib, root = library
    halo = _file(session, "Halo")
    wr.store_edit(session, [halo], {"genre": "Rock"}, "edit")
    set_value(session, halo, "analysis", "replaygain_track_gain", "-3.00 dB")
    session.commit()
    assert wr.discard(session, wr.survey(session, lib, wr.Selection()).files) == (1, 1)
    assert resolve(halo.values)["genre"].value == "Pop"  # the tag again
    [pending] = wr.survey(session, lib, wr.Selection()).files
    assert pending.fields == {"replaygain_track_gain": "-3.00 dB"}


def test_store_edit_refuses_unknown_fields(library):
    session, lib, root = library
    with pytest.raises(ValueError, match="Not tag fields: colour"):
        wr.store_edit(session, [_file(session, "Halo")], {"colour": "red"}, "edit")


def test_catalog_without_the_pending_table_is_filled(library, tmp_path):
    """A catalog from before the table existed gets it, filled for every file."""
    session, lib, root = library
    set_value(session, _file(session, "Halo"), "manual", "genre", "Rock")
    session.commit()
    engine = session.get_bind()
    session.close()
    with engine.begin() as connection:
        connection.execute(text("drop table pending_field"))
    with make_sessionmaker(f"sqlite:///{tmp_path / 'catalog.db'}")() as fresh:
        assert _pending(fresh) == {
            ("Texas/Greatest Hits/11 Halo.mp3", "genre"): ("Rock", "manual", "Pop")
        }


def test_cli_edit_and_pending(library):
    session, lib, root = library
    db = str(session.get_bind().url)
    run = lambda *args: CliRunner().invoke(app, ["--db", db, *args])  # noqa: E731

    result = run("edit", "Texas/Greatest Hits/11 Halo.mp3", "--set", "genre=Rock")
    assert result.exit_code == 0, result.output
    assert "Saved; not yet in the file: genre." in result.output
    assert "aorg pending --path 'Texas/Greatest Hits/11 Halo.mp3' --write" in result.output
    run("edit", "P!nk/Funhouse/01 So What.m4a", "--set", "genre=Pop")

    result = run("pending")
    assert "2 files have values not yet in them (genre 2)." in result.output
    assert "  P!nk – Funhouse: 1 files" in result.output
    assert "  Texas – Greatest Hits: 1 files" in result.output

    result = run("pending", "--artist", "Texas")
    assert "Texas – Greatest Hits\n  Texas/Greatest Hits/11 Halo.mp3\n" in result.output
    assert "genre: 'Pop' -> 'Rock'" in result.output

    result = run("pending", "--artist", "Texas", "--write")
    assert result.exit_code == 0, result.output
    assert "Wrote 1 files (changeset 1; undo with `aorg changes undo 1`)." in result.output
    assert tags.read(root / "Texas/Greatest Hits/11 Halo.mp3").fields["genre"] == "Rock"
    assert tags.read(root / "P!nk/Funhouse/01 So What.m4a").fields.get("genre") is None

    result = run("pending", "--artist", "P!nk", "--discard")
    assert "Dropped 1 edits (" in result.output
    assert "0 files have values" in run("pending").output


def test_write_gaps_reports_each_batch_and_stops_between_them(library):
    session, lib, root = library
    for part in ("Halo", "Insane", "Loose"):
        set_value(session, _file(session, part), "analysis", "replaygain_track_gain", "-3.00 dB")
    set_value(session, _file(session, "Halo"), "manual", "genre", "Rock")  # left for review
    session.commit()
    progress, batches = [], []

    def stop() -> bool:
        return len(batches) == 2

    ids, written, failed = wr.write_gaps(
        session, lib, wr.Selection(), batch=1,
        progress=lambda done, total: progress.append((done, total)),
        written_batch=lambda *batch: batches.append(batch), should_stop=stop,
    )  # fmt: skip
    assert progress == [(0, 3), (1, 3), (2, 3)]
    assert batches == [(ids[0], 1, []), (ids[1], 1, [])]
    assert (written, failed) == (2, [])
    gains = [
        tags.read(path).fields.get("replaygain_track_gain")
        for path in sorted(root.rglob("*.mp3"))  # Halo, Loose, Insane
    ]
    assert sorted(gains, key=str) == ["-3.00 dB", "-3.00 dB", None]
    assert tags.read(root / "Texas/Greatest Hits/11 Halo.mp3").fields.get("genre") != "Rock"

    # the next run writes the rest
    ids, written, failed = wr.write_gaps(session, lib, wr.Selection(), batch=1)
    assert (len(ids), written) == (1, 1)
