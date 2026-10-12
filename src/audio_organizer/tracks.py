"""Streaming over a library's tracks with their resolved metadata.

Resolving every field of every file is the expensive part of working with a large
library, so it is done in batches and the result streamed, for consumers (sync,
playlists) that look at each track once.
"""

from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import PurePosixPath

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from .models import AudioFile, EffectiveMetadata, FieldValue
from .provenance import resolve

BATCH = 2000
# The fields an album's tracks usually share, offered for editing all of them at once
ALBUM_FIELDS = ("album", "albumartist", "artist", "date", "originaldate", "genre",
                "compilation", "disctotal", "albumartistsort")  # fmt: skip
# The fields offered for editing chosen tracks at once (say, the genre of a search result):
# what tracks from several albums plausibly share; not track numbers or titles
SELECTION_FIELDS = ("artist", "album", "albumartist", "date", "genre", "mood", "rating",
                    "composer", "grouping", "compilation")  # fmt: skip
# The track list's "show" choices, as conditions on EffectiveMetadata
FILTERS = {
    "all": None,
    "disputed": EffectiveMetadata.disputed,
    "no-album": EffectiveMetadata.album.is_(None),
    "no-title": EffectiveMetadata.title.is_(None),
}


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


def album_filter(
    album: str | None = None, albumartist: str | None = None, folder: str | None = None
) -> list:
    """Conditions choosing the tracks of an album (by its album and album artist, ignoring
    case, wherever they lie) or of one folder (not its subfolders). Queries using them
    join EffectiveMetadata."""
    conditions = []
    M = EffectiveMetadata
    for column, value in ((M.album, album), (M.albumartist, albumartist)):
        if value is not None:
            conditions.append(func.lower(column) == value.lower())
    if folder is not None:
        prefix = folder.strip("/") + "/"
        escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        conditions += [
            AudioFile.path.startswith(prefix, autoescape=True),
            ~AudioFile.path.like(escaped + "%/%", escape="\\"),
        ]
    return conditions


def search(
    library_id: int,
    q: str = "",
    show: str = "all",
    album: str | None = None,
    albumartist: str | None = None,
    artist: str | None = None,
    folder: str | None = None,
):
    """A query for the present tracks with their EffectiveMetadata, as the track list
    shows them: each word of q in the artist, album, title or path, the show filter (see
    FILTERS) and one album, artist or folder."""
    M = EffectiveMetadata
    query = select(AudioFile, M).join(M, M.file_id == AudioFile.id).where(present(library_id))
    for word in q.split():
        pattern = f"%{word}%"
        query = query.where(
            or_(
                M.artist.ilike(pattern),
                M.album.ilike(pattern),
                M.title.ilike(pattern),
                AudioFile.path.ilike(pattern),
            )
        )
    if FILTERS.get(show) is not None:
        query = query.where(FILTERS[show])
    query = query.where(*album_filter(album, albumartist, folder))
    if artist is not None:
        query = query.where(func.lower(M.artist) == artist.lower())
    return query


def is_narrowed(
    q: str = "",
    show: str = "all",
    album: str | None = None,
    albumartist: str | None = None,
    artist: str | None = None,
    folder: str | None = None,
) -> bool:
    """Whether search's criteria choose some tracks rather than the whole library: only
    those are edited at once, so that no slip edits every track."""
    return bool(q.split() or show != "all" or {album, albumartist, artist, folder} != {None})


def search_files(
    session: Session, library_id: int, limit: int | None = None, **criteria
) -> list[AudioFile]:
    """The files search finds (with its criteria), in path order; at most limit."""
    query = search(library_id, **criteria).with_only_columns(AudioFile).order_by(AudioFile.path)
    return list(session.scalars(query.limit(limit) if limit is not None else query))


def chosen_files(session: Session, library_id: int, ids: Iterable[int]) -> list[AudioFile]:
    """The library's present files among the ids (others are left out), in path order."""
    ids = list(dict.fromkeys(ids))
    files: list[AudioFile] = []
    for start in range(0, len(ids), BATCH):
        chunk = ids[start : start + BATCH]
        files += session.scalars(
            select(AudioFile).where(present(library_id), AudioFile.id.in_(chunk))
        )
    return sorted(files, key=lambda f: f.path)


def album_files(
    session: Session,
    library_id: int,
    album: str | None = None,
    albumartist: str | None = None,
    folder: str | None = None,
) -> list[AudioFile]:
    """The present files of an album or folder (see album_filter), in path order."""
    if album is None and folder is None:
        raise ValueError("Choose an album or a folder")
    return list(
        session.scalars(
            select(AudioFile)
            .join(EffectiveMetadata, EffectiveMetadata.file_id == AudioFile.id)
            .where(present(library_id), *album_filter(album, albumartist, folder))
            .order_by(AudioFile.path)
        )
    )


def shared_values(
    session: Session, files: list[AudioFile], fields: Iterable[str]
) -> dict[str, str | None]:
    """The resolved value of each field that all the files agree on ("" when none has
    one), or None where they differ."""
    values: dict[int, list] = {}
    ids = [f.id for f in files]
    for start in range(0, len(ids), BATCH):
        for row in session.execute(
            select(FieldValue.file_id, FieldValue.field, FieldValue.source, FieldValue.value)
            .where(FieldValue.file_id.in_(ids[start : start + BATCH]))
        ):  # fmt: skip
            values.setdefault(row.file_id, []).append(row)
    resolved = [resolve(values.get(i, [])) for i in ids]
    shared: dict[str, str | None] = {}
    for name in fields:
        found = {r[name].value if name in r else "" for r in resolved}
        shared[name] = found.pop() if len(found) == 1 else None if found else ""
    return shared


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
