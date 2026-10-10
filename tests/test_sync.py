import os
import unicodedata
from urllib.parse import unquote

import boto3
import pytest
from moto import mock_aws
from sqlalchemy import select

from audio_organizer import sync as sy
from audio_organizer.models import AudioFile, Library, Playlist, RemoteObject, SyncTarget
from audio_organizer.provenance import set_value
from audio_organizer.scanner import scan

BUCKET = "music"


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
    copy_audio("mp3", root / "Texas/Greatest Hits/11 Halo.mp3")
    (root / "Texas/Greatest Hits/Folder.jpg").write_bytes(b"\xff\xd8 not really a jpeg")
    session = Session()
    library = Library(name="music", root=str(root))
    target = SyncTarget(name="s3", library=library, bucket=BUCKET, prefix="lib")
    session.add_all([library, target])
    session.commit()
    scan(session, library)
    yield session, root, target, sy.store_for(target)
    session.close()


def _sync(session, target, store, **kwargs):
    plan = sy.plan(session, target, **kwargs)
    result = sy.execute(session, target, store, plan.actions)
    assert not result.errors, result.errors
    return plan


def _meta(s3, key):
    head = s3.head_object(Bucket=BUCKET, Key=key)
    return head["ContentType"], {k: unquote(v) for k, v in head["Metadata"].items()}


def test_first_sync_uploads_tracks_and_covers(setup, s3):
    session, root, target, store = setup
    plan = _sync(session, target, store)
    assert plan.counts() == {"upload": 2}
    content_type, meta = _meta(s3, "lib/Texas/Greatest Hits/11 Halo.mp3")
    assert content_type == "audio/mpeg"
    assert meta["artist"] == "Texas"
    assert meta["album"] == "Greatest Hits"
    assert meta["tracknumber"] == "11"
    assert meta["year"] == "2000"
    assert meta["image"] == "lib/Texas/Greatest Hits/Folder.jpg"
    assert 900 < int(meta["length"]) < 1200
    assert _meta(s3, "lib/Texas/Greatest Hits/Folder.jpg")[0] == "image/jpeg"

    again = sy.plan(session, target)
    assert again.actions == [] and again.unchanged == 2


def test_metadata_change_is_copied_server_side(setup, s3):
    session, root, target, store = setup
    _sync(session, target, store)
    file = session.scalar(select(AudioFile))
    set_value(session, file, "manual", "title", "Halo (Remastered)")
    session.commit()
    plan = _sync(session, target, store)
    assert plan.counts() == {"metadata": 1}
    assert plan.upload_bytes == 0
    assert _meta(s3, "lib/Texas/Greatest Hits/11 Halo.mp3")[1]["title"] == "Halo (Remastered)"


def test_local_move_is_moved_server_side(setup, s3):
    session, root, target, store = setup
    _sync(session, target, store)
    os.rename(root / "Texas/Greatest Hits/11 Halo.mp3", root / "Texas/Greatest Hits/Halo.mp3")
    scan(session, target.library)
    plan = _sync(session, target, store)
    assert plan.counts() == {"move": 1}
    assert plan.upload_bytes == 0
    keys = {o["Key"] for o in s3.list_objects_v2(Bucket=BUCKET)["Contents"]}
    assert keys == {"lib/Texas/Greatest Hits/Halo.mp3", "lib/Texas/Greatest Hits/Folder.jpg"}
    assert sy.plan(session, target).actions == []


def test_deletes_only_when_asked(setup, s3):
    session, root, target, store = setup
    _sync(session, target, store)
    (root / "Texas/Greatest Hits/11 Halo.mp3").unlink()
    scan(session, target.library)
    plan = sy.plan(session, target)
    assert plan.actions == []
    # The cover goes too: no track in the bucket refers to it any more
    assert sorted(plan.stale) == [
        "lib/Texas/Greatest Hits/11 Halo.mp3",
        "lib/Texas/Greatest Hits/Folder.jpg",
    ]
    plan = _sync(session, target, store, delete_stale=True)
    assert plan.counts() == {"delete": 2}
    assert "Contents" not in s3.list_objects_v2(Bucket=BUCKET)
    assert session.scalar(select(RemoteObject)) is None


