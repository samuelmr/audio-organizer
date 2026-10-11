"""Tracks that are one recording in several encodings, and which encoding to keep.

`duplicates` finds identical audio. This finds copies whose audio differs because the
track was encoded again: an album ripped twice (MP3 at 160 kbps and AAC at 256), iTunes
copies at another bitrate ("Title 1.mp3"), a loose MP3 of an album's track. Files are
taken as one recording when their durations are within TOLERANCE seconds and

- they are in one album folder with the same title (and number, where both have one), or
- one lies loose (in an artist folder or "Unknown Album") with the artist and title of
  a track in exactly one album folder.

Different edits, live versions and hidden tracks differ by more than TOLERANCE; groups
whose durations differ are only listed, as versions for the user to listen to.

The best encoding is kept: lossless, then a format most players read (not WMA), then the
higher bitrate, then the album folder's copy, one that is not an iTunes copy, the one
with the most tags. The others go to the trash, and static playlists move to the kept
one. A kept iTunes copy is renamed to the name of the album's copy it replaces, and a
kept loose copy is moved into the album folder under that name, taking the album's tags;
such replacements of an album's copy by a loose one are only proposed when asked for.
"""

from dataclasses import dataclass, field
from pathlib import PurePosixPath

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import changes as ch
from . import duplicates as du
from . import operations as ops
from .models import AudioFile, EffectiveMetadata, FieldValue, Library, Operation
from .pathparse import normalize_name, unflip
from .tracks import present

TOLERANCE = 2.0  # seconds
LOSSLESS = {"flac", "wave", "aiff"}
UNPLAYABLE = {"asf"}  # WMA: browsers, phones and many players cannot play it
# Measured from the audio, so another encoding's values are not copied to the kept file
MEASURED = ("replaygain_track_gain", "replaygain_track_peak", "replaygain_album_gain",
            "replaygain_album_peak")  # fmt: skip
# Taken from the album's copy when a loose copy takes its place
ALBUM_FIELDS = ("album", "albumartist", "albumartistsort", "tracknumber", "tracktotal",
                "discnumber", "disctotal", "date", "originaldate", "compilation")  # fmt: skip


@dataclass
class Track:
    file: AudioFile
    artist: str | None
    albumartist: str | None
    title: str
    tracknumber: int | None
    discnumber: int | None
    album_folder: str | None  # the folder, when it is an album folder
    compilation: bool
    album: str | None = None  # effective album
    folder_album: str | None = None  # the album name from the folder name

    @property
    def duration(self) -> float:
        return self.file.duration or 0.0

    @property
    def artists(self) -> set[str]:
        top = PurePosixPath(self.file.path).parts[0]
        names = {self.artist, self.albumartist, unflip(top)}
        return {normalize_name(n) for n in names if n}


@dataclass
class Proposal(du.Proposal):
    move_to: str | None = None  # where the kept file goes: the album copy's name

    @property
    def replaces(self) -> bool:
        """A loose copy takes the place of the album's copy: listen before proposing."""
        return not self.keep.in_album_folder


