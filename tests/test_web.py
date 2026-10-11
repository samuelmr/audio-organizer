import re
import sqlite3
import subprocess
import threading
import time

import pytest
from conftest import make_audio
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from audio_organizer import changes as ch
from audio_organizer import covers, tags
from audio_organizer.models import AudioFile, Changeset, CoverLookup, Library, SyncTarget
from audio_organizer.scanner import scan
from audio_organizer.web import create_app


@pytest.fixture
def web(Session, copy_audio, tmp_path):
    root = tmp_path / "music"
    copy_audio("mp3", root / "Texas/Greatest Hits/11 Halo.mp3")
    copy_audio("mp3", root / "Texas/Texas_Halo.mp3")  # identical audio, flat copy
    tags.write(root / "Texas/Texas_Halo.mp3", {"album": None, "mood": "Upbeat"})
    copy_audio("m4a", root / "P!nk/Funhouse/01 So What.m4a")
    with Session() as session:
        library = Library(name="music", root=str(root))
        session.add(library)
        session.commit()
        scan(session, library)
    client = TestClient(create_app(Session))
    client.root = root
    return client


def _file_id(Session, path):
    with Session() as session:
        return session.scalar(select(AudioFile.id).where(AudioFile.path == path))


def test_browse_search_and_filter(web):
    response = web.get("/")  # the only library
    assert response.status_code == 200
    assert response.url.path == "/libraries/music"
    assert "3 tracks" in response.text
    assert "So What" in response.text

    response = web.get("/libraries/music", params={"q": "p!nk so"})
    assert "1 tracks" in response.text

    response = web.get("/libraries/music", params={"show": "no-album"})
    assert "1 tracks" in response.text  # the flat copy: no album tag, no album folder

    response = web.get("/libraries/music", params={"sort": "title"})
    titles = re.findall(r'<a href="/tracks/\d+">([^<]+)</a>', response.text)
    assert titles == ["Halo", "Halo", "So What"]


def test_track_page(web, Session):
    file_id = _file_id(Session, "Texas/Texas_Halo.mp3")
    page = web.get(f"/tracks/{file_id}").text
    assert "Texas/Greatest Hits/11 Halo.mp3" in page  # identical audio
    assert 'value="Upbeat"' in page
    assert "Tags when first scanned" in page


def test_edit_then_write_and_undo(web, Session):
    file_id = _file_id(Session, "Texas/Texas_Halo.mp3")
    form = {"artist": "Texas", "title": "Halo", "album": "Greatest Hits", "tracknumber": "11"}
    response = web.post(f"/tracks/{file_id}/edit", data=form)
    assert response.status_code == 200
    assert response.url.path == f"/tracks/{file_id}"
    assert "Saved; not yet in the file." in response.text
    assert f"/libraries/music/pending?file={file_id}" in response.text
    assert "1 value not yet in the file" in response.text  # the track number was 11
    assert "album" not in tags.read(web.root / "Texas/Texas_Halo.mp3").fields
    with Session() as session:
        assert session.scalar(select(Changeset)) is None  # an edit is no changeset

    page = web.get("/libraries/music/pending", params={"file": file_id}).text
    assert "<h1>Pending</h1>" in page
    assert page.count('name="file_ids"') == 1
    assert "Album: Greatest Hits" in page

    page = web.post(
        "/libraries/music/pending", data={"action": "write", "file_ids": [file_id]}
    ).text
    assert "Written" in page and 'class="failed"' not in page
    assert ">was retagged</span>" in page
    fields = tags.read(web.root / "Texas/Texas_Halo.mp3").fields
    assert (fields["album"], fields["tracknumber"]) == ("Greatest Hits", "11")
    assert "Nothing to write" in web.get("/libraries/music/pending").text

    changeset_path = re.search(r'action="(/changes/\d+)/undo"', page).group(1)
    web.post(f"{changeset_path}/undo")
    assert "album" not in tags.read(web.root / "Texas/Texas_Halo.mp3").fields
    page = web.get("/libraries/music/pending").text  # the edit stays, so it is pending again
    assert page.count('name="file_ids"') == 1


def test_discard_edits(web, Session):
    file_id = _file_id(Session, "Texas/Texas_Halo.mp3")
    web.post(f"/tracks/{file_id}/edit", data={"genre": "Pop-rock"})
    page = web.post(
        "/libraries/music/pending", data={"action": "discard", "file_ids": [file_id]}
    ).text
    assert "Dropped 1 edits" in page and "Nothing to write" in page
    page = web.get(f"/tracks/{file_id}").text
    assert "not yet in the file" not in page


def test_edit_without_changes(web, Session):
    file_id = _file_id(Session, "P!nk/Funhouse/01 So What.m4a")
    response = web.post(f"/tracks/{file_id}/edit", data={"artist": "P!nk", "title": "So What"})
    assert "Nothing changed" in response.text


def test_select_single_changes(web, Session):
    with Session() as session:
        file = session.scalar(select(AudioFile).where(AudioFile.path == "Texas/Texas_Halo.mp3"))
        changeset = ch.new_changeset(session, "retag", "manual")
        ch.propose_retag(changeset, file, {"genre": "Pop-rock"})
        session.commit()
        changeset_path = f"/changes/{changeset.id}"
    response = web.get(changeset_path)
    [change_id] = re.findall(r'name="change_ids" value="(\d+)"', response.text)
    page = web.post(
        f"{changeset_path}/status", data={"action": "reject", "change_ids": [change_id]}
    ).text
    assert "1 changes updated" in page and "rejected" in page
    web.post(f"{changeset_path}/status", data={"action": "approve-all"})
    page = web.post(f"{changeset_path}/status", data={"action": "reject-all"}).text
    assert "1 changes updated" in page and 'class="status rejected"' in page


