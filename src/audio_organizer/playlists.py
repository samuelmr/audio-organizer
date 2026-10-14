"""Smart and static playlists.

A smart playlist's rule is a list of conditions that must all hold, e.g.

    genre=Rock|Pop rating>=4 year>=1990 title!~live

Operators: = (equals), != (does not equal), ~ (contains), !~ (does not contain), all
case-insensitive, with alternatives separated by |; and >, >=, <, <= for numbers. An
empty value stands for a missing field ("album=" matches tracks without an album).
Values with spaces need no quotes (album=Moon Safari) unless they contain operators.
Multi-valued fields ("Rock; Pop") match if any of their values does. Fields are the
tag fields plus year, path, format, duration (s) and bitrate (kbps).

All playlists of a library are generated in one pass over its tracks (PlaylistSet), so
the cost does not grow with the number of playlists.
"""

import hashlib
import os
import re
import secrets
import shlex
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from sqlalchemy import select
from sqlalchemy.orm import Session

from .changes import TRASH_DIR
from .models import AudioFile, Change, Library, Playlist, PlaylistEntry
from .pathparse import normalize_name
from .playlist_formats import render
from .tags import FIELDS
from .tracks import Track, iter_tracks, present

RULE_FIELDS = (*FIELDS, "year", "path", "format", "duration", "bitrate")
SORTS = ("shuffle", "album", "artist", "title", "added", "path", "manual")
TERM = re.compile(r"^(?P<field>[a-z_]+)(?P<op>!=|!~|>=|<=|=|~|>|<)(?P<value>.*)$", re.DOTALL)
NUMERIC_OPS = (">", ">=", "<", "<=")


class RuleError(ValueError):
    pass


@dataclass(frozen=True)
class Condition:
    field: str
    op: str
    values: tuple[str, ...]

    def matches(self, track: Track) -> bool:
        raw = track.get(self.field)
        if self.op in NUMERIC_OPS:
            number = _number(raw)
            if number is None:
                return False
            limit = float(self.values[0])
            return {
                ">": number > limit, ">=": number >= limit,
                "<": number < limit, "<=": number <= limit,
            }[self.op]  # fmt: skip
        parts = [_fold(p) for p in (raw or "").split("; ") if p.strip()]
        if self.op in ("=", "!="):
            hit = any((v == "" and not parts) or v in parts for v in self.values)
        else:
            hit = any(v and any(v in p for p in parts) for v in self.values)
        return hit if self.op in ("=", "~") else not hit


def parse_rule(rule: str) -> list[Condition]:
    try:
        terms = shlex.split(rule)
    except ValueError as e:
        raise RuleError(f"Cannot parse rule: {e}") from None
    if not terms:
        raise RuleError("The rule is empty")
    # A word without an operator continues the previous value: album=Moon Safari
    merged: list[str] = []
    for term in terms:
        if merged and not TERM.match(term):
            merged[-1] += " " + term
        else:
            merged.append(term)
    conditions = []
    for term in merged:
        match = TERM.match(term)
        if not match:
            raise RuleError(f"Not a condition: {term!r} (expected field=value, field>=number, ...)")
        name, op, value = match["field"], match["op"], match["value"]
        if name not in RULE_FIELDS:
            raise RuleError(f"Unknown field {name!r}; fields: {', '.join(RULE_FIELDS)}")
        if op in NUMERIC_OPS:
            if _number(value) is None:
                raise RuleError(f"{term!r}: {op} needs a number")
            values = (value,)
        else:
            values = tuple(_fold(v) for v in value.split("|"))
        conditions.append(Condition(name, op, values))
    return conditions


def kind(playlist: Playlist) -> str:
    return "smart" if playlist.is_smart else "static"


