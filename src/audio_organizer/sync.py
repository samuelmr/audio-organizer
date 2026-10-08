"""Syncing a library's audio files and cover images to object storage.

Objects are stored under the target's prefix at the file's library path, with the
track's resolved metadata (not its raw tags) as object metadata: artist, albumartist,
album, title, genre, composer, tracknumber, tracktotal, discnumber, disctotal, year,
originaldate, length (ms), image (the full key of the album's cover image), compilation
(1), rating (1-5), mood, replaygaintrackgain / replaygainalbumgain (signed dB, no unit),
replaygaintrackpeak / replaygainalbumpeak (linear) and artistsort, albumartistsort,
percent-encoded; numbers as plain integers; several values separated by "; ";
truncated lists the keys cut to fit 2 KB. The player reads these; keep them in step.

Sync is planned first and only does what is needed, to save bandwidth:
- unchanged files are skipped
- a metadata change of an unchanged file is a server-side copy of the object onto itself
- a file moved locally is copied to its new key server-side, and the old key removed
- only new and changed files are uploaded
Playlists are generated in the same pass and stored as .m3u, .json and .html under
playlists/, referring to the tracks by relative paths; they are uploaded when their
content changes. Objects with no counterpart in the library are only deleted when asked to.
"""

import hashlib
import json
import os
import posixpath
import re
import unicodedata
from collections.abc import Callable, Iterable
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath
from urllib.parse import quote

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from .models import AudioFile, RemoteObject, SyncTarget, utcnow
from .objectstore import ObjectStore, S3Store
from .playlist_formats import FORMATS as PLAYLIST_FORMATS
from .playlist_formats import render
from .playlists import PlaylistSet, library_playlists, safe_filename
from .tags import parse_number, parse_year
from .tracks import iter_tracks, present

CONTENT_TYPES = {
    ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".mp4": "audio/mp4", ".aac": "audio/aac",
    ".ogg": "audio/ogg", ".oga": "audio/ogg", ".opus": "audio/ogg", ".flac": "audio/flac",
    ".wma": "audio/x-ms-wma", ".aif": "audio/aiff", ".aiff": "audio/aiff", ".wav": "audio/wav",
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
}  # fmt: skip
COVER_NAMES = ("cover", "folder", "front")
IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png")
METADATA_LIMIT = 2000  # S3 allows 2 KB of user metadata per object
PLAYLIST_FOLDER = "playlists"


@dataclass
class Action:
    kind: str  # upload | metadata | move | delete
    key: str
    object_kind: str = "audio"  # audio | image | playlist
    path: Path | None = None  # local source, for uploads
    data: bytes | None = None  # generated content (playlists), for uploads
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


_TARGET_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


def check_target_name(name: str) -> str:
    """The name, if it can be typed on a command line and used in a URL; else ValueError.
    Letters, digits, '.', '_' and '-': a '/' would make the target unreachable on the web."""
    if not _TARGET_NAME.fullmatch(name):
        raise ValueError(
            f"Invalid target name {name!r}: use letters, digits, '.', '_' and '-', "
            "starting with a letter or digit"
        )
    return name


def remote_summary(session: Session, target: SyncTarget) -> tuple[int, datetime | None]:
    """How many objects this catalog knows in the bucket, and when it last synced one."""
    count, last = session.execute(
        select(func.count(RemoteObject.id), func.max(RemoteObject.synced_at)).where(
            RemoteObject.target_id == target.id
        )
    ).one()
    return count, last


def remove_target(session: Session, target: SyncTarget) -> int:
    """Forget a target and what was synced to it; the bucket itself is left alone.
    Returns how many synced objects were forgotten."""
    objects, _ = remote_summary(session, target)
    session.execute(delete(RemoteObject).where(RemoteObject.target_id == target.id))
    session.delete(target)
    session.commit()
    return objects


