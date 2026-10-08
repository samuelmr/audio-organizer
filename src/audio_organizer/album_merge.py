"""Merging album folders that hold the same album.

iTunes and old copies leave one album in several folders ("Texas/Greatest Hits" and
"Texas/The Greatest Hits"). Two album folders are candidates for merging when they share
identical tracks, and either their album names are the same (ignoring case, punctuation
and a leading "The") or at least half of the smaller one's tracks are shared.

Merging a source folder into a target trashes the source's copies of tracks the target
already has (the target's copy is kept and static playlists move to it) and moves the
source's other tracks into the target. A track whose number the target already uses on
the same disc is a clash: such folders are usually different editions of an album, so
clashing tracks are not moved, and a merge with clashes is never proposed in a batch.
"""

from collections import Counter
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import changes as ch
from .models import AudioFile, Change, Changeset, EffectiveMetadata, FieldValue, Library
from .pathparse import normalize_name
from .tracks import present


@dataclass
class Track:
    file: AudioFile
    tracknumber: int | None
    discnumber: int | None
    is_copy: bool  # an iTunes copy ("Title 1.mp3")

    @property
    def name(self) -> str:
        return PurePosixPath(self.file.path).name


@dataclass
class Folder:
    path: str
    album: str  # the album name from the folder name
    compilation: bool
    tracks: list[Track] = field(default_factory=list)
    tag_albums: Counter = field(default_factory=Counter)  # album tags of its files

    @property
    def distinct(self) -> int:
        return len({t.file.audio_hash for t in self.tracks})

    @property
    def name(self) -> str:
        return PurePosixPath(self.path).name


@dataclass
class Merge:
    source: Folder
    target: Folder
    shared: int  # distinct tracks in both
    same_name: bool
    trash: list[tuple[Track, Track]] = field(default_factory=list)  # (source's, kept)
    move: list[tuple[Track, str]] = field(default_factory=list)  # (track, new path)
    clashes: list[Track] = field(default_factory=list)

    @property
    def batchable(self) -> bool:
        """Safe to propose without looking: same album name, nothing clashes."""
        return self.same_name and not self.clashes


@dataclass
class Survey:
    merges: list[Merge] = field(default_factory=list)
    pending: int = 0  # candidates with changes waiting for review

    @property
    def batchable(self) -> list[Merge]:
        return [m for m in self.merges if m.batchable]


def survey(session: Session, library: Library, *, path_prefix: str | None = None) -> Survey:
    folders = _load(session, library)
    by_hash: dict[str, set[str]] = {}
    for folder in folders.values():
        for track in folder.tracks:
            by_hash.setdefault(track.file.audio_hash, set()).add(folder.path)
    shared: Counter = Counter()
    for paths in by_hash.values():
        for a in paths:
            for b in paths:
                if a < b:
                    shared[(a, b)] += 1
    pending = set(
        session.scalars(select(Change.file_id).where(Change.status.in_(("proposed", "approved"))))
    )
    result = Survey()
    for (a, b), count in sorted(shared.items()):
        fa, fb = folders[a], folders[b]
        if path_prefix and not (a.startswith(path_prefix) or b.startswith(path_prefix)):
            continue
        if fa.compilation != fb.compilation:
            continue  # an album and a compilation sharing a track: both belong
        same_name = normalize_name(fa.album) == normalize_name(fb.album)
        if not same_name and count * 2 < min(fa.distinct, fb.distinct):
            continue
        if any(t.file.id in pending for t in fa.tracks + fb.tracks):
            result.pending += 1
            continue
        source, target = sorted((fa, fb), key=_target_score)
        result.merges.append(plan(source, target, shared=count, same_name=same_name))
    return result


def plan(source: Folder, target: Folder, *, shared: int = 0, same_name: bool = True) -> Merge:
    """What merging source into target would do."""
    merge = Merge(source, target, shared, same_name)
    kept: dict[str, Track] = {}
    for track in sorted(target.tracks, key=lambda t: t.is_copy):
        kept.setdefault(track.file.audio_hash, track)
    names = {t.name.casefold() for t in target.tracks}
    numbers = {(t.discnumber or 1, t.tracknumber) for t in target.tracks if t.tracknumber}
    for track in source.tracks:
        if track.file.audio_hash in kept:
            merge.trash.append((track, kept[track.file.audio_hash]))
        elif track.name.casefold() in names or (
            track.tracknumber and (track.discnumber or 1, track.tracknumber) in numbers
        ):
            merge.clashes.append(track)
        else:
            merge.move.append((track, f"{target.path}/{track.name}"))
            names.add(track.name.casefold())
            if track.tracknumber:
                numbers.add((track.discnumber or 1, track.tracknumber))
    return merge


