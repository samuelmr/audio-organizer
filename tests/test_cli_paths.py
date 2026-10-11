import unicodedata

import pytest
import typer

from audio_organizer.cli import _find_file
from audio_organizer.models import Library
from audio_organizer.scanner import scan


@pytest.fixture
def library(Session, copy_audio, tmp_path):
    root = tmp_path / "music"
    # As macOS writes it: "ä" as "a" and a combining mark
    name = unicodedata.normalize("NFD", "YUP/Käpälä/07 Käpälämäkeen.mp3")
    copy_audio("mp3", root / name)
    session = Session()
    lib = Library(name="music", root=str(root))
    session.add(lib)
    session.commit()
    scan(session, lib)
    yield session, root
    session.close()


@pytest.mark.parametrize("form", ["NFC", "NFD"])
def test_a_file_is_found_however_its_accents_are_typed(library, form):
    session, root = library
    relative = unicodedata.normalize(form, "YUP/Käpälä/07 Käpälämäkeen.mp3")

    assert unicodedata.normalize("NFC", _find_file(session, relative).path) == (
        "YUP/Käpälä/07 Käpälämäkeen.mp3"
    )
    assert _find_file(session, str(root / relative))


def test_an_unknown_file_is_still_refused(library):
    session, _ = library

    with pytest.raises(typer.BadParameter, match="not in the catalog"):
        _find_file(session, "YUP/Käpälä/08 Other.mp3")