def name_taken(
    session: Session, library: Library, name: str, *, smart: bool, except_id: int | None = None
) -> bool:
    """Whether a playlist of this kind already has the name. A smart and a static playlist
    may share one: a static playlist imported from the file a smart one wrote."""
    query = select(Playlist.id).where(Playlist.library_id == library.id, Playlist.name == name)
    query = query.where(Playlist.rule.is_not(None) if smart else Playlist.rule.is_(None))
    if except_id is not None:
        query = query.where(Playlist.id != except_id)
    return session.scalar(query) is not None


def shared_names(playlists: Iterable[Playlist]) -> set[str]:
    """The names that a smart and a static playlist both have."""
    playlists = list(playlists)
    smart = {p.name for p in playlists if p.is_smart}
    return smart & {p.name for p in playlists if not p.is_smart}


def display_name(playlist: Playlist, shared: set[str]) -> str:
    """The playlist's name, with its kind added when another kind has the same name."""
    return f"{playlist.name} ({kind(playlist)})" if playlist.name in shared else playlist.name


def find(session: Session, library: Library, name: str) -> Playlist | None:
    """A playlist by name. When a smart and a static one share it, "Name (smart)" or
    "Name (static)" picks one; the bare name is then ambiguous (RuleError)."""
    matches = list(
        session.scalars(
            select(Playlist).where(Playlist.library_id == library.id, Playlist.name == name)
        )
    )
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise RuleError(
            f"There is a smart and a static playlist named {name!r}; "
            f"say '{name} (smart)' or '{name} (static)'"
        )
    for suffix in (" (smart)", " (static)"):
        if name.endswith(suffix):
            base = name.removesuffix(suffix)
            wanted = suffix.strip(" ()")
            return next(
                (
                    p
                    for p in session.scalars(
                        select(Playlist).where(
                            Playlist.library_id == library.id, Playlist.name == base
                        )
                    )
                    if kind(p) == wanted
                ),
                None,
            )
    return None


def validate(playlist: Playlist) -> None:
    """Raise RuleError if the playlist's settings are inconsistent."""
    if playlist.sort not in SORTS:
        raise RuleError(f"Unknown sort {playlist.sort!r}; one of {', '.join(SORTS)}")
    if playlist.rule is None:
        if playlist.group_by:
            raise RuleError("Only smart playlists can be grouped")
        return
    parse_rule(playlist.rule)
    if playlist.group_by:
        if playlist.group_by not in RULE_FIELDS:
            raise RuleError(f"Unknown field {playlist.group_by!r} to group by")
        if "{" + playlist.group_by + "}" not in playlist.name:
            raise RuleError(
                f"The name of a playlist grouped by {playlist.group_by} must contain "
                f"{{{playlist.group_by}}}, e.g. 'Best Of {{{playlist.group_by}}}'"
            )


@dataclass
class GeneratedPlaylist:
    name: str
    playlist: Playlist
    tracks: list[Track] = field(default_factory=list)
    unresolved: int = 0  # static entries that match no file in the library