def bucket_layout(target: SyncTarget, example: str = "Artist/Album/01 Title.mp3") -> list[str]:
    """Where a target puts things, as s3 URLs: for explaining the bucket to the user."""
    base = f"s3://{target.bucket}/{target.prefix.strip('/')}".rstrip("/")
    folder = posixpath.dirname(example)
    return [
        f"{base}/{example}  (tracks: the library path)",
        f"{base}/{folder}/cover.jpg  (the album's cover, from the track's `image` metadata)",
        f"{base}/playlists/<name>.m3u, .json, .html  (playlists, with relative entries)",
    ]


def object_key(target: SyncTarget, relpath: str) -> str:
    """The key of a library path. NFC, so that the key does not depend on whether the
    file system reported the name decomposed (as macOS often does)."""
    prefix = target.prefix.strip("/")
    key = f"{prefix}/{relpath}" if prefix else relpath
    return unicodedata.normalize("NFC", key)


_ORIGINAL_DATE = re.compile(r"\s*(\d{4}(?:-\d{2}(?:-\d{2})?)?)(?:\D.*)?", re.DOTALL)
_DECIMAL = re.compile(r"\s*([+-]?\d+(?:\.\d+)?)\s*(?:dB)?\s*", re.IGNORECASE)


def _original_date(value: str | None) -> str | None:
    """YYYY, YYYY-MM or YYYY-MM-DD, whatever longer timestamp the tag held."""
    match = _ORIGINAL_DATE.fullmatch(value or "")
    return match.group(1) if match else None


def _decimal(value: str | None) -> str | None:
    """A ReplayGain value as a plain decimal: "-7.23 dB" -> "-7.23", "+3.00 dB" -> "3.00"
    (no unit, no plus sign); None for anything that is not a number."""
    match = _DECIMAL.fullmatch(value or "")
    if not match:
        return None
    text = match.group(1)
    return text.removeprefix("+")


def _rating(value: str | None) -> str | None:
    number = parse_number(value)
    return str(number) if number and 1 <= number <= 5 else None


def _mood(value: str | None) -> str | None:
    """Moods separated by "; " like every multi-valued key; MusicMatch comments
    sometimes separate them with commas instead."""
    parts = [p.strip() for p in re.split(r"[;,]", value or "") if p.strip()]
    return "; ".join(dict.fromkeys(parts)) or None


def _flag(value: str | None) -> str | None:
    return "1" if (value or "").strip().lower() in ("1", "true", "yes") else None


