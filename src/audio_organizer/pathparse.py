"""Metadata evidence from a file's path relative to the library root.

Understands the layouts found in real libraries:
  Artist/Album/NN Title.mp3        (iTunes; the top folder is the album artist)
  Artist/Album [Disc 2]/2-14 Title.mp3
  Artist/Album/CD1/NN Title.mp3
  Compilations/Album/NN Title.mp3
  Lastname, Firstname/Artist_Title.mp3   (flat, one folder per artist)
iTunes copy suffixes ("Title 1.mp3") and timestamp suffixes ("Title_0702163216.mp3")
are reported in the extra field "copy_suffix" rather than as part of the title.
"""

import re
import unicodedata
from pathlib import PurePosixPath

COMPILATION_DIRS = {"compilations", "various artists", "various", "va"}
UNKNOWN_ARTISTS = {"unknown artist", "unknown", "artist"}
UNKNOWN_ALBUMS = {"unknown album", "unknown", "album"}

DISC_SUFFIX = re.compile(
    r"\s*(?:-\s*)?[\[(]?\s*\b(?:cd|disc|disk)\s*(\d{1,2})\s*[\])]?\s*$", re.IGNORECASE
)
DISC_DIR = re.compile(r"^(?:cd|disc|disk)\s*(\d{1,2})$", re.IGNORECASE)
TRACK_PREFIX = re.compile(r"^(?:(\d{1,2})-)?(\d{1,3})(?:\s*[-.]\s*|\s+)(\S.*)$")
TIMESTAMP_SUFFIX = re.compile(r"^(.*\S)_(\d{10})$")
COPY_SUFFIX = re.compile(r"^(.*\S) ([1-9])$")
GENERIC_TITLE = re.compile(r"^(?:audio )?track(?: \d+)?$", re.IGNORECASE)
ARTIST_SEPARATOR = re.compile(r"(?<=\S)_(?=\S)")


def parse(relpath: str) -> dict[str, str]:
    path = PurePosixPath(relpath)
    dirs = list(path.parent.parts)
    stem = unicodedata.normalize("NFC", path.stem).strip()
    dirs = [unicodedata.normalize("NFC", d).strip() for d in dirs]
    fields: dict[str, str] = {}

    artist_dir = album_dir = None
    disc_from_dir = None
    if len(dirs) >= 3 and DISC_DIR.match(dirs[-1]):
        disc_from_dir = str(int(DISC_DIR.match(dirs[-1]).group(1)))
        artist_dir, album_dir = dirs[-3], dirs[-2]
    elif len(dirs) >= 2:
        artist_dir, album_dir = dirs[0], dirs[-1]
    elif len(dirs) == 1:
        artist_dir = dirs[0]

    compilation = artist_dir is not None and artist_dir.casefold() in COMPILATION_DIRS
    if compilation:
        fields["albumartist"] = "Various Artists"
        fields["compilation"] = "1"
        artist_dir = None
    elif artist_dir and artist_dir.casefold() in UNKNOWN_ARTISTS:
        artist_dir = None

    if album_dir:
        match = DISC_SUFFIX.search(album_dir)
        if match and match.start() > 0:
            fields["discnumber"] = str(int(match.group(1)))
            album_dir = album_dir[: match.start()].rstrip(" -")
        if album_dir.casefold() not in UNKNOWN_ALBUMS:
            fields["album"] = album_dir
    if disc_from_dir:
        fields["discnumber"] = disc_from_dir

    match = TIMESTAMP_SUFFIX.match(stem)
    if match:
        stem, fields["copy_suffix"] = match.group(1), match.group(2)
    elif COPY_SUFFIX.match(stem):
        fields["copy_suffix"] = COPY_SUFFIX.match(stem).group(2)

    artist_from_name = None
    match = TRACK_PREFIX.match(stem)
    if match:
        disc, track, title = match.groups()
        if disc:
            fields["discnumber"] = str(int(disc))
        if int(track) > 0:
            fields["tracknumber"] = str(int(track))
        left, sep, right = title.partition(" - ")
        if sep and artist_dir and _same_name(left, artist_dir):
            artist_from_name, title = left, right
    elif len(dirs) <= 1 and ARTIST_SEPARATOR.search(stem):
        artist_from_name, title = ARTIST_SEPARATOR.split(stem, maxsplit=1)
    elif album_dir:
        title = stem
    else:
        title = None  # a bare name in an artist folder: can't tell artist from title

    # The title keeps a possible copy suffix: "Part 1" is as likely as an iTunes copy
    if title and not GENERIC_TITLE.match(_strip_copy(title, fields)):
        fields["title"] = title

    if artist_from_name:
        fields["artist"] = artist_from_name.strip()
    if artist_dir:
        name = unflip(artist_dir)
        if album_dir:
            fields.setdefault("albumartist", name)
        fields.setdefault("artist", name)
    return fields


def unflip(name: str) -> str:
    """'Adams, Bryan' -> 'Bryan Adams', leaving band names like 'Earth, Wind & Fire' alone."""
    parts = name.split(", ")
    if len(parts) != 2:
        return name
    last, first = parts
    words = (last + " " + first).casefold().split()
    if any(w in ("&", "and", "the", "of") for w in words):
        return name
    if not (1 <= len(last.split()) <= 2 and 1 <= len(first.split()) <= 2):
        return name
    return f"{first} {last}"


def normalize_name(name: str) -> str:
    """A loose comparison key: case, accents, punctuation and a leading 'The' ignored."""
    text = unicodedata.normalize("NFKD", name.casefold())
    text = "".join(c for c in text if c.isalnum() or c.isspace())
    text = " ".join(text.split())
    return text.removeprefix("the ")


def _same_name(a: str, b: str) -> bool:
    return normalize_name(a) == normalize_name(b) or normalize_name(a) == normalize_name(unflip(b))


def _strip_copy(title: str, fields: dict[str, str]) -> str:
    if "copy_suffix" in fields and COPY_SUFFIX.match(title):
        return COPY_SUFFIX.match(title).group(1)
    return title
