"""Reviewable, undoable modifications of files.

Changes are proposed into a Changeset, approved or rejected one by one (or all at
once), and only approved changes are applied. Applying records what is needed to undo
each change. A file is only touched if it is unchanged on disk since it was last
scanned, so edits made elsewhere are never silently overwritten.
"""

import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from mutagen import MutagenError
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import covers, pathparse, tags
from .models import (
    AudioFile,
    Change,
    Changeset,
    CoverImage,
    FileDesire,
    Library,
    PlaylistEntry,
    utcnow,
)
from .provenance import set_value, set_values

log = logging.getLogger(__name__)

# Trashed files are kept here, inside the library root, until `aorg trash empty`. The
# scanner skips folders whose names start with a dot.
TRASH_DIR = ".aorg-trash"


# The FileDesire kind (models.py) that a change of this kind carries out
DESIRE_KINDS = {"cover": "embed", "image": "image", "trash": "trash", "move": "move"}


class ChangeError(Exception):
    pass


@dataclass
class Outcome:
    change: Change
    ok: bool
    message: str


# Features that propose changesets, recorded in Changeset.source so that changesets can be
# filtered by feature and rules can be scoped to one.
SOURCES = (
    "writing", "layout", "duplicates", "recordings", "album_merge", "neighbours", "covers",
    "leftovers", "manual",
)  # fmt: skip


def new_changeset(session: Session, description: str, source: str) -> Changeset:
    if source not in SOURCES:
        raise ValueError(f"Unknown changeset source: {source}")
    changeset = Changeset(description=description, source=source)
    session.add(changeset)
    return changeset


def propose_retag(
    changeset: Changeset,
    file: AudioFile,
    fields: dict[str, str | None],
    id3_version: int | None = None,
) -> Change:
    """Propose setting tag fields (None removes one). id3_version (3 or 4) writes ID3 tags
    in that version; by default the file keeps its own."""
    unknown = set(fields) - set(tags.FIELDS)
    if unknown:
        raise ChangeError(f"Not tag fields: {', '.join(sorted(unknown))}")
    payload: dict = {"fields": fields}
    if id3_version:
        payload["id3_version"] = id3_version
    change = Change(file=file, kind="retag", payload=payload, status="proposed")
    changeset.changes.append(change)
    return change


def propose_move(
    changeset: Changeset,
    file: AudioFile,
    to: str,
    extras: list[tuple[str, str]] | None = None,
) -> Change:
    """Propose moving a file. Extras are other files (such as the album's cover image)
    to move along with it, as (from, to) library paths; they are moved if they still exist
    and the destination is free."""
    payload: dict = {"to": Path(to).as_posix()}
    if extras:
        payload["extras"] = [[source, target] for source, target in extras]
    change = Change(file=file, kind="move", payload=payload, status="proposed")
    changeset.changes.append(change)
    return change


def propose_trash(
    changeset: Changeset,
    file: AudioFile,
    replacement: AudioFile | None = None,
    *,
    same: str = "audio",
) -> Change:
    """Propose moving a file to the library's trash folder (TRASH_DIR), from where undo
    can bring it back. Static playlists that contain it get the replacement instead,
    which has the same audio, or (same="recording") is another encoding of it."""
    payload = {
        "replacement": replacement.id if replacement else None,
        "replacement_path": replacement.path if replacement else None,
    }
    if same != "audio":
        payload["same"] = same
    change = Change(file=file, kind="trash", payload=payload, status="proposed")
    changeset.changes.append(change)
    return change


def propose_trash_leftover(
    changeset: Changeset, library: Library, path: str, same_as: str | None = None
) -> Change:
    """Propose moving a file the catalog does not know (litter such as Folder.jpg or an old
    playlist in a folder without music) to the trash; same_as names an identical file that
    stays. Its size and modification time are recorded, so a file that changed in the
    meantime is left alone."""
    st = (Path(library.root) / path).stat()
    payload = {
        "library_id": library.id,
        "path": path,
        "size": st.st_size,
        "mtime_ns": st.st_mtime_ns,
    }
    if same_as:
        payload["same_as"] = same_as
    change = Change(file=None, kind="leftover", payload=payload, status="proposed")
    changeset.changes.append(change)
    return change


