"""Star ratings from MusicMatch Jukebox's "preference" values.

MusicMatch kept the user's opinion of a track in a comment ("Excellent", "Very Good",
...), which only it and this tool read. A mapping from those words to 1-5 stars turns
them into ratings that other players read. The mapping is the user's (stored per
library); applying it sets ratings from the "rule" source, which ranks below the file's
own tags, so a rating already in a file is never overridden. Applying is an operation
that can be undone. The ratings are then written into the files like any other value not
yet in them.
"""

import json
from collections import Counter

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import operations as ops
from .models import AudioFile, FieldValue, Library, LibrarySetting
from .tracks import present

DEFAULT_MAP = {
    "Excellent": 5,
    "Very Good": 4,
    "Good": 3,
    "Fair": 2,
    "Poor": 1,
    "Bad Taste": 1,
}
SETTING = "preference_ratings"


def get_map(session: Session, library: Library) -> dict[str, int | None]:
    setting = session.get(LibrarySetting, (library.id, SETTING))
    return json.loads(setting.value) if setting else dict(DEFAULT_MAP)


def set_map(session: Session, library: Library, mapping: dict[str, int | None]) -> None:
    for value, stars in mapping.items():
        if stars is not None and stars not in range(1, 6):
            raise ValueError(f"{value}: stars must be 1-5 or none, not {stars}")
    session.merge(LibrarySetting(library_id=library.id, key=SETTING, value=json.dumps(mapping)))


def preferences(session: Session, library: Library) -> Counter:
    """The preference values in the library's tags, with how many files have each."""
    return Counter(
        session.scalars(
            select(FieldValue.value)
            .join(AudioFile, AudioFile.id == FieldValue.file_id)
            .where(present(library.id), FieldValue.source == "tag")
            .where(FieldValue.field == "preference")
        )
    )


def apply(session: Session, library: Library, mapping: dict[str, int | None]) -> int:
    """Set the rule ratings of files with a preference; values mapped to no stars get no
    rating from the rule. Returns how many files got a rating."""
    rows = session.execute(
        select(FieldValue.file_id, FieldValue.value)
        .join(AudioFile, AudioFile.id == FieldValue.file_id)
        .where(present(library.id), FieldValue.source == "tag")
        .where(FieldValue.field == "preference")
    ).all()
    values = [
        ops.Value(file_id, "rule", "rating", str(mapping[p]) if mapping.get(p) else None)
        for file_id, p in rows
    ]
    ops.record(session, library.id, "ratings", "Ratings from MusicMatch preferences", values)
    session.commit()
    return sum(1 for value in values if value.value)
