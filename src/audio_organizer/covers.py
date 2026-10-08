"""Cover images embedded in audio files (ID3 APIC frames, MP4 covr atoms, FLAC
pictures): reading them, and embedding an album's cover image into files without one.
"""

from pathlib import Path

from mutagen.aac import AAC
from mutagen.flac import FLAC, Picture
from mutagen.id3 import APIC, ID3
from mutagen.mp4 import MP4, MP4Cover

from . import tags

IMAGE_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png"}


def embedded(path: str | Path) -> tuple[str, bytes] | None:
    """(MIME type, data) of the file's front cover (or first picture), or None."""
    audio, file_tags = tags.load_tags(path)
    if isinstance(file_tags, ID3):
        pictures = file_tags.getall("APIC")
        pictures.sort(key=lambda p: p.type != 3)  # the front cover first
        if pictures:
            return pictures[0].mime or "image/jpeg", pictures[0].data
    elif isinstance(audio, MP4) and audio.tags and audio.tags.get("covr"):
        cover = audio.tags["covr"][0]
        mime = "image/png" if cover.imageformat == MP4Cover.FORMAT_PNG else "image/jpeg"
        return mime, bytes(cover)
    elif isinstance(audio, FLAC) and audio.pictures:
        picture = sorted(audio.pictures, key=lambda p: p.type != 3)[0]
        return picture.mime, picture.data
    return None


def embed(path: str | Path, image: str | Path) -> None:
    """Embed the image as the file's front cover."""
    image = Path(image)
    mime = IMAGE_TYPES.get(image.suffix.lower())
    if mime is None:
        raise ValueError(f"Not a JPEG or PNG image: {image.name}")
    data = image.read_bytes()
    audio, file_tags = tags.load_tags(path)
    if audio is None:
        raise tags.UnsupportedFormat(f"Not a recognized audio file: {path}")
    if isinstance(audio, MP4):
        if audio.tags is None:
            audio.add_tags()
        fmt = MP4Cover.FORMAT_PNG if mime == "image/png" else MP4Cover.FORMAT_JPEG
        audio.tags["covr"] = [MP4Cover(data, imageformat=fmt)]
        audio.save()
    elif isinstance(audio, FLAC):
        picture = Picture()
        picture.type, picture.mime, picture.data = 3, mime, data
        audio.add_picture(picture)
        audio.save()
    else:
        _save_id3(audio, file_tags, path, lambda t: t.add(
            APIC(encoding=3, mime=mime, type=3, desc="", data=data)
        ))  # fmt: skip


def remove(path: str | Path) -> None:
    """Remove the file's embedded pictures (to undo embedding one)."""
    audio, file_tags = tags.load_tags(path)
    if isinstance(audio, MP4):
        if audio.tags is not None:
            audio.tags.pop("covr", None)
            audio.save()
    elif isinstance(audio, FLAC):
        audio.clear_pictures()
        audio.save()
    elif isinstance(file_tags, ID3):
        _save_id3(audio, file_tags, path, lambda t: t.delall("APIC"))


def _save_id3(audio, file_tags, path, change) -> None:
    had_tag = file_tags is not None
    if isinstance(audio, AAC):
        file_tags = file_tags if had_tag else ID3()
    elif not had_tag:
        audio.add_tags()
        file_tags = audio.tags
    version = tags.id3_tag_version(file_tags) if had_tag else 3
    change(file_tags)
    tags.prepare_id3(file_tags, version)
    if isinstance(audio, AAC):
        file_tags.save(path, v2_version=version)
    else:
        audio.save(v2_version=version)
