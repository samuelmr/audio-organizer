"""Facts about JPEG and PNG images read from their headers, without an imaging library."""

IMAGE_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png"}


def dimensions(head: bytes) -> tuple[int, int] | None:
    """(width, height) of a JPEG or PNG image from its first bytes (64 KB is plenty for
    any JPEG whose metadata comes before the image), or None."""
    if head[:8] == b"\x89PNG\r\n\x1a\n" and len(head) >= 24:
        return int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big")
    if head[:2] != b"\xff\xd8":
        return None
    pos = 2
    while pos + 9 < len(head):
        if head[pos] != 0xFF:
            return None
        marker, length = head[pos + 1], int.from_bytes(head[pos + 2 : pos + 4], "big")
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):  # start of frame
            height = int.from_bytes(head[pos + 5 : pos + 7], "big")
            width = int.from_bytes(head[pos + 7 : pos + 9], "big")
            return width, height
        pos += 2 + length
    return None


def mime_type(data: bytes) -> str | None:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:2] == b"\xff\xd8":
        return "image/jpeg"
    return None


def format_size(size: tuple[int, int] | None) -> str:
    """The value of the embedded_cover field: "500x500", or "?" when the size is unknown."""
    return f"{size[0]}x{size[1]}" if size else "?"


def parse_size(value: str | None) -> tuple[int, int] | None:
    width, sep, height = (value or "").partition("x")
    return (int(width), int(height)) if sep and width.isdigit() and height.isdigit() else None