class PlaylistSet:
    """Generates many playlists in one pass: feed() every track, then results().

    Shuffled playlists get a new order each time a set is made (a fresh random salt), so
    regenerating a playlist reshuffles it; within one set the order is fixed."""

    def __init__(self, playlists: Iterable[Playlist]) -> None:
        self.salt = secrets.token_hex(8)
        self.smart: list[tuple[Playlist, list[Condition]]] = []
        self.static: list[Playlist] = []
        self.members: dict[int, list[tuple[int, int]]] = {}  # file id -> (playlist id, pos)
        for playlist in playlists:
            if playlist.is_smart:
                self.smart.append((playlist, parse_rule(playlist.rule)))
            else:
                self.static.append(playlist)
                for entry in playlist.entries:
                    if entry.file_id is not None:
                        self.members.setdefault(entry.file_id, []).append(
                            (playlist.id, entry.position)
                        )
        self.matches: dict[tuple[int, str | None], list[Track]] = {}
        self.group_names: dict[tuple[int, str], str] = {}
        self.static_tracks: dict[int, dict[int, Track]] = {}

    def feed(self, track: Track) -> None:
        for playlist, conditions in self.smart:
            if not all(c.matches(track) for c in conditions):
                continue
            if not playlist.group_by:
                self.matches.setdefault((playlist.id, None), []).append(track)
                continue
            for value in (track.get(playlist.group_by) or "").split("; "):
                value = value.strip()
                if value:
                    key = (playlist.id, _fold(value))
                    self.group_names.setdefault(key, value)
                    self.matches.setdefault(key, []).append(track)
        for playlist_id, position in self.members.get(track.file_id, ()):
            self.static_tracks.setdefault(playlist_id, {})[position] = track

    def results(self) -> list[GeneratedPlaylist]:
        results = []
        for playlist, _ in self.smart:
            keys = [k for k in self.matches if k[0] == playlist.id]
            if not playlist.group_by and not keys:
                keys = [(playlist.id, None)]
            for key in keys:
                name = playlist.name
                if key[1] is not None:
                    name = name.replace("{" + playlist.group_by + "}", self.group_names[key])
                tracks = _dedupe(self.matches.get(key, []))
                tracks = order(tracks, playlist.sort, seed=f"{self.salt}\0{name}")[
                    : playlist.max_tracks
                ]
                results.append(GeneratedPlaylist(name, playlist, tracks))
        for playlist in self.static:
            found = self.static_tracks.get(playlist.id, {})
            tracks = [found[e.position] for e in playlist.entries if e.position in found]
            tracks = order(tracks, playlist.sort, seed=f"{self.salt}\0{playlist.name}")[
                : playlist.max_tracks
            ]
            results.append(
                GeneratedPlaylist(
                    playlist.name, playlist, tracks, len(playlist.entries) - len(found)
                )
            )
        return sorted(results, key=lambda r: r.name.casefold())


def generate(
    session: Session, library: Library, playlists: Iterable[Playlist] | None = None
) -> list[GeneratedPlaylist]:
    if playlists is None:
        playlists = library_playlists(session, library)
    playlist_set = PlaylistSet(playlists)
    for track in iter_tracks(session, library.id):
        playlist_set.feed(track)
    return playlist_set.results()


def library_playlists(session: Session, library: Library) -> list[Playlist]:
    return list(
        session.scalars(
            select(Playlist).where(Playlist.library_id == library.id).order_by(Playlist.name)
        )
    )


def order(tracks: list[Track], sort: str, *, seed: str = "") -> list[Track]:
    """Sort tracks. 'shuffle' is a stable shuffle: the same tracks always come out in the
    same order for the same seed, and adding a track does not reorder the others."""
    if sort == "manual":
        return list(tracks)
    if sort == "shuffle":
        return sorted(tracks, key=lambda t: _shuffle_key(seed, t))
    if sort == "added":
        return sorted(
            tracks, key=lambda t: (-(t.first_seen.timestamp() if t.first_seen else 0), t.path)
        )
    if sort == "path":
        return sorted(tracks, key=lambda t: t.path)
    if sort == "title":
        return sorted(tracks, key=lambda t: (_fold(t.get("title")), _fold(t.get("artist"))))

    def position(t: Track) -> tuple:
        return (_number(t.get("discnumber")) or 0, _number(t.get("tracknumber")) or 0, t.path)

    if sort == "album":
        # Album first, so that the tracks of an album by several artists (soundtracks,
        # compilations) stay together; the album artist separates same-named albums.
        return sorted(
            tracks, key=lambda t: (_fold(t.get("album")), _fold(t.get("albumartist")), *position(t))
        )
    return sorted(  # artist
        tracks, key=lambda t: (_fold(t.get("artist")), _fold(t.get("album")), *position(t))
    )


