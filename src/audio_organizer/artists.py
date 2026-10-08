"""Artists and their sort names ("Alanis Morissette" filed as "Morissette, Alanis").

Sort names are the user's: set one by one, or by accepting suggestions. Suggestions come
from the library's own folders: a top folder "Morissette, Alanis" whose name, turned around,
is an artist's name ("Alanis Morissette") suggests that sort name. Names are not turned
around by rule, which would also file bands under their last word ("Floyd, Pink").
"""

from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import PurePosixPath

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .models import ArtistSortName, AudioFile, EffectiveMetadata, Library
from .pathparse import normalize_name, unflip
from .tracks import present


@dataclass
class Artist:
    name: str
    tracks: int
    sort_name: str | None  # set by the user
    suggestion: str | None  # from a folder name, when no sort name is set

    @property
    def key(self) -> str:
        return normalize_name(self.name)


def sort_names(session: Session, library: Library) -> dict[str, str]:
    """normalize_name(artist) -> sort name."""
    return {
        row.key: row.sort_name
        for row in session.scalars(
            select(ArtistSortName).where(ArtistSortName.library_id == library.id)
        )
    }


def sorted_name(names: dict[str, str], name: str | None) -> str | None:
    """The name to file an artist under: its sort name, or the name itself."""
    if not name:
        return name
    return names.get(normalize_name(name), name)


def set_sort_name(session: Session, library: Library, name: str, sort_name: str | None) -> None:
    """Set an artist's sort name; None or an empty one removes it."""
    key = normalize_name(name)
    session.execute(
        delete(ArtistSortName).where(
            ArtistSortName.library_id == library.id, ArtistSortName.key == key
        )
    )
    sort_name = (sort_name or "").strip()
    if sort_name and sort_name != name:
        session.add(ArtistSortName(library_id=library.id, key=key, name=name, sort_name=sort_name))


def album_artists(session: Session, library: Library) -> list[Artist]:
    """The artists that albums are filed under (album artist, or artist when there is
    none; compilations are left out), with track counts, sort names and suggestions."""
    rows = session.execute(
        select(AudioFile.path, EffectiveMetadata.albumartist, EffectiveMetadata.artist,
               EffectiveMetadata.compilation)
        .join(EffectiveMetadata, EffectiveMetadata.file_id == AudioFile.id)
        .where(present(library.id))
    ).all()  # fmt: skip
    spellings: dict[str, Counter] = defaultdict(Counter)
    folders: dict[str, str] = {}  # normalize_name of a turned-around folder name -> folder
    for path, albumartist, artist, compilation in rows:
        top = PurePosixPath(path).parts[0]
        if ", " in top and unflip(top) != top:
            folders.setdefault(normalize_name(unflip(top)), top)
        name = albumartist or artist
        if name and compilation != "1":
            spellings[normalize_name(name)][name] += 1
    names = sort_names(session, library)
    artists = []
    for key, counts in spellings.items():
        name = counts.most_common(1)[0][0]
        sort_name = names.get(key)
        suggestion = None if sort_name else folders.get(key)
        artists.append(Artist(name, sum(counts.values()), sort_name, suggestion))
    return sorted(artists, key=lambda a: (a.sort_name or a.name).casefold())


def accept_suggestions(session: Session, library: Library, artists: list[Artist]) -> int:
    count = 0
    for artist in artists:
        if artist.suggestion and not artist.sort_name:
            set_sort_name(session, library, artist.name, artist.suggestion)
            count += 1
    return count
