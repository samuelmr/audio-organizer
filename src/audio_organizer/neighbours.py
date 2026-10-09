"""Putting loose tracks back into their album folders.

Old flat folders hold tracks whose album folder exists, with a gap where the track
belongs: "Williams, John/John Williams_Cantina Band.mp3" is track 64 of the album in
"John Williams/Star Wars Trilogy_ The Original Soundtrack Anthology". A loose track (in
an artist folder or "Unknown Album") is proposed to join an album folder when

- its album tag names exactly one album folder (by the folder's name or its tracks'
  album tags, ignoring case, punctuation and a leading "The") of one of its artists,
- its disc and track number is free there, and no track there has its title, and
- no other loose track claims the same number.

Tracks with identical audio in an album folder are left to `duplicates`, and another
encoding of a track already there is the album's own track (see `encodings`).

The track is named like its new neighbours ("07 Title.mp3", or "1-07 Title.mp3") and
gets the album-wide tags they agree on (album artist, year, totals, ...), so it sits in
the album as if it had always been there.
"""

import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import changes as ch
from . import duplicates as du
from .encodings import Track, load_tracks
from .layout import file_name
from .models import Change, Changeset, Library
from .pathparse import normalize_name

# Album-wide tags a moved track takes from its neighbours, when they all agree; a genre
# only when it has none, as genres can differ from track to track
ALBUM_FIELDS = ("album", "albumartist", "albumartistsort", "tracktotal", "disctotal", "date",
                "originaldate", "compilation", "genre")  # fmt: skip
FILL_ONLY = ("genre",)
DISC_PREFIX = re.compile(r"^\d{1,2}-\d{1,3}\s")
NUMBER_PREFIX = re.compile(r"^\d{1,3}(?:\s*[-.]\s*|\s+)")


@dataclass
class Move:
    copy: du.Copy
    to: str
    fill: dict[str, str]


@dataclass
class Album:
    """An album folder and the loose tracks proposed to join it."""

    folder: str
    tracks: int  # tracks already in the folder
    moves: list[Move] = field(default_factory=list)


@dataclass
class Survey:
    albums: list[Album] = field(default_factory=list)
    loose: int = 0  # loose tracks considered
    no_album_tag: int = 0
    no_folder: int = 0  # the album tag names no album folder of the artist
    several_folders: int = 0
    duplicates: int = 0  # identical audio in an album folder
    taken: int = 0  # the number or title is already in the folder
    no_number: int = 0
    conflicts: int = 0  # loose tracks claiming the same number
    pending: int = 0

    @property
    def move_count(self) -> int:
        return sum(len(a.moves) for a in self.albums)


def survey(session: Session, library: Library, *, path_prefix: str | None = None) -> Survey:
    result = Survey()
    tracks = load_tracks(session, library)
    folders: dict[str, list[Track]] = {}
    for track in tracks:
        if track.album_folder is not None:
            folders.setdefault(track.album_folder, []).append(track)
    index: dict[tuple[str, str], set[str]] = {}
    for folder, members in folders.items():
        albums = {normalize_name(members[0].folder_album or "")}
        albums |= {normalize_name(t.album) for t in members if t.album}
        for artist in set().union(*(t.artists for t in members)):
            for album in albums - {""}:
                index.setdefault((artist, album), set()).add(folder)
    in_albums = {t.file.audio_hash for t in tracks if t.album_folder is not None}
    pending = set(
        session.scalars(select(Change.file_id).where(Change.status.in_(("proposed", "approved"))))
    )

    claims: dict[tuple[str, int, int], list[Track]] = {}
    for track in tracks:
        if track.album_folder is not None:
            continue
        if path_prefix and not track.file.path.startswith(path_prefix):
            continue
        result.loose += 1
        if track.file.audio_hash in in_albums:
            result.duplicates += 1
            continue
        if not track.album:
            result.no_album_tag += 1
            continue
        candidates = set().union(
            *(index.get((a, normalize_name(track.album)), set()) for a in track.artists)
        )
        if not candidates:
            result.no_folder += 1
            continue
        if len(candidates) > 1:
            result.several_folders += 1
            continue
        [folder] = candidates
        members = folders[folder]
        if not track.tracknumber:
            result.no_number += 1
            continue
        slot = (track.discnumber or 1, track.tracknumber)
        if slot in {(t.discnumber or 1, t.tracknumber) for t in members} or normalize_name(
            track.title
        ) in {normalize_name(t.title) for t in members}:
            result.taken += 1
            continue
        if track.file.id in pending or any(t.file.id in pending for t in members):
            result.pending += 1
            continue
        claims.setdefault((folder, *slot), []).append(track)

    chosen: dict[str, list[Track]] = {}
    for (folder, _, _), claimants in claims.items():
        if len(claimants) > 1:
            result.conflicts += len(claimants)
        else:
            chosen.setdefault(folder, []).extend(claimants)
    neighbours = [t.file for folder in chosen for t in folders[folder]]
    moving = [t.file for ts in chosen.values() for t in ts]
    copies = {c.file.id: c for c in du.load_copies(session, neighbours + moving)}
    for folder, movers in sorted(chosen.items()):
        members = [copies[t.file.id] for t in folders[folder]]
        album = Album(folder, len(members))
        agreed = _agreed(members)
        taken = {PurePosixPath(m.file.path).name.casefold() for m in members}
        for track in sorted(movers, key=lambda t: (t.discnumber or 1, t.tracknumber)):
            copy = copies[track.file.id]
            name = _name(members, track) + PurePosixPath(track.file.path).suffix
            if name.casefold() in taken:
                result.taken += 1
                continue
            taken.add(name.casefold())
            fill = {
                k: v
                for k, v in agreed.items()
                if copy.tags.get(k) != v and not (k in FILL_ONLY and k in copy.tags)
            }
            album.moves.append(Move(copy, f"{folder}/{name}", fill))
        if album.moves:
            result.albums.append(album)
    return result


def batch(albums: list[Album], limit: int) -> list[Album]:
    """Whole albums, up to limit tracks (at least one album)."""
    chosen, count = [], 0
    for album in albums:
        if chosen and count + len(album.moves) > limit:
            break
        chosen.append(album)
        count += len(album.moves)
    return chosen


def propose(session: Session, albums: list[Album], description: str) -> Changeset:
    """One changeset: for each track, its album tags, then the move into the folder."""
    changeset = ch.new_changeset(session, description, "neighbours")
    for album in albums:
        for move in album.moves:
            if move.fill:
                ch.propose_retag(changeset, move.copy.file, move.fill)
            ch.propose_move(changeset, move.copy.file, move.to)
    session.commit()
    return changeset


def _agreed(members: list[du.Copy]) -> dict[str, str]:
    """The album-wide tags all of the folder's tracks have, with the same value."""
    agreed = {}
    for name in ALBUM_FIELDS:
        values = Counter(m.tags.get(name) for m in members)
        if len(values) == 1 and None not in values:
            [agreed[name]] = values
    return agreed


def _name(members: list[du.Copy], track: Track) -> str:
    """A file name like the neighbours': "1-07 Title", "07 Title" or "Title"."""
    names = [PurePosixPath(m.file.path).stem for m in members]
    title = file_name(track.title)
    if sum(bool(DISC_PREFIX.match(n)) for n in names) * 2 > len(names):
        return f"{track.discnumber or 1}-{track.tracknumber:02d} {title}"
    if sum(bool(NUMBER_PREFIX.match(n)) for n in names) * 2 > len(names):
        return f"{track.tracknumber:02d} {title}"
    return title
