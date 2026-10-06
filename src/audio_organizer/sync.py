"""Syncing a library's audio files and cover images to object storage.

Objects are stored under the target's prefix at the file's library path, with the
track's resolved metadata (not its raw tags) as object metadata, compatible with the
earlier upload.sh: artist, album, title, tracknumber, year, genre, length (ms) and
image (the key of the album's cover image), percent-encoded.

Sync is planned first and only does what is needed, to save bandwidth:
- unchanged files are skipped
- a metadata change of an unchanged file is a server-side copy of the object onto itself
- a file moved locally is copied to its new key server-side, and the old key removed
- only new and changed files are uploaded
Objects with no counterpart in the library are only deleted when asked to.
"""

import hashlib
import json
import os
import unicodedata
from collections.abc import Callable, Iterable
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from urllib.parse import quote

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .models import AudioFile, FieldValue, RemoteObject, SyncTarget, utcnow
from .objectstore import ObjectStore, S3Store
from .provenance import resolve

CONTENT_TYPES = {
    ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".mp4": "audio/mp4", ".aac": "audio/aac",
    ".ogg": "audio/ogg", ".oga": "audio/ogg", ".opus": "audio/ogg", ".flac": "audio/flac",
    ".wma": "audio/x-ms-wma", ".aif": "audio/aiff", ".aiff": "audio/aiff", ".wav": "audio/wav",
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
}  # fmt: skip
COVER_NAMES = ("cover", "folder", "front")
IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png")
METADATA_LIMIT = 2000  # S3 allows 2 KB of user metadata per object
BATCH = 2000


@dataclass
class Action:
    kind: str  # upload | metadata | move | delete
    key: str
    object_kind: str = "audio"  # audio | image
    path: Path | None = None  # local source, for uploads
    source_key: str | None = None  # for moves
    file_id: int | None = None
    size: int = 0
    mtime_ns: int | None = None
    content_type: str = "application/octet-stream"
    metadata: dict[str, str] = field(default_factory=dict)
    meta_digest: str | None = None

    def describe(self) -> str:
        if self.kind == "move":
            return f"move {self.source_key} -> {self.key}"
        return f"{self.kind} {self.key}"


@dataclass
class Plan:
    actions: list[Action] = field(default_factory=list)
    unchanged: int = 0
    stale: list[str] = field(default_factory=list)  # remote keys not in the library (kept)
    upload_bytes: int = 0

    def counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for action in self.actions:
            counts[action.kind] = counts.get(action.kind, 0) + 1
        return counts


@dataclass
class SyncResult:
    done: dict[str, int] = field(default_factory=dict)
    errors: list[tuple[str, str]] = field(default_factory=list)


def store_for(target: SyncTarget) -> ObjectStore:
    return S3Store(
        target.bucket,
        endpoint_url=target.endpoint_url,
        region=target.region,
        profile=target.profile,
    )


def object_key(target: SyncTarget, relpath: str) -> str:
    """The key of a library path. NFC, so that the key does not depend on whether the
    file system reported the name decomposed (as macOS often does)."""
    prefix = target.prefix.strip("/")
    key = f"{prefix}/{relpath}" if prefix else relpath
    return unicodedata.normalize("NFC", key)


