"""Looking up albums in gnudb (gnudb.org), the free successor of CDDB/freedb: given a disc's
track lengths it finds the album's artist, title, year, genre and track titles.

An album folder has no real table of contents, so a pseudo-TOC is made from the tracks'
durations. Rips are rarely sample-exact, so gnudb mostly answers with inexact matches,
often several editions of the album; a candidate is accepted only when its artist, title
and track count agree with the folder's, and (when the folder's tracks have titles) most
of its track titles do too. Accepted entries are stored as values from the source
"gnudb" (see provenance.SOURCE_PRIORITY), not written into the files.

gnudb refuses clients that give no contact address ("500 Unknown application"): the hello
carries the user's own email address (AORG_GNUDB_EMAIL, else AORG_CONTACT), never one of
the maintainer's. The HTTP interface is often unreachable, so this speaks cddbp
(protocol level 6) on port 8880, one session for all lookups, at most one request every
three seconds. A refused connection or a 5xx refusal of the hello ends the lookups, since
every further request would fail, and retrying only adds to what got the address blocked.
"""

import os
import socket
import time
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import __version__, provenance
from .coverart import comparable_title
from .covers import album_key
from .models import AudioFile, EffectiveMetadata, GnudbLookup, Library, utcnow
from .pathparse import normalize_name
from .tracks import present

HOST, PORT = "gnudb.gnudb.org", 8880
FRAMES = 75  # CD frames per second
LEAD_IN = 150  # the first track starts 2 seconds in
PAUSE = 3.0  # seconds between requests; gnudb may block an address that looks like a bot
APPLICATION = "audio-organizer"
MIN_TITLE_MATCH = 0.5  # of the folder's titled tracks that a candidate must agree with
VARIOUS = {"various", "various artists", "va", "varios"}


class GnudbError(Exception):
    """The service could not be reached, or answered with an error."""


class GnudbRefused(GnudbError):
    """The service refused the connection or this application: stop asking."""


def has_email() -> bool:
    try:
        email()
    except GnudbRefused:
        return False
    return True


def email() -> str:
    address = os.environ.get("AORG_GNUDB_EMAIL") or os.environ.get("AORG_CONTACT") or ""
    if "@" not in address:
        raise GnudbRefused(
            "gnudb asks clients for a contact email address: set AORG_GNUDB_EMAIL to yours"
        )
    return address


# --- disc IDs ---------------------------------------------------------------------------


def pseudo_toc(durations: list[float]) -> tuple[list[int], int]:
    """Frame offsets of the tracks and the disc length in seconds, as if the tracks were
    laid out on a CD (no gaps between them)."""
    offsets, position = [], LEAD_IN
    for duration in durations:
        offsets.append(position)
        position += round(duration * FRAMES)
    return offsets, position // FRAMES


