"""Reading and writing tags as a flat dict of normalized fields.

Field values are strings; multiple values are joined with "; ". Track and disc
numbers are split into number and total and stripped of leading zeros. Ratings are
stars, "1" to "5". The mood/tempo/situation/preference comments written by MusicMatch
Jukebox are read as fields of their own; mood is also read from, and written to, the
standard MOOD tag, so that other players see it.

ID3 tags are written as version 2.3 or 2.4: by default the version the file already
has (2.3 for files without a tag, as more players read it), or the one asked for.
"""

import base64
import io
import re
from contextlib import nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO

import mutagen
from mutagen import MutagenError
from mutagen.aac import AAC, AACError, AACInfo
from mutagen.flac import Picture
from mutagen.id3 import (
    COMM,
    ID3,
    POPM,
    TCMP,
    TCON,
    TDRC,
    TPOS,
    TRCK,
    TXXX,
    Frames,
    ID3NoHeaderError,
)
from mutagen.mp4 import MP4Cover, MP4FreeForm, MP4Tags

from . import audiohash, images

FIELDS = (
    "artist",
    "albumartist",
    "album",
    "title",
    "tracknumber",
    "tracktotal",
    "discnumber",
    "disctotal",
    "date",
    "originaldate",
    "genre",
    "compilation",
    "albumartistsort",
    "artistsort",
    "composer",
    "conductor",
    "grouping",
    "subtitle",
    "language",
    "mood",
    "rating",
    "tempo",
    "situation",
    "preference",
    "discid",
    "replaygain_track_gain",
    "replaygain_track_peak",
    "replaygain_album_gain",
    "replaygain_album_peak",
)

# Not a field one edits: read() reports the size of the file's embedded cover in it,
# "500x500" (or "?" when the image's size is unknown). Catalogs made before cover sizes
# were read have "1" (see covers.refresh_sizes).
EMBEDDED_COVER = "embedded_cover"

# One field per ID3 text frame (ID3v2.3 TORY is read as TDOR)
ID3_TEXT = {
    "artist": "TPE1",
    "albumartist": "TPE2",
    "album": "TALB",
    "title": "TIT2",
    "originaldate": "TDOR",
    "albumartistsort": "TSO2",
    "artistsort": "TSOP",
    "composer": "TCOM",
    "conductor": "TPE3",
    "grouping": "TIT1",
    "subtitle": "TIT3",
    "language": "TLAN",
}
# Fields in ID3 user text frames (TXXX), by description
ID3_TXXX = {
    "mood": "MOOD",
    "discid": "DISCID",
    "replaygain_track_gain": "REPLAYGAIN_TRACK_GAIN",
    "replaygain_track_peak": "REPLAYGAIN_TRACK_PEAK",
    "replaygain_album_gain": "REPLAYGAIN_ALBUM_GAIN",
    "replaygain_album_peak": "REPLAYGAIN_ALBUM_PEAK",
}
# ID3v2.4 frames that iTunes and others also use in ID3v2.3, kept when saving 2.3
V23_KEEP = ("TSOP", "TSOA", "TSOT")
# Ratings in POPM frames: 0-255, read as 1-5 stars; written with Windows Media
# Player's values and name, which most players read
POPM_EMAIL = "Windows Media Player 9 Series"
POPM_STARS = {1: 1, 2: 64, 3: 128, 4: 196, 5: 255}

