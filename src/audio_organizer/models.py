"""Catalog schema.

Tags are evidence, not truth: every candidate value of a metadata field is stored as a
FieldValue with its source (file tag, path, manual edit, gnudb, ...), and the effective
value is resolved from them (see provenance.py). Files are identified across moves and
retags by a hash of the audio data alone (see audiohash.py). Every modification to files
goes through a Changeset of reviewable Changes that record how to undo them.
"""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    DateTime,
    Float,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSON, datetime: DateTime(timezone=True)}


class Library(Base):
    """A collection root. Paths of its files are stored relative to it."""

    __tablename__ = "library"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    root: Mapped[str] = mapped_column(Text)

    files: Mapped[list["AudioFile"]] = relationship(back_populates="library")


class AudioFile(Base):
    __tablename__ = "audio_file"
    __table_args__ = (UniqueConstraint("library_id", "path"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    library_id: Mapped[int] = mapped_column(ForeignKey("library.id"), index=True)
    path: Mapped[str] = mapped_column(Text)  # POSIX path relative to the library root
    size: Mapped[int] = mapped_column(BigInteger)
    mtime_ns: Mapped[int] = mapped_column(BigInteger)
    audio_hash: Mapped[str | None] = mapped_column(String(32), index=True)
    format: Mapped[str | None] = mapped_column(String(20))
    duration: Mapped[float | None] = mapped_column(Float)
    bitrate: Mapped[int | None]
    sample_rate: Mapped[int | None]
    missing: Mapped[bool] = mapped_column(default=False)
    error: Mapped[str | None] = mapped_column(Text)
    first_seen: Mapped[datetime] = mapped_column(default=utcnow)
    scanned_at: Mapped[datetime] = mapped_column(default=utcnow)

    library: Mapped[Library] = relationship(back_populates="files")
    values: Mapped[list["FieldValue"]] = relationship(
        back_populates="file", cascade="all, delete-orphan", passive_deletes=True
    )
    snapshots: Mapped[list["TagSnapshot"]] = relationship(
        back_populates="file", cascade="all, delete-orphan", passive_deletes=True
    )


class FieldValue(Base):
    """One candidate value for one metadata field of a file, from one source."""

    __tablename__ = "field_value"
    __table_args__ = (UniqueConstraint("file_id", "field", "source"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    file_id: Mapped[int] = mapped_column(
        ForeignKey("audio_file.id", ondelete="CASCADE"), index=True
    )
    field: Mapped[str] = mapped_column(String(40), index=True)
    value: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(20))
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)

    file: Mapped[AudioFile] = relationship(back_populates="values")


class EffectiveMetadata(Base):
    """The effective value of each tag field of a file, as provenance.resolve chooses it
    from the FieldValues, so that the catalog can be searched and sorted in SQL. Kept up
    to date by provenance.set_value(s); never written directly.

    Derived data: when its columns change, the table is rebuilt (db.py), so it needs no
    migrations. Track and disc numbers and totals are numbers here (None when the value
    is not one); year is the year of the date."""

    __tablename__ = "effective_metadata"

    file_id: Mapped[int] = mapped_column(
        ForeignKey("audio_file.id", ondelete="CASCADE"), primary_key=True
    )
    artist: Mapped[str | None] = mapped_column(Text, index=True)
    albumartist: Mapped[str | None] = mapped_column(Text)
    album: Mapped[str | None] = mapped_column(Text, index=True)
    title: Mapped[str | None] = mapped_column(Text, index=True)
    tracknumber: Mapped[int | None]
    tracktotal: Mapped[int | None]
    discnumber: Mapped[int | None]
    disctotal: Mapped[int | None]
    date: Mapped[str | None] = mapped_column(Text)
    year: Mapped[int | None] = mapped_column(index=True)
    genre: Mapped[str | None] = mapped_column(Text)
    compilation: Mapped[str | None] = mapped_column(Text)
    mood: Mapped[str | None] = mapped_column(Text)
    tempo: Mapped[str | None] = mapped_column(Text)
    situation: Mapped[str | None] = mapped_column(Text)
    preference: Mapped[str | None] = mapped_column(Text)
    # Some field's sources disagree beyond case and punctuation
    disputed: Mapped[bool] = mapped_column(default=False, index=True)

    file: Mapped[AudioFile] = relationship()


class TagSnapshot(Base):
    """The tags of a file as they were at some point, e.g. when the file was first seen."""

    __tablename__ = "tag_snapshot"

    id: Mapped[int] = mapped_column(primary_key=True)
    file_id: Mapped[int] = mapped_column(
        ForeignKey("audio_file.id", ondelete="CASCADE"), index=True
    )
    taken_at: Mapped[datetime] = mapped_column(default=utcnow)
    reason: Mapped[str] = mapped_column(String(20))
    tags: Mapped[dict[str, Any]]

    file: Mapped[AudioFile] = relationship(back_populates="snapshots")


class Changeset(Base):
    """A batch of proposed changes the user reviews, applies and can undo together."""

    __tablename__ = "changeset"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    description: Mapped[str] = mapped_column(Text, default="")

    changes: Mapped[list["Change"]] = relationship(
        back_populates="changeset", cascade="all, delete-orphan", order_by="Change.id"
    )


class Change(Base):
    """One proposed modification of one file.

    kind "retag": payload {"fields": {field: new value or None to remove}}
    kind "move":  payload {"to": new relative path}
    status: proposed -> approved | rejected; approved -> applied | failed; applied -> undone
    """

    __tablename__ = "change"

    id: Mapped[int] = mapped_column(primary_key=True)
    changeset_id: Mapped[int] = mapped_column(
        ForeignKey("changeset.id", ondelete="CASCADE"), index=True
    )
    file_id: Mapped[int] = mapped_column(ForeignKey("audio_file.id"), index=True)
    kind: Mapped[str] = mapped_column(String(20))
    payload: Mapped[dict[str, Any]]
    status: Mapped[str] = mapped_column(String(20), default="proposed")
    undo: Mapped[dict[str, Any] | None]
    error: Mapped[str | None] = mapped_column(Text)
    applied_at: Mapped[datetime | None]

    changeset: Mapped[Changeset] = relationship(back_populates="changes")
    file: Mapped[AudioFile] = relationship()


class SyncTarget(Base):
    """An object storage bucket (and key prefix) that a library is synced to.
    Credentials are not stored: they come from the usual AWS sources (environment,
    ~/.aws, or the named profile)."""

    __tablename__ = "sync_target"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    library_id: Mapped[int] = mapped_column(ForeignKey("library.id"))
    bucket: Mapped[str] = mapped_column(String(255))
    prefix: Mapped[str] = mapped_column(Text, default="")
    endpoint_url: Mapped[str | None] = mapped_column(Text)
    region: Mapped[str | None] = mapped_column(String(50))
    profile: Mapped[str | None] = mapped_column(String(100))

    library: Mapped[Library] = relationship()


class RemoteObject(Base):
    """What this tool knows to be stored under a key of a sync target: which local
    file (or cover image) version it came from, and with which metadata."""

    __tablename__ = "remote_object"
    __table_args__ = (UniqueConstraint("target_id", "key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    target_id: Mapped[int] = mapped_column(
        ForeignKey("sync_target.id", ondelete="CASCADE"), index=True
    )
    key: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(String(20))  # audio | image
    file_id: Mapped[int | None] = mapped_column(
        ForeignKey("audio_file.id", ondelete="SET NULL"), index=True
    )
    size: Mapped[int] = mapped_column(BigInteger)
    mtime_ns: Mapped[int | None] = mapped_column(BigInteger)  # of the local source
    meta_digest: Mapped[str | None] = mapped_column(String(40))  # None: metadata unknown
    synced_at: Mapped[datetime] = mapped_column(default=utcnow)


class Playlist(Base):
    """A smart playlist (a rule over the catalog) or a static, curated one.

    A smart playlist with group_by generates one playlist per value of that field; its
    name then contains the field as a placeholder, e.g. "Best Of {mood}"."""

    __tablename__ = "playlist"
    __table_args__ = (UniqueConstraint("library_id", "name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    library_id: Mapped[int] = mapped_column(ForeignKey("library.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    rule: Mapped[str | None] = mapped_column(Text)  # None for a static playlist
    group_by: Mapped[str | None] = mapped_column(String(40))
    sort: Mapped[str] = mapped_column(String(20))
    max_tracks: Mapped[int | None]
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)

    library: Mapped[Library] = relationship()
    entries: Mapped[list["PlaylistEntry"]] = relationship(
        back_populates="playlist",
        cascade="all, delete-orphan",
        order_by="PlaylistEntry.position",
    )

    @property
    def is_smart(self) -> bool:
        return self.rule is not None


class PlaylistEntry(Base):
    """A track of a static playlist. Entries refer to files by id, so they follow the
    files when they are moved; an imported entry that matched no file keeps its path."""

    __tablename__ = "playlist_entry"

    id: Mapped[int] = mapped_column(primary_key=True)
    playlist_id: Mapped[int] = mapped_column(
        ForeignKey("playlist.id", ondelete="CASCADE"), index=True
    )
    position: Mapped[int]
    file_id: Mapped[int | None] = mapped_column(
        ForeignKey("audio_file.id", ondelete="SET NULL"), index=True
    )
    path: Mapped[str] = mapped_column(Text)

    playlist: Mapped[Playlist] = relationship(back_populates="entries")