def _dedupe(tracks: list[Track]) -> list[Track]:
    """One track per song: identical audio, or the same artist and title, count as the
    same. The best copy is kept: one with album and track number, then higher bitrate."""
    ranked = sorted(
        tracks,
        key=lambda t: (not t.get("album"), not t.get("tracknumber"), -(t.bitrate or 0), t.path),
    )
    seen: set[str] = set()
    kept = []
    for track in ranked:
        keys = {f"hash:{track.audio_hash}"} if track.audio_hash else set()
        artist, title = track.get("artist"), track.get("title")
        if artist and title:
            keys.add(f"song:{normalize_name(artist)}\0{normalize_name(title)}")
        if keys & seen:
            continue
        seen |= keys
        kept.append(track)
    return kept


def _shuffle_key(seed: str, track: Track) -> str:
    identity = track.audio_hash or track.path
    return hashlib.sha1(f"{seed}\0{identity}".encode()).hexdigest()


def _fold(value: str | None) -> str:
    return unicodedata.normalize("NFC", (value or "").strip()).casefold()


def _number(value: str | None) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except ValueError:
        return None


# ---- static playlists ---------------------------------------------------------------


PLAYLIST_SUFFIXES = (".m3u", ".m3u8")


class PathIndex:
    """The library's files by path, for matching playlist entries.

    A path matches exactly (ignoring case and Unicode composition) or, failing that,
    loosely: every folder and file name compared with normalize_name, which ignores a
    leading "The", punctuation and accents ("The KLF/The White Room/…" finds
    "KLF/The White Room/…"). A loose match is only accepted when one file fits.

    Failing both, a path where a file used to be leads to the file that now stands for
    it (see path_history), so that a playlist written before folders were renamed or
    duplicates trashed still finds its tracks."""

    def __init__(self, session: Session, library: Library) -> None:
        self.session, self.library = session, library
        self.root = Path(library.root)
        self.exact: dict[str, int] = {}
        self.loose: dict[str, int | None] = {}  # None: several files fit
        self._history: dict[str, int | None] | None = None  # read when first needed
        for file_id, path in session.execute(
            select(AudioFile.id, AudioFile.path).where(present(library.id))
        ):
            self.exact[_fold(path)] = file_id
            key = _loose_key(path)
            self.loose[key] = None if key in self.loose else file_id

    def find(self, entry: str, base: Path) -> int | None:
        """The file an entry refers to: an absolute path inside the library, a path
        relative to the library root, or one relative to the playlist's folder (base)."""
        file_id = self.find_here(entry, base)
        return file_id if file_id is not None else self.find_moved(entry, base)

    def find_here(self, entry: str, base: Path) -> int | None:
        """The file that is at the entry's path now, exactly or loosely."""
        relpaths = self._relpaths(entry, base)
        for relpath in relpaths:
            if (file_id := self.exact.get(_fold(relpath))) is not None:
                return file_id
        for relpath in relpaths:
            if (file_id := self.loose.get(_loose_key(relpath))) is not None:
                return file_id
        return None

    def find_moved(self, entry: str, base: Path) -> int | None:
        """The file that stands for one that used to be at the entry's path."""
        if self._history is None:
            self._history = path_history(self.session, self.library, set(self.exact.values()))
        for relpath in self._relpaths(entry, base):
            if (file_id := self._history.get(_fold(relpath))) is not None:
                return file_id
        return None

    def _relpaths(self, entry: str, base: Path) -> list[str]:
        return [r for r in (self._relative(c) for c in _candidates(entry, self.root, base)) if r]

    def _relative(self, path: Path) -> str | None:
        try:
            return path.relative_to(self.root).as_posix()
        except ValueError:
            return None


def _candidates(entry: str, root: Path, base: Path) -> list[Path]:
    path = Path(entry)
    paths = [path] if path.is_absolute() else [root / entry, base / entry]
    return [Path(os.path.normpath(p)) for p in paths]


def _loose_key(relpath: str) -> str:
    return "/".join(normalize_name(part) for part in Path(relpath).parts)


