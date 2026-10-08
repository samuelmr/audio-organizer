"""ReplayGain: how much to turn a track up or down so that tracks play at an even
loudness, measured with ffmpeg's replaygain filter (ReplayGain 1.0, 89 dB reference).

Measurements are stored as values from the "analysis" source, beside what the file's
tags say; writing them into the file is a changeset like any other tag change. Album
gain needs all of an album's tracks measured together and is left for batches.
"""

import re
import shutil
import subprocess
from pathlib import Path

from sqlalchemy.orm import Session

from .models import AudioFile
from .provenance import set_value

_RESULT = re.compile(r"track_(gain|peak) = ([+-]?[\d.]+)")


class LoudnessError(Exception):
    pass


def measure(path: str | Path) -> dict[str, str]:
    """ReplayGain track gain and peak of a file, as tag values ("-6.48 dB", "0.988525").
    Reads the whole file."""
    if shutil.which("ffmpeg") is None:
        raise LoudnessError("ffmpeg is needed to measure loudness")
    result = subprocess.run(
        ["ffmpeg", "-nostats", "-hide_banner", "-i", str(path), "-vn", "-af", "replaygain",
         "-f", "null", "-"],
        capture_output=True, text=True, errors="replace",
    )  # fmt: skip
    found = dict(_RESULT.findall(result.stderr))
    if result.returncode != 0 or {"gain", "peak"} - found.keys():
        last = result.stderr.strip().splitlines()[-1:] or ["no output"]
        raise LoudnessError(f"Could not measure {Path(path).name}: {last[0]}")
    gain = float(found["gain"])
    return {
        "replaygain_track_gain": f"{gain:+.2f} dB",
        "replaygain_track_peak": f"{float(found['peak']):.6f}",
    }


def measure_file(session: Session, file: AudioFile) -> dict[str, str]:
    """Measure a catalog file and store the result as analysis values."""
    values = measure(Path(file.library.root) / file.path)
    for name, value in values.items():
        set_value(session, file, "analysis", name, value)
    return values
