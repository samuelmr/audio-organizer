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

**Object storage.** A sync target says where a library is copied to: a bucket, and
optionally a folder inside it (`--prefix`). Names use letters, digits, `.`, `_` and `-`.
The endpoint is needed for S3-compatible services other than AWS. Credentials are not
stored by `aorg`: they come from the `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`
environment variables, `~/.aws/credentials`, or a named profile (`--profile`).

    aorg target add aws --bucket my-music --prefix mp3 --region eu-north-1
    aorg target add nas --bucket music --endpoint-url https://s3.example.com
    aorg target list                  # bucket, prefix, objects known, last sync
    aorg target show nas              # and where tracks, covers and playlists go
    aorg target rename nas home
    aorg target remove home           # forgets the target; the bucket is not touched

Targets can also be added, edited and removed on the web UI's Sync page. If you add a
removed target again, run `aorg target reconcile` before syncing, so that files are not
uploaded twice.

## Usage

### Scanning

    aorg scan                         # name the library if you have several
    aorg stats
    aorg show "Texas/Greatest Hits/11 Halo.mp3"

The first scan reads every file; later scans only read new and changed ones, so run
`aorg scan` whenever you have added or changed music. Files moved or renamed outside
`aorg` are recognized by their audio and keep their history.

After updating `aorg`, `aorg scan --reread` reads every file again once, so that tags a
newer version reads (such as ratings, sort names or embedded covers) are in the catalog.

A *deep* scan also measures the loudness of every file not measured yet, for ReplayGain,
and looks up missing album covers online:

    aorg scan --deep                  # continues where an interrupted run stopped
    aorg scan --deep --deep-limit 500 # or in parts
    aorg scan --deep --min-pixels 500 # covers smaller than 500 px count as missing