def propose_rename_file(changeset: Changeset, library: Library, path: str, to: str) -> Change:
    """Propose renaming a file the catalog does not know (an album's image to cover.jpg),
    or moving it to another folder (a merged album's booklet). Size and modification time
    are recorded, as for a leftover."""
    st = (Path(library.root) / path).stat()
    change = Change(
        file=None,
        kind="rename",
        payload={
            "library_id": library.id,
            "path": path,
            "to": to,
            "size": st.st_size,
            "mtime_ns": st.st_mtime_ns,
        },
        status="proposed",
    )
    changeset.changes.append(change)
    return change


def propose_embed_cover(
    changeset: Changeset,
    file: AudioFile,
    image: str | None = None,
    *,
    cover_image: str | None = None,
    label: str | None = None,
    replace: bool = False,
) -> Change:
    """Propose embedding an image as the file's front cover: a library path, or a
    CoverImage (by its sha1, described by label). With replace, the file's present cover
    (a thumbnail) makes way for it; otherwise the change fails if the file has one."""
    payload: dict = {"image": image} if image else {"cover_image": cover_image, "label": label}
    if replace:
        payload["replace"] = True
    change = Change(file=file, kind="cover", payload=payload, status="proposed")
    changeset.changes.append(change)
    return change


def propose_save_image(
    changeset: Changeset,
    file: AudioFile,
    cover_image: str,
    to: str,
    label: str,
    replace: bool = False,
) -> Change:
    """Propose saving a CoverImage as an image file in the library (a folder's cover.jpg).
    The change belongs to one of the album's tracks, as every change belongs to a file,
    but leaves the track alone. With replace, an image already there (a thumbnail) is
    overwritten and kept in the catalog for undo; otherwise the change fails if one is."""
    payload: dict = {"cover_image": cover_image, "to": to, "label": label}
    if replace:
        payload["replace"] = True
    change = Change(file=file, kind="image", payload=payload, status="proposed")
    changeset.changes.append(change)
    return change


def set_status(changes: list[Change], status: str) -> int:
    """Approve or reject proposed changes (or return approved/rejected ones to proposed)."""
    if status not in ("approved", "rejected", "proposed"):
        raise ValueError(status)
    count = 0
    for change in changes:
        if change.status in ("proposed", "approved", "rejected") and change.status != status:
            change.status = status
            count += 1
    return count


def retry_failed(changeset: Changeset) -> int:
    """Return failed changes to approved so that applying the changeset tries them again.
    The cause (a file edited elsewhere, a full disk) may be gone by now."""
    count = 0
    for change in changeset.changes:
        if change.status == "failed":
            change.status, change.error = "approved", None
            count += 1
    return count


def delete_changeset(session: Session, changeset: Changeset) -> None:
    """Forget a changeset. Applied changes keep what undo needs in the changeset, so those
    must be undone first."""
    applied = sum(1 for c in changeset.changes if c.status == "applied")
    if applied:
        raise ChangeError(f"{applied} changes are still applied; undo them first")
    session.delete(changeset)
    session.commit()


