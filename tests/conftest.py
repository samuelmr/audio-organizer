import shutil
import subprocess
from pathlib import Path

import pytest

from audio_organizer.db import make_sessionmaker

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")


def make_audio(path: Path, *, freq: int = 440, seconds: float = 1.0, **tags: str) -> Path:
    """Generate a short sine tone file with the given tags; format follows the suffix."""
    path.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
           "-i", f"sine=frequency={freq}:duration={seconds}"]  # fmt: skip
    if path.suffix == ".mp3":
        cmd += ["-c:a", "libmp3lame", "-b:a", "64k", "-id3v2_version", "3"]
    else:
        cmd += ["-c:a", "aac", "-b:a", "64k"]
    for key, value in tags.items():
        cmd += ["-metadata", f"{key}={value}"]
    subprocess.run([*cmd, str(path)], check=True)
    return path


@pytest.fixture(scope="session")
def audio_templates(tmp_path_factory) -> dict[str, Path]:
    """Generated once per session; tests copy them instead of re-encoding."""
    if shutil.which("ffmpeg") is None:
        pytest.skip("needs ffmpeg")
    base = tmp_path_factory.mktemp("templates")
    return {
        "mp3": make_audio(base / "a.mp3", artist="Texas", album="Greatest Hits", title="Halo",
                          track="11/16", date="2000", genre="Pop"),
        "mp3b": make_audio(base / "b.mp3", freq=660, artist="Texas", title="Insane"),
        "m4a": make_audio(base / "a.m4a", artist="P!nk", album="Funhouse", title="So What",
                          track="1"),
    }  # fmt: skip


@pytest.fixture
def copy_audio(audio_templates):
    def copy(name: str, target: Path) -> Path:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(audio_templates[name], target)
        return target

    return copy


@pytest.fixture
def Session(tmp_path):
    return make_sessionmaker(f"sqlite:///{tmp_path / 'catalog.db'}")
