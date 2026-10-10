import subprocess

import pytest
from sqlalchemy import select, update
from typer.testing import CliRunner

from audio_organizer import changes as ch
from audio_organizer import coverart, covers, images, tags
from audio_organizer.cli import app
from audio_organizer.models import AudioFile, CoverLookup, FieldValue, Library
from audio_organizer.scanner import scan


def _image(path, size, color="red"):
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"color=c={color}:s={size}",
                    "-frames:v", "1", str(path)], check=True)  # fmt: skip
    return path


class FakeClient:
    """MusicBrainz and the Cover Art Archive, as far as the tests need them."""

    def __init__(self, covers_by_album: dict[str, bytes], fail: bool = False):
        self.covers, self.fail = covers_by_album, fail
        self.searches: list[tuple[str, str]] = []

    def search(self, artist, album):
        self.searches.append((artist, album))
        if self.fail:
            raise coverart.CoverArtError("network is down")
        return [
            {"id": f"mbid-{album}", "score": 100, "title": f"{album} (Remastered)"},
            {"id": "other", "score": 95, "title": "Something Else"},
        ]

    def front(self, mbid, min_pixels):
        data = self.covers.get(mbid.removeprefix("mbid-"))
        return (data, f"https://example.org/{mbid}.jpg") if data else None


@pytest.fixture
def library(Session, copy_audio, tmp_path):
    root = tmp_path / "music"
    copy_audio("mp3", root / "Texas/Greatest Hits/11 Halo.mp3")
    thumbnail = copy_audio("mp3b", root / "Texas/Greatest Hits/12 Insane.mp3")
    tags.write(thumbnail, {"album": "Greatest Hits", "tracknumber": "12"})
    covers.embed(thumbnail, _image(tmp_path / "thumb.jpg", "75x75", "blue"))
    copy_audio("m4a", root / "P!nk/Funhouse/01 So What.m4a")
    session = Session()
    lib = Library(name="music", root=str(root))
    session.add(lib)
    session.commit()
    scan(session, lib)
    yield session, lib, root, tmp_path
    session.close()


def _cover_value(session, path):
    return session.scalar(
        select(FieldValue.value)
        .join(AudioFile)
        .where(AudioFile.path == path, FieldValue.field == tags.EMBEDDED_COVER)
    )


def test_scan_reads_embedded_cover_size(library):
    session, lib, root, _ = library
    assert _cover_value(session, "Texas/Greatest Hits/12 Insane.mp3") == "75x75"
    assert _cover_value(session, "Texas/Greatest Hits/11 Halo.mp3") is None


def test_min_pixels_decides_what_is_a_thumbnail(library):
    session, lib, root, tmp_path = library
    _image(root / "Texas/Greatest Hits/cover.jpg", "320x240")

    [folder] = covers.survey(session, lib, path_prefix="Texas/").folders
    assert len(folder.files) == 2  # one without a cover, one with a thumbnail
    assert [session.get(AudioFile, i).path for i in folder.replace] == [
        "Texas/Greatest Hits/12 Insane.mp3"
    ]
    found = covers.survey(session, lib, path_prefix="Texas/", min_pixels=300)
    assert found.folders == [] and found.too_small[0].dimensions == (320, 240)
    found = covers.survey(session, lib, path_prefix="Texas/", min_pixels=50)
    [folder] = found.folders
    assert [f.path for f in folder.files] == ["Texas/Greatest Hits/11 Halo.mp3"]


def test_replace_thumbnail_and_undo(library):
    session, lib, root, tmp_path = library
    image = _image(root / "Texas/Greatest Hits/cover.jpg", "320x240")
    thumbnail = covers.embedded(root / "Texas/Greatest Hits/12 Insane.mp3")

    found = covers.survey(session, lib, path_prefix="Texas/")
    changeset = covers.propose(session, lib, found.folders, "Embed")
    assert any("replace the cover of" in ch.describe(c) for c in changeset.changes)
    ch.set_status(changeset.changes, "approved")
    assert all(o.ok for o in ch.apply(session, changeset))
    for name in ("11 Halo.mp3", "12 Insane.mp3"):
        assert covers.embedded(root / "Texas/Greatest Hits" / name) == (
            "image/jpeg", image.read_bytes(),
        )  # fmt: skip
        assert _cover_value(session, f"Texas/Greatest Hits/{name}") == "320x240"
    assert covers.survey(session, lib, path_prefix="Texas/").folders == []

    assert all(o.ok for o in ch.undo(session, changeset))
    assert covers.embedded(root / "Texas/Greatest Hits/12 Insane.mp3") == thumbnail
    assert covers.embedded(root / "Texas/Greatest Hits/11 Halo.mp3") is None