def track_metadata(
    duration: float | None, fields: dict[str, str], cover_key: str | None
) -> dict[str, str]:
    def number(value: int | None) -> str | None:
        return str(value) if value else None

    values = {
        "artist": fields.get("artist"),
        "albumartist": fields.get("albumartist"),
        "album": fields.get("album"),
        "title": fields.get("title"),
        # Numbers as plain integers ("2", not "02." or "2/12"); left out when not numbers
        "tracknumber": number(parse_number(fields.get("tracknumber"))),
        "tracktotal": number(parse_number(fields.get("tracktotal"))),
        "discnumber": number(parse_number(fields.get("discnumber"))),
        "disctotal": number(parse_number(fields.get("disctotal"))),
        "year": number(parse_year(fields.get("date"))),
        "originaldate": _original_date(fields.get("originaldate")),
        "genre": fields.get("genre"),
        "composer": fields.get("composer"),
        "compilation": _flag(fields.get("compilation")),
        "rating": _rating(fields.get("rating")),
        "mood": _mood(fields.get("mood")),
        "replaygaintrackgain": _decimal(fields.get("replaygain_track_gain")),
        "replaygainalbumgain": _decimal(fields.get("replaygain_album_gain")),
        "replaygaintrackpeak": _decimal(fields.get("replaygain_track_peak")),
        "replaygainalbumpeak": _decimal(fields.get("replaygain_album_peak")),
        "artistsort": fields.get("artistsort"),
        "albumartistsort": fields.get("albumartistsort"),
        "length": str(round(duration * 1000)) if duration else None,
        "image": cover_key,
    }
    values = {k: v for k, v in values.items() if v}
    metadata = {k: quote(v, safe="") for k, v in values.items()}
    # Stay within the size limit by shortening the longest values, and say which were cut
    # so that the player can tell (and read the full value from the file's tags)
    cut: list[str] = []

    def size() -> int:
        total = sum(len(k) + len(v) for k, v in metadata.items())
        return total + (len("truncated") + len(quote(",".join(cut), safe="")) if cut else 0)

    while size() > METADATA_LIMIT:
        longest = max(metadata, key=lambda k: len(metadata[k]))
        values[longest] = values[longest][: len(values[longest]) // 2]
        metadata[longest] = quote(values[longest], safe="")
        if longest not in cut:
            cut.append(longest)
    if cut:
        metadata["truncated"] = quote(",".join(sorted(cut)), safe="")
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


def plan(session: Session, target: SyncTarget, *, delete_stale: bool = False) -> Plan:
    root = Path(target.library.root)
    result = Plan()
    remote = {obj.key: obj for obj in session.scalars(
        select(RemoteObject).where(RemoteObject.target_id == target.id)
    )}  # fmt: skip
    remote_by_file = {obj.file_id: obj for obj in remote.values() if obj.file_id is not None}
    desired = {
        object_key(target, path)
        for path in session.scalars(select(AudioFile.path).where(present(target.library_id)))
    }
    covers: dict[str, Path | None] = {}
    moved_from: set[str] = set()
    playlist_set = PlaylistSet(library_playlists(session, target.library))

    for track in iter_tracks(session, target.library_id):
        playlist_set.feed(track)
        folder = str(PurePosixPath(track.path).parent)
        if folder not in covers:
            covers[folder] = find_cover(root / folder)
        cover = covers[folder]
        cover_key = None
        if cover is not None:
            cover_key = object_key(target, (PurePosixPath(folder) / cover.name).as_posix())
        content_type = CONTENT_TYPES.get(PurePosixPath(track.path).suffix.lower(), "audio/mpeg")
        metadata = track_metadata(track.duration, track.fields, cover_key)
        action = Action(
            kind="upload", key=object_key(target, track.path), path=root / track.path,
            file_id=track.file_id, size=track.size, mtime_ns=track.mtime_ns,
            content_type=content_type, metadata=metadata,
            meta_digest=_digest(content_type, metadata),
        )  # fmt: skip
        existing = remote.get(action.key)
        previous = remote_by_file.get(track.file_id)
        if existing and (existing.size, existing.mtime_ns) == (track.size, track.mtime_ns):
            if existing.meta_digest == action.meta_digest:
                result.unchanged += 1
                continue
            action.kind = "metadata"
        elif (
            existing is None
            and previous is not None
            and previous.key not in desired
            and previous.key not in moved_from
            and (previous.size, previous.mtime_ns) == (track.size, track.mtime_ns)
        ):
            action.kind, action.source_key = "move", previous.key
            moved_from.add(previous.key)
        else:
            result.upload_bytes += track.size
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

    playlist_dir = object_key(target, PLAYLIST_FOLDER)
    for generated in playlist_set.results():
        entries = [
            (t, posixpath.relpath(object_key(target, t.path), playlist_dir))
            for t in generated.tracks
        ]
        for fmt, content_type in PLAYLIST_FORMATS.items():
            key = f"{playlist_dir}/{safe_filename(generated.name)}.{fmt}"
            data = render(fmt, generated.name, entries).encode("utf-8")
            digest = hashlib.sha1(content_type.encode() + b"\0" + data).hexdigest()
            desired.add(key)
            existing = remote.get(key)
            if existing and existing.meta_digest == digest:
                result.unchanged += 1
                continue
            result.actions.append(Action(
                kind="upload", key=key, object_kind="playlist", data=data, size=len(data),
                content_type=content_type, meta_digest=digest,
            ))  # fmt: skip
            result.upload_bytes += len(data)

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
        if action.kind == "upload" and action.data is not None:
            store.put(action.key, action.data, action.content_type)
        elif action.kind == "upload":
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

    Objects uploaded by other means (such as other tools) whose key and size match
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
            .where(present(target.library_id))
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
