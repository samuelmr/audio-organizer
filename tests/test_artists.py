import shutil

import pytest
from conftest import make_audio

from audio_organizer import artists as art
from audio_organizer import layout as lo
from audio_organizer.models import Library
from audio_organizer.scanner import scan


@pytest.fixture
def library(Session, tmp_path):
    if shutil.which("ffmpeg") is None:
        pytest.skip("needs ffmpeg")
    root = tmp_path / "music"
    make_audio(root / "Tenkanen, Aarne/Aarne Tenkanen_Paula.mp3", freq=400, seconds=0.3,
               artist="Aarne Tenkanen", album="Aarne Tenkanen", title="Paula",
               track="5")  # fmt: skip
    make_audio(root / "Pink Floyd/The Division Bell/01 Cluster One.mp3", freq=500, seconds=0.3,
               artist="Pink Floyd", album="The Division Bell", title="Cluster One",
               track="1")  # fmt: skip
    make_audio(root / "Various Artists/Hits/Halo.mp3", freq=600, seconds=0.3, artist="Texas",
               album="Hits", title="Halo", track="1")  # fmt: skip
    session = Session()
    lib = Library(name="music", root=str(root))
    session.add(lib)
    session.commit()
    scan(session, lib)
    yield session, lib
    session.close()


def test_suggestions_and_sort_names(library):
    session, lib = library
    listed = {a.name: a for a in art.album_artists(session, lib)}
    assert set(listed) == {"Aarne Tenkanen", "Pink Floyd"}  # compilations are left out
    assert listed["Aarne Tenkanen"].suggestion == "Tenkanen, Aarne"
    assert listed["Pink Floyd"].suggestion is None  # not turned around by rule

    assert art.accept_suggestions(session, lib, list(listed.values())) == 1
    art.set_sort_name(session, lib, "Pink Floyd", "Floyd, Pink")
    session.commit()
    names = art.sort_names(session, lib)
    assert art.sorted_name(names, "aarne tenkanen") == "Tenkanen, Aarne"
    assert art.sorted_name(names, "Air") == "Air"

    art.set_sort_name(session, lib, "Pink Floyd", "")  # removes it
    session.commit()
    listed = {a.name: a for a in art.album_artists(session, lib)}
    assert listed["Pink Floyd"].sort_name is None
    assert listed["Aarne Tenkanen"].suggestion is None  # it has a sort name now


def test_layout_with_sort_names(library):
    session, lib = library
    art.set_sort_name(session, lib, "Aarne Tenkanen", "Tenkanen, Aarne")
    session.commit()
    plan = lo.plan(session, lib, "{albumartistsort}/{album}/{track} {title}")
    targets = sorted(m.target for a in plan.albums for m in a.moves)
    assert targets == [
        "Tenkanen, Aarne/Aarne Tenkanen/05 Paula.mp3",
        "Various Artists/Hits/01 Halo.mp3",
    ]
    assert plan.in_place == 1  # Pink Floyd, no sort name: filed under the name