MP4_TEXT = {
    "artist": "\xa9ART",
    "albumartist": "aART",
    "album": "\xa9alb",
    "title": "\xa9nam",
    "date": "\xa9day",
    "genre": "\xa9gen",
    "albumartistsort": "soaa",
    "artistsort": "soar",
    "composer": "\xa9wrt",
    "grouping": "\xa9grp",
}
MP4_FREEFORM = "----:com.apple.iTunes:"
# Fields in MP4 freeform atoms, by name (the conventions of iTunes, foobar2000, Picard)
MP4_FREE = {
    "originaldate": "ORIGINALDATE",
    "conductor": "CONDUCTOR",
    "subtitle": "SUBTITLE",
    "language": "LANGUAGE",
    "mood": "MOOD",
    "rating": "RATING",
    "discid": "DISCID",
    "replaygain_track_gain": "replaygain_track_gain",
    "replaygain_track_peak": "replaygain_track_peak",
    "replaygain_album_gain": "replaygain_album_gain",
    "replaygain_album_peak": "replaygain_album_peak",
}
MUSICMATCH = {
    "mood": "MusicMatch_Mood",
    "tempo": "MusicMatch_Tempo",
    "situation": "MusicMatch_Situation",
    "preference": "MusicMatch_Preference",
}


class UnsupportedFormat(Exception):
    pass


@dataclass
class AudioInfo:
    format: str | None = None
    duration: float | None = None
    bitrate: int | None = None
    sample_rate: int | None = None
    fields: dict[str, str] = field(default_factory=dict)


def load_tags(source: str | Path | BinaryIO) -> tuple[mutagen.FileType | None, object]:
    """mutagen's object for a file, and its tags.

    Raw AAC streams (ADTS) with an ID3 tag in front lead mutagen's format detection to
    MP3, which then fails, and mutagen's AAC reader ignores the tag; so .aac files are
    read as AAC, with the ID3 tag loaded on its own."""
    name = str(getattr(source, "name", source))
    if name.lower().endswith(".aac"):
        try:
            audio = AAC(source)
        except AACError:
            audio = _aac_after_junk(source)  # None for e.g. an MP4 file named .aac
        if audio is not None:
            _rewind(source)
            try:
                return audio, ID3(source)
            except ID3NoHeaderError:
                return audio, None
        _rewind(source)
    audio = mutagen.File(source)
    return audio, audio.tags if audio is not None else None


def _aac_after_junk(source: str | Path | BinaryIO) -> AAC | None:
    """An AAC object for an ADTS stream whose first frame lies far behind the ID3 tag.

    mutagen looks for the first frame in the 512 bytes after the tag size the ID3 header
    gives. Some taggers write that size too small when they embed a large cover, leaving
    the rest of the picture between the tag and the stream; ffmpeg and players cope, so
    we search further and hand mutagen the stream from the first frame on."""
    context = open(source, "rb") if isinstance(source, str | Path) else nullcontext(source)
    with context as f:
        start, end = audiohash.id3_stream_range(f)
        f.seek(start)
        data = f.read(min(end - start, ADTS_SEARCH_BYTES))
    pos = data.find(b"\xff")
    while pos >= 0 and pos + 1 < len(data) and data[pos + 1] & 0xF6 != 0xF0:
        pos = data.find(b"\xff", pos + 1)
    if pos < 0 or pos + 1 >= len(data):
        return None
    try:
        info = AACInfo(io.BytesIO(data[pos:]))
    except AACError:
        return None
    audio = AAC.__new__(AAC)  # AAC() without a file name is deprecated, and AAC(path) fails
    audio.info = info
    if isinstance(source, str | Path):
        audio.filename = str(source)
    return audio


def _rewind(source) -> None:
    if hasattr(source, "seek"):
        source.seek(0)