def plan_folders(session: Session, library: Library, source: str, target: str) -> Merge:
    """A merge of two given album folders, e.g. the user's choice of direction."""
    folders = _load(session, library)
    for path in (source, target):
        if path not in folders:
            raise ValueError(f"{path} is not an album folder with tracks in the catalog")
    a, b = folders[source], folders[target]
    hashes = {t.file.audio_hash for t in b.tracks}
    count = len({t.file.audio_hash for t in a.tracks if t.file.audio_hash in hashes})
    same_name = normalize_name(a.album) == normalize_name(b.album)
    return plan(a, b, shared=count, same_name=same_name)


def batch(merges: list[Merge], limit: int) -> list[Merge]:
    """Up to limit batchable merges, no folder in more than one of them (merging A into
    B and B into C in one go would trip over each other)."""
    chosen: list[Merge] = []
    used: set[str] = set()
    for merge in merges:
        if len(chosen) >= limit:
            break
        if merge.batchable and not {merge.source.path, merge.target.path} & used:
            chosen.append(merge)
            used |= {merge.source.path, merge.target.path}
    return chosen


def propose(session: Session, merges: list[Merge], description: str) -> Changeset:
    changeset = ch.new_changeset(session, description, "album_merge")
    for merge in merges:
        for track, keep in merge.trash:
            ch.propose_trash(changeset, track.file, replacement=keep.file)
        for track, path in merge.move:
            ch.propose_move(changeset, track.file, path)
    session.commit()
    return changeset


def _target_score(folder: Folder) -> tuple:
    """Sorts the folder to keep last: more distinct tracks, then album tags that agree
    with its name, then fewer iTunes copies."""
    agreeing = sum(
        n
        for album, n in folder.tag_albums.items()
        if normalize_name(album) == normalize_name(folder.album)
    )
    copies = sum(t.is_copy for t in folder.tracks)
    return (folder.distinct, agreeing, -copies, -len(folder.path))


def _load(session: Session, library: Library) -> dict[str, Folder]:
    """The library's album folders (Artist/Album/..., with an album name from the
    folder) and their tracks."""
    path_fields: dict[int, dict[str, str]] = {}
    tag_album: dict[int, str] = {}
    for file_id, name, source, value in session.execute(
        select(FieldValue.file_id, FieldValue.field, FieldValue.source, FieldValue.value)
        .join(AudioFile, AudioFile.id == FieldValue.file_id)
        .where(present(library.id), FieldValue.source.in_(("path", "tag")))
        .where(FieldValue.field.in_(("album", "compilation", "copy_suffix")))
    ):
        if source == "path":
            path_fields.setdefault(file_id, {})[name] = value
        elif name == "album":
            tag_album[file_id] = value
    folders: dict[str, Folder] = {}
    rows = session.execute(
        select(AudioFile, EffectiveMetadata.tracknumber, EffectiveMetadata.discnumber)
        .join(EffectiveMetadata, EffectiveMetadata.file_id == AudioFile.id)
        .where(present(library.id), AudioFile.audio_hash.is_not(None))
    )
    for file, tracknumber, discnumber in rows:
        fields = path_fields.get(file.id, {})
        parts = PurePosixPath(file.path).parts
        if len(parts) < 3 or "album" not in fields:
            continue
        folder_path = str(PurePosixPath(file.path).parent)
        folder = folders.get(folder_path)
        if folder is None:
            folder = folders[folder_path] = Folder(
                folder_path, fields["album"], fields.get("compilation") == "1"
            )
        folder.tracks.append(Track(file, tracknumber, discnumber, "copy_suffix" in fields))
        if file.id in tag_album:
            folder.tag_albums[tag_album[file.id]] += 1
    return folders
