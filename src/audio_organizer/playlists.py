"""Smart and static playlists.

A smart playlist's rule is a list of conditions that must all hold, e.g.

    genre=Rock|Pop preference="Very Good|Excellent" year>=1990 title!~live

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
import shlex
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import AudioFile, Library, Playlist, PlaylistEntry
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
    """Generates many playlists in one pass: feed() every track, then results()."""

    def __init__(self, playlists: Iterable[Playlist]) -> None:
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
                tracks = order(tracks, playlist.sort, seed=name)[: playlist.max_tracks]
                results.append(GeneratedPlaylist(name, playlist, tracks))
        for playlist in self.static:
            found = self.static_tracks.get(playlist.id, {})
            tracks = [found[e.position] for e in playlist.entries if e.position in found]
            tracks = order(tracks, playlist.sort, seed=playlist.name)[: playlist.max_tracks]
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


def path_index(session: Session, library: Library) -> dict[str, int]:
    """Library paths (NFC, case-folded) to file ids, for matching imported entries."""
    return {
        _fold(path): file_id
        for file_id, path in session.execute(
            select(AudioFile.id, AudioFile.path).where(present(library.id))
        )
    }


def resolve_entry(entry: str, library: Library, base: Path, index: dict[str, int]) -> int | None:
    """Find the file an m3u entry refers to: an absolute path inside the library, a path
    relative to the library root, or one relative to the playlist's own folder."""
    root = Path(library.root)
    candidates = []
    path = Path(entry)
    if path.is_absolute():
        candidates.append(path)
    else:
        candidates.append(root / entry)
        candidates.append(base / entry)
    for candidate in candidates:
        try:
            relpath = Path(os.path.normpath(candidate)).relative_to(root).as_posix()
        except ValueError:
            continue
        file_id = index.get(_fold(relpath))
        if file_id is not None:
            return file_id
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


def set_entries(playlist: Playlist, entries: list[tuple[str, int | None]]) -> None:
    """Replace a static playlist's entries with (path, file id or None) pairs."""
    playlist.entries = [
        PlaylistEntry(position=i, path=path, file_id=file_id)
        for i, (path, file_id) in enumerate(entries)
    ]


def import_m3u(
    session: Session, library: Library, m3u: Path, name: str | None = None
) -> tuple[Playlist, list[str]]:
    """Create a static playlist from an .m3u file. Returns it and the unmatched entries,
    which are kept so they can be matched later."""
    name = name or m3u.stem
    if session.scalar(
        select(Playlist).where(Playlist.library_id == library.id, Playlist.name == name)
    ):
        raise RuleError(f"There is already a playlist named {name!r}")
    index = path_index(session, library)
    base = m3u.parent.resolve()
    pairs = [(entry, resolve_entry(entry, library, base, index)) for entry in read_m3u(m3u)]
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