def path_history(
    session: Session, library: Library, present_ids: set[int]
) -> dict[str, int | None]:
    """Where the library's files went: for each path (folded) that a file once had, the
    readable file that now stands for it, or None when it leads to more than one.

    A path leads to the file that was moved away from it by an applied change, to the copy
    kept in place of a file trashed from it, and from a file that is gone (deleted outside
    aorg, or a move that a scan did not see whole) to the one readable file with the same
    audio. These steps are followed one after another: a moved file trashed later leads to
    its replacement."""
    replaced: dict[int, int] = {}  # trashed file -> the copy kept in its place
    claims: list[tuple[str, int]] = []  # (old path, the file that was there)
    for kind, file_id, payload, undo in session.execute(
        select(Change.kind, Change.file_id, Change.payload, Change.undo)
        .join(AudioFile, Change.file_id == AudioFile.id)
        .where(
            AudioFile.library_id == library.id,
            Change.status == "applied",
            Change.kind.in_(("move", "trash")),
        )
        .order_by(Change.id)
    ):
        undo = undo or {}
        if kind == "trash":
            if payload.get("replacement") is not None:
                replaced[file_id] = payload["replacement"]
            # Older trash changes recorded only the path in the trash
            old = undo.get("from") or _untrashed(undo.get("trash") or "")
        else:
            old = undo.get("from")
        if old:
            claims.append((old, file_id))
    hashes: dict[int, str] = {}
    for file_id, path, audio_hash in session.execute(
        select(AudioFile.id, AudioFile.path, AudioFile.audio_hash).where(
            AudioFile.library_id == library.id, AudioFile.missing
        )
    ):
        claims.append((_untrashed(path) or path, file_id))
        if audio_hash:
            hashes[file_id] = audio_hash
    same_audio = _present_by_hash(session, library, set(hashes.values()))

    def current(file_id: int) -> int | None:
        start, seen = file_id, set()
        while file_id not in present_ids and file_id in replaced and file_id not in seen:
            seen.add(file_id)
            file_id = replaced[file_id]
        if file_id in present_ids:
            return file_id
        return same_audio.get(hashes.get(start, ""))

    history: dict[str, int | None] = {}
    for path, file_id in claims:
        found = current(file_id)
        if found is None:
            continue
        key = _fold(path)
        history[key] = found if history.get(key, found) == found else None
    return history


def _untrashed(path: str) -> str | None:
    """The path a file had before it was trashed to TRASH_DIR/<changeset id>/<path>."""
    parts = PurePosixPath(path).parts
    if len(parts) > 2 and parts[0] == TRASH_DIR:
        return PurePosixPath(*parts[2:]).as_posix()
    return None


def read_m3u(path: Path) -> list[str]:
    data = path.read_bytes()
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("latin-1")
    entries = []
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            entries.append(line.removeprefix("file://"))
    return entries


def read_entries(
    m3u: Path, index: PathIndex, _seen: set[Path] | None = None
) -> list[tuple[str, int | None]]:
    """(entry, matching file id or None) for each entry of an .m3u file. Entries that are
    playlists themselves (such as an album's playlist) are replaced by their entries."""
    m3u = m3u.resolve()
    seen = (_seen or set()) | {m3u}
    pairs: list[tuple[str, int | None]] = []
    for entry in read_m3u(m3u):
        if entry.lower().endswith(PLAYLIST_SUFFIXES):
            nested = next(
                (p for p in _candidates(entry, index.root, m3u.parent) if p.is_file()), None
            )
            if nested is not None and nested.resolve() not in seen:
                pairs.extend(read_entries(nested, index, seen))
                continue
        pairs.append((entry, index.find(entry, m3u.parent)))
    return pairs


def set_entries(playlist: Playlist, entries: list[tuple[str, int | None]]) -> None:
    """Replace a static playlist's entries with (path, file id or None) pairs."""
    playlist.entries = [
        PlaylistEntry(position=i, path=path, file_id=file_id)
        for i, (path, file_id) in enumerate(entries)
    ]