@pytest.mark.parametrize("name", ["mp3", "m4a", "aac"])
def test_replace_keeps_other_pictures(copy_audio, tmp_path, name):
    path = copy_audio(name, tmp_path / f"x.{name}")
    old = _image(tmp_path / "old.jpg", "60x60").read_bytes()
    new = _image(tmp_path / "new.png", "300x300").read_bytes()
    covers.embed_data(path, "image/jpeg", old)
    removed = covers.embed_data(path, "image/png", new, replace=True)
    assert [p.data for p in removed] == [old]
    assert covers.embedded(path) == ("image/png", new)
    covers.unembed(path, covers.digest(new), removed)
    assert covers.embedded(path) == ("image/jpeg", old)


def test_unsupported_format_fails_clearly(tmp_path):
    path = tmp_path / "x.mp3"
    path.write_bytes(b"not audio")
    with pytest.raises(tags.UnsupportedFormat, match="Not a readable audio file"):
        covers.embed_data(path, "image/jpeg", b"\xff\xd8")
    ogg = tmp_path / "x.ogg"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=duration=1",
                    "-c:a", "flac", str(ogg)], check=True)  # fmt: skip
    with pytest.raises(tags.UnsupportedFormat, match="can only be embedded"):
        covers.embed_data(ogg, "image/jpeg", b"\xff\xd8")


def test_matching_titles():
    groups = [
        {"id": "a", "score": 100, "title": "Funhouse (Deluxe Edition)"},
        {"id": "b", "score": 100, "title": "Funhouse Tour: Live in Australia"},
        {"id": "c", "score": 85, "title": "Funhouse"},
    ]
    assert coverart.candidates(groups, "Funhouse") == ["a"]
    assert coverart.candidates(groups, "Funhouse CD1") == ["a"]
    assert coverart._quote('Say "Hi" \\ Bye') == '"Say \\"Hi\\" \\\\ Bye"'
    assert "audio-organizer/" in coverart.user_agent()


def test_lookup_and_embed_found_covers(library):
    session, lib, root, tmp_path = library
    jpeg = _image(tmp_path / "found.jpg", "500x500").read_bytes()
    client = FakeClient({"Greatest Hits": jpeg})

    stats = coverart.lookup_library(session, lib, client=client)
    assert (stats.albums, stats.found, stats.not_found) == (2, 1, 1)
    assert sorted(client.searches) == [("P!nk", "Funhouse"), ("Texas", "Greatest Hits")]
    lookup = session.get(CoverLookup, covers.album_key("Texas", "Greatest Hits"))
    assert (lookup.status, lookup.release_group) == ("found", "mbid-Greatest Hits")
    assert (lookup.image.width, lookup.image.height) == (500, 500)

    # Each album is looked up once; a larger size asks again for the found ones
    assert coverart.lookup_library(session, lib, client=client).albums == 0
    assert coverart.lookup_library(session, lib, client=client, min_pixels=600).albums == 1

    [folder] = covers.survey(session, lib).folders
    assert folder.fetched.sha1 == covers.digest(jpeg) and folder.album == "Texas – Greatest Hits"
    changeset = covers.propose(session, lib, [folder], "Embed")
    assert "Cover Art Archive's image of Texas – Greatest Hits (500x500) as" in ch.describe(
        changeset.changes[0]
    )
    ch.set_status(changeset.changes, "approved")
    assert all(o.ok for o in ch.apply(session, changeset))
    for name in ("11 Halo.mp3", "12 Insane.mp3"):
        assert covers.embedded(root / "Texas/Greatest Hits" / name) == ("image/jpeg", jpeg)


def test_lookups_stop_when_the_network_is_down(library):
    session, lib, root, _ = library
    stats = coverart.lookup_library(session, lib, client=FakeClient({}, fail=True))
    assert len(stats.errors) == 2 and stats.stopped is None  # only two albums
    assert session.scalars(select(CoverLookup)).all() == []  # asked again next time


def test_refresh_sizes_of_older_catalogs(library):
    session, lib, root, _ = library
    session.execute(
        update(FieldValue).where(FieldValue.field == tags.EMBEDDED_COVER).values(value="1")
    )
    session.commit()
    assert covers.survey(session, lib, path_prefix="Texas/").missing[0].replace == set()
    assert covers.refresh_sizes(session, lib) == 1
    assert _cover_value(session, "Texas/Greatest Hits/12 Insane.mp3") == "75x75"
    assert covers.refresh_sizes(session, lib) == 0