def describe(change: Change) -> str:
    if change.kind == "move":
        extras = move_extras(change)
        n = len(extras)
        along = f" with {n} other file{'s' * (n > 1)}: {', '.join(extras)}" if extras else ""
        return f"move {move_source(change)} -> {change.payload['to']}{along}"
    if change.kind == "cover":
        image = change.payload.get("image") or change.payload.get("label")
        if change.payload.get("replace"):
            return f"replace the cover of {change.file.path} with {image}"
        return f"embed {image} as the cover of {change.file.path}"
    if change.kind == "image":
        if change.payload.get("replace"):
            return f"replace {change.payload['to']} with {change.payload['label']}"
        return f"save {change.payload['label']} as {change.payload['to']}"
    if change.kind == "rename":
        verb = "rename" if is_rename_in_place(change) else "move"
        return f"{verb} {change.payload['path']} -> {change.payload['to']}"
    if change.kind == "leftover":
        same = change.payload.get("same_as")
        return f"trash {change.payload['path']} ({f'same as {same}' if same else 'not music'})"
    if change.kind == "trash":
        keeper = change.payload.get("replacement_path")
        same = change.payload.get("same", "audio")
        return f"trash {move_source(change)}" + (f" (same {same} as {keeper})" if keeper else "")
    parts = [f"{name}: {old!r} -> {new!r}" for name, old, new in retag_fields(change)]
    return f"retag {change.file.path}: " + "; ".join(parts)


def move_source(change: Change) -> str:
    """Where a move or trash takes the file from (once applied, the file is elsewhere)."""
    if change.status == "applied" and change.undo:
        return change.undo.get("from", change.file.path)  # trashed before "from" was kept
    return change.file.path


def is_rename_in_place(change: Change) -> bool:
    """Whether a rename change keeps the file in its folder (else it moves it)."""
    path, to = PurePosixPath(change.payload["path"]), PurePosixPath(change.payload["to"])
    return path.parent == to.parent


def move_extras(change: Change) -> list[str]:
    """The other files that go along with a move (the folder's cover, booklet, .cue ...),
    relative to the folder they come from."""
    folder, names = PurePosixPath(move_source(change)).parent, []
    for source, _ in change.payload.get("extras", []):
        source = PurePosixPath(source)
        names.append(str(source.relative_to(folder)) if folder in source.parents else str(source))
    return names


def retag_fields(change: Change) -> list[tuple[str, str | None, str | None]]:
    """(field, value before, value after) of a retag."""
    current = {fv.field: fv.value for fv in change.file.values if fv.source == "tag"}
    if change.status == "applied" and change.undo:
        current = change.undo.get("fields", {})
    return [(name, current.get(name), value) for name, value in change.payload["fields"].items()]


def apply(session: Session, changeset: Changeset, *, dry_run: bool = False) -> list[Outcome]:
    """Apply the approved changes of a changeset. Each change is committed on its own,
    so a failure leaves the earlier ones applied (and undoable)."""
    outcomes = []
    for change in changeset.changes:
        if change.status != "approved":
            continue
        if dry_run:
            outcomes.append(Outcome(change, True, "would " + describe(change)))
            continue
        try:
            if change.kind not in ("image", "leftover", "rename"):  # which have no track to protect
                _check_unchanged(change.file)
            if change.kind == "retag":
                change.undo = _retag(
                    session, change.file, change.payload["fields"],
                    change.payload.get("id3_version"),
                )  # fmt: skip
            elif change.kind == "move":
                change.undo = _move(
                    session, change.file, change.payload["to"], change.payload.get("extras")
                )
            elif change.kind == "trash":
                change.undo = _trash(session, change)
            elif change.kind == "leftover":
                change.undo = _trash_leftover(session, change)
            elif change.kind == "rename":
                change.undo = _rename_file(session, change)
            elif change.kind == "cover":
                change.undo = _embed_cover(session, change)
            elif change.kind == "image":
                change.undo = _save_image(session, change)
            else:
                raise ChangeError(f"Unknown change kind {change.kind}")
            change.status, change.error, change.applied_at = "applied", None, utcnow()
            outcomes.append(Outcome(change, True, describe(change)))
        except (ChangeError, OSError, ValueError, MutagenError, tags.UnsupportedFormat) as e:
            change.status, change.error = "failed", str(e)
            log.warning("%s failed: %s", describe(change), e, exc_info=not _expected(e))
            outcomes.append(Outcome(change, False, f"{describe(change)}: FAILED: {e}"))
        session.commit()
    return outcomes