def test_cross_site_posts_are_refused(web, Session):
    file_id = _file_id(Session, "Texas/Texas_Halo.mp3")
    response = web.post(
        f"/tracks/{file_id}/edit", data={"genre": "Evil"},
        headers={"Origin": "https://evil.example"},
    )  # fmt: skip
    assert response.status_code == 403
    response = web.post(
        f"/tracks/{file_id}/edit", data={"genre": "Pop"},
        headers={"Origin": "http://testserver"},
    )  # fmt: skip
    assert response.status_code == 200


def test_playlists(web):
    page = web.get("/libraries/music/playlists").text
    assert "No playlists yet" in page

    response = web.post(
        "/libraries/music/playlists",
        data={"playlist_name": "Moods", "rule": "artist=texas", "group_by": "mood"},
    )
    assert response.status_code == 400
    assert "must contain" in response.text and 'value="Moods"' in response.text

    response = web.post(
        "/libraries/music/playlists",
        data={"playlist_name": "Best Of {mood}", "rule": "artist=texas", "group_by": "mood"},
    )
    assert response.status_code == 200
    playlist_path = response.url.path
    assert "Best Of Upbeat: 1 tracks" in response.text

    response = web.post(
        f"{playlist_path}/edit",
        data={"playlist_name": "All", "rule": "duration>0", "sort": "title", "group_by": ""},
    )
    assert "All: 2 tracks" in response.text  # one Halo: identical audio counts once
    assert "Folders:" in response.text and "Texas/Greatest Hits/" in response.text
    assert (web.root / "Texas/Greatest Hits").as_uri() in response.text

    response = web.post(
        f"{playlist_path}/edit", data={"playlist_name": "All", "rule": "x", "sort": "title"}
    )
    assert response.status_code == 400 and "Not a condition" in response.text

    response = web.post(f"{playlist_path}/delete")
    assert "No playlists yet" in response.text


def test_playlist_files_on_the_web(web):
    created = web.post(
        "/libraries/music/playlists", data={"playlist_name": "All", "rule": "duration>0"}
    )
    page_path = created.url.path
    assert str(web.root / "All.m3u") in created.text  # saved settings: root, m3u
    assert "Write to disk" in created.text and "Delete from database" in created.text

    tried = web.get(
        page_path, params={"place": "disk", "folder": "Lists", "formats": ["m3u", "html"]}
    )
    assert str(web.root / "Lists/All.html") in tried.text
    assert "not the saved settings" in tried.text

    response = web.post(
        f"{page_path}/files",
        data={"place": "disk", "folder": "Lists", "formats": ["m3u"], "do": "write"},
    )
    assert "Wrote 1 of 1 files" in response.text
    written = (web.root / "Lists/All.m3u").read_text().splitlines()
    assert "../Texas/Greatest Hits/11 Halo.mp3" in written

    (web.root / "Lists/All.m3u").write_text("my edits\n")
    response = web.post(
        f"{page_path}/files",
        data={"place": "disk", "folder": "Lists", "formats": ["m3u"], "do": "write"},
    )
    assert "changed elsewhere" in response.text and "Can be restored" in response.text
    backup_id = re.search(r"/files/(\d+)/restore", response.text).group(1)

    response = web.post(f"{page_path}/files/{backup_id}/restore")
    assert "Restored" in response.text
    assert (web.root / "Lists/All.m3u").read_text() == "my edits\n"

    response = web.post(
        f"{page_path}/files",
        data={"place": "disk", "folder": "../out", "formats": ["m3u"], "do": "write"},
    )
    assert "Could not write the files" in response.text

    response = web.post(
        "/libraries/music/playlists/files-settings",
        data={"place": ["disk"], "folder": ["Lists"], "formats": ["disk:m3u", "disk:json"]},
    )
    assert "Settings saved; no files were written" in response.text
    assert str(web.root / "Lists/All.json") in web.get(page_path).text
    assert not (web.root / "Lists/All.json").exists()

    response = web.post(
        "/libraries/music/playlists/files-settings",
        data={"place": ["disk"], "folder": ["../out"], "formats": ["disk:m3u"]},
    )
    assert "Nothing saved" in response.text


def test_albums_artists_and_folders(web, Session):
    page = web.get("/libraries/music/albums").text
    assert "2 albums" in page
    [link] = re.findall(r'href="(/libraries/music\?album=Greatest[^"]*)">Greatest', page)
    page = web.get(link.replace("&amp;", "&")).text
    assert "<h1>Greatest Hits</h1>" in page and "1 tracks" in page

    page = web.get("/libraries/music", params={"artist": "texas"}).text
    assert "<h1>texas</h1>" in page and "2 tracks" in page

    page = web.get("/libraries/music", params={"folder": "Texas"}).text
    assert "1 tracks" in page  # Texas_Halo.mp3, not the one in Texas/Greatest Hits

    page = web.get("/libraries/music", params={"sort": "album"}).text
    assert page.count('<tr class="group">') == 3  # Funhouse, Greatest Hits, no album

    file_id = _file_id(Session, "Texas/Greatest Hits/11 Halo.mp3")
    page = web.get(f"/tracks/{file_id}").text
    assert "Tracks of this album" in page and "folder=Texas%2FGreatest+Hits" in page


def test_menu_links_to_the_library_on_every_page(web):
    for path in ("/changes", "/sync", "/libraries/music"):
        page = web.get(path).text
        assert 'href="/libraries/music/playlists"' in page, path


def _plan_page(web, name):
    """The plan page once the background planning is done."""
    url = f"/sync/{name}"
    for _ in range(200):
        state = web.get(url, headers={"Accept": "application/json"}).json()
        if not state["active"]:
            return web.get(url).text
        time.sleep(0.05)
    raise AssertionError(f"still planning: {state}")


def test_sync_preview(web, Session):
    assert "No sync targets" in web.get("/sync").text
    with Session() as session:
        library = session.scalar(select(Library))
        session.add(SyncTarget(name="backup", library=library, bucket="music", prefix=""))
        session.commit()
    page = _plan_page(web, "backup")
    assert "3 upload" in page
    assert "Texas/Greatest Hits/11 Halo.mp3" in page
    assert web.get("/sync/nothing").status_code == 404


