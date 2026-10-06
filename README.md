# audio-organizer

Maintain a music collection: catalog it, fix its metadata, find duplicates, generate
playlists and sync tracks and playlists to S3-compatible object storage. Built to be
used every time music is added (from files or CDs), on libraries of hundreds of
thousands of tracks, with the user reviewing every change before it touches a file.

Status: foundation in place: catalog, incremental scanner, metadata provenance,
reviewable and undoable changesets, CLI. Next: S3 sync and playlists, web UI, intake
of new music (inbox, gnudb, duplicate and album-neighbour matching), CD ripping.

## Setup

Requires Python 3.13+ and [uv](https://docs.astral.sh/uv/). Tests generate audio with
`ffmpeg`.

    uv sync
    uv run aorg --help
    uv run pytest

The catalog is a SQLite database at `~/.local/share/audio-organizer/catalog.db`
unless `--db` or `AORG_DB` points elsewhere (a path or an SQLAlchemy URL).

## Usage

    aorg library add music /Volumes/Multimedia/Music
    aorg scan                         # first run reads everything; later runs only what changed
    aorg stats
    aorg show "Texas/Greatest Hits/11 Halo.mp3"

    aorg edit "Texas/Texas_Halo.mp3" --set album="Greatest Hits" --set tracknumber=11
    aorg changes list
    aorg changes show 1
    aorg changes approve 1            # or: aorg changes approve 1 3 4  (single changes)
    aorg changes apply 1 --dry-run
    aorg changes apply 1
    aorg changes undo 1

## Design

- **Tags are evidence, not truth.** Each metadata field can have candidate values from
  several sources: the file's tags, its path, a manual edit, gnudb, neighbouring files.
  All are kept (`FieldValue`), and the effective value is resolved by source priority;
  a manual edit always wins. `aorg show` lists the value and the disagreeing candidates.
- **Files are identified by their audio**, a hash of the audio data without tags
  (`audiohash.py`). Retagging keeps the identity; a file moved outside the tool is
  recognized as moved on the next scan and keeps its history; byte-identical copies
  are found for free.
- **Original tags are kept**: a snapshot of each file's tags is stored when it is first seen.
- **Nothing is changed without review.** Retags and moves are proposed in changesets,
  approved one by one or all at once, applied, and can be undone. A file that changed on
  disk after it was scanned is never written to.
- **Incremental scanning.** Unchanged files (same size and modification time) are not
  read again; new and changed files are read in parallel, which matters on network shares.
- **Ready for a hosted service.** The core (`audio_organizer.*`) has no CLI or local-disk
  assumptions beyond the library root; the CLI and a future web UI are thin layers on it,
  and the catalog runs on any SQLAlchemy database.

| Module | Purpose |
| --- | --- |
| `models.py` | Catalog schema |
| `scanner.py` | Incremental scanning, move detection |
| `tags.py` | Reading and writing tags (ID3, MP4, Vorbis comments), incl. MusicMatch mood/tempo |
| `pathparse.py` | Metadata evidence from folder and file names |
| `provenance.py` | Resolving field values from their sources |
| `audiohash.py` | Audio-only content hash |
| `changes.py` | Changesets: propose, approve, apply, undo |
| `cli.py` | `aorg` command |

`reference-material/` holds the earlier PHP and shell scripts this project replaces.
