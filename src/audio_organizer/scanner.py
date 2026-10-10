"""Incremental library scanning.

Files whose size and modification time are unchanged since the last scan are not
read again, so rescanning a large library on a network share is cheap. New and
changed files are read in a thread pool. A file that disappears from one path and
appears at another with the same audio hash is recorded as a move, keeping its
history and manual edits.

On a case-insensitive share, names that differ only in letter case ("Normaalien
Maihinnousu" and "Normaalien maihinnousu") can both be listed while both lead to the same
folder: the server holds two, but this computer reaches one of them under either name.
Such aliases are skipped and reported, so that the files are not cataloged twice and then
taken for duplicates of themselves.
"""

import os
import unicodedata
from collections.abc import Callable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from . import conflicts, covers, pathparse, tags
from .audiohash import audio_hash
from .blockfile import BlockFile
from .models import AudioFile, Library, TagSnapshot, utcnow
from .playlist_scan import PlaylistScan, import_from_disk
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
    # Fields retagged elsewhere that the user has an edit for (conflicts.py)
    conflicts: int = 0
    errors: list[tuple[str, str]] = field(default_factory=list)
    aliases: list[tuple[str, str]] = field(default_factory=list)  # (skipped name, its twin)
    playlists: PlaylistScan = field(default_factory=PlaylistScan)


@dataclass
class _Result:
    relpath: str
    size: int
    mtime_ns: int
    info: tags.AudioInfo | None = None
    audio_hash: str | None = None
    error: str | None = None


def walk(
    root: str | Path,
    folders: dict[str, tuple[int, list[str]]] | None = None,
    aliases: list[tuple[str, str]] | None = None,
) -> Iterator[tuple[str, os.stat_result]]:
    """Yield (relative POSIX path, stat) of the audio files under root, skipping hidden
    and system folders ('.', '@' prefixes) and AppleDouble files ('._').

    The folders that hold audio files are also noted in `folders`, as (modification time,
    names of all their entries), for the cover cache (covers.remember_folders): the
    folder is listed anyway, so this costs one stat. Entries that only reach an earlier
    entry of the same folder under another letter case are skipped, and noted in
    `aliases` as (relative path, relative path of the entry reached); see
    case_aliases."""
    root = os.fspath(root)
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = sorted(os.scandir(current), key=lambda e: e.name)
        except OSError:
            continue
        has_audio = False
        twins = case_aliases(entries)
        for entry in entries:
            if entry.name.startswith((".", "@")):
                continue
            if entry.name in twins:
                if aliases is not None:
                    twin = twins[entry.name].name
                    aliases.append(
                        (
                            _relative(os.path.join(current, entry.name), root),
                            _relative(os.path.join(current, twin), root),
                        )
                    )
                continue
            if entry.is_dir(follow_symlinks=False):
                stack.append(entry.path)
            elif os.path.splitext(entry.name)[1].lower() in AUDIO_EXTENSIONS:
                has_audio = True
                yield _relative(entry.path, root), entry.stat()
        if has_audio and folders is not None:
            try:
                mtime = os.stat(current).st_mtime_ns
            except OSError:
                continue
            folders[_relative(current, root)] = (mtime, [e.name for e in entries])


def _relative(path: str, root: str) -> str:
    return Path(os.path.relpath(path, root)).as_posix()


def case_aliases(entries: list[os.DirEntry]) -> dict[str, os.DirEntry]:
    """The entries of one folder that are another name of an earlier entry: the same name
    ignoring letter case and Unicode composition, and the same file or folder on disk.

    A case-insensitive share may list two folders whose names differ only in case, while
    this computer opens the same one under both names, so the second cannot be read from
    here (it is on the server, to be looked at there). Reading both names would catalog
    each file twice, and trashing one "copy" would trash the only one. Distinct folders of
    such names on a case-sensitive disk are different files and are kept apart."""
    first: dict[str, os.DirEntry] = {}
    result: dict[str, os.DirEntry] = {}
    for entry in entries:
        key = unicodedata.normalize("NFC", entry.name).casefold()
        earlier = first.setdefault(key, entry)
        if earlier is not entry and _same_file(earlier.path, entry.path):
            result[entry.name] = earlier
    return result


def _same_file(a: str, b: str) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


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
    reread: bool = False,
    import_playlists: bool = True,
    progress: Callable[[ScanStats, str], None] | None = None,
) -> ScanStats:
    """Catalog new and changed files. Files that could not be read are only read again
    when they change, or with retry_errors (e.g. after an upgrade that reads more formats);
    with reread, every file is read again (e.g. after an upgrade that reads more tags).
    Playlist files in the library's playlist folder are imported unless import_playlists
    is off (see playlist_scan)."""
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
        stats.conflicts += _update(session, file, result, first_time=file_id is None)
        if result.error:
            stats.errors.append((result.relpath, result.error))
        done_since_commit += 1
        if done_since_commit >= COMMIT_EVERY:
            session.commit()
            done_since_commit = 0
        if progress:
            progress(stats, result.relpath)

    folders: dict[str, tuple[int, list[str]]] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for relpath, st in walk(library.root, folders, stats.aliases):
            stats.seen += 1
            seen.add(relpath)
            entry = known.get(relpath)
            unchanged = entry and entry[1] == st.st_size and entry[2] == st.st_mtime_ns
            if unchanged and not reread and relpath not in failed:
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
    moved = _match_moves(session, gone, new_files, stats)
    stats.moved = len(moved)
    stats.new -= stats.moved
    still_gone = [file_id for file_id in gone if file_id not in moved]
    if still_gone:
        session.execute(update(AudioFile).where(AudioFile.id.in_(still_gone)).values(missing=True))
    stats.missing = len(still_gone)
    covers.remember_folders(session, library, folders)
    session.commit()
    if import_playlists:
        stats.playlists = import_from_disk(session, library)
    return stats


def _update(session: Session, file: AudioFile, result: _Result, *, first_time: bool) -> int:
    """Store what was read from the file; returns how many of its fields are now in
    conflict with the user's edits."""
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
    found = 0 if first_time or result.error else conflicts.detect(session, file, info.fields)
    set_values(session, file, "tag", info.fields)
    set_values(session, file, "path", pathparse.parse(result.relpath))
    if first_time:
        session.add(TagSnapshot(file=file, reason="first_seen", tags=info.fields))
    return found


def _match_moves(
    session: Session, gone_ids: list[int], new_files: list[AudioFile], stats: ScanStats
) -> set[int]:
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
        stats.conflicts += conflicts.detect(session, old, fields["tag"])  # and retagged
        for source, values in fields.items():
            set_values(session, old, source, values)
        moved.add(old.id)
    return moved