@pytest.fixture
def s3(monkeypatch):
    import boto3
    from moto import mock_aws

    for name, value in {
        "AWS_ACCESS_KEY_ID": "test", "AWS_SECRET_ACCESS_KEY": "test",
        "AWS_DEFAULT_REGION": "us-east-1",
    }.items():  # fmt: skip
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    with mock_aws():
        client = boto3.client("s3")
        client.create_bucket(Bucket="music")
        yield client


def _wait_for(web, job_url):
    for _ in range(200):
        state = web.get(job_url, headers={"Accept": "application/json"}).json()
        if not state["active"]:
            return state
        time.sleep(0.05)
    raise AssertionError(f"sync still running: {state}")


def test_start_sync_from_the_web(web, Session, s3):
    with Session() as session:
        library = session.scalar(select(Library))
        session.add(SyncTarget(name="backup", library=library, bucket="music", prefix=""))
        session.commit()
    page = _plan_page(web, "backup")
    expected = re.search(r'name="expected" value="(\w+)"', page).group(1)
    response = web.post("/sync/jobs", data={"target": "backup", "expected": expected},
                        follow_redirects=False)  # fmt: skip
    assert response.status_code == 303
    job_url = response.headers["location"]
    assert 'data-sync-job="' + job_url in web.get(job_url).text
    state = _wait_for(web, job_url)
    assert (state["status"], state["done"], state["total"], state["failed"]) == ("done", 3, 3, 0)
    keys = {o["Key"] for o in s3.list_objects_v2(Bucket="music")["Contents"]}
    assert "Texas/Greatest Hits/11 Halo.mp3" in keys
    page = _plan_page(web, "backup")
    assert "Nothing to do" in page and "Start sync" not in page and "Recent syncs" in page
    assert "Done" in web.get(job_url).text


def test_plan_page_shows_progress_while_planning(web, Session, monkeypatch):
    from audio_organizer import sync as sy

    release = threading.Event()
    real_plan = sy.plan
    calls = []

    def slow_plan(session, target, **kwargs):
        calls.append(target.name)
        kwargs["progress"](1, 3)
        release.wait(5)
        return real_plan(session, target, **kwargs)

    monkeypatch.setattr(sy, "plan", slow_plan)
    with Session() as session:
        library = session.scalar(select(Library))
        session.add(SyncTarget(name="backup", library=library, bucket="music", prefix=""))
        session.commit()
    page = web.get("/sync/backup").text  # returns at once
    assert "data-sync-plan" in page and "Start sync" not in page
    for _ in range(100):
        state = web.get("/sync/backup", headers={"Accept": "application/json"}).json()
        if state["done"]:
            break
        time.sleep(0.02)
    assert state["active"] and (state["done"], state["total"]) == (1, 3)
    assert "Checked 1 of 3 tracks" in state["counts"]
    release.set()
    assert "3 upload" in _plan_page(web, "backup")
    assert calls == ["backup"]  # reloading the page did not plan again


def test_starting_a_sync_runs_the_plan_that_was_shown(web, Session, s3, monkeypatch):
    from audio_organizer import sync as sy

    with Session() as session:
        library = session.scalar(select(Library))
        session.add(SyncTarget(name="backup", library=library, bucket="music", prefix=""))
        session.commit()
    expected = re.search(r'name="expected" value="(\w+)"', _plan_page(web, "backup")).group(1)
    monkeypatch.setattr(sy, "plan", lambda *a, **k: pytest.fail("planned again"))
    response = web.post("/sync/jobs", data={"target": "backup", "expected": expected})
    state = _wait_for(web, str(response.url.path))
    assert (state["status"], state["done"], state["total"]) == ("done", 3, 3)


def test_web_sync_shows_planning_progress(web, Session, s3, monkeypatch):
    from audio_organizer import sync as sy

    release = threading.Event()
    real_plan = sy.plan

    def slow_plan(session, target, **kwargs):
        kwargs["progress"](2, 3)
        release.wait(5)
        return real_plan(session, target, **kwargs)

    monkeypatch.setattr(sy, "plan", slow_plan)
    with Session() as session:
        library = session.scalar(select(Library))
        session.add(SyncTarget(name="backup", library=library, bucket="music", prefix=""))
        session.commit()
    response = web.post("/sync/jobs", data={"target": "backup", "expected": "x"})
    job_url = str(response.url.path)
    for _ in range(100):
        state = web.get(job_url, headers={"Accept": "application/json"}).json()
        if state["done"]:
            break
        time.sleep(0.02)
    assert state["status"] == "planning" and (state["done"], state["total"]) == (2, 3)
    assert "Checked 2 of 3 tracks" in state["counts"]
    release.set()
    assert _wait_for(web, job_url)["status"] == "changed"


def test_web_sync_refuses_a_changed_plan(web, Session, s3):
    with Session() as session:
        library = session.scalar(select(Library))
        session.add(SyncTarget(name="backup", library=library, bucket="music", prefix=""))
        session.commit()
    response = web.post("/sync/jobs", data={"target": "backup", "expected": "outdated"})
    state = _wait_for(web, str(response.url.path))
    assert state["status"] == "changed" and state["label"] == "Not started"
    assert not s3.list_objects_v2(Bucket="music").get("Contents")


def test_web_sync_waits_for_a_running_sync(web, Session):
    from audio_organizer import sync_jobs as jobs

    with Session() as session:
        library = session.scalar(select(Library))
        target = SyncTarget(name="backup", library=library, bucket="music", prefix="")
        session.add(target)
        session.commit()
        job = jobs.start(session, target, "cli")  # as if `aorg sync` were running
        job_id = job.id
    page = web.get("/sync/backup").text
    assert "under way" in page and "Start sync" not in page
    assert "syncing now" in web.get("/sync").text
    response = web.post("/sync/jobs", data={"target": "backup", "expected": "x"})
    assert response.url.path == f"/sync/jobs/{job_id}" and "already being synced" in response.text
    response = web.post(f"/sync/jobs/{job_id}/stop")
    assert "Stopping" in response.text
    with Session() as session:
        assert session.get(jobs.SyncJob, job_id).stop_requested