@dataclass
class Survey:
    proposals: list[Proposal] = field(default_factory=list)
    versions: list[list[AudioFile]] = field(default_factory=list)  # durations differ
    groups: int = 0  # tracks with several encodings, incl. versions
    several_albums: int = 0  # loose copies matching tracks in several album folders
    compilations: int = 0  # loose copies whose album copy is only on a compilation
    pending: int = 0  # some copy already has changes waiting for review

    @property
    def batchable(self) -> list[Proposal]:
        return [p for p in self.proposals if not p.replaces]

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
    """Group the encodings of each recording and propose which to keep. Unless
    include_compilations is set, a loose copy in an artist's folder is left alone when
    the recording's only album copy is on a compilation, as in `duplicates`."""
    result = Survey()
    tracks = load_tracks(session, library)
    songs = _album_songs(tracks)
    loose = [t for t in tracks if t.album_folder is None]
    members: dict[int, list[Track]] = {id(song): list(song) for song in songs}
    versions = {id(song) for song in songs if len(_clusters(song)) > 1}
    by_artist_title: dict[tuple[str, str], list[list[Track]]] = {}
    for song in songs:
        if id(song) in versions:
            continue
        for artist in set().union(*(t.artists for t in song)):
            by_artist_title.setdefault((artist, normalize_name(song[0].title)), []).append(song)
    for track in loose:
        matches = {
            id(song): song
            for artist in track.artists
            for song in by_artist_title.get((artist, normalize_name(track.title)), [])
            if abs(_mean(song) - track.duration) <= TOLERANCE
        }
        if len({song[0].album_folder for song in matches.values()}) > 1:
            result.several_albums += 1
            continue
        if len(matches) != 1:
            continue  # no match, or the same title twice in one folder
        [song] = matches.values()
        top = PurePosixPath(track.file.path).parts[0]
        if (
            not include_compilations
            and song[0].compilation
            and top != PurePosixPath(song[0].file.path).parts[0]
        ):
            result.compilations += 1
            continue
        members[id(song)].append(track)

    pending = ch.waiting_files(session)
    groups = []
    for song in songs:
        group = members[id(song)]
        if len({t.file.audio_hash for t in group}) < 2:
            continue  # one encoding (identical copies are for `duplicates`)
        if path_prefix and not any(t.file.path.startswith(path_prefix) for t in group):
            continue
        result.groups += 1
        if any(t.file.id in pending for t in group):
            result.pending += 1
        elif id(song) in versions:
            result.versions.append(sorted((t.file for t in group), key=lambda f: f.path))
        else:
            groups.append(group)
    copies = {c.file.id: c for c in du.load_copies(session, [t.file for g in groups for t in g])}
    for group in groups:
        group_copies = [copies[t.file.id] for t in group]
        result.proposals.append(keeping(group_copies, max(group_copies, key=quality)))
    result.proposals.sort(key=lambda p: p.keep.file.path)
    result.versions.sort(key=lambda files: files[0].path)
    return result


def quality(copy: du.Copy) -> tuple:
    """Sorts the copy to keep last."""
    file = copy.file
    return (
        file.format in LOSSLESS,
        file.format not in UNPLAYABLE,
        round((file.bitrate or 0) / 32000),  # VBR wobble around a nominal rate is equal
        copy.in_album_folder,
        not copy.is_copy,
        len(copy.tags),
        -len(file.path),
    )


def keeping(members: list[du.Copy], keep: du.Copy) -> Proposal:
    """The proposal that keeps the given copy: the others go to the trash, tags only they
    have are copied to it, and it takes the name of the album's copy where needed."""
    base = du.keeping(members, keep)
    fill = {k: v for k, v in base.fill.items() if k not in MEASURED}
    album = [m for m in members if m.in_album_folder]
    # The name to take: a numbered one, the shortest (as for the kept copy's own name, so
    # an album's tracks end up named alike). Names are never made up: " 2" may well be
    # part of a title ("Stripped Pt. 2").
    named = sorted(
        [m for m in album if not m.is_copy] or album,
        key=lambda m: ("tracknumber" not in m.path, len(m.file.path), m.file.path),
    )
    move_to = None
    if not keep.in_album_folder or (keep.is_copy and not named[0].is_copy):
        model = named[0]
        stem = PurePosixPath(model.file.path).stem
        target = f"{model.folder}/{stem}{PurePosixPath(keep.file.path).suffix}"
        move_to = target if target != keep.file.path else None
        if not keep.in_album_folder:
            for name in ALBUM_FIELDS:
                value = model.tags.get(name)
                if value is not None and keep.effective.get(name) != value:
                    fill[name] = value
    return Proposal(keep, base.trash, fill, move_to)


def choose(proposals: list[Proposal], keep_ids: dict[str, int]) -> list[Proposal]:
    """The proposals with the user's choices of which copy to keep, by the id of the
    file kept by default."""
    chosen = []
    for proposal in proposals:
        members = [proposal.keep, *proposal.trash]
        wanted = keep_ids.get(str(proposal.keep.file.id))
        keep = next((m for m in members if m.file.id == wanted), proposal.keep)
        chosen.append(proposal if keep is proposal.keep else keeping(members, keep))
    return chosen


def batch(proposals: list[Proposal], limit: int, *, replace: bool = False) -> list[Proposal]:
    """Up to limit proposals; those where a loose copy replaces the album's copy only
    with replace."""
    return [p for p in proposals if replace or not p.replaces][:limit]