def disc_id(offsets: list[int], seconds: int) -> str:
    """The CDDB disc ID: a checksum of the track start seconds, the playing time and the
    track count, in eight hex digits."""
    checksum = sum(sum(int(d) for d in str(offset // FRAMES)) for offset in offsets)
    playing = seconds - offsets[0] // FRAMES
    return f"{(checksum % 255) << 24 | playing << 8 | len(offsets):08x}"


# --- entries ----------------------------------------------------------------------------


@dataclass
class Candidate:
    """A line of a query's answer: an entry that may be the disc."""

    category: str
    discid: str
    artist: str
    album: str


@dataclass
class Entry:
    """A gnudb entry (xmcd record)."""

    category: str
    discid: str
    artist: str
    album: str
    year: str | None
    genre: str | None
    titles: list[str]
    artists: list[str | None] = field(default_factory=list)  # per track, on compilations


def split_title(text: str) -> tuple[str, str]:
    """("artist", "title") of "artist / title"; without the slash both are the text."""
    artist, slash, title = text.partition(" / ")
    return (artist.strip(), title.strip()) if slash else (text.strip(), text.strip())


def parse_candidate(line: str) -> Candidate | None:
    """A line like "rock 940aac0b Texas / Greatest Hits"."""
    parts = line.split(None, 2)
    if len(parts) < 3:
        return None
    artist, album = split_title(parts[2])
    return Candidate(parts[0], parts[1], artist, album)


def parse_entry(category: str, discid: str, lines: list[str]) -> Entry:
    """The entry from the lines of an xmcd record. Titles longer than a line are split
    over several lines with the same key, which are joined."""
    values: dict[str, str] = defaultdict(str)
    for line in lines:
        if line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key] += value.rstrip("\r\n")
    artist, album = split_title(values.get("DTITLE", ""))
    titles = []
    index = 0
    while f"TTITLE{index}" in values:
        titles.append(values[f"TTITLE{index}"].strip())
        index += 1
    artists: list[str | None] = [None] * len(titles)
    if normalize_name(artist) in VARIOUS and titles and all(" / " in t for t in titles):
        pairs = [split_title(t) for t in titles]  # "artist / title" on compilations
        artists, titles = [a for a, _ in pairs], [t for _, t in pairs]
    return Entry(
        category, discid, artist, album,
        values.get("DYEAR", "").strip() or None,
        values.get("DGENRE", "").strip() or None,
        titles, artists,
    )  # fmt: skip


# --- the client -------------------------------------------------------------------------


class Client:
    """One cddbp session, opened on the first request."""

    def __init__(self, host: str = HOST, port: int = PORT, *, pause: float = PAUSE,
                 timeout: float = 30, contact: str | None = None):  # fmt: skip
        self.host, self.port, self.pause, self.timeout = host, port, pause, timeout
        self.contact = contact
        self._socket: socket.socket | None = None
        self._lines = None
        self._last = 0.0

    def __enter__(self) -> "Client":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        if self._socket is not None:
            try:  # the server closes the connection without replying
                self._socket.sendall(b"quit\n")
            except OSError:
                pass
            self._socket.close()
            self._socket = self._lines = None

    def _open(self) -> None:
        user, _, host = (self.contact or email()).partition("@")
        try:
            self._socket = socket.create_connection((self.host, self.port), timeout=self.timeout)
        except ConnectionRefusedError:
            raise GnudbRefused(
                f"{self.host}: connection refused (is this address blocked?)"
            ) from None
        except OSError as e:
            raise GnudbError(f"{self.host}: {e}") from None
        self._lines = self._socket.makefile("r", encoding="utf-8", errors="replace", newline="\n")
        try:
            code, text = self._status()
            if code not in (200, 201):
                raise GnudbRefused(f"{code} {text}")
            hello = f"cddb hello {user} {host} {APPLICATION} {__version__}"
            for command in (hello, "proto 6"):
                code, text = self.command(command, opening=True)
                if code >= 500 or code in (431, 432, 433, 434):
                    raise GnudbRefused(f"{code} {text}")
        except GnudbError:
            self.close()  # the next request starts a new session
            raise

    def _status(self) -> tuple[int, str]:
        assert self._lines is not None
        try:
            line = self._lines.readline()
        except (OSError, TimeoutError) as e:
            raise GnudbError(f"{self.host}: {e}") from None
        if not line:
            raise GnudbError(f"{self.host}: connection closed")
        code, _, text = line.strip().partition(" ")
        if not code.isdigit():
            raise GnudbError(f"unexpected answer: {line.strip()}")
        return int(code), text

    def command(self, text: str, *, opening: bool = False) -> tuple[int, str]:
        """Send a command and return its status line."""
        if self._socket is None and not opening:
            self._open()
        assert self._socket is not None
        wait = self._last + self.pause - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        try:
            self._socket.sendall(text.encode("utf-8") + b"\n")
        except OSError as e:
            raise GnudbError(f"{self.host}: {e}") from None
        answer = self._status()
        self._last = time.monotonic()
        return answer

    def _body(self) -> list[str]:
        """The lines up to the terminating ".", dot-stuffing not used by cddbp."""
        assert self._lines is not None
        body = []
        while True:
            line = self._lines.readline()
            if not line:
                raise GnudbError(f"{self.host}: connection closed")
            if line.rstrip("\r\n") == ".":
                return body
            body.append(line.rstrip("\r\n"))

    def query(self, offsets: list[int], seconds: int) -> list[Candidate]:
        """Entries that may be the disc, exact matches first."""
        text = f"cddb query {disc_id(offsets, seconds)} {len(offsets)} "
        text += " ".join(map(str, offsets)) + f" {seconds}"
        code, rest = self.command(text)
        if code == 200:  # one exact match, on the status line itself
            found = parse_candidate(rest)
            return [found] if found else []
        if code in (210, 211):
            return [c for line in self._body() if (c := parse_candidate(line))]
        if code in (202, 403):  # no match; damaged entry
            return []
        raise GnudbRefused(f"{code} {rest}") if code >= 500 else GnudbError(f"{code} {rest}")

    def read(self, candidate: Candidate) -> Entry | None:
        code, rest = self.command(f"cddb read {candidate.category} {candidate.discid}")
        if code == 210:
            return parse_entry(candidate.category, candidate.discid, self._body())
        if code in (401, 403):  # not found; database entry is corrupt
            return None
        raise GnudbRefused(f"{code} {rest}") if code >= 500 else GnudbError(f"{code} {rest}")


# --- matching ---------------------------------------------------------------------------


@dataclass
class Folder:
    """The tracks of one album in one folder, in track order."""

    folder: str
    artist: str
    album: str
    files: list[AudioFile]
    titles: list[str | None]  # the tracks' effective titles
    existing: list[EffectiveMetadata | None]

    @property
    def key(self) -> str:
        return album_key(self.artist, self.album)


def _same_artist(wanted: str, found: str) -> bool:
    if normalize_name(wanted) in VARIOUS and normalize_name(found) in VARIOUS:
        return True
    return normalize_name(wanted) == normalize_name(found)


def title_agreement(wanted: list[str | None], found: list[str]) -> float:
    """The share of the folder's titled tracks whose title is the entry's (1.0 when the
    folder's tracks have no titles to compare)."""
    pairs = [(w, f) for w, f in zip(wanted, found, strict=False) if w]
    if not pairs:
        return 1.0
    same = sum(normalize_name(comparable_title(w)) == normalize_name(comparable_title(f))
               for w, f in pairs)  # fmt: skip
    return same / len(pairs)


def match(client, folder: Folder) -> Entry | None:
    """The entry for the folder: of the candidates with the folder's artist and album
    title, the first whose entry has the folder's track count and mostly its titles."""
    offsets, seconds = pseudo_toc([f.duration or 0 for f in folder.files])
    wanted = comparable_title(folder.album)
    candidates = [
        c
        for c in client.query(offsets, seconds)
        if _same_artist(folder.artist, c.artist) and comparable_title(c.album) == wanted
    ]
    for candidate in candidates[:4]:
        entry = client.read(candidate)
        if (
            entry
            and len(entry.titles) == len(folder.files)
            and title_agreement(folder.titles, entry.titles) >= MIN_TITLE_MATCH
        ):
            return entry
    return None


# --- the library ------------------------------------------------------------------------


def folders(session: Session, library: Library) -> list[Folder]:
    """The album folders that can be looked up: the files of one folder with the same
    album, a single disc whose track numbers are 1..n (or missing), all with a duration
    and with an album artist (or one common artist) and album title."""
    query = (
        select(AudioFile, EffectiveMetadata)
        .join(EffectiveMetadata, EffectiveMetadata.file_id == AudioFile.id)
        .where(present(library.id), EffectiveMetadata.album.is_not(None))
        .order_by(AudioFile.path)
    )
    groups: dict[tuple[str, str], list] = defaultdict(list)
    for file, meta in session.execute(query):
        groups[(str(PurePosixPath(file.path).parent), normalize_name(meta.album))].append(
            (file, meta)
        )
    result = []
    for (folder, _), members in groups.items():
        if len(members) < 2 or any(not f.duration for f, _ in members):
            continue
        if any((m.disctotal or 1) > 1 or (m.discnumber or 1) > 1 for _, m in members):
            continue
        members.sort(key=lambda fm: (fm[1].tracknumber or 0, fm[0].path))
        numbers = [m.tracknumber for _, m in members]
        if any(numbers) and numbers != list(range(1, len(members) + 1)):
            continue  # missing tracks, or two with the same number
        album = Counter(m.album for _, m in members).most_common(1)[0][0]
        albumartists = {m.albumartist for _, m in members if m.albumartist}
        artists = {m.artist for _, m in members if m.artist}
        artist = (
            albumartists.pop() if len(albumartists) == 1
            else artists.pop() if len(artists) == 1 and not albumartists
            else None
        )  # fmt: skip
        if artist is None:
            continue
        result.append(
            Folder(
                folder, artist, album, [f for f, _ in members],
                [m.title for _, m in members], [m for _, m in members],
            )
        )  # fmt: skip
    return result


def entry_values(entry: Entry, index: int, existing: EffectiveMetadata | None) -> dict[str, str]:
    """The values of the entry for its index'th track: the fields it knows, without those
    the file already has the same (so that a year does not replace a full date)."""
    values = {
        "artist": entry.artists[index] or entry.artist,
        "albumartist": entry.artist,
        "album": entry.album,
        "title": entry.titles[index],
        "tracknumber": str(index + 1),
        "tracktotal": str(len(entry.titles)),
        "discid": entry.discid,
    }
    if entry.year:
        values["date"] = entry.year
    if entry.genre:
        values["genre"] = entry.genre
    if existing is None:
        return values
    same = {
        name: provenance.comparable(name, value)
        == provenance.comparable(name, str(getattr(existing, name)))
        for name, value in values.items()
        if hasattr(existing, name) and getattr(existing, name) is not None
    }
    return {name: value for name, value in values.items() if not same.get(name)}


def apply(session: Session, folder: Folder, entry: Entry) -> None:
    """Store the entry's values as the "gnudb" values of the folder's files."""
    for index, file in enumerate(folder.files):
        provenance.set_values(
            session, file, "gnudb", entry_values(entry, index, folder.existing[index])
        )


@dataclass
class LookupStats:
    albums: int = 0  # folders to look up
    found: int = 0
    not_found: int = 0
    errors: list[tuple[str, str]] = field(default_factory=list)
    stopped: str | None = None  # why the lookups stopped early


def lookup_library(
    session: Session,
    library: Library,
    *,
    limit: int | None = None,
    client: Client | None = None,
    progress: Callable[[int, int, str, str | None], None] | None = None,
) -> LookupStats:
    """Look up the album folders not looked up before (or whose track count changed).
    Each lookup is committed, so an interrupted run continues where it stopped."""
    stats = LookupStats()
    done = {
        (row.folder, row.key): row.tracks
        for row in session.scalars(select(GnudbLookup).where(GnudbLookup.library_id == library.id))
    }
    todo = [f for f in folders(session, library) if done.get((f.folder, f.key)) != len(f.files)]
    stats.albums = len(todo)
    todo = todo[:limit]
    if not todo:
        return stats
    owned = client is None
    client = client or Client()
    failures = 0
    try:
        for count, folder in enumerate(todo, start=1):
            error = None
            try:
                entry = match(client, folder)
            except GnudbRefused as e:
                stats.stopped = str(e)
                break
            except GnudbError as e:
                error = str(e)
                stats.errors.append((folder.folder, error))
                failures += 1
            else:
                failures = 0
                if entry:
                    apply(session, folder, entry)
                    stats.found += 1
                else:
                    stats.not_found += 1
                _record(session, library, folder, entry)
                session.commit()
            if progress:
                progress(count, len(todo), f"{folder.artist} – {folder.album}", error)
            if failures >= 3:
                stats.stopped = "three lookups in a row failed; is the network down?"
                break
    finally:
        if owned:
            client.close()
    return stats


def _record(session: Session, library: Library, folder: Folder, entry: Entry | None) -> None:
    session.merge(
        GnudbLookup(
            library_id=library.id, folder=folder.folder, key=folder.key, tracks=len(folder.files),
            status="found" if entry else "not_found",
            category=entry.category if entry else None,
            discid=entry.discid if entry else None,
            looked_up_at=utcnow(),
        )
    )  # fmt: skip
