import pytest

from audio_organizer import playlist_files as pf


def test_clean_folder():
    assert pf.clean_folder("") == ""
    assert pf.clean_folder(" /Playlists//Mixes/ ") == "Playlists/Mixes"
    assert pf.clean_folder("a\\b/./c") == "a/b/c"
    with pytest.raises(ValueError):
        pf.clean_folder("../elsewhere")


def test_parse_formats():
    assert pf.parse_formats("M3U, json,m3u") == ["m3u", "json"]
    with pytest.raises(ValueError):
        pf.parse_formats("m3u,wav")
    with pytest.raises(ValueError):
        pf.parse_formats(" , ")


def test_entries_are_relative_to_the_folder():
    track = "Texas/Greatest Hits/11 Halo.mp3"
    assert pf.entry_location("", track) == track
    assert pf.entry_location("Playlists", track) == "../" + track
    assert pf.entry_location("a/b", track) == "../../" + track


def test_file_path():
    assert pf.file_path("", "Mix: 1", "m3u") == "Mix- 1.m3u"
    assert pf.file_path("lists", "Mix", "json") == "lists/Mix.json"