@dataclass
class Repeats:
    """The entries of a static playlist that name a track again."""

    playlist: Playlist
    # (entry to drop, the earlier entry it repeats)
    dropped: list[tuple[PlaylistEntry, PlaylistEntry]] = field(default_factory=list)


def find_repeats(session: Session, playlists: Iterable[Playlist]) -> list[Repeats]:
    """The static playlists that name one track more than once, and which entries repeat.

    Entries count as the same track when they refer to the same file or to files with
    identical audio, which is what a trash of duplicate copies leaves behind (both copies
    were in the playlist, and both entries now point at the kept file), or an import of an
    old playlist that listed a song under two names. An entry that matched no file is the
    same when its path is. Of each track the first entry stays, so the order the playlist
    was given is kept. Smart playlists hold one copy of each song anyway. Different
    recordings of one song (a live take, another edition) are not touched."""
    playlists = [p for p in playlists if not p.is_smart]
    ids = {e.file_id for p in playlists for e in p.entries if e.file_id is not None}
    hashes: dict[int, str | None] = {}
    wanted = sorted(ids)
    for start in range(0, len(wanted), 500):
        rows = session.execute(
            select(AudioFile.id, AudioFile.audio_hash).where(
                AudioFile.id.in_(wanted[start : start + 500])
            )
        )
        hashes.update(rows.all())
    found = []
    for playlist in playlists:
        seen: dict[tuple[str, str | int], PlaylistEntry] = {}
        repeats = Repeats(playlist)
        for entry in sorted(playlist.entries, key=lambda e: e.position):
            if entry.file_id is None:
                key = ("path", entry.path)
            elif hashes.get(entry.file_id):
                key = ("audio", hashes[entry.file_id])
            else:
                key = ("file", entry.file_id)
            if key in seen:
                repeats.dropped.append((entry, seen[key]))
            else:
                seen[key] = entry
        if repeats.dropped:
            found.append(repeats)
    return found


def collapse(session: Session, repeats: Iterable[Repeats]) -> int:
    """Remove the repeated entries found by find_repeats and number the rest again.
    The entries that stay are the same rows, so what refers to them keeps working.
    Returns how many entries were removed. The caller commits."""
    removed = 0
    for found in repeats:
        removed += _remove_entries(found.playlist, {entry.id for entry, _ in found.dropped})
    session.flush()
    return removed


def _remove_entries(playlist: Playlist, gone: set[int]) -> int:
    """Remove entries by id and number the rest again, keeping their order and rows."""
    kept = [e for e in sorted(playlist.entries, key=lambda e: e.position) if e.id not in gone]
    for position, entry in enumerate(kept):
        entry.position = position
    removed = len(playlist.entries) - len(kept)
    playlist.entries = kept
    return removed


# ---- entries whose track is not found ---------------------------------------------------


def missing_entries(session: Session, playlist: Playlist) -> list[PlaylistEntry]:
    """The entries of a static playlist that the generated playlist leaves out: those that
    matched no file when imported, and those whose file has gone or cannot be read."""
    ids = sorted({e.file_id for e in playlist.entries if e.file_id is not None})
    found: set[int] = set()
    for start in range(0, len(ids), 500):
        found.update(
            session.scalars(
                select(AudioFile.id).where(
                    AudioFile.id.in_(ids[start : start + 500]), present(playlist.library_id)
                )
            )
        )
    return [e for e in sorted(playlist.entries, key=lambda e: e.position)
            if e.file_id not in found]  # fmt: skip


@dataclass
class Located:
    """A file proposed for an entry whose track was not found, and how it was found."""

    entry: PlaylistEntry
    file_id: int
    path: str
    how: str
    sure: bool  # False: only the file name fits, so it may be another recording