def read(path: str | Path | BinaryIO) -> AudioInfo:
    """Read a file given by path, or an open, seekable binary file object."""
    audio, tags = load_tags(path)
    if audio is None:
        return AudioInfo()
    info = AudioInfo(
        format=type(audio).__name__.lower(),
        duration=getattr(audio.info, "length", None),
        bitrate=getattr(audio.info, "bitrate", None) or None,
        sample_rate=getattr(audio.info, "sample_rate", None) or None,
    )
    if isinstance(audio, AAC) and audio.info._type == "ADTS":
        measured = adts_length(path)
        if measured:
            info.duration, info.bitrate = measured
    if isinstance(tags, ID3):
        raw = _read_id3(tags)
    elif isinstance(tags, MP4Tags):
        raw = _read_mp4(tags)
    elif tags is not None and hasattr(tags, "as_dict"):  # Vorbis comments (Ogg, FLAC, Opus)
        raw = {k.lower(): v for k, v in tags.as_dict().items()}
        raw = {name: raw.get(name, []) for name in FIELDS}
    else:
        raw = {}
    for name, values in raw.items():
        _put(info.fields, name, values)
    _split_pair(info.fields, "tracknumber", "tracktotal")
    _split_pair(info.fields, "discnumber", "disctotal")
    picture = front_picture(audio, tags)
    if picture is not None:
        info.fields[EMBEDDED_COVER] = images.format_size(images.dimensions(picture[1][:65536]))
    return info


ADTS_SEARCH_BYTES = 1 << 20  # how far behind an ID3 tag to look for the first ADTS frame
ADTS_SAMPLE_RATES = (
    96000, 88200, 64000, 48000, 44100, 32000, 24000, 22050, 16000, 12000, 11025, 8000, 7350,
)  # fmt: skip


def adts_length(source: str | Path | BinaryIO) -> tuple[float, int] | None:
    """(Duration in seconds, bitrate) of a raw AAC stream (ADTS), from its frame headers.

    ADTS has no index or length field, and mutagen estimates the length from the first
    frames' bitrate, which can be off by minutes for streams that start quietly. Counting
    every frame is exact, but reads the whole stream; it is done for ADTS files only.
    None when no frames are found."""
    context = open(source, "rb") if isinstance(source, str | Path) else nullcontext(source)
    with context as f:
        start, end = audiohash.id3_stream_range(f)
        f.seek(start)
        data = f.read(end - start)
    pos = data.find(b"\xff")
    while pos >= 0 and pos + 1 < len(data) and data[pos + 1] & 0xF6 != 0xF0:
        pos = data.find(b"\xff", pos + 1)  # skip junk before the first frame
    if pos < 0:
        return None
    first, samples, rate = pos, 0, 0
    while pos + 7 <= len(data):
        header = data[pos : pos + 7]
        if header[0] != 0xFF or header[1] & 0xF6 != 0xF0:
            break
        length = (header[3] & 0x03) << 11 | header[4] << 3 | header[5] >> 5
        rate_index = header[2] >> 2 & 0x0F
        if length < 7 or rate_index >= len(ADTS_SAMPLE_RATES):
            break
        rate = ADTS_SAMPLE_RATES[rate_index]
        samples += 1024 * ((header[6] & 0x03) + 1)
        pos += length
    if not samples:
        return None
    duration = samples / rate
    return duration, round((min(pos, len(data)) - first) * 8 / duration)


def front_picture(audio, tags) -> tuple[str, bytes] | None:
    """(MIME type, data) of the embedded front cover (or first picture), or None."""
    if isinstance(tags, ID3):
        pictures = sorted(tags.getall("APIC"), key=lambda p: p.type != 3)  # front cover first
        if pictures:
            return pictures[0].mime or "image/jpeg", pictures[0].data
    elif isinstance(tags, MP4Tags):
        if tags.get("covr"):
            cover = tags["covr"][0]
            mime = "image/png" if cover.imageformat == MP4Cover.FORMAT_PNG else "image/jpeg"
            return mime, bytes(cover)
    elif getattr(audio, "pictures", None):  # FLAC
        picture = sorted(audio.pictures, key=lambda p: p.type != 3)[0]
        return picture.mime, picture.data
    elif tags is not None and hasattr(tags, "get") and tags.get("metadata_block_picture"):
        try:  # Ogg Vorbis and Opus
            picture = Picture(base64.b64decode(tags["metadata_block_picture"][0]))
        except (ValueError, MutagenError):
            return None
        return picture.mime, picture.data
    return None


