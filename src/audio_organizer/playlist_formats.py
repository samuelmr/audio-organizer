"""Playlist file formats: extended M3U, and schema.org MusicPlaylist as JSON-LD and as
an HTML page with microdata.

Each writer takes the playlist name and (track, location) pairs, where the location is
how the track is referred to from the playlist: a relative path or URL.
"""

import html
import json
from collections.abc import Sequence
from urllib.parse import quote

from .tracks import Track

FORMATS = {
    "m3u": "audio/x-mpegurl",
    "json": "application/ld+json",
    "html": "text/html; charset=utf-8",
}

Entries = Sequence[tuple[Track, str]]


def render(fmt: str, name: str, entries: Entries) -> str:
    return {"m3u": to_m3u, "json": to_json, "html": to_html}[fmt](name, entries)


def to_m3u(name: str, entries: Entries) -> str:
    lines = ["#EXTM3U", f"#PLAYLIST:{name}"]
    for track, location in entries:
        seconds = round(track.duration) if track.duration else -1
        lines.append(f"#EXTINF:{seconds},{_display(track)}")
        lines.append(location)
    return "\n".join(lines) + "\n"


def to_json(name: str, entries: Entries) -> str:
    data = {
        "@context": "https://schema.org",
        "@type": "MusicPlaylist",
        "name": name,
        "numTracks": len(entries),
        "track": [_recording(track, location) for track, location in entries],
    }
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def to_html(name: str, entries: Entries) -> str:
    h = html.escape
    items = []
    for track, location in entries:
        r = _recording(track, location)
        details = [
            f'<span itemprop="{prop}">{h(r[prop])}</span>'
            for prop in ("inAlbum", "datePublished", "genre")
            if prop in r
        ]
        duration = ""
        if "duration" in r:
            seconds = round(track.duration)
            shown = (
                f"{seconds // 3600}:{seconds % 3600 // 60:02}:{seconds % 60:02}"
                if seconds >= 3600
                else f"{seconds // 60}:{seconds % 60:02}"
            )
            duration = f'    <meta itemprop="duration" content="{r["duration"]}">{shown}\n'
        items.append(
            '   <li itemprop="track" itemscope itemtype="https://schema.org/MusicRecording">\n'
            f'    <span itemprop="byArtist">{h(r.get("byArtist", ""))}</span>:\n'
            f'    <a itemprop="url" href="{h(quote(location, safe="/:"))}">'
            f'<span itemprop="name">{h(r["name"])}</span></a>\n'
            + (f"    ({', '.join(details)})\n" if details else "")
            + duration
            + (
                f'    <meta itemprop="keywords" content="{h(r["keywords"])}">\n'
                if "keywords" in r
                else ""
            )
            + "   </li>\n"
        )
    return (
        '<!DOCTYPE html>\n<html>\n<head>\n <meta charset="utf-8">\n'
        f" <title>{h(name)}</title>\n</head>\n<body>\n"
        '<div itemscope itemtype="https://schema.org/MusicPlaylist">\n'
        f' <h1 itemprop="name">{h(name)}</h1>\n'
        f' <meta itemprop="numTracks" content="{len(entries)}">\n'
        " <ol>\n" + "".join(items) + " </ol>\n</div>\n</body>\n</html>\n"
    )


def iso_duration(seconds: float | None) -> str | None:
    """PT4M38S"""
    if not seconds or seconds <= 0:
        return None
    s = round(seconds)
    hours, minutes = s // 3600, s % 3600 // 60
    return "PT" + (f"{hours}H" if hours else "") + (f"{minutes}M" if minutes else "") + f"{s % 60}S"


def _recording(track: Track, location: str) -> dict[str, str]:
    values = {
        "@type": "MusicRecording",
        "byArtist": track.get("artist"),
        "duration": iso_duration(track.duration),
        "inAlbum": track.get("album"),
        "datePublished": track.get("year"),
        "genre": track.get("genre"),
        "keywords": track.get("mood"),
        "name": track.get("title") or _stem(track.path),
        "url": location,
    }
    return {k: v for k, v in values.items() if v}


def _display(track: Track) -> str:
    title = track.get("title") or _stem(track.path)
    artist = track.get("artist")
    return f"{artist} - {title}" if artist else title


def _stem(path: str) -> str:
    name = path.rsplit("/", 1)[-1]
    return name.rsplit(".", 1)[0]
