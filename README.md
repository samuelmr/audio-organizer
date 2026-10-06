# audio-organizer

Maintain a music collection: catalog it, fix its metadata, make playlists and sync
tracks and playlists to S3-compatible object storage. Made to be used whenever you add music, on libraries of
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

### Playlists

*Smart* playlists are rules over the catalog and stay up to date as the library
changes; *static* playlists are lists you curate or import.

    aorg playlists create "90s Rock" --rule "genre=Rock year>=1990 year<2000"
    aorg playlists create "Best Of {mood}" --group-by mood --rule 'preference="Very Good|Excellent"'
    aorg playlists show "90s Rock"
    aorg playlists list

A rule is a list of conditions that must all hold. `=` equals, `!=` does not equal,
`~` contains and `!~` does not contain, all case-insensitive, with alternatives
separated by `|`; an empty value means a missing field (`album=`). `>`, `>=`, `<` and
`<=` compare numbers. Fields are the tag fields (artist, albumartist, album, title,
tracknumber, discnumber, date, genre, compilation, mood, tempo, situation, preference)
plus year, path, format, duration (seconds) and bitrate (kbps).

With `--group-by`, one playlist is made per value of a field, named after it: the
example makes "Best Of Mellow", "Best Of Upbeat" and so on. Smart playlists hold one
copy of each song (identical audio, or the same artist and title) and are shuffled by
default; the shuffled order stays the same from run to run. `--sort` also takes
`album`, `artist`, `title`, `added` (newest first) or `path`, and `--max` limits the
length. `aorg playlists edit` changes any of these.

Static playlists:

    aorg playlists import ~/Playlists/*.m3u     # entries may be absolute or relative paths
    aorg playlists create "Party"
    aorg playlists add "Party" "Texas/Greatest Hits/11 Halo.mp3"
    aorg playlists remove "Party" "Texas/Greatest Hits/11 Halo.mp3"

Their tracks stay in them when files are moved or renamed. To write playlists into a
folder, with tracks referred to by relative paths:

    aorg playlists export ~/Music/Playlists --format m3u,json,html

Besides `.m3u`, playlists can be written as schema.org `MusicPlaylist` JSON-LD and as an
HTML page.

### Syncing to object storage

    aorg target reconcile backup      # once, if the bucket already holds your music
    aorg sync backup --dry-run
    aorg sync backup

Tracks are stored at their path in the library (under the target's `--prefix`, if
given), with these object metadata, percent-encoded: `artist`, `albumartist`, `album`,
`title`, `tracknumber`, `discnumber`, `year`, `genre`, `length` (milliseconds) and
`image` (the key of the album's cover image, which is uploaded too). The values are the
catalog's, so your edits are synced even before they are written into the files.

All playlists of the library are synced too, as `.m3u`, `.json` and `.html` files in
`playlists/` (under the prefix), referring to the tracks by relative paths
(`../Artist/Album/01 Title.mp3`).

Only new and changed files are uploaded. Metadata changes and moved files are handled
with server-side copies, without uploading the audio again. Objects that are no longer
in the library, such as the files of a deleted playlist, are kept unless you add
`--delete`.

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
| `tracks.py` | Streaming tracks with resolved metadata, in batches |
| `playlists.py` | Playlist rules, generation in one pass, import and export |
| `playlist_formats.py` | M3U, JSON-LD and HTML playlist files |
| `sync.py` | Planning and carrying out syncs to object storage |
| `objectstore.py` | Object storage backends (S3-compatible) |
| `cli.py` | The `aorg` command |
