import pytest

from audio_organizer import changes as ch
from audio_organizer import leftovers as lo
from audio_organizer.models import Library


def _write(root, files):
    for path, data in files.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_bytes(data if isinstance(data, bytes) else data.encode())


def _approve_all(changeset):
    for change in changeset.changes:
        change.status = "approved"


def test_playlist_entries_by_format(tmp_path):
    _write(
        tmp_path,
        {
            "a.m3u": "#EXTM3U\n#EXTINF:1,x\nOne.mp3\n\nTwo.mp3\n",
            "a.pls": "[playlist]\nFile1=One.mp3\nTitle1=x\nFile2=Two.mp3\n",
            "a.wpl": '<media src="One.mp3"/><media src="Fish &amp; Chips.mp3"/>',
            "a.json": '{"track": [{"@type": "MusicRecording", "url": "One.mp3", "name": "x"}]}',
            "a.html": '<a href="One%20Two.mp3">x</a><a href="https://example.com/">y</a>',
            "latin1.m3u": b"M\xe4\xe4.mp3\n",
            "Album\u6bed3u": "One.mp3\nTwo.mp3\n",  # garbled name, no extension
            "Notes": "Remember to buy milk.mp3\nand eggs\n",
            "page.html": "<p>no tracks here</p>",
            "info.json": '{"url": "https://example.com"}',
        },
    )

    def entries(name):
        return lo.playlist_entries(tmp_path / name)

    assert entries("a.m3u") == ["One.mp3", "Two.mp3"]
    assert entries("a.pls") == ["One.mp3", "Two.mp3"]
    assert entries("a.wpl") == ["One.mp3", "Fish & Chips.mp3"]
    assert entries("a.json") == ["One.mp3"]
    assert entries("a.html") == ["One Two.mp3"]
    assert entries("latin1.m3u") == ["Mää.mp3"]
    assert entries("Album\u6bed3u") == ["One.mp3", "Two.mp3"]
    assert entries("Notes") is None  # not every line is a track
    assert entries("page.html") == [] and entries("info.json") == []
    assert entries("missing.m3u") is None


def test_dead_playlists(tmp_path):
    _write(
        tmp_path,
        {
            "Artist/Album/01 Song.mp3": "audio",
            "Artist/dead.m3u": "Gone.mp3\nAlso gone.mp3\n",
            "Artist/alive.m3u": "Gone.mp3\nAlbum/01 Song.mp3\n",  # one is enough
            "Artist/from root.m3u": "Artist/Album/01 Song.mp3\n",  # relative to the library
            "Artist/dead.html": '<a href="Gone%20too.mp3">x</a>',
            "Artist/windows.m3u": "C:\\Music\\Song.mp3\n",  # cannot be checked from here
            "Artist/stream.m3u": "http://example.com/a.mp3\n",
            "Artist/empty.m3u": "#EXTM3U\n",
            "Artist/page.html": "<p>hello</p>",
        },
    )
    root = tmp_path

    assert lo.is_dead_playlist(root / "Artist/dead.m3u", root)
    assert lo.is_dead_playlist(root / "Artist/dead.html", root)
    for alive in ("alive.m3u", "from root.m3u", "windows.m3u", "stream.m3u", "empty.m3u"):
        assert not lo.is_dead_playlist(root / "Artist" / alive, root), alive
    assert not lo.is_dead_playlist(root / "Artist/page.html", root)
    assert not lo.is_dead_playlist(root / "Artist/Album/01 Song.mp3", root)


def test_a_decomposed_name_is_found(tmp_path):
    _write(tmp_path, {"Mää.mp3": "audio", "list.m3u": "Ma\u0308a\u0308.mp3\n"})

    assert not lo.is_dead_playlist(tmp_path / "list.m3u", tmp_path)


@pytest.fixture
def dead_library(Session, tmp_path):
    root = tmp_path / "music"
    _write(
        root,
        {
            "YUP/Album/01 Song.mp3": "audio",
            "YUP/YUP.m3u": "YUP_Gone.mp3\n",
            "YUP/YUP.json": '{"track": [{"url": "YUP_Gone.mp3"}]}',
            "YUP/YUP.html": '<a href="YUP_Gone.mp3">x</a>',
            "YUP/Min\u68ef len": "YUP_Gone.mp3\n",  # garbled name, no extension
            "YUP/Album/Album.m3u": "01 Song.mp3\n",  # alive
            "YUP/Album/notes.json": "{}",
            "Other/Empty/old.m3u": "Gone.mp3\n",  # a folder without music: litter
            "Other/Empty/keep.m3u": "../../YUP/Album/01 Song.mp3\n",
            "Dead.m3u": "Gone.mp3\n",  # in the library root: not looked at
            "Playlists/Best.m3u": "Gone.mp3\n",  # the library's playlist folder
        },
    )
    session = Session()
    lib = Library(root=str(root), name="test", playlist_folder="Playlists")
    session.add(lib)
    session.commit()
    yield session, lib, root
    session.close()


def test_survey_finds_dead_playlists_in_folders_with_music(dead_library):
    _, lib, _ = dead_library

    found = lo.survey(lib)

    assert found.dead == ["YUP/Min\u68ef len", "YUP/YUP.html", "YUP/YUP.json", "YUP/YUP.m3u"]
    # In a folder without music a dead playlist is litter, a live one still is not
    folders = {f.path: f for f in found.folders}
    empty = folders["Other/Empty"]
    assert (empty.litter, empty.other) == (["Other/Empty/old.m3u"], ["Other/Empty/keep.m3u"])
    # The library's playlist folder is never judged: its playlist is only listed
    assert (folders["Playlists"].litter, folders["Playlists"].other) == ([], ["Playlists/Best.m3u"])


def test_propose_trashes_dead_playlists_and_undo_restores_them(dead_library):
    session, lib, root = dead_library

    changeset = lo.propose(session, lib, lo.survey(lib), "x")
    _approve_all(changeset)
    outcomes = ch.apply(session, changeset)

    assert all(o.ok for o in outcomes), [o.message for o in outcomes]
    assert sorted(p.name for p in (root / "YUP").iterdir()) == ["Album"]
    assert (root / "YUP/Album/Album.m3u").is_file()
    assert (root / "Dead.m3u").is_file() and (root / "Playlists/Best.m3u").is_file()

    outcomes = ch.undo(session, changeset)

    assert all(o.ok for o in outcomes), [o.message for o in outcomes]
    assert (root / "YUP/YUP.m3u").read_bytes() == b"YUP_Gone.mp3\n"


def test_dead_playlists_follow_the_selected_path(dead_library):
    _, lib, _ = dead_library

    assert lo.survey(lib, "Other").dead == []
    assert len(lo.survey(lib, "YUP").dead) == 4