def _expected(e: Exception) -> bool:
    """Failures that are the file's or the user's doing; others get a traceback in the log."""
    return isinstance(e, ChangeError | tags.UnsupportedFormat | FileNotFoundError)


def undo(session: Session, changeset: Changeset, *, dry_run: bool = False) -> list[Outcome]:
    """Revert the applied changes of a changeset, newest first."""
    outcomes = []
    for change in reversed(changeset.changes):
        if change.status != "applied":
            continue
        if dry_run:
            outcomes.append(Outcome(change, True, "would undo " + describe(change)))
            continue
        try:
            if change.kind == "trash":
                _untrash(session, change)
            elif change.kind == "leftover":
                _untrash_leftover(session, change)
            elif change.kind == "rename":
                _unrename_file(session, change)
            elif change.kind != "image":
                _check_unchanged(change.file)
            if change.kind == "retag":
                _retag(session, change.file, change.undo["fields"])
                # Changesets made when an edit proposed its own changeset revert the edit
                # too; writes of pending values leave it, so the file is pending again
                for name, value in change.payload.get("manual_before", {}).items():
                    set_value(session, change.file, "manual", name, value)
            elif change.kind == "move":
                back = [[target, source] for source, target in change.undo.get("extras", [])]
                _move(session, change.file, change.undo["from"], back)
            elif change.kind == "cover":
                _unembed_cover(session, change)
            elif change.kind == "image":
                _unsave_image(session, change)
            change.status = "undone"
            if desire := desire_of(session, change):
                desire.written = False  # the decided cover is pending again
            outcomes.append(Outcome(change, True, "undid " + describe(change)))
        except (ChangeError, OSError, ValueError, MutagenError, tags.UnsupportedFormat) as e:
            change.error = f"undo failed: {e}"
            log.warning("undo %s failed: %s", describe(change), e, exc_info=not _expected(e))
            outcomes.append(Outcome(change, False, f"undo {describe(change)}: FAILED: {e}"))
        session.commit()
    return outcomes


def waiting_files(session: Session, kinds: tuple[str, ...] = ("trash", "move")) -> set[int]:
    """The files that something is already to do with: changes proposed or approved for
    review, and decided state (models.FileDesire of these kinds) not yet written. Other
    features leave them alone, so that nothing is proposed twice for one file."""
    in_changesets = select(Change.file_id).where(Change.status.in_(("proposed", "approved")))
    decided = select(FileDesire.file_id).where(
        FileDesire.written.is_(False), FileDesire.kind.in_(kinds)
    )
    return set(session.scalars(in_changesets)) | set(session.scalars(decided))


def desire_of(session: Session, change: Change) -> FileDesire | None:
    """The decided state (models.FileDesire) that this change carries out, if any. A move
    made by another feature is not the decided move unless it goes to the same path."""
    kind = DESIRE_KINDS.get(change.kind)
    desire = session.get(FileDesire, (change.file_id, kind)) if kind and change.file_id else None
    if desire and kind == "move" and desire.spec.get("to") != change.payload.get("to"):
        return None
    return desire


def _full_path(file: AudioFile) -> Path:
    return Path(file.library.root) / file.path


def _check_unchanged(file: AudioFile) -> None:
    path = _full_path(file)
    try:
        st = path.stat()
    except FileNotFoundError:
        raise ChangeError(f"{file.path} no longer exists; rescan the library") from None
    if st.st_size != file.size or st.st_mtime_ns != file.mtime_ns:
        raise ChangeError(f"{file.path} has changed since it was scanned; rescan the library")


def _refresh(session: Session, file: AudioFile) -> None:
    path = _full_path(file)
    st = path.stat()
    file.size, file.mtime_ns, file.scanned_at = st.st_size, st.st_mtime_ns, utcnow()
    set_values(session, file, "tag", tags.read(path).fields)
    set_values(session, file, "path", pathparse.parse(file.path))


