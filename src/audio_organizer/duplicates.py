"""Finding identical copies of tracks, and proposing to remove the loose ones.

Files with the same audio hash are copies of one track. When exactly one album folder
(Artist/Album/..., not "Unknown Album") holds copies, its best copy is kept: one without
an iTunes copy suffix (" 1"), with the most tags. The other copies, which lie loose in
an artist folder, in "Unknown Album" or next to the keeper as iTunes copies, are proposed
for the trash. Tags that only the loose copies have (such as old MusicMatch moods) are
first copied to the keeper, and static playlists move over to it.

When copies are in several album folders, which album the track belongs to is a
decision for the user; such groups are left alone here.
"""

from dataclasses import dataclass, field
from pathlib import PurePosixPath

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import changes as ch
from . import operations as ops
from .models import AudioFile, FieldValue, Library, Operation
from .provenance import resolve
from .tags import FIELDS
from .tracks import present

BATCH = 2000


@dataclass
class Copy:
    file: AudioFile
    tags: dict[str, str]  # from the file's tags
    path: dict[str, str]  # from its folder and file name
    effective: dict[str, str]

    @property
    def folder(self) -> str:
        return str(PurePosixPath(self.file.path).parent)

    @property
    def in_album_folder(self) -> bool:
        """Artist/Album/file, where the path parser found an album (so not "Unknown
        Album")."""
        return len(PurePosixPath(self.file.path).parts) >= 3 and "album" in self.path

    @property
    def is_copy(self) -> bool:
        return "copy_suffix" in self.path

    @property
    def top_folder(self) -> str:
        return PurePosixPath(self.file.path).parts[0]

    @property
    def on_compilation(self) -> bool:
        return self.path.get("compilation") == "1" or self.effective.get("compilation") == "1"


@dataclass
class Proposal:
    keep: Copy
    trash: list[Copy]
    fill: dict[str, str]  # tags to copy to the kept file


@dataclass
class Survey:
    proposals: list[Proposal] = field(default_factory=list)
    groups: int = 0
    several_albums: int = 0  # copies in more than one album folder
    no_album: int = 0  # no copy in an album folder
    pending: int = 0  # some copy already has changes waiting for review or to be written
    compilations: int = 0  # left out: only a compilation's copy would be kept

    @property
    def trash_count(self) -> int:
        return sum(len(p.trash) for p in self.proposals)


def survey(
    session: Session,
    library: Library,
    *,
    path_prefix: str | None = None,
    include_compilations: bool = False,
) -> Survey:
    """Group identical copies and propose what to keep. Unless include_compilations is
    set, a track is left alone when the copy to keep is on a compilation (Various
    Artists, Compilations) and another copy lies in an artist's own folder: removing it
    would take the track out of the artist's folder."""
    result = Survey()
    hashes = (
        select(AudioFile.audio_hash)
        .where(present(library.id), AudioFile.audio_hash.is_not(None))
        .group_by(AudioFile.audio_hash)
        .having(func.count() > 1)
    )
    files = session.scalars(
        select(AudioFile)
        .where(present(library.id), AudioFile.audio_hash.in_(hashes))
        .order_by(AudioFile.path)
    ).all()
    pending = ch.waiting_files(session)
    copies = load_copies(session, files)
    groups: dict[str, list[Copy]] = {}
    for copy in copies:
        groups.setdefault(copy.file.audio_hash, []).append(copy)
    for members in groups.values():
        if path_prefix and not any(m.file.path.startswith(path_prefix) for m in members):
            continue
        result.groups += 1
        if any(m.file.id in pending for m in members):
            result.pending += 1
            continue
        in_albums = [m for m in members if m.in_album_folder]
        if not in_albums:
            result.no_album += 1
            continue
        if len({m.folder for m in in_albums}) > 1:
            result.several_albums += 1
            continue
        keep = max(
            in_albums,
            key=lambda m: (not m.is_copy, len(m.tags), m.file.bitrate or 0, -len(m.file.path)),
        )
        proposal = keeping(members, keep)
        if (
            not include_compilations
            and keep.on_compilation
            and any(m.top_folder != keep.top_folder for m in proposal.trash)
        ):
            result.compilations += 1
            continue
        result.proposals.append(proposal)
    result.proposals.sort(key=lambda p: p.keep.file.path)
    return result


def keeping(members: list[Copy], keep: Copy) -> Proposal:
    """The proposal for a group of copies that keeps the given one: the others go to the
    trash, and tags only they have are copied to it."""
    trash = [m for m in members if m is not keep]
    fill = {}
    for name in FIELDS:
        if name in keep.effective:
            continue
        value = next((m.tags[name] for m in trash if name in m.tags), None)
        if value is not None:
            fill[name] = value
    return Proposal(keep, trash, fill)


def choose(proposals: list[Proposal], keep_ids: dict[str, int]) -> list[Proposal]:
    """The proposals with the user's choices of which copy to keep, by audio hash."""
    chosen = []
    for proposal in proposals:
        members = [proposal.keep, *proposal.trash]
        wanted = keep_ids.get(proposal.keep.file.audio_hash)
        keep = next((m for m in members if m.file.id == wanted), proposal.keep)
        chosen.append(proposal if keep is proposal.keep else keeping(members, keep))
    return chosen


def decide(
    session: Session, library: Library, proposals: list[Proposal], description: str
) -> Operation | None:
    """Decide on the proposals: for each, the tags only the other copies have become
    the kept file's own values, and the other copies are to be trashed. Only the catalog
    changes (as an operation that can be undone); the files are written from the Pending
    page (writing.py). None when it was all decided already."""
    values, desires = [], []
    for proposal in proposals:
        values += [
            ops.Value(proposal.keep.file.id, "manual", n, v) for n, v in proposal.fill.items()
        ]
        spec = {
            "replacement": proposal.keep.file.id,
            "replacement_path": proposal.keep.file.path,
        }
        desires += [ops.Desire(copy.file.id, "trash", spec) for copy in proposal.trash]
    operation = ops.record(session, library.id, "duplicates", description, values, desires=desires)
    session.commit()
    return operation


def load_copies(session: Session, files: list[AudioFile]) -> list[Copy]:
    """The files with their tags, path evidence and effective values."""
    values: dict[int, list] = {}
    for start in range(0, len(files), BATCH):
        ids = [f.id for f in files[start : start + BATCH]]
        for row in session.execute(
            select(FieldValue.file_id, FieldValue.field, FieldValue.source, FieldValue.value)
            .where(FieldValue.file_id.in_(ids))
        ):  # fmt: skip
            values.setdefault(row.file_id, []).append(row)
    copies = []
    for f in files:
        rows = values.get(f.id, [])
        copies.append(
            Copy(
                file=f,
                tags={r.field: r.value for r in rows if r.source == "tag"},
                path={r.field: r.value for r in rows if r.source == "path"},
                effective={k: r.value for k, r in resolve(rows).items()},
            )
        )
    return copies
