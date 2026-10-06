import os
import unicodedata
from urllib.parse import unquote

import boto3
import pytest
from moto import mock_aws
from sqlalchemy import select

from audio_organizer import sync as sy
from audio_organizer.models import AudioFile, Library, RemoteObject, SyncTarget
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
    meta = sy.track_metadata(file, {"title": "ä" * 3000, "artist": "x"}, None)
    assert sum(len(k) + len(v) for k, v in meta.items()) <= sy.METADATA_LIMIT
    assert meta["artist"] == "x"