def _read_id3(tags: ID3) -> dict[str, list]:
    raw: dict[str, list] = {name: _frame_text(tags, fid) for name, fid in ID3_TEXT.items()}
    raw["tracknumber"] = _frame_text(tags, "TRCK")
    raw["discnumber"] = _frame_text(tags, "TPOS")
    raw["date"] = _frame_text(tags, "TDRC")
    raw["genre"] = [g for frame in tags.getall("TCON") for g in frame.genres]
    raw["compilation"] = _frame_text(tags, "TCMP")
    for frame in tags.getall("COMM"):
        for name, desc in MUSICMATCH.items():
            if frame.desc == desc:
                raw[name] = list(frame.text)
    for name, desc in ID3_TXXX.items():
        values = _txxx(tags, desc)
        if values:
            raw[name] = values  # the standard MOOD tag wins over MusicMatch's
    if not _txxx(tags, "MOOD") and tags.getall("TMOO"):
        raw["mood"] = _frame_text(tags, "TMOO")
    ratings = [frame.rating for frame in tags.getall("POPM") if frame.rating]
    if ratings:
        raw["rating"] = [str(_stars(ratings[0]))]
    return raw


def _frame_text(tags: ID3, frame_id: str) -> list:
    return [str(t) for frame in tags.getall(frame_id) for t in frame.text]


def _txxx(tags: ID3, desc: str) -> list:
    """The values of the user text frames with this description (case-insensitive)."""
    return [
        str(t)
        for frame in tags.getall("TXXX")
        if frame.desc.casefold() == desc.casefold()
        for t in frame.text
    ]


def _stars(popm: int) -> int:
    """A POPM rating (1-255) as 1-5 stars, the way Windows Media Player reads them."""
    return 1 if popm < 32 else 2 if popm < 96 else 3 if popm < 160 else 4 if popm < 224 else 5


def _read_mp4(tags: MP4Tags) -> dict[str, list]:
    raw: dict[str, list] = {name: tags.get(atom, []) for name, atom in MP4_TEXT.items()}
    for name, atom in (("tracknumber", "trkn"), ("discnumber", "disk")):
        pairs = tags.get(atom) or []
        if pairs:
            number, total = pairs[0]
            raw[name] = [f"{number}/{total}" if total else str(number)]
    if tags.get("cpil"):
        raw["compilation"] = ["1"]
    for name, desc in MUSICMATCH.items():
        raw[name] = _freeform(tags, desc)
    for name, key in MP4_FREE.items():
        values = _freeform(tags, key)
        if values:
            raw[name] = values
    return raw


def _freeform(tags: MP4Tags, name: str) -> list:
    values = []
    for atom, items in tags.items():
        if (
            atom.startswith(MP4_FREEFORM)
            and atom[len(MP4_FREEFORM) :].casefold() == name.casefold()
        ):
            values += [bytes(v).decode("utf-8", "replace") for v in items]
    return values


def _put(fields: dict[str, str], name: str, values) -> None:
    if isinstance(values, str):
        values = [values]
    cleaned = [str(v).strip() for v in values if str(v).strip()]
    if cleaned:
        fields[name] = "; ".join(dict.fromkeys(cleaned))


NUMBER_FIELDS = ("tracknumber", "tracktotal", "discnumber", "disctotal")
_NUMBER = re.compile(r"\s*(\d+)\s*[.:)-]?\s*")
_YEAR = re.compile(r"\s*(\d{4})(?:\D.*)?", re.DOTALL)


def parse_number(value: str | None) -> int | None:
    """A track or disc number or total as written in real tags: '2', '02', '02.' and
    '13-' are numbers; anything else (text in the wrong field) is not."""
    match = _NUMBER.fullmatch(value or "")
    return int(match.group(1)) if match else None


def parse_year(value: str | None) -> int | None:
    """The year of a date: '1996', '1996-05-01', '2017-03-03T08:00:00Z'."""
    match = _YEAR.fullmatch(value or "")
    return int(match.group(1)) if match else None