def test_sync_target_with_slash_in_legacy_name_is_reachable(web, Session):
    with Session() as session:
        library = session.scalar(select(Library))
        session.add(SyncTarget(name="old/backup", library=library, bucket="music"))
        session.commit()
    page = web.get("/sync").text
    assert 'href="/sync/old/backup"' in page
    assert "3 upload" in _plan_page(web, "old/backup")


def test_sync_target_form(web, Session):
    form = {"name": "nas", "library": "music", "bucket": "tunes", "prefix": "/mp3/",
            "endpoint_url": "https://s3.example.com", "region": "", "profile": "home"}  # fmt: skip
    assert "Add a sync target" in web.get("/sync/targets/new").text
    bad = web.post("/sync/targets", data={**form, "name": "a/b"})
    assert bad.status_code == 400 and "Invalid target name" in bad.text
    assert web.post("/sync/targets", data={**form, "bucket": ""}).status_code == 400
    assert web.post("/sync/targets", data=form, follow_redirects=False).status_code == 303
    with Session() as session:
        target = session.scalar(select(SyncTarget))
        assert (target.name, target.bucket, target.prefix, target.profile, target.region) == (
            "nas", "tunes", "mp3", "home", None)  # fmt: skip
        target_id = target.id
    assert "s3://tunes/mp3/Artist/Album/01 Title.mp3" in web.get(f"/sync/targets/{target_id}").text
    assert web.post(f"/sync/targets/{target_id}", data={**form, "name": "nas2"}).status_code == 200
    assert web.post("/sync/targets", data={**form, "name": "nas2"}).status_code == 400  # taken
    web.post(f"/sync/targets/{target_id}/remove", data={})  # not confirmed
    with Session() as session:
        assert session.scalar(select(SyncTarget)) is not None
    page = web.post(f"/sync/targets/{target_id}/remove", data={"confirm": "1"}).text
    assert "Removed target nas2" in page and "No sync targets" in page


def _keep_choices(page: str) -> dict[str, list[str]]:
    """The keep radio buttons of the duplicates page: name -> values, checked first."""
    choices: dict[str, list[str]] = {}
    for name, value, checked in re.findall(
        r'type="radio" id="keep-\d+" name="(keep-\w+)" value="(\d+)" (checked)?', page
    ):
        values = choices.setdefault(name, [])
        values.insert(0, value) if checked else values.append(value)
    return choices


def test_duplicates(web):
    page = web.get("/libraries/music/duplicates").text
    assert "<strong>1</strong>" in page and "Texas/Texas_Halo.mp3" in page
    assert "mood: Upbeat" in page  # only the flat copy has a mood; copied to the album copy
    assert "data-play" in page
    [(name, (proposed, other))] = _keep_choices(page).items()
    response = web.post("/libraries/music/duplicates", data={name: proposed})
    assert response.url.path == "/libraries/music/pending"
    assert "Trash: move to the trash (a copy of Texas/Greatest Hits/11 Halo.mp3)" in response.text
    assert "Mood: Upbeat" in response.text  # the tag copied to the kept copy
    assert "1 with changes waiting" in web.get("/libraries/music/duplicates").text
    assert 'for="keep-' in page  # clicking a copy's path selects it


def test_duplicates_keep_another_copy(web):
    page = web.get("/libraries/music/duplicates").text
    [(name, (proposed, other))] = _keep_choices(page).items()
    response = web.post("/libraries/music/duplicates", data={name: other})
    assert "Trash: move to the trash (a copy of Texas/Texas_Halo.mp3)" in response.text


def test_duplicates_limit_clamped(web):
    """A hand-edited URL can still ask for more than the page's own max="1000"."""
    page = web.get("/libraries/music/duplicates", params={"limit": 5000}).text
    assert 'max="1000"' in page
    assert 'value="1000"' in page  # the batch size shown back is clamped too


def test_batch_too_large_is_refused_gracefully(web):
    """A posted batch form has one field per row; Starlette refuses a form over 1000
    fields, so a hand-edited form that still asks for more gets a friendly error."""
    response = web.post("/libraries/music/duplicates", data={f"keep-{i}": "1" for i in range(1001)})
    assert response.status_code == 400
    assert "too many rows" in response.text
    assert "Nothing was changed" in response.text


def test_encodings(web, Session):
    make_audio(web.root / "P!nk/Funhouse/01 So What.mp3", freq=330, bitrate="128k",
               title="So What", track="1")  # fmt: skip
    with Session() as session:
        scan(session, session.scalar(select(Library)))
    page = web.get("/libraries/music/encodings").text
    assert "<strong>1</strong>" in page and "mp3 128 kbps" in page
    [(name, (proposed, other))] = _keep_choices(page).items()
    assert proposed == str(_file_id(Session, "P!nk/Funhouse/01 So What.mp3"))
    response = web.post("/libraries/music/encodings", data={name: other})
    assert response.url.path == "/libraries/music/pending"
    assert "P!nk/Funhouse/01 So What.mp3" in response.text
    assert "move to the trash (another encoding of P!nk/Funhouse/01 So What.m4a)" in response.text
    assert "1 with changes pending" in web.get("/libraries/music/encodings").text


def test_neighbours(web, Session):
    make_audio(web.root / "Texas/Texas_Say What You Want.mp3", freq=350, artist="Texas",
               album="Greatest Hits", title="Say What You Want", track="12")  # fmt: skip
    with Session() as session:
        scan(session, session.scalar(select(Library)))
    page = web.get("/libraries/music/neighbours").text
    assert "<strong>1</strong>" in page and "12 Say What You Want.mp3" in page
    response = web.post(
        "/libraries/music/neighbours", data={"folder": "Texas/Greatest Hits"}
    )  # fmt: skip
    assert response.url.path == "/libraries/music/pending"
    assert "Texas/Greatest Hits/12 Say What You Want.mp3" in response.text
    assert "1 with changes pending" in web.get("/libraries/music/neighbours").text