def locate_missing(
    session: Session, library: Library, entries: Iterable[PlaylistEntry]
) -> list[Located]:
    """Look for the files of entries whose track is not found. In this order:

    - the entry's path, or the path its file had, matched as an import matches it
      (exactly, or loosely when only one file fits); a relative path counts from the
      library root or the playlist folder;
    - the one file with the same audio as the file the entry had, e.g. a copy elsewhere;
    - the file that stands for one that used to be at the path (see path_history): where
      aorg moved it, the copy kept when it was trashed, or the one other copy of its audio;
    - a file with the same name (ignoring case, punctuation, accents and the extension,
      so that a re-encoded file fits too) whose folders best match the end of the entry's
      path: what a playlist made on another computer, or before a reorganisation, needs.
      The best match must be the only one; when only the file name fits it is not sure.

    Entries for which nothing is found are left out."""
    entries = list(entries)
    if not entries:
        return []
    index = PathIndex(session, library)
    base = Path(library.root, library.playlist_folder)
    old_ids = sorted({e.file_id for e in entries if e.file_id is not None})
    old: dict[int, tuple[str, str | None]] = {}
    for start in range(0, len(old_ids), 500):
        rows = session.execute(
            select(AudioFile.id, AudioFile.path, AudioFile.audio_hash).where(
                AudioFile.id.in_(old_ids[start : start + 500])
            )
        )
        old.update((file_id, (path, audio_hash)) for file_id, path, audio_hash in rows)
    same_audio = _present_by_hash(session, library, {h for _, h in old.values() if h})
    names: _NameIndex | None = None  # built when needed, as it reads every path again
    proposals: list[tuple[PlaylistEntry, int, str, bool]] = []
    for entry in entries:
        old_path, old_hash = old.get(entry.file_id, (None, None))
        paths = [entry.path] + ([old_path] if old_path and old_path != entry.path else [])
        file_id = _first(index.find_here(p, base) for p in paths)
        if file_id is not None:
            proposals.append((entry, file_id, "path", True))
            continue
        if old_hash and (file_id := same_audio.get(old_hash)) is not None:
            proposals.append((entry, file_id, "same audio", True))
            continue
        if (file_id := _first(index.find_moved(p, base) for p in paths)) is not None:
            proposals.append((entry, file_id, "where its file went", True))
            continue
        names = names or _NameIndex(session, library)
        for p in paths:
            if (match := names.find(p)) is not None:
                proposals.append((entry, *match))
                break
    found_paths = _paths(session, {file_id for _, file_id, _, _ in proposals})
    return [
        Located(entry, file_id, found_paths[file_id], how, sure)
        for entry, file_id, how, sure in proposals
    ]


def _first(ids: Iterable[int | None]) -> int | None:
    return next((i for i in ids if i is not None), None)


def _present_by_hash(session: Session, library: Library, hashes: set[str]) -> dict[str, int]:
    """For each audio hash, the readable file that has it, when there is only one."""
    found: dict[str, int | None] = {}
    wanted = sorted(hashes)
    for start in range(0, len(wanted), 500):
        for file_id, audio_hash in session.execute(
            select(AudioFile.id, AudioFile.audio_hash).where(
                AudioFile.audio_hash.in_(wanted[start : start + 500]), present(library.id)
            )
        ):
            found[audio_hash] = None if audio_hash in found else file_id
    return {h: file_id for h, file_id in found.items() if file_id is not None}


def _paths(session: Session, ids: set[int]) -> dict[int, str]:
    wanted = sorted(ids)
    paths: dict[int, str] = {}
    for start in range(0, len(wanted), 500):
        rows = session.execute(
            select(AudioFile.id, AudioFile.path).where(
                AudioFile.id.in_(wanted[start : start + 500])
            )
        )
        paths.update((file_id, path) for file_id, path in rows)
    return paths


