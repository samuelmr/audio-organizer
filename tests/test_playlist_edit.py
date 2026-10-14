import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from audio_organizer import playlists as pl
from audio_organizer.models import AudioFile, EffectiveMetadata, Library, Playlist
from audio_organizer.scanner import scan
from audio_organizer.web import create_app

HALO = "Texas/Greatest Hits/11 Halo.mp3"
INSANE = "Texas/Texas_Insane.mp3"


@pytest.fixture
def library(Session, copy_audio, tmp_path):
    root = tmp_path / "music"
    copy_audio("mp3", root / HALO)
    copy_audio("mp3b", root / INSANE)
    session = Session()
    lib = Library(name="music", root=str(root))
    session.add(lib)
    session.commit()
    scan(session, lib)
    yield session, lib
    session.close()


def _file(session, path):
    return session.scalar(select(AudioFile).where(AudioFile.path == path))


def _static(session, lib, name="Mix"):
    playlist = Playlist(library=lib, name=name, sort="manual")
    session.add(playlist)
    session.commit()
    return playlist


def _paths(playlist):
    return [e.path for e in playlist.entries]


def test_add_tracks_appends_and_skips_repeats(library):
    session, lib = library
    playlist = _static(session, lib)
    halo, insane = _file(session, HALO), _file(session, INSANE)
    assert pl.add_tracks(playlist, [halo]) == 1
    assert pl.add_tracks(playlist, [halo, insane, insane]) == 1
    session.commit()
    assert _paths(playlist) == [HALO, INSANE]
    assert [e.position for e in playlist.entries] == [0, 1]


def test_add_tracks_refuses_a_smart_playlist(library):
    session, lib = library
    smart = Playlist(library=lib, name="All", rule="duration>0", sort="title")
    with pytest.raises(pl.RuleError):
        pl.add_tracks(smart, [_file(session, HALO)])


def test_remove_tracks_renumbers(library):
    session, lib = library
    playlist = _static(session, lib)
    halo, insane = _file(session, HALO), _file(session, INSANE)
    pl.add_tracks(playlist, [halo, insane])
    session.commit()
    assert pl.remove_tracks(playlist, [halo.id]) == 1
    session.commit()
    assert _paths(playlist) == [INSANE]
    assert playlist.entries[0].position == 0
    assert pl.remove_tracks(playlist, [halo.id]) == 0


def test_find_tracks_by_words_and_rule(library):
    session, lib = library
    found, total = pl.find_tracks(session, lib, q="halo")
    assert [t.path for t in found] == [HALO] and total == 1
    found, total = pl.find_tracks(session, lib, rule="artist=texas")
    assert total == 2
    found, total = pl.find_tracks(session, lib, rule="artist=texas", limit=1)
    assert len(found) == 1 and total == 2
    found, total = pl.find_tracks(session, lib, q="halo", rule="artist=nobody")
    assert total == 0
    with pytest.raises(pl.RuleError):
        pl.find_tracks(session, lib, rule="colour=red")


@pytest.fixture
def web(library, Session):
    session, lib = library
    playlist = _static(session, lib)
    smart = Playlist(library=lib, name="Smart", rule="duration>0", sort="title")
    session.add(smart)
    session.commit()
    client = TestClient(create_app(Session))
    client.playlist_id, client.smart_id = playlist.id, smart.id
    client.halo = _file(session, HALO).id
    client.insane = _file(session, INSANE).id
    return client


def _entries(Session, playlist_id):
    with Session() as session:
        return _paths(session.get(Playlist, playlist_id))


def test_listings_offer_static_playlists(web):
    page = web.get("/libraries/music").text
    assert f'formaction="/playlists/{web.playlist_id}/add"' in page
    assert f'formaction="/playlists/{web.smart_id}/add"' not in page  # a rule fills it
    assert "Add to playlist" in web.get("/libraries/music/albums").text


def test_add_tracks_from_a_listing(web, Session):
    response = web.post(
        f"/playlists/{web.playlist_id}/add", data={"file": [web.halo], "back": "/libraries/music"}
    )
    assert response.url.path == "/libraries/music"
    assert "Added 1 track to Mix" in response.text
    assert _entries(Session, web.playlist_id) == [HALO]

    web.post(f"/playlists/{web.playlist_id}/add", data={"file": [web.halo, web.insane]})
    assert _entries(Session, web.playlist_id) == [HALO, INSANE]  # the repeat was left out

    assert "smart playlist" in web.post(f"/playlists/{web.smart_id}/add", data={"file": 1}).text
    assert web.post(f"/playlists/{web.playlist_id}/add").status_code == 200  # nothing chosen