def decide(
    session: Session, library: Library, proposals: list[Proposal], description: str
) -> Operation | None:
    """Decide on the proposals: for each recording, the tags only the other encodings
    have become the kept file's own values, the other encodings are to be trashed, and the
    kept file is to move into the place of the album's copy. Only the catalog changes (as
    an operation that can be undone); the files are written from the Pending page
    (writing.py), which trashes before it moves. None when it was all decided already."""
    values, desires = [], []
    for proposal in proposals:
        keep = proposal.keep.file
        values += [ops.Value(keep.id, "manual", n, v) for n, v in proposal.fill.items()]
        spec = {"replacement": keep.id, "replacement_path": keep.path, "same": "recording"}
        desires += [ops.Desire(copy.file.id, "trash", spec) for copy in proposal.trash]
        if proposal.move_to:
            desires.append(ops.Desire(keep.id, "move", {"to": proposal.move_to}))
    operation = ops.record(session, library.id, "encodings", description, values, desires=desires)
    session.commit()
    return operation


def _album_songs(tracks: list[Track]) -> list[list[Track]]:
    """The tracks of each album folder grouped by title and number. A track without a
    number joins the one numbered track of its title, if there is just one."""
    by_title: dict[tuple[str, str], list[Track]] = {}
    for track in tracks:
        if track.album_folder is not None:
            key = (track.album_folder, normalize_name(track.title))
            by_title.setdefault(key, []).append(track)
    songs = []
    for same_title in by_title.values():
        numbered: dict[tuple[int, int], list[Track]] = {}
        for track in same_title:
            if track.tracknumber:
                numbered.setdefault((track.discnumber or 1, track.tracknumber), []).append(track)
        unnumbered = [t for t in same_title if not t.tracknumber]
        if len(numbered) == 1:
            [song] = numbered.values()
            song.extend(unnumbered)
        elif not numbered:
            numbered[(0, 0)] = unnumbered
        songs.extend(numbered.values())
    return songs


def _clusters(song: list[Track]) -> list[list[Track]]:
    """The tracks split where durations are more than TOLERANCE apart."""
    ordered = sorted(song, key=lambda t: t.duration)
    clusters = [[ordered[0]]]
    for track in ordered[1:]:
        if track.duration - clusters[-1][-1].duration > TOLERANCE:
            clusters.append([])
        clusters[-1].append(track)
    return clusters


def _mean(song: list[Track]) -> float:
    return sum(t.duration for t in song) / len(song)


def load_tracks(session: Session, library: Library) -> list[Track]:
    """The library's tracks with a title and a duration, with what grouping needs."""
    path_fields: dict[int, dict[str, str]] = {}
    for file_id, name, value in session.execute(
        select(FieldValue.file_id, FieldValue.field, FieldValue.value)
        .join(AudioFile, AudioFile.id == FieldValue.file_id)
        .where(present(library.id), FieldValue.source == "path")
        .where(FieldValue.field.in_(("album", "compilation")))
    ):
        path_fields.setdefault(file_id, {})[name] = value
    tracks = []
    rows = session.execute(
        select(
            AudioFile, EffectiveMetadata.artist, EffectiveMetadata.albumartist,
            EffectiveMetadata.title, EffectiveMetadata.tracknumber, EffectiveMetadata.discnumber,
            EffectiveMetadata.compilation, EffectiveMetadata.album,
        )
        .join(EffectiveMetadata, EffectiveMetadata.file_id == AudioFile.id)
        .where(present(library.id), AudioFile.audio_hash.is_not(None))
        .where(EffectiveMetadata.title.is_not(None), AudioFile.duration.is_not(None))
    )  # fmt: skip
    for file, artist, albumartist, title, tracknumber, discnumber, compilation, album in rows:
        fields = path_fields.get(file.id, {})
        path = PurePosixPath(file.path)
        in_album = len(path.parts) >= 3 and "album" in fields
        tracks.append(
            Track(
                file, artist, albumartist, title, tracknumber, discnumber,
                str(path.parent) if in_album else None,
                fields.get("compilation") == "1" or compilation == "1", album,
                fields.get("album") if in_album else None,
            )
        )  # fmt: skip
    return tracks
