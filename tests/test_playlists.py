import json
import os

import pytest
from sqlalchemy import select

from audio_organizer import playlists as pl
from audio_organizer import tags
from audio_organizer.models import AudioFile, Library, Playlist
from audio_organizer.playlist_formats import iso_duration, to_m3u
from audio_organizer.scanner import scan
from audio_organizer.tracks import Track


def track(path="a/b.mp3", **fields) -> Track:
    return Track(
        file_id=1, path=path, size=1, mtime_ns=1, duration=200.4, format="mp3",
        bitrate=192000, audio_hash=None, first_seen=None, fields=fields,
    )  # fmt: skip


def matches(rule: str, t: Track) -> bool:
    return all(c.matches(t) for c in pl.parse_rule(rule))


def test_rule_operators():
    t = track(genre="Rock; Pop", preference="Very Good", date="1994-05-01", title="Halo (Live)")
    assert matches("genre=pop", t)
    assert matches('preference="Very Good|Excellent"', t)
    assert not matches("preference=Excellent", t)
    assert matches("genre!=Jazz", t)
    assert matches("title~live", t)
    assert not matches("title!~LIVE", t)
    assert matches("year>=1990 year<2000", t)
    assert not matches("year>1994", t)
    assert matches("album=", t)  # missing
    assert not matches("album!=", t)
    assert matches("duration>200 bitrate>=192", t)
    assert matches("path~a/b", t)
    assert matches("title=halo (live) preference=very good", t)  # values with spaces


@pytest.mark.parametrize(
    ("rule", "message"),
    [
        ("", "empty"),
        ("colour=red", "Unknown field"),
        ("genre", "Not a condition"),
        ("year>=nineties", "needs a number"),
        ('title="unclosed', "Cannot parse"),
    ],
)
def test_rule_errors(rule, message):
    with pytest.raises(pl.RuleError, match=message):
        pl.parse_rule(rule)


def test_grouped_name_needs_placeholder():
    playlist = Playlist(name="Best Of", rule="genre~", group_by="mood", sort="shuffle")
    with pytest.raises(pl.RuleError, match="must contain"):
        pl.validate(playlist)
    playlist.name = "Best Of {mood}"
    pl.validate(playlist)


def test_stable_shuffle_and_album_order():
    tracks = [
        track(f"x/{i}.mp3", album="A" if i % 2 else "B", tracknumber=str(10 - i)) for i in range(10)
    ]
    once = pl.order(tracks, "shuffle", seed="mix")
    assert [t.path for t in once] == [t.path for t in pl.order(tracks[::-1], "shuffle", seed="mix")]
    assert [t.path for t in once] != [t.path for t in pl.order(tracks, "shuffle", seed="other")]
    # A new track does not reorder the others
    more = pl.order([*tracks, track("x/new.mp3")], "shuffle", seed="mix")
    assert [t.path for t in more if t.path != "x/new.mp3"] == [t.path for t in once]
    soundtrack = [
        track("s/2.mp3", album="OST", artist="Zed", tracknumber="1"),
        track("s/1.mp3", album="OST", artist="Abe", tracknumber="2"),
    ]
    assert [t.path for t in pl.order(soundtrack, "album")] == ["s/2.mp3", "s/1.mp3"]
    by_album = pl.order(tracks, "album")
    assert [(t.get("album"), t.get("tracknumber")) for t in by_album][:3] == [
        ("A", "1"), ("A", "3"), ("A", "5"),
    ]  # fmt: skip


# ---- with a catalog -------------------------------------------------------------------

SONGS = {
    "Texas/Greatest Hits/11 Halo.mp3": dict(
        artist="Texas", title="Halo", album="Greatest Hits", mood="Upbeat",
        preference="Excellent", genre="Pop",
    ),
    "Texas/Texas_Halo.mp3": dict(  # the same song again, a worse copy
        artist="Texas", title="Halo", mood="Upbeat", preference="Excellent", genre="Pop",
    ),
    "Air/Moon Safari/01 La Femme d'Argent.mp3": dict(
        artist="Air", title="La Femme d'Argent", album="Moon Safari", mood="Mellow",
        preference="Very Good", genre="Electronic",
    ),
    "Air/Moon Safari/02 Sexy Boy.mp3": dict(
        artist="Air", title="Sexy Boy", album="Moon Safari", mood="Upbeat",
        preference="Good", genre="Electronic",
    ),
}  # fmt: skip