def _retag(
    session: Session, file: AudioFile, fields: dict[str, str | None], id3_version: int | None = None
) -> dict:
    path = _full_path(file)
    before = tags.read(path).fields
    tags.write(path, fields, id3_version=id3_version)
    _refresh(session, file)
    return {"fields": {name: before.get(name) for name in fields}}


def _embed_cover(session: Session, change: Change) -> dict:
    path = _full_path(change.file)
    if "cover_image" in change.payload:
        stored = session.get(CoverImage, change.payload["cover_image"])
        if stored is None:
            raise ChangeError("the cover image is no longer in the catalog")
        mime, data = stored.mime, stored.data
    else:
        image = Path(change.file.library.root) / change.payload["image"]
        mime = covers.IMAGE_TYPES.get(image.suffix.lower())
        if mime is None:
            raise ChangeError(f"not a JPEG or PNG image: {image.name}")
        data = image.read_bytes()
    replace = bool(change.payload.get("replace"))
    if not replace and covers.embedded(path) is not None:
        # The catalog may predate cover info; a second cover would be a surprise
        raise ChangeError("the file already has an embedded cover")
    size_before = path.stat().st_size
    replaced = covers.embed_data(path, mime, data, replace=replace)
    kept = [
        {"sha1": covers.store_image(session, p.data, "replaced", mime=p.mime).sha1,
         "mime": p.mime, "type": p.type, "desc": p.desc}
        for p in replaced
    ]  # fmt: skip
    try:
        _refresh(session, change.file)
    except MutagenError:
        # The file was readable before the cover went in; keep what is needed to see why not
        with open(path, "rb") as f:
            head = f.read(16)
        log.error(
            "%s unreadable after embedding a %d byte %s cover: size %d -> %d, starts %r",
            path, len(data), mime, size_before, path.stat().st_size, head,
        )  # fmt: skip
        raise
    return {"embedded": covers.digest(data), "replaced": kept}


def _unembed_cover(session: Session, change: Change) -> None:
    path = _full_path(change.file)
    if not change.undo or "embedded" not in change.undo:
        covers.remove(path)  # applied by an earlier version, to a file without a cover
    else:
        restore = []
        for picture in change.undo["replaced"]:
            stored = session.get(CoverImage, picture["sha1"])
            if stored is None:
                raise ChangeError("the replaced cover is no longer in the catalog")
            restore.append(
                covers.Embedded(picture["mime"], stored.data, picture["type"], picture["desc"])
            )
        covers.unembed(path, change.undo["embedded"], restore)
    _refresh(session, change.file)


def _save_image(session: Session, change: Change) -> dict:
    stored = session.get(CoverImage, change.payload["cover_image"])
    if stored is None:
        raise ChangeError("the image is no longer in the catalog")
    target = Path(change.file.library.root) / change.payload["to"]
    replaced = None
    if target.exists():
        if not change.payload.get("replace"):
            raise ChangeError(f"{change.payload['to']} already exists")
        replaced = covers.store_image(session, target.read_bytes(), "replaced").sha1
    _write_atomically(target, stored.data)
    return {"saved": stored.sha1, "replaced": replaced}


def _unsave_image(session: Session, change: Change) -> None:
    to = change.payload["to"]
    target = Path(change.file.library.root) / to
    if not target.is_file():
        raise ChangeError(f"{to} no longer exists")
    if covers.digest(target.read_bytes()) != change.undo["saved"]:
        raise ChangeError(f"{to} has changed since it was saved")
    if change.undo.get("replaced") is None:
        target.unlink()
        return
    stored = session.get(CoverImage, change.undo["replaced"])
    if stored is None:
        raise ChangeError("the replaced image is no longer in the catalog")
    _write_atomically(target, stored.data)


