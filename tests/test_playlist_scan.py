import pytest
from sqlalchemy import select
from typer.testing import CliRunner

from audio_organizer import playlist_store as ps
from audio_organizer import playlists as pl
from audio_organizer.cli import app
from audio_organizer.models import Library, Playlist, PlaylistFile
from audio_organizer.scanner import scan

TRACK = "Texas/Greatest Hits/11 Halo.mp3"


@pytest.fixture
def setup(Session, copy_audio, tmp_path):
    root = tmp_path / "music"
    copy_audio("mp3", root / TRACK)
    session = Session()
    library = Library(name="music", root=str(root))
    session.add(library)
    session.commit()
    yield session, library, root
    session.close()


def _statics(session):
    return {p.name: p for p in session.scalars(select(Playlist)) if not p.is_smart}


def test_playlist_files_in_the_playlist_folder_are_imported_once(setup):
    session, library, root = setup
    (root / "Party.m3u").write_text(f"{TRACK}\nGone/Missing.mp3\n")
    (root / "Texas/Greatest Hits/Texas - Greatest Hits.m3u").write_text(f"{TRACK}\n")

    stats = scan(session, library)

    assert stats.playlists.imported == ["Party"]  # not the album's own playlist
    party = _statics(session)["Party"]
    assert [(e.path, e.file_id is not None) for e in party.entries] == [
        (TRACK, True),
        ("Gone/Missing.mp3", False),
    ]
    again = scan(session, library)
    assert (again.playlists.imported, again.playlists.known) == ([], 1)


def test_another_playlist_folder_is_read_instead_of_the_root(setup):
    session, library, root = setup
    library.playlist_folder = "Lists"
    (root / "Lists").mkdir()
    (root / "Lists/Mix.m3u").write_text(f"../{TRACK}\n")
    (root / "Root.m3u").write_text(f"{TRACK}\n")

    stats = scan(session, library)

    assert stats.playlists.imported == ["Mix"]
    assert _statics(session)["Mix"].entries[0].file_id is not None


def test_files_written_by_the_tool_are_not_imported_but_edited_ones_are(setup):
    session, library, root = setup
    scan(session, library)
    smart = Playlist(library=library, name="Hits", rule="artist=texas", sort="album")
    session.add(smart)
    session.commit()
    place = ps.Place(library)
    ps.apply_write(session, place, ps.plan_playlists(session, place, [smart]))
    session.commit()

    assert scan(session, library).playlists.imported == []

    (root / "Hits.m3u").write_text(f"{TRACK}\n")  # someone edited the written file
    stats = scan(session, library)

    assert stats.playlists.imported == ["Hits"]
    playlists = list(session.scalars(select(Playlist).order_by(Playlist.id)))
    assert [pl.kind(p) for p in playlists] == ["smart", "static"]
    assert pl.shared_names(playlists) == {"Hits"}
    assert [pl.display_name(p, {"Hits"}) for p in playlists] == ["Hits (smart)", "Hits (static)"]
    assert pl.find(session, library, "Hits (smart)") is playlists[0]
    assert pl.find(session, library, "Hits (static)") is playlists[1]
    with pytest.raises(pl.RuleError, match="smart and a static"):
        pl.find(session, library, "Hits")


def test_a_file_that_differs_from_a_static_playlist_is_reported_not_imported(setup):
    session, library, root = setup
    (root / "Party.m3u").write_text(f"{TRACK}\n")
    scan(session, library)
    party = _statics(session)["Party"]
    party.entries = []  # edited in the catalog
    session.commit()
    assert scan(session, library).playlists.conflicts == []  # the file has not changed

    (root / "Party.m3u").write_text(f"{TRACK}\n{TRACK}\n")  # edited on disk, too
    stats = scan(session, library)
    assert stats.playlists.conflicts == ["Party.m3u"]
    assert stats.playlists.imported == []
    assert party.entries == []  # the catalog's playlist is not replaced by the file


def test_an_identical_file_for_an_existing_playlist_is_just_recorded(setup):
    session, library, root = setup
    scan(session, library)
    playlist, _ = pl.import_m3u(session, library, _write(root / "In.m3u", f"{TRACK}\n"))
    session.commit()

    stats = scan(session, library)

    assert (stats.playlists.imported, stats.playlists.conflicts) == ([], [])
    assert session.scalar(select(PlaylistFile.playlist_id)) == playlist.id


def test_a_playlist_deleted_from_the_catalog_is_found_again(setup):
    session, library, root = setup
    (root / "Party.m3u").write_text(f"{TRACK}\n")
    scan(session, library)
    session.delete(_statics(session)["Party"])
    session.commit()

    assert scan(session, library).playlists.imported == ["Party"]


def test_playlists_can_be_left_out_of_a_scan(setup):
    session, library, root = setup
    (root / "Party.m3u").write_text(f"{TRACK}\n")
    assert scan(session, library, import_playlists=False).playlists.imported == []
    assert _statics(session) == {}


def test_commands_name_the_kind_when_a_name_is_shared(setup):
    session, library, root = setup
    scan(session, library)
    session.add_all([
        Playlist(library=library, name="Hits", rule="artist=texas", sort="album"),
        Playlist(library=library, name="Hits", sort="manual"),
    ])  # fmt: skip
    session.commit()
    db = ["--db", str(session.get_bind().url.database)]
    runner = CliRunner()

    listed = runner.invoke(app, [*db, "playlists", "list"]).output
    assert "Hits (smart)\t" in listed and "Hits (static)\t" in listed
    assert runner.invoke(app, [*db, "playlists", "show", "Hits"]).exit_code != 0
    assert runner.invoke(app, [*db, "playlists", "show", "Hits (smart)"]).exit_code == 0
    again = runner.invoke(app, [*db, "playlists", "create", "Hits"])
    assert again.exit_code != 0 and "already a static playlist" in again.output
    ok = runner.invoke(app, [*db, "playlists", "create", "Hits", "--rule", "artist=x"])
    assert ok.exit_code != 0 and "already a smart playlist" in ok.output


def _write(path, text):
    path.write_text(text)
    return path
