import re
import subprocess

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from audio_organizer import covers, tags
from audio_organizer.models import AudioFile, CoverLookup, Library, SyncTarget
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
    assert ">was retagged</span>" in page
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

    response = web.post(
        f"{playlist_path}/edit", data={"playlist_name": "All", "rule": "x", "sort": "title"}
    )
    assert response.status_code == 400 and "Not a condition" in response.text

    response = web.post(f"{playlist_path}/delete")
    assert "No playlists yet" in response.text


def test_albums_artists_and_folders(web, Session):
    page = web.get("/libraries/music/albums").text
    assert "2 albums" in page
    [link] = re.findall(r'href="(/libraries/music\?album=Greatest[^"]*)"', page)
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
    assert re.fullmatch(r"/changes/\d+", response.url.path)
    assert "trash Texas/Texas_Halo.mp3 (same audio as Texas/Greatest Hits/11 Halo.mp3)" in (
        response.text
    )
    assert f'href="/tracks/{proposed}/audio"' in response.text  # play the kept copy too
    assert ">will be trashed</span>" in response.text
    assert ">will be kept</span>" in response.text
    assert re.search(r'href="/static/style\.css\?v=\d+"', response.text)
    assert "data-select-row" in response.text and 'data-select-all="change_ids"' in response.text
    assert "1 with changes waiting" in web.get("/libraries/music/duplicates").text
    assert 'for="keep-' in page  # clicking a copy's path selects it


def test_duplicates_keep_another_copy(web):
    page = web.get("/libraries/music/duplicates").text
    [(name, (proposed, other))] = _keep_choices(page).items()
    response = web.post("/libraries/music/duplicates", data={name: other})
    assert "trash Texas/Greatest Hits/11 Halo.mp3 (same audio as Texas/Texas_Halo.mp3)" in (
        response.text
    )


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
    changeset_path = web.post("/libraries/music/duplicates", data={name: proposed}).url.path
    web.post(f"{changeset_path}/status", data={"action": "approve-all"})
    web.post(f"{changeset_path}/apply", data={})
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

    response = web.post(f"/tracks/{file_id}/write", data={"id3_version": "4"})
    assert re.fullmatch(r"/changes/\d+", response.url.path)
    assert "will be retagged as ID3v2.4" in response.text
    changeset_path = response.url.path
    web.post(f"{changeset_path}/status", data={"action": "approve-all"})
    web.post(f"{changeset_path}/apply", data={})
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

    response = web.post(f"/tracks/{file_id}/cover")
    assert "will get the cover image" in response.text
    changeset_path = response.url.path
    web.post(f"{changeset_path}/status", data={"action": "approve-all"})
    web.post(f"{changeset_path}/apply", data={})
    assert web.get(f"/tracks/{file_id}/cover").content == (folder / "Folder.jpg").read_bytes()
    web.post(f"{changeset_path}/undo")
    assert web.get(f"/tracks/{file_id}/cover").status_code == 404


def test_write_tags_in_bulk(web, Session):
    from audio_organizer import loudness
    from audio_organizer.models import Library as Lib

    with Session() as session:
        lib = session.scalar(select(Lib))
        assert loudness.measure_library(session, lib).measured == 3
    halo = _file_id(Session, "Texas/Texas_Halo.mp3")
    web.post(f"/tracks/{halo}/edit", data={"rating": "5"})  # an edit, proposed at once

    page = web.get("/libraries/music/write-tags").text
    assert "2 files have values not yet in them" in page  # the edited one is waiting
    assert "1 more have changes waiting" in page
    assert "replaygain_track_gain" in page and "albumartist" not in page  # evidence only

    page = web.get("/libraries/music/write-tags", params={"only": "replaygain_album"}).text
    assert "replaygain_track_gain:" not in page and "replaygain_album_gain:" in page

    response = web.post(
        "/libraries/music/write-tags", data={"only": "replaygain", "id3_version": "3"}
    )
    assert re.fullmatch(r"/changes/\d+", response.url.path)
    assert "will be retagged as ID3v2.3" in response.text
    changeset_path = response.url.path
    web.post(f"{changeset_path}/status", data={"action": "approve-all"})
    web.post(f"{changeset_path}/apply", data={})
    fields = tags.read(web.root / "Texas/Greatest Hits/11 Halo.mp3").fields
    assert fields["replaygain_track_gain"].endswith(" dB") and "replaygain_album_peak" in fields
    assert "0 files have values" in web.get("/libraries/music/write-tags").text


def test_ratings_and_covers_pages(web, Session):
    tags.write(web.root / "Texas/Texas_Halo.mp3", {"preference": "Very Good"})
    (web.root / "Texas/Greatest Hits/Folder.jpg").write_bytes(b"\xff\xd8\xff\xe0 cover")
    with Session() as session:
        scan(session, session.scalar(select(Library)))

    page = web.get("/libraries/music/ratings").text
    assert "Very Good" in page and 'option value="4" selected' in page
    page = web.post("/libraries/music/ratings", data={"pref-1": "Very Good", "stars-1": "5"}).text
    assert "Rated 1 files" in page and "rating: 5" in page  # on the write tags page

    page = web.get("/libraries/music/covers").text
    assert "Texas/Greatest Hits/" in page and "Folder.jpg" in page
    response = web.post("/libraries/music/covers", data={"folder": "Texas/Greatest Hits"})
    assert re.fullmatch(r"/changes/\d+", response.url.path)
    assert "will get the cover image" in response.text


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
    assert "Greatest Hits, found online" not in page and "scan --deep --min-pixels 400" in page

    response = web.post("/libraries/music/covers", data={"folder": "Texas/Greatest Hits"})
    assert "Archive&#39;s image of Texas – Greatest Hits (300x300)" in response.text
    assert "will be saved as" in response.text and "Texas/Greatest Hits/cover.jpg" in response.text

    page = web.get("/libraries/music/covers", params={"save_images": "false"}).text
    assert "to be saved as" not in page and "Greatest Hits, found online" in page


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


def test_layout_page(web):
    page = web.get("/libraries/music/layout").text
    assert "0 files in 0 albums can move" in page  # all in place or lacking an album

    response = web.post("/libraries/music/layout", data={"template": "{artist}/{colour}"})
    assert response.status_code == 400 and "Unknown fields" in response.text

    template = "{albumartist}/{year} {album}/{track} {title}"
    page = web.post("/libraries/music/layout", data={"template": template}).text
    assert "Layout saved" in page and "Texas/2000 Greatest Hits/" in page
    response = web.post(
        "/libraries/music/layout/propose", data={"folder": "Texas/2000 Greatest Hits"}
    )
    assert re.fullmatch(r"/changes/\d+", response.url.path)
    assert "Texas/2000 Greatest Hits/11 Halo.mp3" in response.text


def test_album_merges_page(web):
    page = web.get("/libraries/music/album-merges").text
    assert "0 pairs of album folders" in page
    response = web.post(
        "/libraries/music/album-merges", data={"source": "No/Such", "target": "Texas/Greatest Hits"}
    )
    assert response.status_code == 400
    assert web.post("/libraries/music/album-merges", data={}).url.path.endswith("album-merges")