def test_reconcile_adopts_existing_uploads(Session, copy_audio, tmp_path, s3):
    root = tmp_path / "music"
    path = copy_audio("mp3", root / "Pepe Deluxé/Pepe Deluxé_The Ride.mp3")
    session = Session()
    library = Library(name="music", root=str(root))
    target = SyncTarget(name="s3", library=library, bucket=BUCKET, prefix="")
    session.add_all([library, target])
    session.commit()
    scan(session, library)
    # Uploaded earlier by another tool, with a decomposed (NFD) key
    nfd_key = unicodedata.normalize("NFD", "Pepe Deluxé/Pepe Deluxé_The Ride.mp3")
    s3.put_object(Bucket=BUCKET, Key=nfd_key, Body=path.read_bytes())
    s3.put_object(Bucket=BUCKET, Key="somewhere/else.mp3", Body=b"x")
    store = sy.store_for(target)

    result = sy.reconcile(session, target, store)
    assert result.adopted == 1
    assert result.unknown == ["somewhere/else.mp3"]
    plan = _sync(session, target, store)
    assert plan.counts() == {"move": 1}  # to the NFC key, server-side
    assert plan.upload_bytes == 0
    keys = {o["Key"] for o in s3.list_objects_v2(Bucket=BUCKET)["Contents"]}
    assert unicodedata.normalize("NFC", nfd_key) in keys and nfd_key not in keys


def test_upload_refuses_file_changed_since_scan(setup, s3):
    session, root, target, store = setup
    path = root / "Texas/Greatest Hits/11 Halo.mp3"
    os.utime(path, ns=(0, path.stat().st_mtime_ns + 10**9))
    plan = sy.plan(session, target)
    result = sy.execute(session, target, store, plan.actions)
    assert len(result.errors) == 1 and "rescan" in result.errors[0][1]


def test_metadata_fits_size_limit(setup):
    session, root, target, store = setup
    file = session.scalar(select(AudioFile))
    meta = sy.track_metadata(file.duration, {"title": "ä" * 3000, "artist": "x"}, None)
    assert sum(len(k) + len(v) for k, v in meta.items()) <= sy.METADATA_LIMIT
    assert meta["artist"] == "x" and meta["truncated"] == "title"
    assert "truncated" not in sy.track_metadata(1, {"title": "short"}, None)


def test_metadata_numbers_are_plain_integers():
    fields = {"tracknumber": "02.", "tracktotal": "12", "discnumber": "1", "disctotal": "x",
              "date": "2017-03-03T08:00:00Z"}  # fmt: skip
    meta = sy.track_metadata(200.4, fields, None)
    assert {k: meta.get(k) for k in ("tracknumber", "tracktotal", "discnumber", "disctotal",
            "year", "length")} == {"tracknumber": "2", "tracktotal": "12", "discnumber": "1",
            "disctotal": None, "year": "2017", "length": "200400"}  # fmt: skip


def test_metadata_player_fields():
    fields = {
        "originaldate": "1996-05-01T00:00:00Z", "composer": "Elfman", "rating": "4",
        "mood": "Upbeat, Happy", "compilation": "1", "replaygain_track_gain": "-7.23 dB",
        "replaygain_album_gain": "+3.00 dB", "replaygain_track_peak": "0.988000",
        "replaygain_album_peak": "x", "artistsort": "Texas", "albumartistsort": "Texas",
    }  # fmt: skip
    meta = sy.track_metadata(100, fields, "Music/Texas/cover.jpg")
    assert meta == {
        "originaldate": "1996-05-01", "composer": "Elfman", "rating": "4",
        "mood": "Upbeat%3B%20Happy", "compilation": "1", "replaygaintrackgain": "-7.23",
        "replaygainalbumgain": "3.00", "replaygaintrackpeak": "0.988000",
        "artistsort": "Texas", "albumartistsort": "Texas", "length": "100000",
        "image": "Music%2FTexas%2Fcover.jpg",
    }  # fmt: skip
    assert "rating" not in sy.track_metadata(100, {"rating": "7"}, None)


def test_playlists_are_synced(setup, s3):
    session, root, target, store = setup
    target.playlist_folder = "playlists"
    session.add(Playlist(library=target.library, name="Texas: hits", rule="artist=texas",
                         sort="album"))  # fmt: skip
    session.commit()
    plan = _sync(session, target, store)
    playlist_keys = sorted(a.key for a in plan.actions if a.object_kind == "playlist")
    assert playlist_keys == [
        "lib/playlists/Texas- hits.html",
        "lib/playlists/Texas- hits.json",
        "lib/playlists/Texas- hits.m3u",
    ]
    m3u = s3.get_object(Bucket=BUCKET, Key="lib/playlists/Texas- hits.m3u")
    assert m3u["ContentType"] == "audio/x-mpegurl"
    assert m3u["Body"].read().decode().splitlines()[-1] == "../Texas/Greatest Hits/11 Halo.mp3"
    assert sy.plan(session, target).actions == []

    session.scalar(select(Playlist)).name = "Texas"
    session.commit()
    plan = sy.plan(session, target)
    assert plan.counts() == {"upload": 3}
    assert sorted(plan.stale) == playlist_keys


