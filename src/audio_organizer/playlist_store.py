"""Writing playlist files to the library disk or a bucket, and taking them away again.

Playlists live in the catalog; their files are generated each time they are written. A
write is planned first, so that a page or command can show exactly what will happen to
each file: created, replaced (it is this tool's own earlier output) or replaced although
someone else changed it. In that last case, and when a file is deleted, the old content
is kept in a backup so that the change can be undone. What was written is recorded in
PlaylistFile; a file whose content still matches is this tool's own output.
"""

import hashlib
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Library, Playlist, PlaylistFile, PlaylistFileBackup, SyncTarget, utcnow
from .objectstore import ObjectStore
from .playlist_files import entry_location, file_path, parse_formats
from .playlist_formats import FORMATS, render
from .playlists import GeneratedPlaylist, generate
from .sync import object_key, store_for


class Place:
    """Where playlist files go: a library's disk (target None) or a sync target's bucket.
    Paths are relative to the library root or to the target's prefix."""

    def __init__(
        self, library: Library, target: SyncTarget | None = None, store: ObjectStore | None = None
    ) -> None:
        self.library = library
        self.target = target
        self._store = store
        source = target or library
        self.folder: str = source.playlist_folder
        self.formats: list[str] = parse_formats(source.playlist_formats)

    @property
    def target_id(self) -> int | None:
        return self.target.id if self.target else None

    @property
    def label(self) -> str:
        return f"target {self.target.name}" if self.target else f"library {self.library.name}"

    def describe(self, path: str) -> str:
        """The full path or s3 URL of a file, for showing to the user."""
        if self.target:
            return f"s3://{self.target.bucket}/{object_key(self.target, path)}"
        return str(Path(self.library.root, path))

    def folder_description(self) -> str:
        return self.describe(self.folder).rstrip("/")

    def read(self, path: str) -> bytes | None:
        if self.target:
            return self._get_store().get(object_key(self.target, path))
        try:
            return self._disk_path(path).read_bytes()
        except FileNotFoundError:
            return None

    def write(self, path: str, data: bytes, content_type: str) -> None:
        if self.target:
            self._get_store().put(object_key(self.target, path), data, content_type)
            return
        target = self._disk_path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    def remove(self, path: str) -> None:
        if self.target:
            self._get_store().delete(object_key(self.target, path))
        else:
            self._disk_path(path).unlink(missing_ok=True)

    def _get_store(self) -> ObjectStore:
        if self._store is None:
            assert self.target is not None
            self._store = store_for(self.target)
        return self._store

    def _disk_path(self, path: str) -> Path:
        root = Path(self.library.root).resolve()
        full = (root / path).resolve()
        if root != full and root not in full.parents:
            raise ValueError(f"{path!r} is outside the library")
        return full


@dataclass
class PlannedFile:
    path: str
    fmt: str
    name: str  # the playlist's name (for a grouped smart playlist, one of its names)
    playlist_id: int
    data: bytes
    # new: no file yet; unchanged: same content; replace: the file is this tool's own earlier
    # output; modified: the file differs from what this tool last wrote (or it never did)
    state: str
    existing: bytes | None = None

    @property
    def changes(self) -> bool:
        return self.state != "unchanged"