class _NameIndex:
    """The library's readable files by loose file name without extension, with the loose
    names of their folders, for finding a file by the end of a path."""

    def __init__(self, session: Session, library: Library) -> None:
        self.files: dict[str, list[tuple[int, tuple[str, ...], str]]] = {}
        for file_id, path in session.execute(
            select(AudioFile.id, AudioFile.path).where(present(library.id))
        ):
            folders, stem, suffix = _name_parts(path)
            self.files.setdefault(stem, []).append((file_id, folders, suffix))

    def find(self, entry: str) -> tuple[int, str, bool] | None:
        """(file id, how it was found, whether it is sure) of the only best match. Files
        score by how many of the folders above them match, then by the same extension."""
        folders, stem, suffix = _name_parts(entry)
        scored = []
        for file_id, their_folders, their_suffix in self.files.get(stem, ()):
            same = 0
            for mine, theirs in zip(reversed(folders), reversed(their_folders), strict=False):
                if mine != theirs:
                    break
                same += 1
            scored.append(((same, suffix == their_suffix), file_id))
        if not scored:
            return None
        scored.sort(reverse=True)
        if len(scored) > 1 and scored[1][0] == scored[0][0]:
            return None
        (same, _), file_id = scored[0]
        if not same:
            return file_id, "file name", False
        return (
            file_id,
            "file name and folder" if same == 1 else f"file name and {same} folders",
            True,
        )


def _name_parts(path: str) -> tuple[tuple[str, ...], str, str]:
    """(loose folder names, loose file name without extension, extension) of a path,
    which may be a Windows one."""
    parts = [p for p in path.replace("\\", "/").split("/") if p not in ("", ".", "..")]
    name = parts.pop() if parts else ""
    stem, dot, suffix = name.rpartition(".")
    if not dot:
        stem, suffix = name, ""
    return tuple(normalize_name(p) for p in parts), normalize_name(stem), suffix.casefold()


def relink(session: Session, located: Iterable[Located]) -> int:
    """Point entries at the files found for them; returns how many. The entry's path
    becomes the file's, as if the track had been added now. The caller commits."""
    changed = 0
    for item in located:
        item.entry.file_id, item.entry.path = item.file_id, item.path
        changed += 1
    session.flush()
    return changed


def drop_missing(session: Session, playlist: Playlist) -> int:
    """Remove the entries whose track is not found and number the rest again; returns how
    many were removed. The caller commits."""
    removed = _remove_entries(playlist, {e.id for e in missing_entries(session, playlist)})
    session.flush()
    return removed


def import_m3u(
    session: Session, library: Library, m3u: Path, name: str | None = None
) -> tuple[Playlist, list[str]]:
    """Create a static playlist from an .m3u file. Returns it and the unmatched entries,
    which are kept so they can be matched later."""
    name = name or m3u.stem
    if name_taken(session, library, name, smart=False):
        raise RuleError(f"There is already a static playlist named {name!r}")
    pairs = read_entries(m3u, PathIndex(session, library))
    playlist = Playlist(library=library, name=name, sort="manual")
    set_entries(playlist, pairs)
    session.add(playlist)
    return playlist, [entry for entry, file_id in pairs if file_id is None]


# ---- export ---------------------------------------------------------------------------


def safe_filename(name: str) -> str:
    """A playlist name as a file name (or object key) component."""
    name = re.sub(r"[/\\:\0]", "-", unicodedata.normalize("NFC", name)).strip()
    return name.lstrip(".") or "playlist"


def export(
    generated: Iterable[GeneratedPlaylist], root: Path, out_dir: Path, formats: Iterable[str]
) -> list[Path]:
    """Write playlist files into out_dir, referring to tracks by paths relative to it."""
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for playlist in generated:
        entries = [
            (t, Path(os.path.relpath(root / t.path, out_dir)).as_posix()) for t in playlist.tracks
        ]
        for fmt in formats:
            path = out_dir / f"{safe_filename(playlist.name)}.{fmt}"
            path.write_text(render(fmt, playlist.name, entries), encoding="utf-8")
            written.append(path)
    return written
