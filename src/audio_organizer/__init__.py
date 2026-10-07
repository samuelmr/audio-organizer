"""Maintain a music collection: catalog, metadata, duplicates, playlists and S3 sync."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("audio-organizer")  # from pyproject.toml, the single source
except PackageNotFoundError:  # running from a source tree that is not installed
    __version__ = "0.0.0"