def _split_pair(fields: dict[str, str], number_key: str, total_key: str) -> None:
    """'03/12' -> number '3', total '12'; '02.' -> '2'. Values that are not numbers are
    kept as they are."""
    value = fields.get(number_key)
    if value is not None:
        number_text, _, total_text = value.partition("/")
        number, total = parse_number(number_text), parse_number(total_text)
        if number == 0:
            del fields[number_key]  # iTunes writes 0 for "no track number"
        elif number is not None:
            fields[number_key] = str(number)
        if total and total_key not in fields:
            fields[total_key] = str(total)
    total = parse_number(fields.get(total_key))
    if total is not None:
        fields[total_key] = str(total)


def id3_version(path: str | Path) -> int | None:
    """The ID3v2 version of a file's tag (3 or 4), or None if it has none."""
    _, tags = load_tags(path)
    if isinstance(tags, ID3) and tags.version >= (2, 3, 0):
        return 4 if tags.version >= (2, 4, 0) else 3
    return None


def write(
    path: str | Path, changes: dict[str, str | None], *, id3_version: int | None = None
) -> None:
    """Set the given fields (None removes the field), leaving all other tags as they are.
    ID3 tags are saved as id3_version (3 or 4), by default the version the file has."""
    unknown = set(changes) - set(FIELDS)
    if unknown:
        raise ValueError(f"Unknown fields: {', '.join(sorted(unknown))}")
    if id3_version not in (None, 3, 4):
        raise ValueError(f"ID3 version must be 3 or 4, not {id3_version}")
    audio, tags = load_tags(path)
    if audio is None:
        raise UnsupportedFormat(f"Not a recognized audio file: {path}")
    current = read(path).fields
    merged = {**current, **changes}
    if isinstance(audio, AAC):  # an ID3 tag in front of the stream; see _load
        had_tag = tags is not None
        tags = tags if had_tag else ID3()
        _write_id3(tags, changes, merged)
        version = id3_version or (id3_tag_version(tags) if had_tag else 3)
        prepare_id3(tags, version)
        tags.save(path, v2_version=version)
        return
    had_tag = tags is not None
    if not had_tag:
        audio.add_tags()
        tags = audio.tags
    if isinstance(tags, ID3):
        _write_id3(tags, changes, merged)
        version = id3_version or (id3_tag_version(tags) if had_tag else 3)
        prepare_id3(tags, version)
        audio.save(v2_version=version)
    elif isinstance(tags, MP4Tags):
        _write_mp4(tags, changes, merged)
        audio.save()
    elif hasattr(tags, "as_dict"):
        for name, value in changes.items():
            if value is None:
                tags.pop(name, None)
            else:
                tags[name] = [value]
        audio.save()
    else:
        raise UnsupportedFormat(f"Writing tags is not supported for {type(audio).__name__}")