def digest(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def plan_write(
    session: Session, place: Place, generated: list[GeneratedPlaylist]
) -> list[PlannedFile]:
    """What writing these playlists to the place would do to each of their files."""
    known = {
        row.path: row
        for row in session.scalars(
            select(PlaylistFile).where(
                PlaylistFile.library_id == place.library.id,
                PlaylistFile.target_id == place.target_id,
            )
        )
    }
    planned = []
    for item in generated:
        entries = [(t, entry_location(place.folder, t.path)) for t in item.tracks]
        for fmt in place.formats:
            path = file_path(place.folder, item.name, fmt)
            data = render(fmt, item.name, entries).encode("utf-8")
            existing = place.read(path)
            if existing is None:
                state = "new"
            elif existing == data:
                state = "unchanged"
            elif path in known and known[path].content_hash == digest(existing):
                state = "replace"
            else:
                state = "modified"
            planned.append(
                PlannedFile(path, fmt, item.name, item.playlist.id, data, state, existing)
            )
    return planned


def plan_playlists(session: Session, place: Place, playlists: list[Playlist]) -> list[PlannedFile]:
    return plan_write(session, place, generate(session, place.library, playlists))


def apply_write(session: Session, place: Place, planned: list[PlannedFile]) -> int:
    """Write the planned files; returns how many were changed. A file someone else
    changed is backed up first. The caller commits."""
    changed = 0
    for item in planned:
        if item.state == "modified" and item.existing is not None:
            _backup(session, place, item.path, item.existing, "overwritten")
        if item.changes:
            place.write(item.path, item.data, FORMATS[item.fmt])
            changed += 1
        _record(session, place, item)
    return changed


def _record(session: Session, place: Place, item: PlannedFile) -> None:
    row = session.scalar(
        select(PlaylistFile).where(
            PlaylistFile.library_id == place.library.id,
            PlaylistFile.target_id == place.target_id,
            PlaylistFile.path == item.path,
        )
    )
    if row is None:
        row = PlaylistFile(library_id=place.library.id, target_id=place.target_id, path=item.path)
        session.add(row)
    row.playlist_id, row.format = item.playlist_id, item.fmt
    row.content_hash, row.size, row.written_at = digest(item.data), len(item.data), utcnow()


def _backup(session: Session, place: Place, path: str, content: bytes, reason: str) -> None:
    session.add(
        PlaylistFileBackup(
            library_id=place.library.id,
            target_id=place.target_id,
            path=path,
            reason=reason,
            content=content,
        )
    )


# ---- deleting --------------------------------------------------------------------------


@dataclass
class PlannedDelete:
    path: str
    existing: bytes | None  # None: the file is already gone; only the record is dropped
    row: PlaylistFile | None


def plan_delete(session: Session, place: Place, playlist: Playlist) -> list[PlannedDelete]:
    """The files of a playlist at the place: those this tool recorded for it, and the ones
    its name and the place's formats point at."""
    rows = {
        row.path: row
        for row in session.scalars(
            select(PlaylistFile).where(
                PlaylistFile.library_id == place.library.id,
                PlaylistFile.target_id == place.target_id,
                PlaylistFile.playlist_id == playlist.id,
            )
        )
    }
    paths = list(rows)
    if not playlist.group_by:
        for fmt in FORMATS:  # any format, not only the chosen ones: it may have changed
            path = file_path(place.folder, playlist.name, fmt)
            if path not in rows:
                paths.append(path)
    plans = [PlannedDelete(path, place.read(path), rows.get(path)) for path in paths]
    return [p for p in plans if p.existing is not None or p.row is not None]


def apply_delete(session: Session, place: Place, planned: list[PlannedDelete]) -> int:
    """Delete the files, keeping their content for undo; returns how many files were
    removed. The caller commits."""
    removed = 0
    for item in planned:
        if item.existing is not None:
            _backup(session, place, item.path, item.existing, "deleted")
            place.remove(item.path)
            removed += 1
        if item.row is not None:
            session.delete(item.row)
    return removed


# ---- undo ------------------------------------------------------------------------------


def backups(session: Session, place: Place, *, pending: bool = True) -> list[PlaylistFileBackup]:
    """Backups at the place, newest first; by default only those not yet restored."""
    query = (
        select(PlaylistFileBackup)
        .where(
            PlaylistFileBackup.library_id == place.library.id,
            PlaylistFileBackup.target_id == place.target_id,
        )
        .order_by(PlaylistFileBackup.id.desc())
    )
    if pending:
        query = query.where(PlaylistFileBackup.restored_at.is_(None))
    return list(session.scalars(query))


def restore(session: Session, place: Place, backup: PlaylistFileBackup) -> None:
    """Put a backed up file back. A file there that is not this tool's own output is backed
    up first, so that nothing is lost. The caller commits."""
    if backup.restored_at is not None:
        raise ValueError("This backup has been restored already")
    row = session.scalar(
        select(PlaylistFile).where(
            PlaylistFile.library_id == place.library.id,
            PlaylistFile.target_id == place.target_id,
            PlaylistFile.path == backup.path,
        )
    )
    current = place.read(backup.path)
    own_output = row is not None and current is not None and row.content_hash == digest(current)
    if current is not None and current != backup.content and not own_output:
        _backup(session, place, backup.path, current, "overwritten")
    fmt = backup.path.rsplit(".", 1)[-1]
    place.write(backup.path, backup.content, FORMATS.get(fmt, "application/octet-stream"))
    backup.restored_at = utcnow()  # the record keeps its old hash, so the file counts as
    # changed outside this tool and is backed up again before the next overwrite


def describe_state(item: PlannedFile) -> str:
    return {
        "new": "will be created",
        "unchanged": "is up to date",
        "replace": "will be replaced (written by this tool earlier)",
        "modified": "will be replaced; it was changed outside this tool, so its content is kept"
        " for undo",
    }[item.state]