def test_add_an_album(web, Session):
    with Session() as session:
        meta = session.get(EffectiveMetadata, web.halo)
        album, albumartist = meta.album, meta.albumartist
    assert album
    web.post(
        f"/playlists/{web.playlist_id}/add",
        data={"album": album, "albumartist": albumartist or ""},
    )
    assert HALO in _entries(Session, web.playlist_id)


def test_a_back_address_off_site_is_ignored(web):
    response = web.post(
        f"/playlists/{web.playlist_id}/add", data={"file": [web.halo], "back": "//evil.example/x"}
    )
    assert response.url.path == f"/playlists/{web.playlist_id}"


def test_search_the_playlist_page_and_add_the_results(web):
    url = f"/playlists/{web.playlist_id}"
    page = web.get(url, params={"rule": "artist=texas"}).text
    assert f'type="checkbox" name="file" value="{web.halo}"' in page
    assert f'type="checkbox" name="file" value="{web.insane}"' in page
    assert "Add the selected tracks" in page
    assert "Unknown field" in web.get(url, params={"rule": "colour=red"}).text
    assert "No tracks match" in web.get(url, params={"q": "zzzz"}).text

    web.post(f"{url}/add", data={"file": [web.halo], "back": url})
    page = web.get(url, params={"rule": "artist=texas"}).text
    assert f'type="checkbox" name="file" value="{web.halo}"' not in page  # already in it
    assert f'type="checkbox" name="file" value="{web.insane}"' in page


def test_remove_from_the_playlist_page(web, Session):
    web.post(f"/playlists/{web.playlist_id}/add", data={"file": [web.halo, web.insane]})
    page = web.get(f"/playlists/{web.playlist_id}").text
    assert f'action="/playlists/{web.playlist_id}/remove"' in page
    response = web.post(f"/playlists/{web.playlist_id}/remove", data={"file": [web.halo]})
    assert "Removed 1 entry" in response.text
    assert _entries(Session, web.playlist_id) == [INSANE]
    # A smart playlist has no remove buttons nor an add form
    smart_page = web.get(f"/playlists/{web.smart_id}").text
    assert "/remove" not in smart_page and "Add tracks" not in smart_page


def test_move_track_in_a_manual_playlist(library):
    session, lib = library
    playlist = _static(session, lib)
    halo, insane = _file(session, HALO), _file(session, INSANE)
    pl.add_tracks(playlist, [halo, insane])
    session.commit()
    assert not pl.move_track(session, playlist, halo.id, "up")  # already first
    assert pl.move_track(session, playlist, halo.id, "down")
    session.commit()
    assert _paths(playlist) == [INSANE, HALO]
    assert [e.position for e in playlist.entries] == [0, 1]
    assert pl.move_track(session, playlist, halo.id, "top")
    assert _paths(playlist) == [HALO, INSANE]
    assert pl.move_track(session, playlist, halo.id, "bottom")
    assert _paths(playlist) == [INSANE, HALO]
    assert not pl.move_track(session, playlist, 0, "up")  # not in the playlist


def test_move_track_passes_entries_that_are_not_found(library):
    session, lib = library
    playlist = _static(session, lib)
    halo, insane = _file(session, HALO), _file(session, INSANE)
    pl.add_tracks(playlist, [halo])
    playlist.entries.append(pl.PlaylistEntry(position=1, path="gone.mp3", file_id=None))
    pl.add_tracks(playlist, [insane])
    session.commit()
    assert pl.move_track(session, playlist, halo.id, "down")  # past the missing entry
    session.commit()
    assert _paths(playlist) == ["gone.mp3", INSANE, HALO]


def test_move_track_needs_a_manual_order(library):
    session, lib = library
    playlist = _static(session, lib)
    playlist.sort = "title"
    with pytest.raises(pl.RuleError):
        pl.move_track(session, playlist, 1, "up")
    smart = Playlist(library=lib, name="S", rule="x=y", sort="manual")
    with pytest.raises(pl.RuleError):
        pl.move_track(session, smart, 1, "up")


def test_reorder_on_the_playlist_page(web, Session):
    url = f"/playlists/{web.playlist_id}"
    web.post(f"{url}/add", data={"file": [web.halo, web.insane]})
    assert f'action="{url}/move"' in web.get(url).text
    web.post(f"{url}/move", data={"file": web.insane, "to": "up"})
    assert _entries(Session, web.playlist_id) == [INSANE, HALO]
    assert web.post(f"{url}/move", data={"file": web.insane, "to": "sideways"}).status_code == 400

    web.post(f"{url}/edit", data={"playlist_name": "Mix", "sort": "title"})
    page = web.get(url).text
    assert f'action="{url}/move"' not in page and "set Sort to manual" in page
    assert "manually" in web.post(f"{url}/move", data={"file": web.insane, "to": "down"}).text