def _write_id3(tags: ID3, changes: dict[str, str | None], merged: dict[str, str | None]) -> None:
    for name, value in changes.items():
        if name in ID3_TEXT:
            frame_id = ID3_TEXT[name]
            tags.delall(frame_id)
            if value is not None:
                tags.add(Frames[frame_id](encoding=3, text=[value]))
        elif name == "date":
            tags.delall("TDRC")
            tags.delall("TYER")
            if value is not None:
                tags.add(TDRC(encoding=3, text=[value]))
        elif name == "genre":
            tags.delall("TCON")
            if value is not None:
                tags.add(TCON(encoding=3, text=[value]))
        elif name == "compilation":
            tags.delall("TCMP")
            if value is not None:
                tags.add(TCMP(encoding=3, text=[value]))
        elif name == "rating":
            stars = int(value) if value and value.isdigit() and 1 <= int(value) <= 5 else None
            frames = tags.getall("POPM")
            for frame in frames:
                del tags[frame.HashKey]
            if stars:
                email = frames[0].email if frames else POPM_EMAIL
                count = frames[0].count if frames and hasattr(frames[0], "count") else 0
                tags.add(POPM(email=email, rating=POPM_STARS[stars], count=count))
        if name in ID3_TXXX:
            desc = ID3_TXXX[name]
            for frame in tags.getall("TXXX"):
                if frame.desc.casefold() == desc.casefold():
                    del tags[frame.HashKey]
            if name == "mood":
                tags.delall("TMOO")
            if value is not None:
                tags.add(TXXX(encoding=3, desc=desc, text=[value]))
        if name in MUSICMATCH:
            desc = MUSICMATCH[name]
            had = any(frame.desc == desc for frame in tags.getall("COMM"))
            for frame in tags.getall("COMM"):
                if frame.desc == desc:
                    del tags[frame.HashKey]
            # Mood goes to the standard MOOD tag; MusicMatch's copy only if it had one
            if value is not None and (name != "mood" or had):
                tags.add(COMM(encoding=3, lang="eng", desc=desc, text=[value]))
    for number_key, total_key, frame_cls in (
        ("tracknumber", "tracktotal", TRCK),
        ("discnumber", "disctotal", TPOS),
    ):
        if number_key in changes or total_key in changes:
            tags.delall(frame_cls.__name__)
            pair = _join_pair(merged.get(number_key), merged.get(total_key))
            if pair:
                tags.add(frame_cls(encoding=3, text=[pair]))


def id3_tag_version(tags: ID3) -> int:
    return 4 if tags.version >= (2, 4, 0) else 3


def prepare_id3(tags: ID3, version: int) -> None:
    """Make the frames fit the ID3 version: mutagen keeps tags as 2.4 in memory, so for
    2.3 they are converted (TDRC to TYER, TDOR to TORY, ...); the sort frames that
    iTunes and others also use in 2.3 are kept."""
    if version == 3:
        kept = [frame for key in V23_KEEP for frame in tags.getall(key)]
        tags.update_to_v23()
        for frame in kept:
            tags.add(frame)
    else:
        tags.update_to_v24()


def _write_mp4(
    tags: MP4Tags, changes: dict[str, str | None], merged: dict[str, str | None]
) -> None:
    for name, value in changes.items():
        if name in MP4_TEXT:
            atom = MP4_TEXT[name]
            if value is None:
                tags.pop(atom, None)
            else:
                tags[atom] = [value]
        elif name == "compilation":
            if value in (None, "", "0"):
                tags.pop("cpil", None)
            else:
                tags["cpil"] = True
        if name in MP4_FREE:
            _set_freeform(tags, MP4_FREE[name], value)
        if name in MUSICMATCH:
            had = bool(_freeform(tags, MUSICMATCH[name]))
            # Mood goes to the standard MOOD atom; MusicMatch's copy only if it had one
            _set_freeform(tags, MUSICMATCH[name], value if name != "mood" or had else None)
    for number_key, total_key, atom in (
        ("tracknumber", "tracktotal", "trkn"),
        ("discnumber", "disctotal", "disk"),
    ):
        if number_key in changes or total_key in changes:
            number = merged.get(number_key)
            if number is None or not number.isdigit():
                tags.pop(atom, None)
            else:
                total = merged.get(total_key)
                tags[atom] = [(int(number), int(total) if total and total.isdigit() else 0)]


def _set_freeform(tags: MP4Tags, name: str, value: str | None) -> None:
    for atom in [a for a in tags if a.startswith(MP4_FREEFORM)]:
        if atom[len(MP4_FREEFORM) :].casefold() == name.casefold():
            del tags[atom]
    if value is not None:
        tags[MP4_FREEFORM + name] = [MP4FreeForm(value.encode("utf-8"))]


def _join_pair(number: str | None, total: str | None) -> str | None:
    if not number:
        return None
    return f"{number}/{total}" if total else number