def test_cli_min_pixels(library, monkeypatch):
    session, lib, root, tmp_path = library
    _image(root / "Texas/Greatest Hits/cover.jpg", "320x240")
    db = str(session.get_bind().url)
    result = CliRunner().invoke(app, ["--db", db, "covers", "--min-pixels", "300"])
    assert result.exit_code == 0, result.output
    assert "1 more have only a thumbnail (under 300 pixels)" in result.output
    result = CliRunner().invoke(app, ["--db", db, "covers"])
    assert "Texas/Greatest Hits/: cover.jpg (320×240" in result.output
    assert "replacing 1 thumbnails" in result.output

    jpeg = _image(tmp_path / "found.jpg", "500x500").read_bytes()
    monkeypatch.setattr(coverart, "Client", lambda: FakeClient({"Funhouse": jpeg}))
    monkeypatch.setattr("audio_organizer.loudness.measure_library", _no_measuring)
    result = CliRunner().invoke(app, ["--db", db, "scan", "--deep", "--min-pixels", "300"])
    assert "Covers" not in result.output  # a deep scan alone does not ask online
    result = CliRunner().invoke(app, ["--db", db, "scan", "--with-covers", "--min-pixels", "300"])
    assert result.exit_code == 0, result.output  # and --with-covers needs no --deep
    assert "Covers: 1 found, 1 not found, 0 errors, of 2 albums" in result.output
    assert "Measuring" not in result.output


def test_lookup_one_folder_again(library):
    session, lib, root, tmp_path = library
    jpeg = _image(tmp_path / "found.jpg", "500x500").read_bytes()
    coverart.lookup_library(session, lib, client=FakeClient({}))  # nothing found for either
    client = FakeClient({"Funhouse": jpeg})

    [(album, found)] = coverart.lookup_folder(
        session, lib, str(root) + "/P!nk/Funhouse/", client=client
    )  # absolute, and looked up before
    assert album.album == "Funhouse" and found.release_group == "mbid-Funhouse"
    assert client.searches == [("P!nk", "Funhouse")]
    assert coverart.lookup_folder(session, lib, "P!nk/Funhouse", client=client) == []  # has one
    assert coverart.lookup_folder(session, lib, "Texas", client=client) == []  # no album there
    with pytest.raises(coverart.CoverArtError, match="not inside"):
        coverart.lookup_folder(session, lib, "/elsewhere/Texas", client=client)


def test_cli_coverart(library, monkeypatch):
    session, lib, root, tmp_path = library
    db = str(session.get_bind().url)
    jpeg = _image(tmp_path / "found.jpg", "500x500").read_bytes()
    monkeypatch.setattr(coverart, "Client", lambda: FakeClient({"Funhouse": jpeg}))
    result = CliRunner().invoke(app, ["--db", db, "coverart", "--path", "P!nk/Funhouse"])
    assert result.exit_code == 0, result.output
    assert "P!nk – Funhouse: found a cover (mbid-Funhouse)" in result.output
    result = CliRunner().invoke(app, ["--db", db, "coverart", "--path", "Texas"])
    assert result.exit_code == 1 and "no album there needs a cover" in result.output
    result = CliRunner().invoke(app, ["--db", db, "coverart", "--limit", "1"])
    assert "Covers: 0 found, 1 not found" in result.output  # the other album, Greatest Hits


def _no_measuring(session, lib, **kwargs):
    from audio_organizer.loudness import DeepScanStats

    return DeepScanStats()


def test_image_sizes():
    assert images.parse_size("500x400") == (500, 400)
    assert images.parse_size("?") is None and images.parse_size("1") is None
    assert images.format_size(None) == "?"


def test_survey_leaves_out_tracks_whose_format_cannot_hold_a_cover(library):
    session, lib, root, _ = library
    _image(root / "Texas/Greatest Hits/cover.jpg", "320x240")
    halo = session.scalar(select(AudioFile).where(AudioFile.path.like("%Halo.mp3")))
    halo.format = "oggvorbis"  # as a scan of an Ogg file would record
    session.commit()

    [folder] = covers.survey(session, lib, path_prefix="Texas/").folders
    assert [session.get(AudioFile, i).path for i in folder.replace] == [
        "Texas/Greatest Hits/12 Insane.mp3"
    ]
    assert halo.id not in [f.id for f in folder.files]
