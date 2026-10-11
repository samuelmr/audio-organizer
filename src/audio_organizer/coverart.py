"""Looking up album covers online: the album is searched in MusicBrainz, and its front
cover fetched from the Cover Art Archive (coverartarchive.org), which holds the covers
of MusicBrainz releases. Of the free sources it has the widest coverage, needs no key,
and serves each cover in several sizes, so only as many pixels as wanted are downloaded.
(gnudb has no images; Wikipedia's covers are hard to match to albums and to license.)

Found covers are kept in the catalog (CoverImage), not in the library, and every lookup
is remembered (CoverLookup), so a lookup asks once per album. `aorg covers` then
proposes embedding them. MusicBrainz asks clients to identify themselves with a contact
address and to send at most one request a second.
"""

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from email.utils import parseaddr
from importlib.metadata import PackageNotFoundError, metadata

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import __version__, covers, images
from .models import CoverLookup, Library, utcnow
from .pathparse import normalize_name
from .tracks import relative_folder

SEARCH_URL = "https://musicbrainz.org/ws/2/release-group/"
COVER_URL = "https://coverartarchive.org/release-group/{mbid}/front{size}"
SIZES = (250, 500, 1200)  # the thumbnails' longer side; beyond that, the original
MIN_SCORE = 90  # MusicBrainz's search score (0-100) of an acceptable match
PAUSE = 1.0  # seconds between requests to MusicBrainz


class CoverArtError(Exception):
    """The service could not be reached or answered with an error."""


def user_agent() -> str:
    """audio-organizer/<version> ( <contact> ): AORG_CONTACT, else the package author's
    email address."""
    contact = os.environ.get("AORG_CONTACT")
    if not contact:
        try:
            contact = parseaddr(metadata("audio-organizer").get("Author-email", ""))[1]
        except PackageNotFoundError:
            contact = ""
    return f"audio-organizer/{__version__}" + (f" ( {contact} )" if contact else "")


# The Cover Art Archive answers 500 for images its backing store (archive.org) is slow to
# serve; they usually work a moment later.
RETRY_CODES = (500, 502, 503, 504)


class Client:
    """HTTP requests to MusicBrainz and the Cover Art Archive, at most one a second to
    MusicBrainz."""

    def __init__(self, pause: float = PAUSE, timeout: float = 30):
        self.pause, self.timeout = pause, timeout
        self._last_search = 0.0
        self.headers = {"User-Agent": user_agent(), "Accept": "application/json"}

    def get(self, url: str) -> tuple[bytes, str] | None:
        """The response body and final URL (after redirects), or None for 404."""
        for attempt in range(3):
            request = urllib.request.Request(url, headers=self.headers)
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    return response.read(), response.url
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    return None
                if e.code in RETRY_CODES and attempt < 2:  # a service is busy; slow down
                    time.sleep(2 * (attempt + 1))
                    continue
                raise CoverArtError(f"{url}: HTTP {e.code}") from None
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                raise CoverArtError(f"{url}: {getattr(e, 'reason', e)}") from None
        raise CoverArtError(f"{url}: no answer")

    def search(self, artist: str, album: str) -> list[dict]:
        """Release groups (albums) matching the artist and title, best first."""
        wait = self._last_search + self.pause - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        query = f"releasegroup:{_quote(album)} AND artist:{_quote(artist)}"
        url = SEARCH_URL + "?" + urllib.parse.urlencode({"query": query, "fmt": "json", "limit": 5})
        try:
            found = self.get(url)
        finally:
            self._last_search = time.monotonic()
        return json.loads(found[0]).get("release-groups", []) if found else []

    def front(self, mbid: str, min_pixels: int) -> tuple[bytes, str] | None:
        """The release group's front cover: the smallest size with at least min_pixels on
        each side (the original when no thumbnail has), or None when it has no cover."""
        for size in [*(f"-{s}" for s in SIZES if s >= min_pixels), ""]:
            found = self.get(COVER_URL.format(mbid=mbid, size=size))
            if found is None:
                return None  # no front cover at all
            if not covers.small(images.dimensions(found[0][:65536]), min_pixels) or not size:
                return found
        return None


def _quote(text: str) -> str:
    """A Lucene phrase."""
    return '"' + re.sub(r'(["\\])', r"\\\1", text) + '"'


def comparable_title(title: str) -> str:
    """A title without what editions add: "(Remastered)", "[Disc 2]", "CD1"."""
    title = re.sub(r"[(\[{][^)\]}]*[)\]}]", " ", title)
    title = re.sub(r"\b(disc|disk|cd)\s*\d+\s*$", " ", title, flags=re.IGNORECASE)
    return normalize_name(title)


