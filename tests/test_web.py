import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from audio_organizer import tags
from audio_organizer.models import AudioFile, Library, SyncTarget
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


def test_edit_review_apply_undo(web, Session):
    file_id = _file_id(Session, "Texas/Texas_Halo.mp3")
    form = {"artist": "Texas", "title": "Halo", "album": "Greatest Hits", "tracknumber": "11"}
    response = web.post(f"/tracks/{file_id}/edit", data=form)
    assert response.status_code == 200
    changeset_path = response.url.path
    assert re.fullmatch(r"/changes/\d+", changeset_path)
    assert "proposed" in response.text

    page = web.post(f"{changeset_path}/apply", data={}).text
    assert "Nothing to do" in page  # not approved yet

    web.post(f"{changeset_path}/status", data={"action": "approve-all"})
    page = web.post(f"{changeset_path}/apply", data={"dry_run": "true"}).text
    assert "would retag" in page
    assert "album" not in tags.read(web.root / "Texas/Texas_Halo.mp3").fields

    page = web.post(f"{changeset_path}/apply", data={}).text
    assert "Applied" in page and 'class="failed"' not in page
    fields = tags.read(web.root / "Texas/Texas_Halo.mp3").fields
    assert (fields["album"], fields["tracknumber"]) == ("Greatest Hits", "11")

    web.post(f"{changeset_path}/undo")
    assert "album" not in tags.read(web.root / "Texas/Texas_Halo.mp3").fields
    assert "0 changes" not in web.get("/changes?all=1").text


def test_edit_without_changes(web, Session):
    file_id = _file_id(Session, "P!nk/Funhouse/01 So What.m4a")
    response = web.post(f"/tracks/{file_id}/edit", data={"artist": "P!nk", "title": "So What"})
    assert "Nothing changed" in response.text


def test_select_single_changes(web, Session):
    file_id = _file_id(Session, "Texas/Texas_Halo.mp3")
    response = web.post(f"/tracks/{file_id}/edit", data={"genre": "Pop-rock"})
    changeset_path = response.url.path
    [change_id] = re.findall(r'name="change_ids" value="(\d+)"', response.text)
    page = web.post(
        f"{changeset_path}/status", data={"action": "reject", "change_ids": [change_id]}
    ).text
    assert "1 changes updated" in page and "rejected" in page


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

    response = web.post(
        f"{playlist_path}/edit", data={"playlist_name": "All", "rule": "x", "sort": "title"}
    )
    assert response.status_code == 400 and "Not a condition" in response.text

    response = web.post(f"{playlist_path}/delete")
    assert "No playlists yet" in response.text


def test_sync_preview(web, Session):
    assert "No sync targets" in web.get("/sync").text
    with Session() as session:
        library = session.scalar(select(Library))
        session.add(SyncTarget(name="backup", library=library, bucket="music", prefix=""))
        session.commit()
    page = web.get("/sync/backup").text
    assert "3 upload" in page
    assert "Texas/Greatest Hits/11 Halo.mp3" in page
    assert web.get("/sync/nothing").status_code == 404
