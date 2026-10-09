"""Streaming over a library's tracks with their resolved metadata.

Resolving every field of every file is the expensive part of working with a large
library, so it is done in batches and the result streamed, for consumers (sync,
playlists) that look at each track once.
"""

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import PurePosixPath

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import AudioFile, FieldValue
from .provenance import resolve

BATCH = 2000


@dataclass
class Track:
    file_id: int
    path: str
    size: int
    mtime_ns: int
    duration: float | None
    format: str | None
    bitrate: int | None
    audio_hash: str | None
    first_seen: datetime | None
    fields: dict[str, str] = field(default_factory=dict)

    def get(self, name: str) -> str | None:
        """A resolved field, or one of the derived ones: year, path, format, duration
        (seconds), bitrate (kbps)."""
        if name in self.fields:
            return self.fields[name]
        if name == "year":
            date = self.fields.get("date", "")
            return date[:4] if date[:4].isdigit() else None
        if name == "path":
            return self.path
        if name == "format":
            return self.format
        if name == "duration":
            return str(round(self.duration, 1)) if self.duration else None
        if name == "bitrate":
            return str(self.bitrate // 1000) if self.bitrate else None
        return None


def relative_folder(library, path: str) -> str:
    """A folder as a path relative to the library root, POSIX style: the given path may
    also be absolute, inside the root. ValueError when it is outside."""
    given = PurePosixPath(path)
    if given.is_absolute():
        try:
            given = given.relative_to(library.root)
        except ValueError:
            raise ValueError(f"{path} is not inside the library {library.root}") from None
    return str(given).strip("/")


def present(library_id: int):
    """Files that exist and are readable."""
    return (AudioFile.library_id == library_id) & ~AudioFile.missing & AudioFile.error.is_(None)


def iter_tracks(session: Session, library_id: int, *, batch: int = BATCH) -> Iterator[Track]:
    ids = session.scalars(
        select(AudioFile.id).where(present(library_id)).order_by(AudioFile.path)
    ).all()
    for start in range(0, len(ids), batch):
        chunk = ids[start : start + batch]
        values: dict[int, list] = {}
        for row in session.execute(
            select(FieldValue.file_id, FieldValue.field, FieldValue.source, FieldValue.value)
            .where(FieldValue.file_id.in_(chunk))
        ):  # fmt: skip
            values.setdefault(row.file_id, []).append(row)
        files = {f.id: f for f in session.scalars(select(AudioFile).where(AudioFile.id.in_(chunk)))}
        for file_id in chunk:
            f = files[file_id]
            yield Track(
                file_id=f.id, path=f.path, size=f.size, mtime_ns=f.mtime_ns,
                duration=f.duration, format=f.format, bitrate=f.bitrate,
                audio_hash=f.audio_hash, first_seen=f.first_seen,
                fields={k: r.value for k, r in resolve(values.get(f.id, [])).items()},
            )  # fmt: skip