def candidates(release_groups: list[dict], album: str) -> list[str]:
    """MusicBrainz ids of the release groups that are the album: a good search score and
    the same title once editions' additions are left out."""
    wanted = comparable_title(album)
    return [
        rg["id"]
        for rg in release_groups
        if int(rg.get("score", 0)) >= MIN_SCORE and comparable_title(rg.get("title", "")) == wanted
    ]


@dataclass
class Found:
    release_group: str
    data: bytes
    url: str


def find(client: Client, artist: str, album: str, min_pixels: int) -> Found | None:
    """The album's front cover, from the best matching release group that has one."""
    for mbid in candidates(client.search(artist, album), album)[:3]:
        found = client.front(mbid, min_pixels)
        if found is not None:
            return Found(mbid, *found)
    return None


@dataclass
class LookupStats:
    albums: int = 0  # albums that need a cover and have not been looked up
    found: int = 0
    not_found: int = 0
    errors: list[tuple[str, str]] = field(default_factory=list)
    stopped: str | None = None  # why the lookups stopped early


def lookup_library(
    session: Session,
    library: Library,
    *,
    min_pixels: int = covers.MIN_PIXELS,
    limit: int | None = None,
    client: Client | None = None,
    progress: Callable[[int, int, str, str | None], None] | None = None,
    started: Callable[[int, int], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> LookupStats:
    """Look up covers for the albums whose tracks have no usable cover, embedded or in
    their folder, and that were not looked up before (or for a smaller size). Each lookup
    is committed, so an interrupted run continues where it stopped.

    `started` is told how many albums will be looked up now, and how many are left in all,
    before the first request; `should_stop` is asked before each album, so a run in the
    background can be stopped between them (see gnudb.lookup_library)."""
    client = client or Client()
    stats = LookupStats()
    survey = covers.survey(session, library, min_pixels=min_pixels, save_images=False)
    albums = {a.key: a for a in survey.missing if a.key}  # one lookup per album
    done = {
        row.key: row
        for row in session.scalars(select(CoverLookup).where(CoverLookup.key.in_(list(albums))))
    }
    todo = [
        a
        for key, a in albums.items()
        if key not in done or (done[key].status == "found" and done[key].min_pixels < min_pixels)
    ]
    stats.albums = len(todo)
    todo = todo[:limit]
    if started:
        started(len(todo), stats.albums)
    failures = 0
    for count, album in enumerate(todo, start=1):
        if should_stop and should_stop():
            stats.stopped = f"stopped after {count - 1} of {len(todo)} albums"
            break
        error = None
        try:
            found = find(client, album.artist, album.album, min_pixels)
        except CoverArtError as e:
            error = str(e)
            stats.errors.append((f"{album.artist} – {album.album}", error))
            failures += 1
        else:
            failures = 0
            _record(session, album, found, min_pixels)
            if found:
                stats.found += 1
            else:
                stats.not_found += 1
            session.commit()
        if progress:
            progress(count, len(todo), f"{album.artist} – {album.album}", error)
        if failures >= 3:
            stats.stopped = "three lookups in a row failed; the services may be busy, try later"
            break
    return stats


def lookup_folder(
    session: Session,
    library: Library,
    folder: str,
    *,
    min_pixels: int = covers.MIN_PIXELS,
    client: Client | None = None,
) -> list[tuple[covers.Album, Found | None]]:
    """Look up the covers of the albums in one folder now, even if they were looked up
    before. Returns each album that needs a cover (none usable, embedded or in the
    folder) with the cover found, or None; an empty list when none does."""
    try:
        folder = relative_folder(library, folder)
    except ValueError as e:
        raise CoverArtError(str(e)) from None
    client = client or Client()
    survey = covers.survey(
        session,
        library,
        path_prefix=folder + "/" if folder else None,
        min_pixels=min_pixels,
        save_images=False,
    )
    results = []
    for album in survey.missing:
        if album.folder != folder or not album.key:
            continue
        found = find(client, album.artist, album.album, min_pixels)
        _record(session, album, found, min_pixels)
        session.commit()
        results.append((album, found))
    return results


def _record(session: Session, album: covers.Album, found: Found | None, min_pixels: int) -> None:
    lookup = session.get(CoverLookup, album.key) or CoverLookup(key=album.key)
    lookup.artist, lookup.album, lookup.min_pixels = album.artist, album.album, min_pixels
    lookup.looked_up_at = utcnow()
    if found is None:
        lookup.status, lookup.release_group, lookup.image_sha1 = "not_found", None, None
    else:
        image = covers.store_image(session, found.data, "coverartarchive", found.url)
        lookup.status, lookup.release_group, lookup.image_sha1 = (
            "found", found.release_group, image.sha1,
        )  # fmt: skip
    session.add(lookup)
