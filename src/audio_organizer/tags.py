"""Reading and writing tags as a flat dict of normalized fields.

Field values are strings; multiple values are joined with "; ". Track and disc
numbers are split into number and total and stripped of leading zeros.
The mood/tempo/situation/preference comments written by MusicMatch Jukebox are
read as fields of their own.
"""

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO

import mutagen
from mutagen.aac import AAC, AACError
from mutagen.id3 import COMM, ID3, TCMP, TCON, TDRC, TPOS, TRCK, Frames, ID3NoHeaderError
from mutagen.mp4 import MP4FreeForm, MP4Tags

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
    "genre",
    "compilation",
    "mood",
    "tempo",
    "situation",
    "preference",
)

ID3_TEXT = {"artist": "TPE1", "albumartist": "TPE2", "album": "TALB", "title": "TIT2"}
MP4_TEXT = {
    "artist": "\xa9ART",
    "albumartist": "aART",
    "album": "\xa9alb",
    "title": "\xa9nam",
    "date": "\xa9day",
    "genre": "\xa9gen",
}
MUSICMATCH = {
    "mood": "MusicMatch_Mood",
    "tempo": "MusicMatch_Tempo",
    "situation": "MusicMatch_Situation",
    "preference": "MusicMatch_Preference",
}
MP4_FREEFORM = "----:com.apple.iTunes:"


class UnsupportedFormat(Exception):
    pass


@dataclass
class AudioInfo:
    format: str | None = None
    duration: float | None = None
    bitrate: int | None = None
    sample_rate: int | None = None
    fields: dict[str, str] = field(default_factory=dict)


def _load(source: str | Path | BinaryIO) -> tuple[mutagen.FileType | None, object]:
    """mutagen's object for a file, and its tags.

    Raw AAC streams (ADTS) with an ID3 tag in front lead mutagen's format detection to
    MP3, which then fails, and mutagen's AAC reader ignores the tag; so .aac files are
    read as AAC, with the ID3 tag loaded on its own."""
    name = str(getattr(source, "name", source))
    if name.lower().endswith(".aac"):
        try:
            audio = AAC(source)
        except AACError:
            pass  # e.g. an MP4 file named .aac
        else:
            _rewind(source)
            try:
                return audio, ID3(source)
            except ID3NoHeaderError:
                return audio, None
        _rewind(source)
    audio = mutagen.File(source)
    return audio, audio.tags if audio is not None else None


def _rewind(source) -> None:
    if hasattr(source, "seek"):
        source.seek(0)


def read(path: str | Path | BinaryIO) -> AudioInfo:
    """Read a file given by path, or an open, seekable binary file object."""
    audio, tags = _load(path)
    if audio is None:
        return AudioInfo()
    info = AudioInfo(
        format=type(audio).__name__.lower(),
        duration=getattr(audio.info, "length", None),
        bitrate=getattr(audio.info, "bitrate", None) or None,
        sample_rate=getattr(audio.info, "sample_rate", None) or None,
    )
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
    return info


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
    return raw


def _frame_text(tags: ID3, frame_id: str) -> list:
    return [str(t) for frame in tags.getall(frame_id) for t in frame.text]


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
        raw[name] = [bytes(v).decode("utf-8", "replace") for v in tags.get(MP4_FREEFORM + desc, [])]
    return raw


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


def write(path: str | Path, changes: dict[str, str | None]) -> None:
    """Set the given fields (None removes the field), leaving all other tags as they are."""
    unknown = set(changes) - set(FIELDS)
    if unknown:
        raise ValueError(f"Unknown fields: {', '.join(sorted(unknown))}")
    audio, tags = _load(path)
    if audio is None:
        raise UnsupportedFormat(f"Not a recognized audio file: {path}")
    current = read(path).fields
    merged = {**current, **changes}
    if isinstance(audio, AAC):  # an ID3 tag in front of the stream; see _load
        tags = tags if tags is not None else ID3()
        _write_id3(tags, changes, merged)
        tags.save(path, v2_version=4 if tags.version >= (2, 4, 0) else 3)
        return
    if tags is None:
        audio.add_tags()
        tags = audio.tags
    if isinstance(tags, ID3):
        _write_id3(tags, changes, merged)
        audio.save(v2_version=4 if tags.version >= (2, 4, 0) else 3)
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
        elif name in MUSICMATCH:
            desc = MUSICMATCH[name]
            for frame in tags.getall("COMM"):
                if frame.desc == desc:
                    del tags[frame.HashKey]
            if value is not None:
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
        elif name in MUSICMATCH:
            atom = MP4_FREEFORM + MUSICMATCH[name]
            if value is None:
                tags.pop(atom, None)
            else:
                tags[atom] = [MP4FreeForm(value.encode("utf-8"))]
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


def _join_pair(number: str | None, total: str | None) -> str | None:
    if not number:
        return None
    return f"{number}/{total}" if total else number