It reads every file in full, so the first one takes long (about 1 file per second from
a network share). It uses ReplayGain 2.0 (EBU R128 loudness, -18 LUFS): track gain and
peak for each file, and album gain and peak once all of an album's tracks (the files of
one folder with the same album tag) are measured. The album gain is computed from the
tracks' loudness rather than by measuring the whole album again, which is usually within
a few tenths of a dB. The results stay in the catalog until you write them into the
files (see [Writing tags](#writing-tags)); a file whose audio changes is measured again.

Albums whose tracks have no embedded cover, or one smaller than `--min-pixels` (200 by
default) on a side, and whose folder has no image at least that big, are looked up in
[MusicBrainz](https://musicbrainz.org) and their front cover fetched from the
[Cover Art Archive](https://coverartarchive.org), in the smallest size that is big
enough (250, 500 or 1200 pixels, else the original). An album is found by its album
artist (or the tracks' common artist, else "Various Artists") and title. The covers are
kept in the catalog until you embed them and save them in their folders (see
[Covers](#covers)); each album is looked up once, and again only for a larger
`--min-pixels`. MusicBrainz asks for a contact address in every request: `aorg` sends
the one in `AORG_CONTACT`, else the package author's.

`aorg show` lists each field with where its value comes from: the file's tags (`tag`),
its folder and file name (`path`) or your edit (`manual`), and the other candidates when
they disagree.

### Web UI

    aorg web                          # then open http://127.0.0.1:8000/

The web UI shows the same catalog: browse, search and sort your tracks, list those whose
sources disagree or that lack an album or title, see a track's values and where they
come from, its identical copies and its original tags, and edit them. Click an album,
artist or folder to see just its tracks (an album's in disc and track order), or browse
the albums list with their covers. Albums are grouped by their tags, so the tracks of an album scattered
across folders show up together. It is also where changesets are easiest to review:
select changes (or all, then deselect some) to approve or reject, try a dry run, apply
and undo. The ▶ buttons on track, duplicate and changeset pages play the files in the
browser, so copies can be compared by ear (MP3, M4A and AAC play everywhere; WMA does
not). Playlists can be viewed, created and edited, and a sync target's plan previewed.

The web UI has no login, so it only listens on your own computer unless you give
`--host`. It refuses form submissions that come from other websites.

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

Besides title, artist, album, track and disc numbers, year and genre, these fields can
be read and edited: original year, album artist and artist sort names, composer,
conductor, grouping, subtitle, language, mood, a 1–5 star rating, the gnudb disc ID and
ReplayGain values. They are written in the usual tags for MP3 (ID3), M4A and FLAC/Ogg,
so that other players read them; moods also go to the standard MOOD tag rather than only
the MusicMatch comment they may have come from.

On a track's page in the web UI:

- **Measure ReplayGain** measures the track's loudness with ffmpeg and stores the track
  gain and peak (and the album's, once all its tracks are measured) in the catalog.
  Values that are not yet in the file are marked, and **Write them to the file**
  proposes writing them.
- When writing to MP3 files, you can keep their ID3 version or write ID3v2.3 or 2.4.
  ID3v2.3 is read by more players and devices; both keep all of the fields above.
- **Cover** shows the cover embedded in the file and the image in its folder, and can
  embed the folder's image in a file that has none.

### Writing tags

Edits, measurements and lookups are kept in the catalog until written into the files;
values only inferred from file and folder names are not written. To write them in bulk:

    aorg write-tags                           # what is waiting to be written
    aorg write-tags --only replaygain --path "Texas" --propose
    aorg write-tags --propose --limit 200 --id3-version 3

`--only` takes field names or their beginnings (`replaygain` covers all ReplayGain
fields), `--id3-version` writes MP3 tags as ID3v2.3 or 2.4 (by default each file keeps
its own), and each batch becomes a changeset to review. The web UI's Write tags page
does the same.

### Ratings from MusicMatch preferences

MusicMatch Jukebox kept a "preference" for tracks (Excellent, Very Good, Good, Fair,
Poor, Bad Taste). They can become 1–5 star ratings that other players read:

    aorg ratings                              # preferences, with the stars they map to
    aorg ratings --set "Good=" --set "Bad Taste=1" --apply
    aorg write-tags --only rating --propose

The default mapping is Excellent ★★★★★, Very Good ★★★★, Good ★★★, Fair ★★, Poor and Bad
Taste ★; an empty value gives no rating. A rating already in a file is kept. The web
UI's Ratings page does the same. Smart playlists can then use rules such as `rating>=4`.

### Covers

Phones, car stereos and many players only show covers embedded in the files:

    aorg covers                               # tracks without a cover, and the cover
    aorg covers --path "Björk" --propose      # embed it into those tracks
    aorg covers --min-pixels 500 --propose    # also replace covers under 500 px

The cover is the folder's image, `cover`, `folder` or `front` (`.jpg`, `.jpeg` or
`.png`), or its only image; when the folder has none at least `--min-pixels` (200 by
default) on a side, it is the cover a deep scan found online. Smaller images, such as
Windows Media Player's `AlbumArtSmall.jpg`, are thumbnails: listed but not embedded,
and an embedded thumbnail is replaced (undo puts it back). The image is copied into
every track, so mind large images. A file whose cover is big enough is never given
another one. The web UI's Covers page does the same, also for single folders, and a
track's page can embed its folder's cover into that one file.

A cover found online is also saved as the folder's `cover.jpg` (`cover.png` for a PNG),
so that players that read folder images and [sync](#syncing-to-object-storage) have it
too; also for albums whose tracks already have it embedded. That happens only when the
folder holds just that album and has no image big enough. A smaller `cover.jpg` is
replaced (undo puts it back); other images are left alone, as `cover.jpg` comes first.
With `--no-save-images` covers are only embedded.

Catalogs made before `aorg` read the size of embedded covers know only that a file has
one; the next deep scan reads their sizes (only the tags, not the whole files).

### Duplicates

Files with identical audio are copies of one track, even when their tags and names
differ. `aorg dupes` finds them and proposes, in batches, to trash the loose copies:

    aorg dupes                                # what would be proposed
    aorg dupes --path "Texas" --propose       # a changeset for the copies under Texas/
    aorg dupes --propose --limit 100          # or the next 100 tracks anywhere

When exactly one album folder (`Artist/Album/…`) holds a copy, that copy is kept. Copies
lying loose in an artist folder, in "Unknown Album", or next to it as iTunes copies
("Title 1.mp3") are proposed for the trash. Tags only the loose copies have are first
copied to the kept file, and static playlists switch to it. Tracks with copies in
several album folders are left for you to decide, and so are tracks whose only album
copy is on a compilation while another copy is in the artist's own folder (add
`--include-compilations` to include them). The web UI's Duplicates page does the same,
and there you can choose another copy to keep for any track, and listen to the copies.

Trashed files are moved to `.aorg-trash/` in the library folder, and undoing the
changeset brings them back. They are only deleted when you empty the trash:

    aorg trash                                # what is in it
    aorg trash empty

### Album folders

The same album often sits in several folders ("Texas/Greatest Hits" and
"Texas/The Greatest Hits", or a fragment in another artist's folder). `aorg merge-albums`
finds pairs of album folders that share identical tracks and have the same album name
(or share at least half of their tracks):

    aorg merge-albums                         # candidates
    aorg merge-albums --propose --limit 20    # a changeset for a batch of safe merges
    aorg merge-albums --from "Texas/Greatest Hits" --into "Texas/The Greatest Hits"

Merging one folder into another trashes its copies of tracks the other already has (that
copy is kept and playlists switch to it) and moves its other tracks over. A track whose
number the other folder already uses on the same disc is a *clash*, which usually means
a different recording or edition; it stays where it is. Batches only include pairs with
the same album name and no clashes; merge the others one by one in the direction you
choose, here or on the web UI's Album folders page. To go the other way after proposing
a merge, reject its changeset (or undo it, if applied) and propose the other direction.

### Folder layout

The layout is a template of where files belong, filled in from their metadata. The
default is the iTunes-style `{albumartist}/{album}/{disc}{track} {title}`:

    aorg layout                               # show it
    aorg layout --set "{albumartist}/{year} {album}/{track} {title}"
    aorg reorganize                           # what would move
    aorg reorganize --path "Elfman, Danny" --propose
    aorg reorganize --propose --limit 20      # or the next 20 albums

Fields: `{albumartist}`, `{artist}`, `{album}`, `{title}`, `{genre}`, `{year}`, `{track}`
(two digits) and `{disc}` ("2-" on albums with several discs, otherwise empty). Without
an album artist tag, the album artist is decided per album, so its tracks stay
together: the artist of most of its tracks, or "Various Artists"; compilations always
go under "Various Artists". Characters that file systems do not allow become `_`.

`{albumartistsort}` and `{artistsort}` file artists under a sort name you have set,
such as "Morissette, Alanis" for "Alanis Morissette", and under their name otherwise:

    aorg artists                              # sort names, and suggested ones
    aorg artists --set "Alanis Morissette=Morissette, Alanis"
    aorg artists --accept-suggestions
    aorg layout --set "{albumartistsort}/{album}/{disc}{track} {title}"

Suggestions come from your folders: a folder named "Morissette, Alanis" suggests that sort
name for the artist "Alanis Morissette". Names are never turned around by rule, which would
also file bands under their last word. The web UI's Artists page lists the artists, with
their suggestions to accept one by one or all at once.

Files move album by album, and an album's cover image goes with it when its last track
leaves a folder. Files lacking a field the template needs stay where they are, and so do
conflicts: a path that is taken, or that several files would get. These are mostly
duplicates, so clean up duplicates and album folders first. Names that differ only in
letter case are left alone. The web UI's Layout page does the same, and lets you propose
a single album.

### Playlists

*Smart* playlists are rules over the catalog and stay up to date as the library
changes; *static* playlists are lists you curate or import.

    aorg playlists create "90s Rock" --rule "genre=Rock year>=1990 year<2000"
    aorg playlists create "Best Of {mood}" --group-by mood --rule 'preference="Very Good|Excellent"'
    aorg playlists show "90s Rock"
    aorg playlists show "Best Of Mellow"         # one generated by "Best Of {mood}"
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

    aorg playlists import ~/Playlists/*.m3u
    aorg playlists create "Party"
    aorg playlists add "Party" "Texas/Greatest Hits/11 Halo.mp3"
    aorg playlists remove "Party" "Texas/Greatest Hits/11 Halo.mp3"

Imported entries may be absolute paths, or relative to the library or to the `.m3u`
file. When a path does not match exactly, a loose match that ignores a leading "The",
punctuation and accents is accepted if only one file fits. Entries that are playlists
themselves, such as an album's playlist, are replaced by their tracks.

Tracks stay in static playlists when files are moved or renamed. To write playlists
into a folder, with tracks referred to by relative paths:

    aorg playlists export ~/Music/Playlists --format m3u,json,html

Besides `.m3u`, playlists can be written as schema.org `MusicPlaylist` JSON-LD and as an
HTML page.

### Syncing to object storage

    aorg target reconcile backup      # once, if the bucket already holds your music
    aorg sync backup --dry-run
    aorg sync backup

The bucket looks like this (below the target's prefix, if any):

    Artist/Album/01 Title.mp3        tracks, at their library paths
    Artist/Album/cover.jpg           the album's cover image
    playlists/Best Of Upbeat.m3u     playlists as .m3u, .json and .html

Tracks carry these object metadata (`x-amz-meta-*`), all optional, lowercase,
percent-encoded UTF-8, and left out when empty:

| Key | Value |
|---|---|
| `artist`, `albumartist`, `album`, `title`, `genre`, `composer` | text |
| `artistsort`, `albumartistsort` | text |
| `tracknumber`, `tracktotal`, `discnumber`, `disctotal` | plain integers: "02." or "2/12" is `2` |
| `year` | four digits |
| `originaldate` | `YYYY`, `YYYY-MM` or `YYYY-MM-DD` |
| `length` | milliseconds |
| `image` | the full bucket key of the album's cover image, which is uploaded too |
| `compilation` | `1` for a compilation (iTunes TCMP), else left out |
| `rating` | integer 1–5 |
| `mood` | text |
| `replaygaintrackgain`, `replaygainalbumgain` | signed decimal dB without unit, e.g. `-7.23` |
| `replaygaintrackpeak`, `replaygainalbumpeak` | linear float, e.g. `0.988`; true peaks can exceed 1 |
| `truncated` | the keys that were cut to fit, comma-separated |

Several values in one field (two artists, genres or moods) are separated by a semicolon
and a space, as in `Rock; Pop`; commas are not separators, since names such as "Earth, Wind & Fire" contain
them. The values are the catalog's, so your edits are synced even before they are written
into the files. The size limit of S3 user metadata is 2 KB (accented letters take six
characters once encoded); when a track's values exceed it, the longest values are cut
and listed in `truncated`. Adding keys to the list changes the metadata of already synced
tracks: the next sync updates them with server-side copies, without uploading the audio.

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
  The hash covers the audio's length and three samples of it (`audiohash.py`), and
  each file is read once, in large blocks shared by the tag reader and the hasher
  (`blockfile.py`): network round trips and bytes read, not CPU, limit scanning.
- **No file changes without review**: every retag or move goes through a changeset that
  records how to undo it.
- **Scale**: scans and syncs are incremental, and anything per-file runs in batches or
  in parallel.
- **Core first**: the CLI is a thin layer over the `audio_organizer` package, so that
  other front ends (a web UI, a hosted service) can share it.

| Module | Purpose |
| --- | --- |
| `models.py` | Catalog schema |
| `db.py` | Database setup and migrations |
| `migrations/` | Alembic migrations of existing tables (run automatically when the catalog is opened); new tables come from the models |
| `scanner.py` | Incremental scanning, move detection |
| `tags.py` | Reading and writing tags (ID3, MP4, Vorbis comments) |
| `pathparse.py` | Metadata evidence from folder and file names |
| `provenance.py` | Resolving field values from their sources; the effective metadata table |
| `audiohash.py` | Audio-only content hash (sampled) |
| `blockfile.py` | Block-cached reading of a file, for few network round trips |
| `changes.py` | Changesets (each records the feature that proposed it): propose, approve, apply, undo; retag, move and trash |
| `duplicates.py` | Identical copies and what to keep |
| `album_merge.py` | Album folders holding the same album, and merging them |
| `layout.py` | The folder layout template, and moving files into it |
| `artists.py` | Artists' sort names and suggestions for them |
| `loudness.py` | ReplayGain 2.0: measuring tracks with ffmpeg, album gains, deep scan |
| `covers.py` | Embedded cover images: reading, embedding, replacing thumbnails; which tracks get which cover |
| `coverart.py` | Looking up album covers online (MusicBrainz, Cover Art Archive) in the deep scan |
| `images.py` | Size and type of JPEG and PNG images from their headers |
| `writing.py` | Writing values not yet in the files, in batches |
| `ratings.py` | Star ratings from MusicMatch preferences |
| `tracks.py` | Streaming tracks with resolved metadata, in batches |
| `playlists.py` | Playlist rules, generation in one pass, import and export |
| `playlist_formats.py` | M3U, JSON-LD and HTML playlist files |
| `sync.py` | Planning and carrying out syncs to object storage |
| `objectstore.py` | Object storage backends (S3-compatible) |
| `cli.py` | The `aorg` command |
| `web/` | The web UI: FastAPI app (`app.py`), Jinja2 templates, stylesheet |