@pytest.fixture
def catalog(Session, copy_audio, tmp_path):
    root = tmp_path / "music"
    for i, (path, fields) in enumerate(SONGS.items()):
        file = copy_audio("mp3" if i % 2 == 0 else "mp3b", root / path)
        tags.write(file, {name: fields.get(name) for name in ("artist", "title", "album",
                   "mood", "preference", "genre", "tracknumber", "date")})  # fmt: skip
    session = Session()
    library = Library(name="music", root=str(root))
    session.add(library)
    session.commit()
    scan(session, library)
    yield session, library, root
    session.close()


def _names(generated):
    return {g.name: sorted(t.path for t in g.tracks) for g in generated}


def test_best_of_grouped_by_mood(catalog):
    session, library, root = catalog
    session.add(Playlist(library=library, name="Best Of {mood}", group_by="mood",
                         rule='preference="Very Good|Excellent"', sort="shuffle"))  # fmt: skip
    session.commit()
    assert _names(pl.generate(session, library)) == {
        "Best Of Upbeat": ["Texas/Greatest Hits/11 Halo.mp3"],  # one Halo, the better copy
        "Best Of Mellow": ["Air/Moon Safari/01 La Femme d'Argent.mp3"],
    }


def test_max_tracks_and_empty_playlist(catalog):
    session, library, root = catalog
    session.add_all([
        Playlist(library=library, name="Air", rule="artist=air", sort="album", max_tracks=1),
        Playlist(library=library, name="Jazz", rule="genre=jazz", sort="shuffle"),
    ])  # fmt: skip
    session.commit()
    assert _names(pl.generate(session, library)) == {
        "Air": ["Air/Moon Safari/01 La Femme d'Argent.mp3"],
        "Jazz": [],
    }


def test_import_m3u_follows_moves(catalog, tmp_path):
    session, library, root = catalog
    m3u = root / "lists" / "Mix.m3u"
    m3u.parent.mkdir()
    m3u.write_text(
        "#EXTM3U\n"
        "Air/Moon Safari/02 Sexy Boy.mp3\n"  # relative to the library
        "../Texas/Texas_Halo.mp3\n"  # relative to the playlist
        f"{root}/Air/Moon Safari/01 La Femme d'Argent.mp3\n"  # absolute
        "Nobody/Nothing.mp3\n",
        encoding="utf-8",
    )
    playlist, unmatched = pl.import_m3u(session, library, m3u)
    session.commit()
    assert unmatched == ["Nobody/Nothing.mp3"]
    [generated] = pl.generate(session, library)
    assert [t.path for t in generated.tracks] == [
        "Air/Moon Safari/02 Sexy Boy.mp3",
        "Texas/Texas_Halo.mp3",
        "Air/Moon Safari/01 La Femme d'Argent.mp3",
    ]
    assert generated.unresolved == 1

    os.rename(root / "Texas/Texas_Halo.mp3", root / "Texas/Halo (copy).mp3")
    scan(session, library)
    [generated] = pl.generate(session, library)
    assert generated.tracks[1].path == "Texas/Halo (copy).mp3"

    with pytest.raises(pl.RuleError, match="already"):
        pl.import_m3u(session, library, m3u)


def test_import_expands_nested_playlists(catalog):
    session, library, root = catalog
    (root / "Air/Moon Safari/Air - Moon Safari.m3u").write_text(
        "01 La Femme d'Argent.mp3\n02 Sexy Boy.mp3\n../../Likes.m3u\n"  # with a cycle
    )
    likes = root / "Likes.m3u"
    likes.write_text("Texas/Texas_Halo.mp3\nAir/Moon Safari/Air - Moon Safari.m3u\n")
    playlist, unmatched = pl.import_m3u(session, library, likes)
    assert [e.file_id is not None for e in playlist.entries] == [True, True, True, False]
    assert unmatched == ["../../Likes.m3u"]


