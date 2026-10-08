"""Incremental library scanning.

Files whose size and modification time are unchanged since the last scan are not
read again, so rescanning a large library on a network share is cheap. New and
changed files are read in a thread pool. A file that disappears from one path and
appears at another with the same audio hash is recorded as a move, keeping its
history and manual edits.
"""

import os
from collections.abc import Callable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from . import pathparse, tags
from .audiohash import audio_hash
from .blockfile import BlockFile
from .models import AudioFile, Library, TagSnapshot, utcnow
from .provenance import set_values

AUDIO_EXTENSIONS = {
    ".mp3", ".m4a", ".mp4", ".aac", ".ogg", ".oga", ".opus", ".flac", ".wma", ".aif", ".aiff",
    ".wav",
}  # fmt: skip
COMMIT_EVERY = 200


@dataclass
class ScanStats:
    seen: int = 0
    unchanged: int = 0
    new: int = 0
    changed: int = 0
    moved: int = 0
    missing: int = 0
    errors: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class _Result:
    relpath: str
    size: int
    mtime_ns: int
    info: tags.AudioInfo | None = None
    audio_hash: str | None = None
    error: str | None = None


def walk(root: str | Path) -> Iterator[tuple[str, os.stat_result]]:
    """Yield (relative POSIX path, stat) of the audio files under root, skipping hidden
    and system folders ('.', '@' prefixes) and AppleDouble files ('._')."""
    root = os.fspath(root)
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = sorted(os.scandir(current), key=lambda e: e.name)
        except OSError:
            continue
        for entry in entries:
            if entry.name.startswith((".", "@")):
                continue
            if entry.is_dir(follow_symlinks=False):
                stack.append(entry.path)
            elif os.path.splitext(entry.name)[1].lower() in AUDIO_EXTENSIONS:
                relpath = Path(os.path.relpath(entry.path, root)).as_posix()
                yield relpath, entry.stat()


def _read(root: str, relpath: str, st: os.stat_result, hash_audio: bool) -> _Result:
    result = _Result(relpath, st.st_size, st.st_mtime_ns)
    try:
        # One open file, read in large cached blocks, serves both the tag reader and
        # the hasher: few round trips, which is what matters on a network share.
        with BlockFile(os.path.join(root, relpath)) as f:
            try:
                result.info = tags.read(f)
                if result.info.format is None:
                    result.error = "not recognized as audio"
            except Exception as e:  # mutagen raises many kinds of errors on damaged files
                result.error = f"{type(e).__name__}: {e}"
            if hash_audio:
                result.audio_hash = audio_hash(f)
    except OSError as e:
        result.error = result.error or f"{type(e).__name__}: {e}"
    return result


