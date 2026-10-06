# audio-organizer

Maintain a music collection: catalog it, fix its metadata and sync tracks to
S3-compatible object storage. Made to be used whenever you add music, on libraries of
hundreds of thousands of tracks, with you reviewing every change before it touches a
file.

## Installation

Requires [uv](https://docs.astral.sh/uv/) (it installs Python 3.13 if needed). From a
checkout of this repository:

    uv tool install .

This puts the `aorg` command on your path. Run `aorg --help`, or `aorg <command> --help`,
for all commands and options.

## Configuration

**Catalog.** What `aorg` knows about your music is kept in a SQLite database at
`~/.local/share/audio-organizer/catalog.db`. To use another file, set `AORG_DB` or pass
`--db` before the command (`aorg --db ~/music.db scan`). Both also accept an SQLAlchemy
database URL.

**Libraries.** Register each music folder once under a name:

    aorg library add music /Volumes/Multimedia/Music

**Object storage.** Add a sync target for a bucket. The endpoint is needed for
S3-compatible services other than AWS. Credentials are not stored by `aorg`: they come
from the `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` environment variables,
`~/.aws/credentials`, or a named profile (`--profile`).

    aorg target add backup --bucket music --endpoint-url https://s3.example.com
    aorg target list

## Usage

### Scanning

    aorg scan                         # name the library if you have several
    aorg stats
    aorg show "Texas/Greatest Hits/11 Halo.mp3"

The first scan reads every file; later scans only read new and changed ones, so run
`aorg scan` whenever you have added or changed music. Files moved or renamed outside
`aorg` are recognized by their audio and keep their history.

`aorg show` lists each field with where its value comes from: the file's tags (`tag`),
its folder and file name (`path`) or your edit (`manual`), and the other candidates when
they disagree.

### Editing metadata

Edits are stored in the catalog and proposed as a *changeset*. Nothing is written to
your files until you approve and apply it:

    aorg edit "Texas/Texas_Halo.mp3" --set album="Greatest Hits" --set tracknumber=11
    aorg changes list
    aorg changes show 1
    aorg changes approve 1            # all changes in changeset 1, or: approve 1 3 4
    aorg changes apply 1 --dry-run
    aorg changes apply 1
    aorg changes undo 1               # restores the tags and reverts your edit

`aorg` never writes to a file that has changed since it was last scanned; rescan first.
The tags each file had when it was first scanned are kept in the catalog.

### Syncing to object storage

    aorg target reconcile backup      # once, if the bucket already holds your music
    aorg sync backup --dry-run
    aorg sync backup

Tracks are stored at their path in the library (under the target's `--prefix`, if
given), with these object metadata, percent-encoded: `artist`, `albumartist`, `album`,
`title`, `tracknumber`, `discnumber`, `year`, `genre`, `length` (milliseconds) and
`image` (the key of the album's cover image, which is uploaded too). The values are the
catalog's, so your edits are synced even before they are written into the files.

Only new and changed files are uploaded. Metadata changes and moved files are handled
with server-side copies, without uploading the audio again. Objects that are no longer
in the library are kept unless you add `--delete`.

`reconcile` matches what is already in the bucket against your library, so music
uploaded by other tools is not uploaded again. It changes nothing in the bucket.

## Contributing

Development needs uv and, for the tests, `ffmpeg` (they generate short audio files):

    uv sync
    uv run aorg --help
    uv run pytest
    uv run ruff format && uv run ruff check

Design principles:

- **Tags are evidence, not truth.** Every candidate value of a field is stored with its
  source (`FieldValue`), and the effective value is resolved by source priority
  (`provenance.py`); manual edits always win.
- **Files are identified by their audio**, a hash of the audio data without tags, so
  retagging and moving keep a file's identity, and identical copies are easy to find.
- **No file changes without review**: every retag or move goes through a changeset that
  records how to undo it.
- **Scale**: scans and syncs are incremental, and anything per-file runs in batches or
  in parallel.
- **Core first**: the CLI is a thin layer over the `audio_organizer` package, so that
  other front ends (a web UI, a hosted service) can share it.

| Module | Purpose |
| --- | --- |
| `models.py` | Catalog schema |
| `db.py` | Database setup |
| `scanner.py` | Incremental scanning, move detection |
| `tags.py` | Reading and writing tags (ID3, MP4, Vorbis comments) |
| `pathparse.py` | Metadata evidence from folder and file names |
| `provenance.py` | Resolving field values from their sources |
| `audiohash.py` | Audio-only content hash |
| `changes.py` | Changesets: propose, approve, apply, undo |
| `sync.py` | Planning and carrying out syncs to object storage |
| `objectstore.py` | Object storage backends (S3-compatible) |
| `cli.py` | The `aorg` command |
