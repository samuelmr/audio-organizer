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
    Index,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    text,
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
    # Where playlist files are written, relative to the root ("" is the root itself), and
    # in which formats (comma-separated)
    playlist_folder: Mapped[str] = mapped_column(Text, default="", server_default="")
    playlist_formats: Mapped[str] = mapped_column(String(50), default="m3u", server_default="m3u")

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


class PendingField(Base):
    """A value the catalog has decided on (an edit, a measurement, a lookup or a rule) that
    the file's tags do not hold yet: what writing the file would change. Kept up to date
    together with EffectiveMetadata, so that the pending changes of a whole library can be
    counted and grouped by artist and album in SQL.

    Derived data, rebuilt with EffectiveMetadata (db.py)."""

    __tablename__ = "pending_field"

    file_id: Mapped[int] = mapped_column(
        ForeignKey("audio_file.id", ondelete="CASCADE"), primary_key=True
    )
    field: Mapped[str] = mapped_column(String(40), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(20))
    tag: Mapped[str | None] = mapped_column(Text)  # the tag's value now, None if it has none

    file: Mapped[AudioFile] = relationship()


class Operation(Base):
    """A decision the user made in the catalog: an edit, a rule applied to the library,
    lookups accepted, edits discarded. It changes FieldValues only (no file is touched;
    files with values not yet in their tags are pending, see writing.py), and records each
    value's before and after, so that it can be undone (operations.py). Undoing is itself
    an operation, which can be undone in turn."""

    __tablename__ = "operation"

    id: Mapped[int] = mapped_column(primary_key=True)
    library_id: Mapped[int] = mapped_column(ForeignKey("library.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    kind: Mapped[str] = mapped_column(String(20))  # one of operations.KINDS
    description: Mapped[str] = mapped_column(Text, default="")
    # The operation this one undoes (kind "undo"), and the one that undid this one
    reverts: Mapped[int | None] = mapped_column(ForeignKey("operation.id"))
    reverted_by: Mapped[int | None] = mapped_column(ForeignKey("operation.id"))

    values: Mapped[list["OperationValue"]] = relationship(
        back_populates="operation", cascade="all, delete-orphan", passive_deletes=True,
        order_by="OperationValue.id",
    )  # fmt: skip


class OperationValue(Base):
    """One FieldValue an operation set, changed or removed: None before means it had none,
    None after that the operation removed it."""

    __tablename__ = "operation_value"

    id: Mapped[int] = mapped_column(primary_key=True)
    operation_id: Mapped[int] = mapped_column(
        ForeignKey("operation.id", ondelete="CASCADE"), index=True
    )
    file_id: Mapped[int] = mapped_column(
        ForeignKey("audio_file.id", ondelete="CASCADE"), index=True
    )
    field: Mapped[str] = mapped_column(String(40))
    source: Mapped[str] = mapped_column(String(20))
    before: Mapped[str | None] = mapped_column(Text)
    after: Mapped[str | None] = mapped_column(Text)

    operation: Mapped[Operation] = relationship(back_populates="values")
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
    # The feature that proposed it (one of changes.SOURCES); None for changesets made
    # before the column existed.
    source: Mapped[str | None] = mapped_column(String(30), index=True)

    changes: Mapped[list["Change"]] = relationship(
        back_populates="changeset", cascade="all, delete-orphan", order_by="Change.id"
    )


class Change(Base):
    """One proposed modification of one file.

    kind "retag": payload {"fields": {field: new value or None to remove}}
    kind "move":  payload {"to": new relative path}
    kind "cover": payload {"image": library path} or {"cover_image": CoverImage sha1},
                  and "replace": true to replace a (too small) embedded cover
    kind "image": payload {"cover_image": sha1, "to": library path, "label"}: save an image
                  file (a folder's cover.jpg); "replace": true to overwrite a thumbnail
    kind "leftover": payload {"library_id", "path", "size", "mtime_ns"}: trash a file the
                  catalog does not know (litter in a folder without music); has no file
    kind "rename": payload {"library_id", "path", "to", "size", "mtime_ns"}: rename a file
                  the catalog does not know (an album's image to cover.jpg); has no file
    status: proposed -> approved | rejected; approved -> applied | failed; applied -> undone
    """

    __tablename__ = "change"

    id: Mapped[int] = mapped_column(primary_key=True)
    changeset_id: Mapped[int] = mapped_column(
        ForeignKey("changeset.id", ondelete="CASCADE"), index=True
    )
    file_id: Mapped[int | None] = mapped_column(ForeignKey("audio_file.id"), index=True)
    kind: Mapped[str] = mapped_column(String(20))
    payload: Mapped[dict[str, Any]]
    status: Mapped[str] = mapped_column(String(20), default="proposed")
    undo: Mapped[dict[str, Any] | None]
    error: Mapped[str | None] = mapped_column(Text)
    applied_at: Mapped[datetime | None]

    changeset: Mapped[Changeset] = relationship(back_populates="changes")
    file: Mapped[AudioFile | None] = relationship()


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
    # Where playlist files go, relative to the prefix ("" is the prefix itself), and in
    # which formats (comma-separated)
    playlist_folder: Mapped[str] = mapped_column(Text, default="", server_default="")
    playlist_formats: Mapped[str] = mapped_column(
        String(50), default="m3u,json,html", server_default="m3u,json,html"
    )

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


class SyncJob(Base):
    """A sync run, started from the CLI or the web UI: its progress, so that any page can
    show it, and a lock, so that two runs never sync the same target at once. A run whose
    process has gone counts as interrupted; the next sync resumes from the catalog."""

    __tablename__ = "sync_job"

    id: Mapped[int] = mapped_column(primary_key=True)
    target_id: Mapped[int] = mapped_column(
        ForeignKey("sync_target.id", ondelete="CASCADE"), index=True
    )
    origin: Mapped[str] = mapped_column(String(10))  # cli | web
    # planning | running | done | failed | stopped | changed | interrupted
    status: Mapped[str] = mapped_column(String(20))
    stop_requested: Mapped[bool] = mapped_column(default=False)
    host: Mapped[str] = mapped_column(String(255))  # where the run's process is
    pid: Mapped[int] = mapped_column()
    started_at: Mapped[datetime] = mapped_column(default=utcnow)
    heartbeat_at: Mapped[datetime] = mapped_column(default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column()
    total: Mapped[int] = mapped_column(default=0)  # actions
    done: Mapped[int] = mapped_column(default=0)
    failed: Mapped[int] = mapped_column(default=0)
    bytes_total: Mapped[int] = mapped_column(BigInteger, default=0)  # to upload
    bytes_done: Mapped[int] = mapped_column(BigInteger, default=0)
    current: Mapped[str | None] = mapped_column(Text)  # the last action completed
    message: Mapped[str | None] = mapped_column(Text)  # why it failed or what it did
    errors: Mapped[list[Any]] = mapped_column(JSON, default=list)  # the first ERRORS_KEPT

    target: Mapped[SyncTarget] = relationship()


class Playlist(Base):
    """A smart playlist (a rule over the catalog) or a static, curated one.

    A smart playlist with group_by generates one playlist per value of that field; its
    name then contains the field as a placeholder, e.g. "Best Of {mood}"."""

    __tablename__ = "playlist"
    # A smart and a static playlist may share a name (a static one imported from a file
    # next to the smart one that wrote it), so the name is unique per kind
    __table_args__ = (
        Index(
            "uq_playlist_library_name_kind",
            "library_id",
            "name",
            text("(rule IS NULL)"),
            unique=True,
        ),
    )

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


class PlaylistFile(Base):
    """A playlist file this tool wrote, on the library disk (target_id None) or in a sync
    target's bucket: its path relative to the library root or the target's prefix, and a
    hash of the content. A file whose content still has that hash is the tool's own
    output; one that differs was changed by someone else."""

    __tablename__ = "playlist_file"
    __table_args__ = (UniqueConstraint("library_id", "target_id", "path"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    library_id: Mapped[int] = mapped_column(
        ForeignKey("library.id", ondelete="CASCADE"), index=True
    )
    target_id: Mapped[int | None] = mapped_column(
        ForeignKey("sync_target.id", ondelete="CASCADE"), index=True
    )
    playlist_id: Mapped[int | None] = mapped_column(
        ForeignKey("playlist.id", ondelete="SET NULL"), index=True
    )
    path: Mapped[str] = mapped_column(Text)
    format: Mapped[str] = mapped_column(String(10))
    content_hash: Mapped[str] = mapped_column(String(40))
    size: Mapped[int]
    written_at: Mapped[datetime] = mapped_column(default=utcnow)


class PlaylistFileBackup(Base):
    """The content of a playlist file before this tool overwrote it (when it had been
    changed by someone else) or deleted it, so that the change can be undone."""

    __tablename__ = "playlist_file_backup"

    id: Mapped[int] = mapped_column(primary_key=True)
    library_id: Mapped[int] = mapped_column(
        ForeignKey("library.id", ondelete="CASCADE"), index=True
    )
    target_id: Mapped[int | None] = mapped_column(
        ForeignKey("sync_target.id", ondelete="CASCADE"), index=True
    )
    path: Mapped[str] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(String(20))  # overwritten | deleted
    content: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    restored_at: Mapped[datetime | None]


class LibrarySetting(Base):
    """A per-library setting, such as the folder layout ("layout")."""

    __tablename__ = "library_setting"

    library_id: Mapped[int] = mapped_column(
        ForeignKey("library.id", ondelete="CASCADE"), primary_key=True
    )
    key: Mapped[str] = mapped_column(String(50), primary_key=True)
    value: Mapped[str] = mapped_column(Text)


class ArtistSortName(Base):
    """How an artist is sorted and filed, e.g. "Alanis Morissette" as "Morissette, Alanis".
    Set by the user, per library; artists without one are filed under their name."""

    __tablename__ = "artist_sort_name"

    library_id: Mapped[int] = mapped_column(
        ForeignKey("library.id", ondelete="CASCADE"), primary_key=True
    )
    key: Mapped[str] = mapped_column(Text, primary_key=True)  # pathparse.normalize_name
    name: Mapped[str] = mapped_column(Text)
    sort_name: Mapped[str] = mapped_column(Text)


class CoverImage(Base):
    """A cover image kept in the catalog, by the SHA-1 of its data: one found online
    (source "coverartarchive"), or an embedded cover that a change replaced, so that undo
    can put it back (source "replaced")."""

    __tablename__ = "cover_image"

    sha1: Mapped[str] = mapped_column(String(40), primary_key=True)
    mime: Mapped[str] = mapped_column(String(20))
    width: Mapped[int | None]
    height: Mapped[int | None]
    data: Mapped[bytes] = mapped_column(LargeBinary, deferred=True)  # loaded when used
    source: Mapped[str] = mapped_column(String(20))
    url: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    @property
    def size(self) -> tuple[int, int] | None:
        return (self.width, self.height) if self.width and self.height else None


class FolderImages(Base):
    """The image files in a library folder and the dimensions of the one that serves as its
    cover, so that the Covers page need not list folders on a network share on every view.
    A cache of what the disk says, not of other catalog data: dir_mtime_ns is the folder's
    modification time when it was listed, and a folder is listed again when that has
    changed (covers.survey). Safe to delete; it is filled again as pages are viewed."""

    __tablename__ = "folder_images"

    library_id: Mapped[int] = mapped_column(
        ForeignKey("library.id", ondelete="CASCADE"), primary_key=True
    )
    folder: Mapped[str] = mapped_column(Text, primary_key=True)  # relative to the root
    dir_mtime_ns: Mapped[int] = mapped_column(BigInteger)
    images: Mapped[list[str]] = mapped_column(JSON)  # names of the image files
    cover: Mapped[str | None] = mapped_column(Text)  # sync.pick_cover of them
    cover_width: Mapped[int | None]
    cover_height: Mapped[int | None]


class FileDesire(Base):
    """What the user decided a file should become (covers.decide, duplicates.decide) and
    the files do not hold yet: a cover embedded (kind "embed"), an image file saved in its
    folder (kind "image", kept on one of the folder's tracks), the file itself in the
    trash (kind "trash"), or the file at another path (kind "move"). It is pending until
    writing.write has made it so; undoing that write makes it pending again, undoing the
    decision (an operation) removes it.

    spec, for a cover: {"cover_image": CoverImage sha1, "label"} or {"image": library path},
    with "to" (library path to save as, for "image") and "replace" (a thumbnail goes) when
    set; for the trash: {"replacement": id, "replacement_path"} of the copy that stays, and
    "same": "recording" when it is another encoding; for a move: {"to": library path} and
    "extras": [[from, to], ...], the folder's other files that go with the file."""

    __tablename__ = "file_desire"

    file_id: Mapped[int] = mapped_column(
        ForeignKey("audio_file.id", ondelete="CASCADE"), primary_key=True
    )
    kind: Mapped[str] = mapped_column(String(10), primary_key=True)  # embed | image | trash | move
    spec: Mapped[dict[str, Any]]
    written: Mapped[bool] = mapped_column(default=False, index=True)

    file: Mapped[AudioFile] = relationship()


class CoverLookup(Base):
    """The outcome of looking up an album's cover online, so that a scan asks only
    once. Albums are keyed by artist and title (pathparse.normalize_name), not by folder,
    so copies of an album share the lookup and it survives reorganizing."""

    __tablename__ = "cover_lookup"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    artist: Mapped[str] = mapped_column(Text)
    album: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20))  # found | not_found
    release_group: Mapped[str | None] = mapped_column(String(36))  # MusicBrainz id
    image_sha1: Mapped[str | None] = mapped_column(ForeignKey("cover_image.sha1"))
    min_pixels: Mapped[int]  # the size asked for; a larger one is looked up again
    looked_up_at: Mapped[datetime] = mapped_column(default=utcnow)

    image: Mapped[CoverImage | None] = relationship()


class GnudbLookup(Base):
    """The outcome of looking up an album folder in gnudb, so that a scan asks only
    once. Keyed by folder and album title; a folder whose track count changed is looked
    up again."""

    __tablename__ = "gnudb_lookup"

    library_id: Mapped[int] = mapped_column(
        ForeignKey("library.id", ondelete="CASCADE"), primary_key=True
    )
    folder: Mapped[str] = mapped_column(Text, primary_key=True)
    key: Mapped[str] = mapped_column(Text, primary_key=True)  # covers.album_key
    tracks: Mapped[int]
    status: Mapped[str] = mapped_column(String(20))  # found | not_found
    category: Mapped[str | None] = mapped_column(String(20))  # gnudb's genre category
    discid: Mapped[str | None] = mapped_column(String(8))  # of the entry that matched
    looked_up_at: Mapped[datetime] = mapped_column(default=utcnow)