def test_audio(web, Session):
    file_id = _file_id(Session, "Texas/Texas_Halo.mp3")
    response = web.get(f"/tracks/{file_id}/audio")
    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/mpeg"
    assert response.content == (web.root / "Texas/Texas_Halo.mp3").read_bytes()
    partial = web.get(f"/tracks/{file_id}/audio", headers={"Range": "bytes=0-99"})
    assert partial.status_code == 206 and len(partial.content) == 100

    # After trashing, it plays from the trash
    page = web.get("/libraries/music/duplicates").text
    [(name, (proposed, other))] = _keep_choices(page).items()
    web.post("/libraries/music/duplicates", data={name: proposed})
    web.post("/libraries/music/pending", data={"action": "write", "file_ids": [file_id]})
    assert not (web.root / "Texas/Texas_Halo.mp3").exists()
    assert web.get(f"/tracks/{file_id}/audio").status_code == 200
    assert web.get("/tracks/99999/audio").status_code == 404


def test_album_art(web, Session, tmp_path):
    file_id = _file_id(Session, "Texas/Greatest Hits/11 Halo.mp3")
    assert web.get(f"/tracks/{file_id}/art").status_code == 404
    assert f'src="/tracks/{file_id}/art"' in web.get("/libraries/music/albums").text

    # Without an embedded cover, the folder's
    folder = web.root / "Texas/Greatest Hits"
    (folder / "cover.jpg").write_bytes(b"\xff\xd8\xff\xe0 folder")
    response = web.get(f"/tracks/{file_id}/art")
    assert response.content == b"\xff\xd8\xff\xe0 folder"
    etag = response.headers["etag"]
    assert web.get(f"/tracks/{file_id}/art", headers={"If-None-Match": etag}).status_code == 304

    # The embedded one first; the file changed, and so did the ETag
    image = tmp_path / "front.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n embedded")
    covers.embed(folder / "11 Halo.mp3", image)
    response = web.get(f"/tracks/{file_id}/art", headers={"If-None-Match": etag})
    assert response.status_code == 200
    assert response.content == image.read_bytes()


def test_menu_marks_the_current_page(web):
    page = web.get("/libraries/music/albums").text
    assert '<a href="/libraries/music/albums" aria-current="page">' in page
    assert '<a href="/libraries/music" aria-current' not in page
    assert '<a href="/libraries/music" aria-current="page">' in web.get("/libraries/music").text


def test_track_page_sections(web, Session):
    file_id = _file_id(Session, "Texas/Greatest Hits/11 Halo.mp3")
    page = web.get(f"/tracks/{file_id}").text
    for heading in ("Sorting", "Credits", "Mood and rating", "Loudness (ReplayGain)"):
        assert f"<h2>{heading}</h2>" in page
    assert "ID3v2.3 tag" in page and 'name="rating"' in page
    assert "not yet in the file" not in page


def test_measure_and_write_replaygain(web, Session):
    file_id = _file_id(Session, "Texas/Greatest Hits/11 Halo.mp3")
    page = web.post(f"/tracks/{file_id}/replaygain").text
    assert "Measured: gain" in page and "not in file" in page
    # Track gain and peak, and album gain and peak: the album has only this track
    assert "4 values not yet in the file" in page

    assert f'name="file" value="{file_id}"' in page  # Review and write: the Pending page
    page = web.post(
        "/libraries/music/pending",
        data={"action": "write", "file_ids": [file_id], "id3_version": "4"},
    ).text
    assert "was retagged as ID3v2.4" in page
    path = web.root / "Texas/Greatest Hits/11 Halo.mp3"
    assert tags.id3_version(path) == 4
    assert tags.read(path).fields["replaygain_track_gain"].endswith(" dB")
    assert "not yet in the file" not in web.get(f"/tracks/{file_id}").text


def test_embed_folder_cover(web, Session):
    folder = web.root / "Texas/Greatest Hits"
    (folder / "Folder.jpg").write_bytes(b"\xff\xd8\xff\xe0 cover")
    file_id = _file_id(Session, "Texas/Greatest Hits/11 Halo.mp3")
    page = web.get(f"/tracks/{file_id}").text
    assert "No cover in the file" in page and "Folder.jpg" in page
    assert web.get(f"/tracks/{file_id}/cover?folder=1").content.startswith(b"\xff\xd8")
    assert web.get(f"/tracks/{file_id}/cover").status_code == 404

    response = web.post(f"/tracks/{file_id}/cover")  # decided, then reviewed on Pending
    assert response.url.path == "/libraries/music/pending"
    assert "Cover: Texas/Greatest Hits/Folder.jpg" in response.text
    assert web.get(f"/tracks/{file_id}/cover").status_code == 404  # not in the file yet
    page = web.post(
        "/libraries/music/pending", data={"action": "write", "file_ids": [file_id]}
    ).text
    changeset_path = re.search(r'action="(/changes/\d+)/undo"', page).group(1)
    assert web.get(f"/tracks/{file_id}/cover").content == (folder / "Folder.jpg").read_bytes()
    web.post(f"{changeset_path}/undo")
    assert web.get(f"/tracks/{file_id}/cover").status_code == 404
    assert (
        "Cover: Texas/Greatest Hits/Folder.jpg"
        in web.get("/libraries/music/pending", params={"file": file_id}).text
    )  # the decision stays, so the cover is pending again