def test_import_matches_paths_loosely(catalog, copy_audio):
    session, library, root = catalog
    index = pl.PathIndex(session, library)
    base = root
    found = index.find("The Air/Moon Safari/02 sexy BOY!.mp3", base)
    assert found == index.exact["air/moon safari/02 sexy boy.mp3"]
    assert index.find("Air/Moon Safari/03 Talisman.mp3", base) is None

    copy_audio("mp3b", root / "Air/Moon Safari/02 Sexy Boy!.mp3")  # now two files fit
    scan(session, library)
    index = pl.PathIndex(session, library)
    assert index.find("The Air/Moon Safari/02 Sexy Boy.mp3", base) is None
    assert index.find("Air/Moon Safari/02 Sexy Boy.mp3", base) is not None  # exact still works


def test_show_a_generated_playlist(catalog, tmp_path):
    from typer.testing import CliRunner

    from audio_organizer.cli import app

    session, library, root = catalog
    session.add(Playlist(library=library, name="Best Of {mood}", group_by="mood",
                         rule="preference~good", sort="album"))  # fmt: skip
    session.commit()
    db = ["--db", str(tmp_path / "catalog.db")]
    result = CliRunner().invoke(app, [*db, "playlists", "show", "Best Of Mellow"])
    assert result.exit_code == 0, result.output
    assert result.output.splitlines()[0] == "Best Of Mellow: 1 tracks"
    result = CliRunner().invoke(app, [*db, "playlists", "show", "Best Of Nothing"])
    assert result.exit_code != 0


def test_export(catalog, tmp_path):
    session, library, root = catalog
    session.add(Playlist(library=library, name="Air/Moon", rule="album=moon safari",
                         sort="album"))  # fmt: skip
    session.commit()
    out = tmp_path / "out"
    written = pl.export(pl.generate(session, library), root, out, ["m3u", "json", "html"])
    assert sorted(p.name for p in written) == ["Air-Moon.html", "Air-Moon.json", "Air-Moon.m3u"]
    m3u = (out / "Air-Moon.m3u").read_text().splitlines()
    assert m3u[0] == "#EXTM3U"
    assert m3u[2].startswith("#EXTINF:1,Air - La Femme d'Argent")
    assert m3u[3] == "../music/Air/Moon Safari/01 La Femme d'Argent.mp3"
    data = json.loads((out / "Air-Moon.json").read_text())
    assert data["@type"] == "MusicPlaylist" and data["numTracks"] == 2
    assert data["track"][1]["name"] == "Sexy Boy"
    assert data["track"][1]["inAlbum"] == "Moon Safari"
    page = (out / "Air-Moon.html").read_text()
    assert 'href="../music/Air/Moon%20Safari/02%20Sexy%20Boy.mp3"' in page


def test_formats_details():
    assert iso_duration(278) == "PT4M38S"
    assert iso_duration(3661) == "PT1H1M1S"
    assert iso_duration(None) is None
    text = to_m3u("x", [(track(title="T"), "a/b.mp3")])
    assert text.splitlines()[2] == "#EXTINF:200,T"


def test_static_playlist_keeps_entry_of_deleted_file(catalog):
    session, library, root = catalog
    file = session.scalar(select(AudioFile).where(AudioFile.path.like("Air/%02%")))
    playlist = Playlist(library=library, name="Keep", sort="manual")
    pl.set_entries(playlist, [(file.path, file.id)])
    session.add(playlist)
    session.commit()
    (root / file.path).unlink()
    scan(session, library)
    [generated] = pl.generate(session, library)
    assert generated.tracks == [] and generated.unresolved == 1


def test_generated_shuffle_changes_between_runs():
    playlist = Playlist(name="Mix", rule="format=mp3", sort="shuffle")
    tracks = [track(f"x/{i}.mp3", title=f"T{i}") for i in range(30)]
    orders = set()
    for _ in range(3):
        playlists = pl.PlaylistSet([playlist])
        for t in tracks:
            playlists.feed(t)
        orders.add(tuple(t.path for t in playlists.results()[0].tracks))
    assert len(orders) > 1