def track_metadata(
    file: AudioFile, fields: dict[str, str], cover_key: str | None
) -> dict[str, str]:
    values = {
        "artist": fields.get("artist"),
        "albumartist": fields.get("albumartist"),
        "album": fields.get("album"),
        "title": fields.get("title"),
        "tracknumber": fields.get("tracknumber"),
        "discnumber": fields.get("discnumber"),
        "year": (fields.get("date") or "")[:4] or None,
        "genre": fields.get("genre"),
        "length": str(round(file.duration * 1000)) if file.duration else None,
        "image": cover_key,
    }
    values = {k: v for k, v in values.items() if v}
    metadata = {k: quote(v, safe="") for k, v in values.items()}
    # Stay within the size limit by shortening the longest values
    while sum(len(k) + len(v) for k, v in metadata.items()) > METADATA_LIMIT:
        longest = max(metadata, key=lambda k: len(metadata[k]))
        values[longest] = values[longest][: len(values[longest]) // 2]
        metadata[longest] = quote(values[longest], safe="")
    return metadata


def find_cover(directory: Path) -> Path | None:
    """cover/folder/front.jpg|jpeg|png (any case), else the only image in the folder."""
    try:
        names = os.listdir(directory)
    except OSError:
        return None
    images = {n.casefold(): n for n in names if n.casefold().endswith(IMAGE_EXTENSIONS)}
    for stem in COVER_NAMES:
        for ext in IMAGE_EXTENSIONS:
            if stem + ext in images:
                return directory / images[stem + ext]
    if len(images) == 1:
        return directory / next(iter(images.values()))
    return None


def _digest(content_type: str, metadata: dict[str, str]) -> str:
    data = json.dumps([content_type, metadata], sort_keys=True).encode()
    return hashlib.sha1(data).hexdigest()


def _syncable(target: SyncTarget):
    return (
        (AudioFile.library_id == target.library_id) & ~AudioFile.missing & AudioFile.error.is_(None)
    )


def plan(session: Session, target: SyncTarget, *, delete_stale: bool = False) -> Plan:
    root = Path(target.library.root)
    result = Plan()
    remote = {obj.key: obj for obj in session.scalars(
        select(RemoteObject).where(RemoteObject.target_id == target.id)
    )}  # fmt: skip
    remote_by_file = {obj.file_id: obj for obj in remote.values() if obj.file_id is not None}
    paths = session.execute(select(AudioFile.id, AudioFile.path).where(_syncable(target))).all()
    desired = {object_key(target, path) for _, path in paths}
    covers: dict[str, Path | None] = {}
    moved_from: set[str] = set()

    for start in range(0, len(paths), BATCH):
        ids = [file_id for file_id, _ in paths[start : start + BATCH]]
        files = session.scalars(select(AudioFile).where(AudioFile.id.in_(ids))).all()
        rows = session.execute(
            select(FieldValue.file_id, FieldValue.field, FieldValue.source, FieldValue.value)
            .where(FieldValue.file_id.in_(ids))
        ).all()  # fmt: skip
        values_by_file: dict[int, list] = {}
        for row in rows:
            values_by_file.setdefault(row.file_id, []).append(row)

        for file in files:
            folder = str(PurePosixPath(file.path).parent)
            if folder not in covers:
                covers[folder] = find_cover(root / folder)
            cover = covers[folder]
            cover_key = None
            if cover is not None:
                cover_key = object_key(target, (PurePosixPath(folder) / cover.name).as_posix())
            fields = {k: r.value for k, r in resolve(values_by_file.get(file.id, [])).items()}
            content_type = CONTENT_TYPES.get(PurePosixPath(file.path).suffix.lower(), "audio/mpeg")
            metadata = track_metadata(file, fields, cover_key)
            action = Action(
                kind="upload", key=object_key(target, file.path), path=root / file.path,
                file_id=file.id, size=file.size, mtime_ns=file.mtime_ns,
                content_type=content_type, metadata=metadata,
                meta_digest=_digest(content_type, metadata),
            )  # fmt: skip
            existing = remote.get(action.key)
            previous = remote_by_file.get(file.id)
            if existing and (existing.size, existing.mtime_ns) == (file.size, file.mtime_ns):
                if existing.meta_digest == action.meta_digest:
                    result.unchanged += 1
                    continue
                action.kind = "metadata"
            elif (
                existing is None
                and previous is not None
                and previous.key not in desired
                and previous.key not in moved_from
                and (previous.size, previous.mtime_ns) == (file.size, file.mtime_ns)
            ):
                action.kind, action.source_key = "move", previous.key
                moved_from.add(previous.key)
            else:
                result.upload_bytes += file.size
            result.actions.append(action)

    for folder, cover in covers.items():
        if cover is None:
            continue
        key = object_key(target, (PurePosixPath(folder) / cover.name).as_posix())
        desired.add(key)
        try:
            st = cover.stat()
        except OSError:
            continue
        existing = remote.get(key)
        if existing and (existing.size, existing.mtime_ns) == (st.st_size, st.st_mtime_ns):
            result.unchanged += 1
            continue
        content_type = CONTENT_TYPES[cover.suffix.lower()]
        result.actions.append(Action(
            kind="upload", key=key, object_kind="image", path=cover, size=st.st_size,
            mtime_ns=st.st_mtime_ns, content_type=content_type,
            meta_digest=_digest(content_type, {}),
        ))  # fmt: skip
        result.upload_bytes += st.st_size

    for key, obj in remote.items():
        if key in desired or key in moved_from:
            continue
        if delete_stale:
            result.actions.append(Action(kind="delete", key=key, object_kind=obj.kind))
        else:
            result.stale.append(key)
    return result


def execute(
    session: Session,
    target: SyncTarget,
    store: ObjectStore,
    actions: Iterable[Action],
    *,
    workers: int = 4,
    progress: Callable[[Action, str | None], None] | None = None,
) -> SyncResult:
    """Carry out planned actions. Storage operations run in parallel; the catalog is
    updated as each one completes, so an interrupted sync resumes where it stopped."""
    result = SyncResult()
    since_commit = 0

    def run(action: Action) -> None:
        if action.kind == "upload":
            st = action.path.stat()
            if (st.st_size, st.st_mtime_ns) != (action.size, action.mtime_ns):
                raise RuntimeError("changed since it was scanned; rescan the library")
            store.upload(action.key, action.path, action.content_type, action.metadata)
        elif action.kind == "metadata":
            store.copy(action.key, action.key, action.content_type, action.metadata)
        elif action.kind == "move":
            store.copy(action.source_key, action.key, action.content_type, action.metadata)
            store.delete(action.source_key)
        elif action.kind == "delete":
            store.delete(action.key)

    def record(action: Action) -> None:
        if action.kind == "delete":
            session.execute(delete(RemoteObject).where(
                RemoteObject.target_id == target.id, RemoteObject.key == action.key
            ))  # fmt: skip
            return
        lookup_key = action.source_key if action.kind == "move" else action.key
        obj = session.scalar(select(RemoteObject).where(
            RemoteObject.target_id == target.id, RemoteObject.key == lookup_key
        ))  # fmt: skip
        if obj is None:
            obj = RemoteObject(target_id=target.id, key=action.key, kind=action.object_kind)
            session.add(obj)
        obj.key, obj.file_id = action.key, action.file_id
        obj.size, obj.mtime_ns = action.size, action.mtime_ns
        obj.meta_digest, obj.synced_at = action.meta_digest, utcnow()

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures: dict[Future, Action] = {pool.submit(run, a): a for a in actions}
        for future in as_completed(futures):
            action = futures[future]
            error = None
            try:
                future.result()
                record(action)
                result.done[action.kind] = result.done.get(action.kind, 0) + 1
            except Exception as e:  # report and carry on with the rest
                error = f"{type(e).__name__}: {e}"
                result.errors.append((action.describe(), error))
            since_commit += 1
            if since_commit >= 100:
                session.commit()
                since_commit = 0
            if progress:
                progress(action, error)
    session.commit()
    return result


@dataclass
class ReconcileResult:
    adopted: int = 0
    forgotten: int = 0  # known objects that are no longer in the bucket
    unknown: list[str] = field(default_factory=list)  # remote keys matching nothing local


def reconcile(session: Session, target: SyncTarget, store: ObjectStore) -> ReconcileResult:
    """Compare the catalog's record of the target with what the bucket actually holds.

    Objects uploaded by other means (such as the old upload.sh) whose key and size match
    a local file are adopted, so they are not uploaded again; their metadata is marked
    unknown, so the next sync rewrites it with a server-side copy. Records of objects that
    are gone from the bucket are dropped, so the next sync uploads them again."""
    result = ReconcileResult()
    root = Path(target.library.root)
    prefix = target.prefix.strip("/")
    listed = dict(store.list(prefix + "/" if prefix else ""))
    known = {obj.key: obj for obj in session.scalars(
        select(RemoteObject).where(RemoteObject.target_id == target.id)
    )}  # fmt: skip
    for key, obj in known.items():
        if key not in listed:
            session.delete(obj)
            result.forgotten += 1
    local = {
        object_key(target, path): (file_id, size, mtime_ns)
        for file_id, path, size, mtime_ns in session.execute(
            select(AudioFile.id, AudioFile.path, AudioFile.size, AudioFile.mtime_ns)
            .where(_syncable(target))
        )
    }  # fmt: skip
    for key, size in listed.items():
        if key in known:
            continue
        # Keys written by other tools may be decomposed Unicode; such an object is adopted
        # under its actual key, and the next sync moves it to the NFC key server-side.
        nfc_key = unicodedata.normalize("NFC", key)
        if nfc_key in local and local[nfc_key][1] == size:
            file_id, _, mtime_ns = local[nfc_key]
            session.add(RemoteObject(
                target_id=target.id, key=key, kind="audio", file_id=file_id, size=size,
                mtime_ns=mtime_ns, meta_digest=None,
            ))  # fmt: skip
            result.adopted += 1
            continue
        relpath = nfc_key[len(prefix) + 1 :] if prefix else nfc_key
        if relpath.casefold().endswith(IMAGE_EXTENSIONS):
            try:
                st = (root / relpath).stat()
            except OSError:
                st = None
            if st is not None and st.st_size == size:
                session.add(RemoteObject(
                    target_id=target.id, key=key, kind="image", size=size,
                    mtime_ns=st.st_mtime_ns, meta_digest=None,
                ))  # fmt: skip
                result.adopted += 1
                continue
        result.unknown.append(key)
    session.commit()
    return result