def test_pending_page_writes_by_selection(web, Session):
    from audio_organizer import loudness
    from audio_organizer.models import Library as Lib

    with Session() as session:
        lib = session.scalar(select(Lib))
        assert loudness.measure_library(session, lib).measured == 3
        # A changeset proposed elsewhere, waiting for review: its file is left to it
        pink = session.scalar(select(AudioFile).where(AudioFile.path.contains("So What")))
        ch.propose_retag(ch.new_changeset(session, "waiting", "manual"), pink, {"genre": "Pop"})
        session.commit()
    halo = _file_id(Session, "Texas/Texas_Halo.mp3")
    web.post(f"/tracks/{halo}/edit", data={"rating": "5"})  # an edit, only in the catalog

    assert web.get("/libraries/music/write-tags").url.path == "/libraries/music/pending"
    page = web.get("/libraries/music/pending").text
    assert "2 files have values not yet in them" in page
    assert "1 more have changes waiting" in page
    assert "Track gain" in page and "Album artist" not in page  # evidence only
    assert "Albums with pending changes" in page
    assert re.search(r'artist=Texas&amp;album=Greatest\+Hits">Greatest Hits</a>', page)
    assert re.search(r'artist=Texas&amp;album=">\(no album\)</a>', page)
    # Each album's header row has a checkbox for its files (shown by ui.js)
    groups = set(re.findall(r'data-select-group="(\d+)"', page))
    assert len(groups) == 2 and groups == set(
        re.findall(r'<tr data-select-row data-group="(\d+)"', page)
    )

    page = web.get("/libraries/music/pending", params={"only": "replaygain_album"}).text
    assert "Track gain:" not in page and "Album gain:" in page

    # Only the chosen album, and only what is ticked, is written
    page = web.get(
        "/libraries/music/pending", params={"artist": "Texas", "album": "Greatest Hits"}
    ).text
    assert "<h1>Pending: Texas – Greatest Hits</h1>" in page
    assert page.count('name="file_ids"') == 1 and "Albums with pending changes" not in page
    one = _file_id(Session, "Texas/Greatest Hits/11 Halo.mp3")
    page = web.post(
        "/libraries/music/pending",
        data={"action": "write", "artist": "Texas", "album": "Greatest Hits",
              "file_ids": [one, halo], "id3_version": "3"},
    ).text  # fmt: skip
    assert "was retagged as ID3v2.3" in page and "(Texas – Greatest Hits)" in page
    fields = tags.read(web.root / "Texas/Greatest Hits/11 Halo.mp3").fields
    assert fields["replaygain_track_gain"].endswith(" dB") and "replaygain_album_peak" in fields
    assert "rating" not in tags.read(web.root / "Texas/Texas_Halo.mp3").fields  # not chosen
    assert "1 file has values not yet in it" in web.get("/libraries/music/pending").text


def test_pending_page_keeps_tracks_without_an_artist_apart(web, Session):
    """A group without an artist is chosen by artist="", which no redirect may drop."""
    loose = web.root / "Loose.mp3"
    loose.write_bytes((web.root / "Texas/Texas_Halo.mp3").read_bytes())
    tags.write(loose, {"artist": None, "albumartist": None, "album": None, "title": None})
    with Session() as session:
        scan(session, session.scalar(select(Library)))
    loose_id = _file_id(Session, "Loose.mp3")
    web.post(f"/tracks/{loose_id}/edit", data={"rating": "5"})
    web.post(f"/tracks/{_file_id(Session, 'Texas/Texas_Halo.mp3')}/edit", data={"rating": "4"})

    page = web.get("/libraries/music/pending").text
    assert re.search(r'artist=&amp;album=">\(no album\)</a>', page)
    page = web.get("/libraries/music/pending", params={"artist": ""}).text
    assert "<h1>Pending: (no artist)</h1>" in page
    assert page.count('name="file_ids"') == 1 and "Loose.mp3" in page

    response = web.post(
        "/libraries/music/pending",
        data={"action": "discard", "artist": "", "album": "", "file_ids": [loose_id]},
    )  # fmt: skip
    assert response.url.params["artist"] == "" and response.url.params["album"] == ""
    assert "Nothing to write" in web.get(response.url.path, params={"artist": ""}).text


def test_ratings_and_covers_pages(web, Session):
    tags.write(web.root / "Texas/Texas_Halo.mp3", {"preference": "Very Good"})
    (web.root / "Texas/Greatest Hits/Folder.jpg").write_bytes(b"\xff\xd8\xff\xe0 cover")
    with Session() as session:
        scan(session, session.scalar(select(Library)))

    page = web.get("/libraries/music/ratings").text
    assert "Very Good" in page and 'option value="4" selected' in page
    page = web.post("/libraries/music/ratings", data={"pref-1": "Very Good", "stars-1": "5"}).text
    assert "Rated 1 files" in page and "Rating: 5" in page  # on the Pending page

    page = web.get("/libraries/music/covers").text
    assert "Texas/Greatest Hits/" in page and "Folder.jpg" in page
    response = web.post("/libraries/music/covers", data={"folder": "Texas/Greatest Hits"})
    assert response.url.path == "/libraries/music/pending"
    assert "Cover: Texas/Greatest Hits/Folder.jpg" in response.text

    # The ticked folders are chosen; none ticked chooses nothing, not the whole batch
    assert 'name="pick" value="Texas/Greatest Hits"' in page
    response = web.post("/libraries/music/covers", data={"picking": "true"})
    assert "No folder selected" in response.text
    response = web.post(
        "/libraries/music/covers", data={"picking": "true", "pick": "Texas/Greatest Hits"}
    )
    # The first POST decided them, so the page no longer suggests that folder
    assert response.url.path == "/libraries/music/covers"
    assert "Nothing to embed" in response.text
    assert 'name="pick" value="Texas/Greatest Hits"' not in web.get("/libraries/music/covers").text