def _write_atomically(target: Path, data: bytes) -> None:
    """Write next to the target, then rename, so an interruption leaves no half image."""
    temporary = target.with_name(f".{target.name}.aorg-tmp")
    temporary.write_bytes(data)
    os.replace(temporary, target)


def _move(
    session: Session, file: AudioFile, to: str, extras: list[list[str]] | None = None
) -> dict:
    root = Path(file.library.root)
    source, target = _full_path(file), root / to
    if target.exists():
        raise ChangeError(f"{to} already exists")
    clash = session.scalar(
        select(AudioFile.id).where(
            AudioFile.library_id == file.library_id, AudioFile.path == to, AudioFile.id != file.id
        )
    )
    if clash:
        raise ChangeError(f"{to} is still in the catalog as a missing file; rescan first")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(source, target)
    moved_extras = _move_extras(root, extras or [])
    _remove_empty_dirs(source.parent, root)
    old_path = file.path
    file.path = Path(to).as_posix()
    _refresh(session, file)
    return {"from": old_path, "extras": moved_extras}


def _move_extras(root: Path, pairs: list[list[str]]) -> list[list[str]]:
    """Move the extra files that still exist to free destinations; returns those moved.
    Subfolders they empty (Scans, Artwork) are removed."""
    moved = []
    for source, target in pairs:
        if (root / source).is_file() and not (root / target).exists():
            (root / target).parent.mkdir(parents=True, exist_ok=True)
            shutil.move(root / source, root / target)
            _remove_empty_dirs((root / source).parent, root)
            moved.append([source, target])
    return moved


