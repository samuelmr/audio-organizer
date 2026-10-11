"""ReplayGain 2.0: how much to turn a track (or album) up or down so that everything
plays at an even loudness of -18 LUFS, measured with ffmpeg's EBU R128 meter.

Each track is decoded once. Its integrated loudness and true peak are stored as
"analysis" values (the loudness in "loudness_lufs", which is not a tag), from which:

    track gain = -18 LUFS - track loudness
    album gain = -18 LUFS - album loudness, where the album loudness is the energy
                 average of its tracks' loudness weighted by duration
    album peak = the highest track peak

The album loudness is an approximation of measuring the whole album at once (usually
within a few tenths of a dB), which saves decoding every album a second time. Values
are only written into files through changesets, like any other tag value. A file's
measurement stays valid as long as its audio does: the scanner drops it when the audio
hash changes.

A file that cannot be measured (DRM-protected, corrupt) is remembered with the reason in
"loudness_error", the same way, so that it is not tried again on every deep scan.
"""

import math
import re
import shutil
import subprocess
from collections import defaultdict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import audiohash
from .models import AudioFile, EffectiveMetadata, FieldValue, Library
from .pathparse import normalize_name
from .provenance import set_value
from .tracks import present

REFERENCE = -18.0  # LUFS, ReplayGain 2.0
LOUDNESS = "loudness_lufs"
FAILED = "loudness_error"
_INTEGRATED = re.compile(r"I:\s+(-?[\d.]+|-inf) LUFS")
_PEAK = re.compile(r"Peak:\s+(-?[\d.]+|-inf) dBFS")


class LoudnessError(Exception):
    def __init__(self, message: str, reason: str | None = None):
        super().__init__(message)
        self.reason = reason or message  # without the file name, kept for the file


def _skip_tag(path: str | Path) -> list[str]:
    """ffmpeg options to start after the ID3 tag when a WAV (RIFF) header follows it.

    Some old MP3s are an MP3 stream in a WAV container with an ID3 tag in front. ffmpeg
    recognises the container behind the tag but then reads it from the file's start and
    fails, so we point it at the container."""
    try:
        with open(path, "rb") as f:
            start, _ = audiohash.id3_stream_range(f)
            f.seek(start)
            riff = f.read(4) == b"RIFF"
    except OSError:
        return []
    return ["-skip_initial_bytes", str(start)] if start and riff else []


def measure(path: str | Path, label: str | None = None) -> dict[str, str]:
    """Loudness, ReplayGain track gain and peak of a file, as stored values
    ("-14.30", "-3.70 dB", "0.977237"). Reads the whole file. Errors name the file by
    label (default: its file name; a library-relative path tells same-named tracks apart)."""
    label = label or Path(path).name
    if shutil.which("ffmpeg") is None:
        raise LoudnessError("ffmpeg is needed to measure loudness")
    result = subprocess.run(
        ["ffmpeg", "-nostats", "-hide_banner", *_skip_tag(path), "-i", str(path), "-vn",
         "-af", "ebur128=peak=true", "-f", "null", "-"],
        capture_output=True, text=True, errors="replace",
    )  # fmt: skip
    summary = result.stderr.rpartition("Summary:")[2]
    integrated, peak = _INTEGRATED.search(summary), _PEAK.search(summary)
    if result.returncode != 0 or not integrated or not peak:
        if "DRM protected" in result.stderr:
            reason = "DRM-protected, cannot be decoded"
        else:
            reason = (result.stderr.strip().splitlines()[-1:] or ["no output"])[0]
        raise LoudnessError(f"Could not measure {label}: {reason}", reason)
    if integrated.group(1) == "-inf":
        raise LoudnessError(f"{label} is silent", "silent")
    lufs = float(integrated.group(1))
    peak_linear = 0.0 if peak.group(1) == "-inf" else 10 ** (float(peak.group(1)) / 20)
    return {
        LOUDNESS: f"{lufs:.2f}",
        "replaygain_track_gain": _gain(REFERENCE - lufs),
        "replaygain_track_peak": f"{peak_linear:.6f}",
    }


def measure_file(session: Session, file: AudioFile) -> dict[str, str]:
    """Measure a catalog file, store the result as analysis values, and update its
    album's gain when all of the album's tracks have been measured."""
    values = measure(Path(file.library.root) / file.path)
    for name, value in values.items():
        set_value(session, file, "analysis", name, value)
    set_value(session, file, "analysis", FAILED, None)
    update_albums(session, file.library, [file.id])
    return values


@dataclass
class DeepScanStats:
    measured: int = 0
    albums: int = 0
    errors: list[tuple[str, str]] = field(default_factory=list)