def test_covers_found_online(web, Session, tmp_path):
    image = tmp_path / "found.jpg"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=red:s=300x300",
                    "-frames:v", "1", str(image)], check=True)  # fmt: skip
    with Session() as session:
        stored = covers.store_image(session, image.read_bytes(), "coverartarchive")
        session.add(CoverLookup(
            key=covers.album_key("Texas", "Greatest Hits"), artist="Texas",
            album="Greatest Hits", status="found", image_sha1=stored.sha1, min_pixels=200,
        ))  # fmt: skip
        session.commit()
    page = web.get("/libraries/music/covers").text
    assert "Texas – Greatest Hits, found online" in page and f"/cover-images/{stored.sha1}" in page
    assert web.get(f"/cover-images/{stored.sha1}").content == image.read_bytes()
    page = web.get("/libraries/music/covers", params={"min_pixels": 400}).text
    assert "Greatest Hits, found online" not in page and "coverart --min-pixels 400" in page

    page = web.get("/libraries/music/covers", params={"save_images": "false"}).text
    assert "to be saved as" not in page and "Greatest Hits, found online" in page

    response = web.post("/libraries/music/covers", data={"folder": "Texas/Greatest Hits"})
    assert "Archive&#39;s image of Texas – Greatest Hits (300x300)" in response.text
    assert "Folder image: cover.jpg from" in response.text
    # Chosen, so not suggested again until written or undone
    assert "Greatest Hits, found online" not in web.get("/libraries/music/covers").text


def test_artists_page(web):
    page = web.get("/libraries/music/artists", params={"show": "all"}).text
    assert "2 artists" in page and "P!nk" in page
    page = web.post(
        "/libraries/music/artists",
        data={"artist": "Texas", "sort_name": "Texas (band)", "show": "named"},
    ).text
    assert "Saved the sort name of Texas" in page and 'value="Texas (band)"' in page
    page = web.post("/libraries/music/artists", data={"accept": "all", "show": "suggested"}).text
    assert "Accepted 0 suggested sort names" in page
    page = web.post(
        "/libraries/music/artists", data={"artist": "P!nk", "sort_name": "P!nk", "show": "named"}
    ).text
    assert 'aria-label="Keep ' not in page  # both artists have a sort name now
    assert 'value="P!nk"' in page


def test_layout_page(web):
    page = web.get("/libraries/music/layout").text
    assert "0 files in 0 albums can move" in page  # all in place or lacking an album

    response = web.post("/libraries/music/layout", data={"template": "{artist}/{colour}"})
    assert response.status_code == 400 and "Unknown fields" in response.text

    template = "{albumartist}/{year} {album}/{track} {title}"
    page = web.post("/libraries/music/layout", data={"template": template}).text
    assert "Layout saved" in page and "Texas/2000 Greatest Hits/" in page
    response = web.post(
        "/libraries/music/layout/decide", data={"folder": "Texas/2000 Greatest Hits"}
    )
    assert response.url.path == "/libraries/music/pending"
    assert "Texas/2000 Greatest Hits/11 Halo.mp3" in response.text


def test_album_merges_page(web):
    page = web.get("/libraries/music/album-merges").text
    assert "0 pairs of album folders" in page
    response = web.post(
        "/libraries/music/album-merges", data={"source": "No/Such", "target": "Texas/Greatest Hits"}
    )
    assert response.status_code == 400
    assert web.post("/libraries/music/album-merges", data={}).url.path.endswith("album-merges")


def test_gnudb_button_and_lookup(web, monkeypatch):
    from audio_organizer import gnudb

    assert "Look up in gnudb" not in web.get("/libraries/music").text
    for params in ({"folder": "P!nk/Funhouse"}, {"album": "Funhouse"}):
        page = web.get("/libraries/music", params=params).text
        assert 'name="folder" value="P!nk/Funhouse"' in page

    def post():
        return web.post("/libraries/music/gnudb", data={"folder": "P!nk/Funhouse"})

    assert "Set AORG_GNUDB_EMAIL" in post().text  # redirected back with a message
    monkeypatch.setenv("AORG_GNUDB_EMAIL", "me@example.org")
    album = gnudb.Folder("P!nk/Funhouse", "P!nk", "Funhouse", [], [], [])
    entry = gnudb.Entry("pop", "1", "P!nk", "Funhouse", "2008", "Pop", ["So What"])
    asked = []

    def lookup(session, lib, folder):
        asked.append(folder)
        return results

    monkeypatch.setattr(gnudb, "lookup_folder", lookup)
    results = [(album, entry)]
    assert "Found in gnudb: Funhouse" in post().text
    results = [(album, None)]
    assert "no matching entry" in post().text
    results = []
    assert "Not an album folder" in post().text
    assert asked == ["P!nk/Funhouse"] * 3


def test_one_long_action_at_a_time(web, monkeypatch):
    from audio_organizer import gnudb

    monkeypatch.setenv("AORG_GNUDB_EMAIL", "me@example.org")
    started, release = threading.Event(), threading.Event()

    def slow_lookup(session, lib, folder):
        started.set()
        release.wait(5)
        return []

    monkeypatch.setattr(gnudb, "lookup_folder", slow_lookup)
    first = threading.Thread(
        target=web.post, args=("/libraries/music/gnudb",), kwargs={"data": {"folder": "x"}}
    )
    first.start()
    try:
        assert started.wait(5)
        refused = web.post("/changes/1/apply")  # another action, while the lookup waits
        assert refused.status_code == 409
        assert "still in progress" in refused.text
        assert web.get("/libraries/music").status_code == 200  # browsing is not blocked
    finally:
        release.set()
        first.join()
    assert web.post("/changes/1/apply").status_code == 404  # free again (no such changeset)


def test_long_action_forms_disable_their_buttons(web):
    """Forms of long-running actions carry data-busy, which ui.js turns into spinners."""
    page = web.get("/libraries/music", params={"folder": "P!nk/Funhouse"}).text
    assert page.count("data-busy") == 2  # the gnudb and cover lookups


