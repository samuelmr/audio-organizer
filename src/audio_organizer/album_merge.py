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
When all of the source's tracks leave, its other files (cover, booklet, .cue, Scans) move
too, so that the folder disappears; see leftovers().
"""

import filecmp
import os
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import changes as ch
from . import leftovers as lo
from . import operations as ops
from .images import IMAGE_TYPES, dimensions
from .models import AudioFile, EffectiveMetadata, FieldValue, FileDesire, Library, Operation
from .pathparse import normalize_name, path_key
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
    pending = ch.waiting_files(session)
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


def decide(
    session: Session, library: Library, merges: list[Merge], description: str
) -> Operation | None:
    """Decide on the merges: the source folders' copies of tracks the target has are to be
    trashed, and their other tracks moved into the target. Only the catalog changes (as an
    operation that can be undone); the files are written from the Pending page
    (writing.py). The source folders' other files (see leftovers) are kept in the decision
    of every track, as it is not known which will be written last; they follow it, so that
    the source folders disappear. None when it was all decided already."""
    desires = []
    reserved = reserved_paths(session, library)
    for merge in merges:
        found = leftovers(merge, Path(library.root), reserved)
        reserved.update({path_key(a.to): a.path for a in found if a.to})
        loose = [[a.path, a.to, a.same_as] for a in found]
        extra = {"leftovers": loose} if loose else {}
        for track, keep in merge.trash:
            spec = {"replacement": keep.file.id, "replacement_path": keep.file.path, **extra}
            desires.append(ops.Desire(track.file.id, "trash", spec))
        for track, path in merge.move:
            desires.append(ops.Desire(track.file.id, "move", {"to": path, **extra}))
    operation = ops.record(session, library.id, "merge", description, [], desires=desires)
    session.commit()
    return operation


@dataclass
class Leftover:
    """A file of the source folder (or, to make way, of the target) and what becomes of
    it: moved to `to`, or trashed (to is None), possibly as a copy of same_as."""

    path: str
    to: str | None
    same_as: str | None = None


def reserved_paths(session: Session, library: Library) -> dict[str, str]:
    """Destinations that decisions not yet written have claimed, as path_key -> the source
    file of a decided leftover ("" for a track), so that another merge into the same folder
    does not plan for a name the disk still shows as free."""
    reserved: dict[str, str] = {}
    for spec in session.scalars(
        select(FileDesire.spec)
        .join(AudioFile, AudioFile.id == FileDesire.file_id)
        .where(
            AudioFile.library_id == library.id,
            FileDesire.written.is_(False),
            FileDesire.kind.in_(("move", "trash")),
        )
    ):
        if "to" in spec:
            reserved[path_key(spec["to"])] = ""
        for path, to, _ in spec.get("leftovers", []):
            if to:
                reserved[path_key(to)] = path
    return reserved


def leftovers(merge: Merge, root: Path, reserved: dict[str, str] | None = None) -> list[Leftover]:
    """What becomes of the source folder's other files (cover, .cue, booklet, Scans; see
    leftovers.folder_files) when all its tracks leave: they move into the target. When
    the target has a file of that name, an identical copy (or a system file such as
    .DS_Store) is trashed. Of two different images the bigger one ends up under the name,
    and the other next to it with its folder's name added ("cover (Greatest Hits).jpg");
    any other file gets the source folder's name added. Nothing when some audio stays.

    reserved (see reserved_paths) are destinations that decided but unwritten moves will
    fill: a name there counts as taken, as if the file were already in the target."""
    source, target = merge.source, merge.target
    contents = lo.folder_files(root / source.path)
    if merge.clashes or contents is None or contents[0] > len(source.tracks):
        return []
    result: list[Leftover] = []
    planned: set[str] = set()  # destinations taken by earlier actions
    reserved = reserved or {}

    def claimed_by(path: str, source: str) -> str | None:
        """The source file of another decision that will put a file at path, else None. A
        decision of source itself (deciding again) is no clash."""
        claim = reserved.get(path_key(path))
        return None if claim is None or claim == source else claim

    def free(path: str, source: str = "") -> bool:
        return (
            path_key(path) not in planned
            and claimed_by(path, source) is None
            and not os.path.lexists(root / path)
        )

    # Folders of the same name ("Texas/Greatest Hits", "Texas (1)/Greatest Hits") are told
    # apart by their parents
    same_name = source.name.casefold() == target.name.casefold()

    def suffixed(path: str, folder: Folder) -> str:
        """The path with the folder's name added, and a number if that is taken too."""
        stem, extension = os.path.splitext(path)
        label = " - ".join(PurePosixPath(folder.path).parts[-2:]) if same_name else folder.name
        for n in range(1, 1000):
            candidate = f"{stem} ({label}{f' {n}' if n > 1 else ''}){extension}"
            if free(candidate):
                return candidate
        raise ValueError(f"No free name for {path}")

    for relative in contents[1]:
        path, to = f"{source.path}/{relative}", f"{target.path}/{relative}"
        # What stands at the name: a file on disk, or one that a decided merge will move there
        there = claimed_by(to, path) or to
        if free(to, path):
            result.append(Leftover(path, to))
        elif _same_content(root / path, root / there):
            result.append(Leftover(path, None, same_as=to))
        elif lo.is_system_file(PurePosixPath(relative).name):
            result.append(Leftover(path, None))
        elif (
            _is_image(relative)
            and os.path.lexists(root / to)
            and _image_size(root / path) > _image_size(root / to)
        ):
            result.append(Leftover(to, suffixed(to, target)))  # the target's smaller image
            result.append(Leftover(path, to))
        else:
            result.append(Leftover(path, suffixed(to, source)))
        planned.update(path_key(a.to) for a in result[-2:] if a.to)
    return result


def _same_content(a: Path, b: Path) -> bool:
    try:
        return filecmp.cmp(a, b, shallow=False)
    except OSError:
        return False


def _is_image(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in IMAGE_TYPES


def _image_size(path: Path) -> tuple[int, int]:
    """(pixels, bytes) of an image, to tell the bigger of two; unknown pixels count as 0."""
    try:
        with open(path, "rb") as f:
            size = dimensions(f.read(65536))
        return (size[0] * size[1] if size else 0, path.stat().st_size)
    except OSError:
        return (0, 0)


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