def unmeasured(
    session: Session, library: Library, *, retry_failed: bool = False
) -> list[AudioFile]:
    """Files without a measurement; those that failed before only with retry_failed."""
    fields = (LOUDNESS,) if retry_failed else (LOUDNESS, FAILED)
    measured = select(FieldValue.file_id).where(
        FieldValue.source == "analysis", FieldValue.field.in_(fields)
    )
    return list(
        session.scalars(
            select(AudioFile)
            .where(present(library.id), AudioFile.id.not_in(measured))
            .order_by(AudioFile.path)
        )
    )


def failed(session: Session, library: Library) -> list[tuple[AudioFile, str]]:
    """Files that could not be measured, with the reason, by path."""
    return [
        (file, reason)
        for file, reason in session.execute(
            select(AudioFile, FieldValue.value)
            .join(FieldValue, FieldValue.file_id == AudioFile.id)
            .where(present(library.id), FieldValue.source == "analysis", FieldValue.field == FAILED)
            .order_by(AudioFile.path)
        )
    ]


def measure_library(
    session: Session,
    library: Library,
    *,
    workers: int = 4,
    limit: int | None = None,
    retry_failed: bool = False,
    progress: Callable[[int, int, str, str | None], None] | None = None,
) -> DeepScanStats:
    """Measure the files not measured yet (all of them, the first time), in parallel,
    committing as it goes so an interrupted run continues where it stopped; then the
    album gains. Files that failed before are skipped unless retry_failed."""
    stats = DeepScanStats()
    todo = unmeasured(session, library, retry_failed=retry_failed)[:limit]
    root = Path(library.root)
    done = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(measure, root / f.path, f.path): f for f in todo}
        for count, future in enumerate(as_completed(futures), start=1):
            file = futures[future]
            error = None
            try:
                for name, value in future.result().items():
                    set_value(session, file, "analysis", name, value)
                if retry_failed:
                    set_value(session, file, "analysis", FAILED, None)
                done.append(file.id)
                stats.measured += 1
            except LoudnessError as e:
                error = str(e)
                stats.errors.append((file.path, error))
                set_value(session, file, "analysis", FAILED, e.reason)
            if count % 50 == 0:
                session.commit()
            if progress:
                progress(count, len(todo), file.path, error)
    session.commit()
    stats.albums = update_albums(session, library, done)
    session.commit()
    return stats


def update_albums(session: Session, library: Library, file_ids: list[int]) -> int:
    """Set the album gain and peak of the albums of these files whose tracks have all
    been measured. An album is the files of one folder with the same album tag."""
    if not file_ids:
        return 0
    rows = session.execute(
        select(AudioFile.id, AudioFile.path, AudioFile.duration, EffectiveMetadata.album)
        .join(EffectiveMetadata, EffectiveMetadata.file_id == AudioFile.id)
        .where(present(library.id), EffectiveMetadata.album.is_not(None))
    ).all()
    albums: dict[tuple[str, str], list] = defaultdict(list)
    key_of = {}
    for file_id, path, duration, album in rows:
        key = (str(PurePosixPath(path).parent), normalize_name(album))
        albums[key].append((file_id, duration))
        key_of[file_id] = key
    wanted = {key_of[i] for i in file_ids if i in key_of}
    member_ids = [file_id for key in wanted for file_id, _ in albums[key]]
    values: dict[int, dict[str, str]] = defaultdict(dict)
    for file_id, name, value in session.execute(
        select(FieldValue.file_id, FieldValue.field, FieldValue.value).where(
            FieldValue.file_id.in_(member_ids),
            FieldValue.source == "analysis",
            FieldValue.field.in_((LOUDNESS, "replaygain_track_peak")),
        )
    ):
        values[file_id][name] = value
    updated = 0
    for key in wanted:
        members = albums[key]
        if any(LOUDNESS not in values[file_id] for file_id, _ in members):
            continue  # not all tracks measured yet
        energy = sum(
            (duration or 1) * 10 ** (float(values[file_id][LOUDNESS]) / 10)
            for file_id, duration in members
        )
        total = sum(duration or 1 for _, duration in members)
        album_lufs = 10 * math.log10(energy / total)
        peak = max(float(values[file_id]["replaygain_track_peak"]) for file_id, _ in members)
        for file_id, _ in members:
            file = session.get(AudioFile, file_id)
            set_value(
                session, file, "analysis", "replaygain_album_gain", _gain(REFERENCE - album_lufs)
            )
            set_value(session, file, "analysis", "replaygain_album_peak", f"{peak:.6f}")
        updated += 1
    return updated


def _gain(db: float) -> str:
    return f"{db:+.2f} dB"