def test_cover_lookup_button(web, monkeypatch):
    from audio_organizer import coverart

    page = web.get("/libraries/music", params={"folder": "P!nk/Funhouse"}).text
    assert "Look up cover" in page
    assert "Look up cover" not in web.get("/libraries/music").text

    results = []
    asked = []

    def lookup(session, lib, folder):
        asked.append(folder)
        return results

    monkeypatch.setattr(coverart, "lookup_folder", lookup)

    def post():
        return web.post("/libraries/music/coverart", data={"folder": "P!nk/Funhouse"})

    album = type("Album", (), {"album": "Funhouse"})()
    results.append((album, object()))
    assert "Found a cover for Funhouse" in post().text
    results[:] = [(album, None)]
    assert "No cover found online" in post().text
    results[:] = []
    assert "No album here needs a cover" in post().text
    assert asked == ["P!nk/Funhouse"] * 3


def test_review_gnudb_values_on_the_pending_page(web, Session):
    from audio_organizer import provenance

    with Session() as session:
        file = session.scalar(select(AudioFile).where(AudioFile.path.contains("So What")))
        provenance.set_value(session, file, "gnudb", "title", "So What Now")
        session.commit()
    base = "/libraries/music/pending"
    plain = web.get(base, params={"path": "P!nk/"}).text
    assert "0 files have values not yet in them" in plain  # a lookup below the tag is no value
    page = web.get(base, params={"path": "P!nk/", "prefer_gnudb": "true"}).text
    assert 'class="old">So What</span> → So What Now' in page
    assert 'name="prefer_gnudb" value="true" checked' in page
    assert 'value="accept"' in page and 'value="write"' not in page
    assert "Albums with pending changes" in page  # its links keep the mode
    assert re.search(
        r'artist=P%21nk&amp;album=Funhouse&amp;path=P%21nk%2F&amp;prefer_gnudb=True">', page
    )
    file_id = _file_id(Session, "P!nk/Funhouse/01 So What.m4a")
    form = {"path": "P!nk/", "prefer_gnudb": "true", "file_ids": [file_id]}
    assert web.post(base, data={"action": "write", **form}).status_code == 400
    page = web.post(base, data={"action": "accept", **form}).text
    assert "Accepted 1 of gnudb&#39;s values" in page
    assert 'class="old">So What</span> → So What Now' in page  # now pending without prefer
    page = web.get("/libraries/music/history").text
    assert "Accept gnudb&#39;s values for 1 file (P!nk/)" in page
    page = web.post(base, data={"action": "write", "path": "P!nk/", "file_ids": [file_id]}).text
    assert tags.read(web.root / "P!nk/Funhouse/01 So What.m4a").fields["title"] == "So What Now"


def test_history_shows_and_undoes_decisions(web, Session):
    file_id = _file_id(Session, "Texas/Texas_Halo.mp3")
    web.post(f"/tracks/{file_id}/edit", data={"genre": "Pop-rock"})
    page = web.get("/libraries/music/history").text
    assert "Edit Texas/Texas_Halo.mp3" in page and ">Undo</button>" in page
    operation_id = re.search(r'/history/(\d+)/undo"', page).group(1)
    page = web.get(f"/libraries/music/history/{operation_id}").text
    assert "Texas/Texas_Halo.mp3" in page and "Pop-rock" in page
    page = web.post(f"/libraries/music/history/{operation_id}/undo").text
    assert "Set 1 values back" in page
    assert "undone by #" in page and "Undo: Edit Texas/Texas_Halo.mp3" in page
    assert "Pop-rock" not in web.get(f"/tracks/{file_id}").text
    page = web.post(f"/libraries/music/history/{operation_id}/undo").text
    assert "already been undone" in page
    assert web.get("/libraries/music/history/999").status_code == 404


def test_folders_of_albums_and_artists_link_to_disk(web):
    uri = (web.root / "P!nk/Funhouse").as_uri()
    for params in ({"album": "Funhouse"}, {"artist": "P!nk"}, {"folder": "P!nk/Funhouse"}):
        page = web.get("/libraries/music", params=params).text
        assert f'href="{uri}"' in page and "Folder:" in page
    assert "Folder:" not in web.get("/libraries/music").text


def test_message_links_stay_on_the_site(web):
    page = web.get(
        "/libraries/music", params={"msg": "Hi", "msg_href": "/changes", "msg_link": "Go"}
    )
    assert '<a href="/changes">Go</a>' in page.text
    page = web.get(
        "/libraries/music", params={"msg": "Hi", "msg_href": "//evil.example", "msg_link": "Go"}
    )
    assert "evil.example" not in page.text


def test_albums_list_shows_folders(web):
    page = web.get("/libraries/music/albums").text
    assert f'href="{(web.root / "P!nk/Funhouse").as_uri()}"' in page
    assert "P!nk/Funhouse/" in page


def test_locked_database_asks_to_retry(web, monkeypatch):
    def locked(*args, **kwargs):
        raise OperationalError("INSERT", {}, sqlite3.OperationalError("database is locked"))

    monkeypatch.setattr("audio_organizer.web.app.du.survey", locked)
    response = web.get("/libraries/music/duplicates")
    assert response.status_code == 503
    assert "busy" in response.text


def test_unmeasurable_files_are_listed_on_pending_and_track_pages(web, Session, monkeypatch):
    from audio_organizer import loudness
    from audio_organizer.models import Library as Lib

    def measure(path, label=None):
        raise loudness.LoudnessError(f"Could not measure {label}: DRM-protected", "DRM-protected")

    monkeypatch.setattr(loudness, "measure", measure)
    with Session() as session:
        lib = session.scalar(select(Lib))
        assert len(loudness.measure_library(session, lib, limit=1).errors) == 1
    page = web.get("/libraries/music/pending").text
    assert "1 file could not be measured" in page and "DRM-protected" in page

    with Session() as session:
        failed = loudness.failed(session, session.scalar(select(Lib)))
    [(file, reason)] = failed
    assert reason == "DRM-protected"
    assert "Batch measuring skips this track" in web.get(f"/tracks/{file.id}").text