def scan(
    session: Session,
    library: Library,
    *,
    hash_audio: bool = True,
    workers: int = 8,
    retry_errors: bool = False,
    progress: Callable[[ScanStats, str], None] | None = None,
) -> ScanStats:
    """Catalog new and changed files. Files that could not be read are only read again
    when they change, or with retry_errors (e.g. after an upgrade that reads more formats)."""
    stats = ScanStats()
    known = {
        path: (file_id, size, mtime_ns, missing)
        for file_id, path, size, mtime_ns, missing in session.execute(
            select(
                AudioFile.id, AudioFile.path, AudioFile.size, AudioFile.mtime_ns, AudioFile.missing
            ).where(AudioFile.library_id == library.id)
        )
    }
    failed = set()
    if retry_errors:
        failed = set(
            session.scalars(
                select(AudioFile.path).where(
                    AudioFile.library_id == library.id, AudioFile.error.is_not(None)
                )
            )
        )
    seen: set[str] = set()
    reappeared: list[int] = []
    new_files: list[AudioFile] = []
    pending: list[Future[_Result]] = []
    done_since_commit = 0

    def handle(result: _Result) -> None:
        nonlocal done_since_commit
        file_id = known.get(result.relpath, (None,))[0]
        if file_id is None:
            file = AudioFile(library=library, path=result.relpath)
            session.add(file)
            new_files.append(file)
            stats.new += 1
        else:
            file = session.get(AudioFile, file_id)
            stats.changed += 1
        _update(session, file, result, first_time=file_id is None)
        if result.error:
            stats.errors.append((result.relpath, result.error))
        done_since_commit += 1
        if done_since_commit >= COMMIT_EVERY:
            session.commit()
            done_since_commit = 0
        if progress:
            progress(stats, result.relpath)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for relpath, st in walk(library.root):
            stats.seen += 1
            seen.add(relpath)
            entry = known.get(relpath)
            unchanged = entry and entry[1] == st.st_size and entry[2] == st.st_mtime_ns
            if unchanged and relpath not in failed:
                stats.unchanged += 1
                if entry[3]:
                    reappeared.append(entry[0])
                continue
            pending.append(pool.submit(_read, library.root, relpath, st, hash_audio))
            if len(pending) >= workers * 4:
                handle(pending.pop(0).result())
        for future in pending:
            handle(future.result())

    if reappeared:
        session.execute(update(AudioFile).where(AudioFile.id.in_(reappeared)).values(missing=False))

    gone = [entry[0] for path, entry in known.items() if path not in seen and not entry[3]]
    moved = _match_moves(session, gone, new_files)
    stats.moved = len(moved)
    stats.new -= stats.moved
    still_gone = [file_id for file_id in gone if file_id not in moved]
    if still_gone:
        session.execute(update(AudioFile).where(AudioFile.id.in_(still_gone)).values(missing=True))
    stats.missing = len(still_gone)
    session.commit()
    return stats


def _update(session: Session, file: AudioFile, result: _Result, *, first_time: bool) -> None:
    info = result.info or tags.AudioInfo()
    file.size = result.size
    file.mtime_ns = result.mtime_ns
    if file.audio_hash and result.audio_hash and result.audio_hash != file.audio_hash:
        set_values(session, file, "analysis", {})  # measured from audio that is gone
    file.audio_hash = result.audio_hash or file.audio_hash
    file.format = info.format
    file.duration = info.duration
    file.bitrate = info.bitrate
    file.sample_rate = info.sample_rate
    file.error = result.error
    file.missing = False
    file.scanned_at = utcnow()
    set_values(session, file, "tag", info.fields)
    set_values(session, file, "path", pathparse.parse(result.relpath))
    if first_time:
        session.add(TagSnapshot(file=file, reason="first_seen", tags=info.fields))


def _match_moves(session: Session, gone_ids: list[int], new_files: list[AudioFile]) -> set[int]:
    """Turn (vanished file, new file with the same audio hash) pairs into moves of the
    original record, so its id, snapshots and manual edits survive. Returns the moved ids."""
    moved: set[int] = set()
    if not gone_ids or not new_files:
        return moved
    by_hash: dict[str, list[AudioFile]] = {}
    for file_id in gone_ids:
        old = session.get(AudioFile, file_id)
        if old.audio_hash:
            by_hash.setdefault(old.audio_hash, []).append(old)
    for new in new_files:
        candidates = by_hash.get(new.audio_hash or "")
        if not candidates:
            continue
        old = candidates.pop(0)
        fields = {
            source: {fv.field: fv.value for fv in new.values if fv.source == source}
            for source in ("tag", "path")
        }
        attrs = {
            name: getattr(new, name)
            for name in ("path", "size", "mtime_ns", "format", "duration", "bitrate",
                         "sample_rate", "error", "scanned_at")
        }  # fmt: skip
        session.delete(new)
        session.flush()
        for name, value in attrs.items():
            setattr(old, name, value)
        old.missing = False
        for source, values in fields.items():
            set_values(session, old, source, values)
        moved.add(old.id)
    return moved