def test_playlists_go_to_the_prefix_root_by_default(setup, s3):
    session, root, target, store = setup
    target.playlist_formats = "m3u"
    session.add(Playlist(library=target.library, name="Hits", rule="artist=texas", sort="album"))
    session.commit()
    plan = _sync(session, target, store)
    assert [a.key for a in plan.actions if a.object_kind == "playlist"] == ["lib/Hits.m3u"]
    m3u = s3.get_object(Bucket=BUCKET, Key="lib/Hits.m3u")["Body"].read().decode()
    assert m3u.splitlines()[-1] == "Texas/Greatest Hits/11 Halo.mp3"


def test_target_commands(setup, tmp_path):
    from typer.testing import CliRunner

    from audio_organizer.cli import app

    session, root, target, store = setup
    db = ["--db", str(session.get_bind().url.database)]
    runner = CliRunner()
    bad = runner.invoke(app, [*db, "target", "add", "a/b", "--bucket", "x"])
    assert bad.exit_code != 0 and "Invalid target name" in bad.output
    result = runner.invoke(app, [*db, "target", "show", target.name])
    assert result.exit_code == 0, result.output
    assert "never synced" in result.output and "<name> .m3u, .json, .html" in result.output
    changed = runner.invoke(
        app,
        [
            *db,
            "target",
            "set",
            target.name,
            "--playlist-folder",
            "lists/",
            "--playlist-formats",
            "json",
        ],
    )
    assert "/lists/<name> .json" in changed.output
    bad = runner.invoke(app, [*db, "target", "set", target.name, "--playlist-folder", "../x"])
    assert bad.exit_code != 0
    assert runner.invoke(app, [*db, "target", "rename", target.name, "nas"]).exit_code == 0
    assert "nas" in runner.invoke(app, [*db, "target", "list"]).output
    assert runner.invoke(app, [*db, "target", "remove", "nas"]).exit_code == 0
    assert runner.invoke(app, [*db, "target", "list"]).output == ""


def test_execute_stops_and_resumes(setup, s3, copy_audio):
    session, root, target, store = setup
    for n in range(5):
        copy_audio("mp3b", root / f"Texas/Other/{n:02} Song.mp3")
    scan(session, target.library)
    plan = sy.plan(session, target)
    result = sy.execute(session, target, store, plan.actions, workers=1,
                        should_stop=lambda: True)  # fmt: skip
    assert result.stopped and sum(result.done.values()) < len(plan.actions)
    rest = sy.plan(session, target)
    assert len(rest.actions) == len(plan.actions) - sum(result.done.values())
    _sync(session, target, store)
    assert not sy.plan(session, target).actions


def test_plan_fingerprint(setup, s3):
    session, root, target, store = setup
    first = sy.plan(session, target).fingerprint()
    assert sy.plan(session, target).fingerprint() == first
    file = session.scalar(select(AudioFile))
    set_value(session, file, "manual", "title", "Halo (edit)")
    session.commit()
    assert sy.plan(session, target).fingerprint() != first


def test_jobs_lock_and_record_progress(setup, s3):
    from audio_organizer import sync_jobs as jobs

    session, root, target, store = setup
    job = jobs.start(session, target, "cli")
    with pytest.raises(jobs.SyncBusy):
        jobs.start(session, target, "web")
    with pytest.raises(jobs.SyncBusy):
        sy.remove_target(session, target)
    plan = sy.plan(session, target)
    jobs.run(session, job, plan, store)
    assert (job.status, job.done, job.total, job.failed) == ("done", len(plan.actions),
                                                             len(plan.actions), 0)  # fmt: skip
    assert job.bytes_done == plan.upload_bytes > 0
    assert jobs.active_job(session, target) is None
    jobs.start(session, target, "cli")  # free again


def test_job_of_a_gone_process_is_interrupted(setup):
    from audio_organizer import sync_jobs as jobs

    session, root, target, store = setup
    job = jobs.start(session, target, "cli")
    job.pid = 2**22 + 12345  # no such process
    session.commit()
    assert jobs.active_job(session, target) is None
    assert job.status == "interrupted"


def test_stop_before_running(setup, s3):
    from audio_organizer import sync_jobs as jobs

    session, root, target, store = setup
    job = jobs.start(session, target, "web")
    jobs.request_stop(session, job)
    assert jobs.run(session, job, sy.plan(session, target), store) is None
    assert job.status == "stopped" and not s3.list_objects_v2(Bucket=BUCKET).get("Contents")