def _trash(session: Session, change: Change) -> dict:
    """Move the file into TRASH_DIR/<changeset id>/<its path>. The catalog keeps it as
    missing, at its path in the trash, so undo can restore it with its history, and its
    old path is free for another file (e.g. a better copy moved into its place)."""
    file = change.file
    root = Path(file.library.root)
    trash_path = (PurePosixPath(TRASH_DIR) / str(change.changeset_id) / file.path).as_posix()
    target = root / trash_path
    if target.exists():
        raise ChangeError(f"{trash_path} already exists")
    replacement = change.payload.get("replacement")
    if replacement is not None:
        kept = session.get(AudioFile, replacement)
        kept_path = kept.path if kept else change.payload["replacement_path"]
        _check_kept(_full_path(file), root / kept_path, file.path, kept_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(_full_path(file), target)
    _remove_empty_dirs(_full_path(file).parent, root)
    old_path = file.path
    file.missing, file.path = True, trash_path
    entries = []
    if replacement is not None:
        for entry in session.scalars(select(PlaylistEntry).where(PlaylistEntry.file_id == file.id)):
            entry.file_id = replacement
            entries.append(entry.id)
    return {"trash": trash_path, "from": old_path, "entries": entries}


def _trash_leftover(session: Session, change: Change) -> dict:
    """Move a file outside the catalog into TRASH_DIR/<changeset id>/<its path>."""
    library = session.get(Library, change.payload["library_id"])
    root, path = Path(library.root), change.payload["path"]
    source = root / path
    try:
        st = source.stat()
    except FileNotFoundError:
        raise ChangeError(f"{path} no longer exists") from None
    if (st.st_size, st.st_mtime_ns) != (change.payload["size"], change.payload["mtime_ns"]):
        raise ChangeError(f"{path} has changed since it was proposed")
    trash_path = (PurePosixPath(TRASH_DIR) / str(change.changeset_id) / path).as_posix()
    target = root / trash_path
    if target.exists():
        raise ChangeError(f"{trash_path} already exists")
    if same_as := change.payload.get("same_as"):
        _check_kept(source, root / same_as, path, same_as)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(source, target)
    _remove_empty_dirs(source.parent, root)
    return {"trash": trash_path}


def _check_kept(path: Path, kept: Path, name: str, kept_name: str) -> None:
    """Refuse to trash a file as a copy of one that is not there, or that is the same file
    under another name. The kept copy can have gone since the decision (deleted or moved
    outside aorg); trashing the copy then would lose the track. On a case-insensitive
    share "Album/01.mp3" and "album/01.mp3" can both be in the catalog while they open one
    file, and trashing either loses it. A hard link (a second link of the file) can go, as
    the other link keeps it."""
    if not kept.exists():
        raise ChangeError(f"{kept_name}, the copy to keep, is not there; rescan the library")
    try:
        same = os.path.samefile(path, kept) and path.stat().st_nlink < 2
    except OSError:
        return  # either is gone, so they are not one file
    if same:
        raise ChangeError(
            f"{name} and {kept_name} are one file under two names (letter case on a "
            "case-insensitive share?); rescan the library, which reads only one of them"
        )


def _rename_file(session: Session, change: Change) -> dict:
    root = Path(session.get(Library, change.payload["library_id"]).root)
    path, to = change.payload["path"], change.payload["to"]
    try:
        st = (root / path).stat()
    except FileNotFoundError:
        raise ChangeError(f"{path} no longer exists") from None
    if (st.st_size, st.st_mtime_ns) != (change.payload["size"], change.payload["mtime_ns"]):
        raise ChangeError(f"{path} has changed since it was proposed")
    if (root / to).exists():
        raise ChangeError(f"{to} already exists")
    (root / to).parent.mkdir(parents=True, exist_ok=True)
    os.rename(root / path, root / to)
    _remove_empty_dirs((root / path).parent, root)
    return {}


def _unrename_file(session: Session, change: Change) -> None:
    root = Path(session.get(Library, change.payload["library_id"]).root)
    path, to = change.payload["path"], change.payload["to"]
    if not (root / to).is_file():
        raise ChangeError(f"{to} no longer exists")
    if (root / path).exists():
        raise ChangeError(f"{path} exists again; move it away first")
    (root / path).parent.mkdir(parents=True, exist_ok=True)
    os.rename(root / to, root / path)
    _remove_empty_dirs((root / to).parent, root)


def _untrash_leftover(session: Session, change: Change) -> None:
    root = Path(session.get(Library, change.payload["library_id"]).root)
    source, target = root / change.undo["trash"], root / change.payload["path"]
    if not source.exists():
        raise ChangeError(f"{change.undo['trash']} is no longer in the trash")
    if target.exists():
        raise ChangeError(f"{change.payload['path']} exists again; move it away first")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(source, target)
    _remove_empty_dirs(source.parent, root)


def _untrash(session: Session, change: Change) -> None:
    file = change.file
    root = Path(file.library.root)
    original = change.undo.get("from", file.path)  # trashed before the path was changed
    source, target = root / change.undo["trash"], root / original
    if not source.exists():
        raise ChangeError(f"{change.undo['trash']} is no longer in the trash")
    if target.exists():
        raise ChangeError(f"{original} exists again; move it away first")
    taken = session.scalar(
        select(AudioFile.id).where(
            AudioFile.library_id == file.library_id, AudioFile.path == original,
            AudioFile.id != file.id,
        )
    )  # fmt: skip
    if taken:
        raise ChangeError(f"{original} is in the catalog again; undo what moved it there first")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(source, target)
    _remove_empty_dirs(source.parent, root)
    file.missing, file.path = False, original
    for entry_id in change.undo.get("entries", []):
        entry = session.get(PlaylistEntry, entry_id)
        if entry is not None:
            entry.file_id = file.id


def _remove_empty_dirs(directory: Path, root: Path) -> None:
    """Remove now-empty folders up to (not including) the library root. A folder that
    only holds macOS/Windows litter (.DS_Store, desktop.ini) counts as non-empty."""
    while directory != root and root in directory.parents:
        try:
            if any(True for _ in os.scandir(directory)):
                return  # macOS would rmdir a folder holding only AppleDouble ("._") files
            os.rmdir(directory)
        except OSError:
            return
        directory = directory.parent
