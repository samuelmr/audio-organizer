import boto3
import pytest
from moto import mock_aws
from sqlalchemy import select
from typer.testing import CliRunner

from audio_organizer import playlist_store as ps
from audio_organizer import sync as sy
from audio_organizer.cli import app
from audio_organizer.models import Library, Playlist, PlaylistFile, SyncTarget
from audio_organizer.playlists import generate
from audio_organizer.scanner import scan

BUCKET = "bucket"
TRACK = "Texas/Greatest Hits/11 Halo.mp3"


@pytest.fixture
def s3(monkeypatch):
    for name, value in {
        "AWS_ACCESS_KEY_ID": "test", "AWS_SECRET_ACCESS_KEY": "test",
        "AWS_DEFAULT_REGION": "us-east-1",
    }.items():  # fmt: skip
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    with mock_aws():
        client = boto3.client("s3")
        client.create_bucket(Bucket=BUCKET)
        yield client


@pytest.fixture
def setup(Session, copy_audio, tmp_path, s3):
    root = tmp_path / "music"
    copy_audio("mp3", root / TRACK)
    session = Session()
    library = Library(name="music", root=str(root))
    target = SyncTarget(name="s3", library=library, bucket=BUCKET, prefix="lib")
    playlist = Playlist(library=library, name="Hits", rule="artist=texas", sort="album")
    session.add_all([library, target, playlist])
    session.commit()
    scan(session, library)
    yield session, library, target, playlist, root
    session.close()


def _plan(session, place, playlist):
    return ps.plan_write(session, place, generate(session, place.library, [playlist]))


def test_write_to_disk_and_up_to_date_afterwards(setup):
    session, library, target, playlist, root = setup
    library.playlist_formats = "m3u,json"
    place = ps.Place(library)

    planned = _plan(session, place, playlist)
    assert [(p.path, p.state) for p in planned] == [("Hits.m3u", "new"), ("Hits.json", "new")]
    assert ps.apply_write(session, place, planned) == 2
    session.commit()

    assert (root / "Hits.m3u").read_text().splitlines()[-1] == TRACK
    assert {p.state for p in _plan(session, place, playlist)} == {"unchanged"}
    assert place.describe("Hits.m3u") == str(root / "Hits.m3u")


def test_changed_playlist_replaces_own_file_but_edited_file_is_backed_up(setup):
    session, library, target, playlist, root = setup
    place = ps.Place(library)
    ps.apply_write(session, place, _plan(session, place, playlist))
    session.commit()

    playlist.rule = "artist=nobody"
    assert [p.state for p in _plan(session, place, playlist)] == ["replace"]

    (root / "Hits.m3u").write_text("my own edits\n")
    planned = _plan(session, place, playlist)
    assert [p.state for p in planned] == ["modified"]
    ps.apply_write(session, place, planned)
    session.commit()

    [backup] = ps.backups(session, place)
    assert (backup.reason, backup.content) == ("overwritten", b"my own edits\n")
    ps.restore(session, place, backup)
    assert (root / "Hits.m3u").read_text() == "my own edits\n"
    assert ps.backups(session, place) == []


def test_delete_keeps_content_for_undo(setup):
    session, library, target, playlist, root = setup
    place = ps.Place(library)
    ps.apply_write(session, place, _plan(session, place, playlist))
    session.commit()
    written = (root / "Hits.m3u").read_bytes()

    assert ps.apply_delete(session, place, ps.plan_delete(session, place, playlist)) == 1
    session.commit()
    assert not (root / "Hits.m3u").exists()
    assert session.scalars(select(PlaylistFile)).all() == []

    [backup] = ps.backups(session, place)
    assert backup.reason == "deleted"
    ps.restore(session, place, backup)
    assert (root / "Hits.m3u").read_bytes() == written


def test_bucket_and_disk_places_are_separate(setup, s3):
    session, library, target, playlist, root = setup
    target.playlist_folder = "lists"
    target.playlist_formats = "m3u"
    place = ps.Place(library, target, sy.store_for(target))

    ps.apply_write(session, place, _plan(session, place, playlist))
    session.commit()
    body = s3.get_object(Bucket=BUCKET, Key="lib/lists/Hits.m3u")["Body"].read().decode()
    assert body.splitlines()[-1] == "../" + TRACK
    assert place.describe("lists/Hits.m3u") == f"s3://{BUCKET}/lib/lists/Hits.m3u"
    assert not (root / "Hits.m3u").exists()

    ps.apply_delete(session, place, ps.plan_delete(session, place, playlist))
    session.commit()
    assert "Contents" not in s3.list_objects_v2(Bucket=BUCKET)
    [backup] = ps.backups(session, place)
    assert backup.target_id == target.id
    assert ps.backups(session, ps.Place(library)) == []


def test_disk_paths_stay_inside_the_library(setup):
    session, library, target, playlist, root = setup
    with pytest.raises(ValueError):
        ps.Place(library).write("../outside.m3u", b"x", "audio/x-mpegurl")


def test_commands(setup, s3):
    session, library, target, playlist, root = setup
    db = ["--db", str(session.get_bind().url.database)]
    runner = CliRunner()

    dry = runner.invoke(app, [*db, "playlists", "write", "--dry-run"])
    assert dry.exit_code == 0, dry.output
    assert str(root / "Hits.m3u") in dry.output and not (root / "Hits.m3u").exists()

    assert runner.invoke(app, [*db, "playlists", "write", "Hits"]).exit_code == 0
    assert (root / "Hits.m3u").exists()
    result = runner.invoke(app, [*db, "playlists", "write", "--target", "s3"])
    assert f"s3://{BUCKET}/lib/Hits.html" in result.output

    result = runner.invoke(app, [*db, "playlists", "delete-files", "Hits"])
    assert result.exit_code == 0 and "Deleted 1 files" in result.output
    listed = runner.invoke(app, [*db, "playlists", "restore-files"]).output
    assert "deleted" in listed and str(root / "Hits.m3u") in listed
    backup_id = listed.split("\t")[0]
    assert runner.invoke(app, [*db, "playlists", "restore-files", backup_id]).exit_code == 0
    assert (root / "Hits.m3u").exists()
