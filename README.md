# audio-organizer

Maintain a music collection: catalog it, fix its metadata, make playlists and sync
tracks and playlists to S3-compatible object storage. Made to be used whenever you add music, on libraries of
hundreds of thousands of tracks, with you reviewing every change before it touches a
file.

## Contents

- [Installation](#installation)
- [Configuration](#configuration)
- [Usage](#usage)
  - [Scanning](#scanning)
  - [Web UI](#web-ui)
  - [Folder layout](#folder-layout)
  - [Album folders](#album-folders)
  - [Loose tracks](#loose-tracks)
  - [Duplicates](#duplicates)
  - [Tracks in several encodings](#tracks-in-several-encodings)
  - [Leftover files](#leftover-files)
  - [Editing metadata](#editing-metadata)
  - [Covers](#covers)
  - [Ratings from MusicMatch preferences](#ratings-from-musicmatch-preferences)
  - [Writing tags](#writing-tags)
  - [Undoing decisions](#undoing-decisions)
  - [Playlists](#playlists)
  - [Syncing to object storage](#syncing-to-object-storage)
  - [Trash](#trash)
- [Contributing](#contributing)

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
    aorg target set nas --playlist-folder Playlists --playlist-formats m3u,json
    aorg target remove home           # forgets the target; the bucket is not touched

Playlist files go to the root of the bucket (under the prefix) by default, in the `.m3u`,
`.json` and `.html` formats; `aorg target set` changes the folder and formats. The same
settings exist for a library's disk: `aorg library set music --playlist-folder ''
--playlist-formats m3u`. Entries in a playlist are relative to its folder, so the files
are rewritten when the folder changes; copies in the old place are left behind.

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

**Folders whose names differ only in letter case.** A NAS can hold both "Normaalien
Maihinnousu" and "Normaalien maihinnousu" and list both, while a Mac (or any computer
that treats the share as case-insensitive) opens the same one of them under either name.
The other cannot be read from that computer. `aorg scan` reads such a folder once, under
the name that sorts first, and lists the other names it skipped; files cataloged under
them by an earlier version become missing. Look at the skipped folder on the NAS itself:
if it holds the same files, delete it there; otherwise give it a name of its own, and
the next scan reads it.

After updating `aorg`, `aorg scan --reread` reads every file again once, so that tags a
newer version reads (such as ratings, sort names or embedded covers) are in the catalog.

A scan can also do slower work, each part asked for with its own option:

    aorg scan --deep                  # measure loudness (ReplayGain) of every file
    aorg scan --with-covers           # look up missing album covers online
    aorg scan --with-gnudb            # look up albums in gnudb
    aorg scan --deep --with-covers --with-gnudb   # all of them
    aorg scan --deep --deep-limit 500 # in parts: at most 500 files, and 500 albums per lookup
    aorg scan --with-covers --min-pixels 500      # covers smaller than 500 px count as missing

The options are independent. When several are given, the steps run one after another,
each finishing before the next starts, and every one continues where an interrupted run
stopped:

1. the normal scan of new and changed files;
2. `--deep`: loudness of the files not measured yet (about a second per file from a
   network share, so a library of 100,000 tracks takes about a day);
3. `--with-gnudb`: gnudb lookups of the album folders not looked up yet (6 to 15 seconds
   per album, see below), skipped without `AORG_GNUDB_EMAIL`;
4. `--with-covers`: cover lookups in MusicBrainz for the albums not looked up yet (a few
   seconds per album).

`--deep-limit N` caps each step: N files measured, N albums asked of gnudb, N albums of
MusicBrainz. **With `--with-gnudb` or `--with-covers` a scan takes much longer**: they
ask a web service about one album at a time, and politely, so thousands of albums take
hours each, on top of the loudness step's day. They are off by default because they work
best on a tidy library: albums are found by their folder, artist, title and track count,
so duplicates, loose tracks and misnamed folders give misses and wasted requests. Remove
duplicates, put tracks into their album folders and rename them first, then look the
albums up, in parts with `aorg gnudb` and `aorg coverart` (below).

**Loudness.** `--deep` reads every file in full, so the first one takes long. It uses
ReplayGain 2.0 (EBU R128 loudness, -18 LUFS): track gain and peak for each file, and
album gain and peak once all of an album's tracks (the files of one folder with the same
album tag) are measured. The album gain is computed from the tracks' loudness rather than
by measuring the whole album again, which is usually within a few tenths of a dB. The
results stay in the catalog until you write them into the files (see
[Writing tags](#writing-tags)); a file whose audio changes is measured again. A file
that cannot be measured (DRM-protected WMA, a corrupt file) is reported and remembered,
and skipped by later scans; `aorg scan --deep --retry-failed` tries those again. The
Pending page lists them with the reason (DRM-protected files say so).

**Covers.** Albums whose tracks have no embedded cover, or one smaller than `--min-pixels`
(200 by default) on a side, and whose folder has no image at least that big, are looked up
in [MusicBrainz](https://musicbrainz.org) and their front cover fetched from the
[Cover Art Archive](https://coverartarchive.org), in the smallest size that is big
enough (250, 500 or 1200 pixels, else the original). An album is found by its album
artist (or the tracks' common artist, else "Various Artists") and title. The covers are
kept in the catalog until you embed them and save them in their folders (see
[Covers](#covers)); each album is looked up once, and again only for a larger
`--min-pixels`. MusicBrainz asks for a contact address in every request: `aorg` sends
the one in `AORG_CONTACT`, else the package author's. To look up covers apart from a
scan, in parts or for one album, use `aorg coverart`, or the *Look up cover* button above
the tracks of an album or folder in the web UI (it waits for the answer, a few seconds).
A single album is looked up again even if nothing was found before:

    aorg coverart --limit 100         # ask about 100 albums; the next run continues
    aorg coverart --path "Texas/Greatest Hits"

**gnudb.** Complete single-disc album folders (all tracks present, numbered 1..n or not
at all, with a known album artist and title) are looked up in [gnudb](https://gnudb.org),
the free CDDB database, by a table of contents made from the tracks' durations. A gnudb
entry is accepted only when its artist and track count agree with the folder's, its album
title is much like the folder's (a typo, or a trailing "CD" or "(Remastered)", does not
matter) and most of its track titles are like the tracks'. Its album, artist, year, genre
and track titles are kept in the catalog as `gnudb` values, which are only evidence: they
fill in what the tags lack, but where they differ from the tags the tags stay effective
and the track shows as disputed. Nothing is written into the files until you ask. A wrong
match is easy to discard. Each folder is looked up once, and again when its track count
changes.

gnudb asks every client for a contact address, and refuses those that give none. Set
`AORG_GNUDB_EMAIL` to your own email address (`AORG_CONTACT` is used if it is not set);
without one `aorg scan --with-gnudb` says so before it starts and, at a terminal, asks
whether to scan without the lookups (scripts just skip them). To be kind to gnudb, which may block an
address that sends too many requests, `aorg` waits three seconds between requests, and
stops at the first refusal. An album takes one request to find candidates and up to four
more to read them, so 6 to 15 seconds: about 10 to 25 minutes for 100 albums, and a few
hours for thousands. To spread that over several days, or to run it apart from a scan,
use `aorg gnudb`, and to look up one album at once, give its folder (or use the *Look up
in gnudb* button above the tracks of an album or folder in the web UI; it waits for the
answer, a few seconds). The one album is looked up again even if it was looked up before,
and the entry found is listed:

    aorg gnudb --limit 100            # ask about 100 albums; the next run continues
    aorg gnudb --path "Texas/Greatest Hits"

`aorg show` lists each field with where its value comes from: the file's tags (`tag`),
its folder and file name (`path`) or your edit (`manual`), and the other candidates when
they disagree.

**Please support gnudb.** gnudb is not affiliated with audio-organizer, but it is a
valuable resource, which Rene Mogensen operates as an individual. As the developer of
audio-organizer I have donated 40 €. If you find audio-organizer valuable, please consider
[contributing to gnudb.org](https://gnudb.org/donate.php) too.

### Web UI

    aorg web                          # then open http://127.0.0.1:8000/

The web UI shows the same catalog: browse, search and sort your tracks, list those whose
sources disagree or that lack an album or title, see a track's values and where they
come from, its identical copies and its original tags, and edit them. Click an album,
artist or folder to see just its tracks (an album's in disc and track order), or browse
the albums list with their covers. Albums are grouped by their tags, so the tracks of an album scattered
across folders show up together. The Pending page shows, album by album, what your edits
and measurements would change in the files, and writes the albums you choose; the
History page lists your edits and other decisions, and undoes them. The web UI
is also where changesets are easiest to review:
select changes (or all, then deselect some) to approve or reject, try a dry run, apply
and undo. The ▶ buttons on track, duplicate and changeset pages play the files in the
browser, so copies can be compared by ear (MP3, M4A and AAC play everywhere; WMA does
not). Playlists can be viewed, created and edited; a playlist's page also writes its files to the library disk or a bucket (showing each file's full path, which you can change before writing), deletes them again and restores deleted or replaced ones, and the Playlists page saves the default folders and formats. On the Sync page, a target's plan
can be reviewed and started, and its progress followed.

The web UI has no login, so it only listens on your own computer unless you give
`--host`. It refuses form submissions that come from other websites.

### Folder layout

The layout is a template of where files belong, filled in from their metadata. The
default is the iTunes-style `{albumartist}/{album}/{disc}{track} {title}`:

    aorg layout                               # show it
    aorg layout --set "{albumartist}/{year} {album}/{track} {title}"
    aorg reorganize                           # what would move
    aorg reorganize --path "Elfman, Danny" --decide
    aorg reorganize --decide --limit 20       # or the next 20 albums
    aorg pending --only path --write          # review, then move the files (undoable)

Choosing only changes the catalog (`aorg history` undoes it); the files move when written
from the Pending page, together with any tags waiting for them. The chosen paths are kept
as they were worked out, so after changing the layout, undo the choice and choose again.

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
also file bands under their last word. To stop suggestions for an artist whose name is right
as it is, set the name as its own sort name (`--set "Yello=Yello"`). The web UI's Artists
page lists the artists, with their suggestions to accept one by one or all at once, and a
"Keep as is" button for the same purpose.

Files move album by album. When an album's last track leaves a folder, the folder's other
files go with it: the cover, a `.cue` or `.nfo`, a PDF booklet, and subfolders without
music such as Scans, so the old folder empties and disappears. A folder where other
tracks stay keeps all of them, and nothing is taken from the library's top folder. The
Pending page lists the files each move takes along; when the tracks are written in parts,
they go with the last one. Files lacking a field the template needs stay where they are, and so do
conflicts: a path that is taken, or that several files would get. These are mostly
duplicates, so clean up duplicates and album folders first. Names that differ only in
letter case are left alone. The web UI's Layout page does the same, and lets you choose
a single album.

### Album folders

The same album often sits in several folders ("Texas/Greatest Hits" and
"Texas/The Greatest Hits", or a fragment in another artist's folder). `aorg merge-albums`
finds pairs of album folders that share identical tracks and have the same album name
(or share at least half of their tracks):

    aorg merge-albums                         # candidates
    aorg merge-albums --decide --limit 20     # choose a batch of safe merges
    aorg pending --write --path Texas         # review, then trash and move (undoable)
    aorg merge-albums --from "Texas/Greatest Hits" --into "Texas/The Greatest Hits"

Merging one folder into another trashes its copies of tracks the other already has (that
copy is kept and playlists switch to it) and moves its other tracks over. A track whose
number the other folder already uses on the same disc is a *clash*, which usually means
a different recording or edition; it stays where it is. Batches only include pairs with
the same album name and no clashes; merge the others one by one in the direction you
choose, here or on the web UI's Album folders page. Choosing only changes the catalog
(`aorg history` undoes it); the trashing and moving are written from the Pending page. To
go the other way after choosing a merge, undo it and choose the other direction.

When no track stays behind, the folder's other files move too (the cover, a `.cue` or
`.nfo`, a booklet, a Scans folder), so the merged folder disappears. Where the other folder
already has a file of that name, an identical copy and system files such as `.DS_Store`
are trashed. Of two different images, the bigger one keeps the name and the other one is
kept beside it with its folder's name added: `cover (Greatest Hits).jpg`, or
`cover (Texas - Greatest Hits).jpg` when both folders have the same name. Any other file,
such as a different `.cue`, gets the merged folder's name added in the same way. These
files are dealt with when the folder's last track is written; each is a change of its own
in the changeset that is written.

### Loose tracks

Flat folders ("Williams, John/John Williams_Cantina Band.mp3") often hold tracks whose
album folder exists, with a gap where they belong. `aorg neighbours` puts them back:

    aorg neighbours                           # what would move
    aorg neighbours --path "Williams, John" --decide
    aorg neighbours --decide --limit 100      # whole albums, up to 100 tracks
    aorg pending --path "Williams, John" --write   # review, then move them (undoable)

A loose track (in an artist folder or "Unknown Album") joins an album folder when its
album tag names exactly one album folder of its artist (ignoring case, punctuation and a
leading "The"), and its track number and title are not there yet. It is named like the
album's other tracks ("07 Title.mp3", or "1-07 Title.mp3" on multi-disc albums), and
gets the album tags those all agree on: album name, album artist, year, track and disc
totals, and the genre if it has none. Two loose tracks claiming the same number are left
alone, and so are tracks with no album tag or track number. Clean up duplicates and
encodings first: loose copies of tracks an album already has are theirs. The web UI's
Loose tracks page does the same, album by album.

### Duplicates

Files with identical audio are copies of one track, even when their tags and names
differ. `aorg dupes` finds them and chooses, in batches, to trash the loose copies:

    aorg dupes                                # what would be chosen
    aorg dupes --path "Texas" --decide        # choose the copies under Texas/
    aorg dupes --decide --limit 100           # or the next 100 tracks anywhere
    aorg pending --path "Texas" --write       # review and trash them (undoable)

Choosing only changes the catalog (`aorg history` undoes it); the trashing is written
from the Pending page, with other pending changes.

When exactly one album folder (`Artist/Album/…`) holds a copy, that copy is kept. Copies
lying loose in an artist folder, in "Unknown Album", or next to it as iTunes copies
("Title 1.mp3") are chosen for the trash. Tags only the loose copies have are first
copied to the kept file, and static playlists switch to it. Tracks with copies in
several album folders are left for you to decide, and so are tracks whose only album
copy is on a compilation while another copy is in the artist's own folder (add
`--include-compilations` to include them). The web UI's Duplicates page does the same,
and there you can choose another copy to keep for any track, and listen to the copies.

A file is never trashed as a copy of itself: if two catalog entries open the same file
(letter case on a case-insensitive share, see [Scanning](#scanning)), writing refuses and
asks for a rescan. A hard link to the kept file can be trashed, as the kept one stays.

### Tracks in several encodings

A track ripped or converted twice is the same recording, but its audio differs, so it is
not a duplicate: an album as both MP3 and AAC, an iTunes copy at another bitrate, a loose
MP3 of an album's track. `aorg encodings` finds them and chooses to keep the best
encoding (only the catalog changes, and `aorg history` undoes it; the files are written
from the Pending page):

    aorg encodings                           # what would be chosen
    aorg encodings --path "Yello" --decide
    aorg encodings --decide --limit 100
    aorg pending --path Yello --write        # review, then trash and move (undoable)

Files count as one recording when they are in one album folder with the same title (and
track number), or when a loose file has the artist and title of a track in exactly one
album folder, and their durations are within two seconds. The lossless or highest-bitrate
file is kept (WMA only if there is nothing else, as many players cannot play it), under
the name of the album's copy; the others go to the trash, after copying tags only they
have (except ReplayGain values, which differ between encodings), and playlists switch to
the kept file. Files with the same title but different durations, such as a live take or
an edit, are only listed, for you to listen to.

When a loose file is better than the album's copy (say, 320 kbps against 160), it would
replace the album's copy, taking its name and album tags. Those are proposed only with
`--replace`, after you have listened to them. Loose files whose album copy is only on a
compilation are left alone unless you add `--include-compilations`, as with duplicates.
The web UI's Encodings page does the same, and there you can choose which file to keep.

Earlier versions of `aorg` read the length of raw AAC (`.aac`) files wrongly, by up to
minutes; if your catalog was made with one, run `aorg scan --reread` once first.

### Leftover files

Merging albums and trashing duplicates empties folders, but Folder.jpg, desktop.ini and
old generated playlists stay behind, and a folder that still holds a file is not
removed. `aorg leftovers` looks at the disk for folders without any music below them
(an album's own Scans or Artwork folder does not count) and lists their files:

    aorg leftovers                            # folders and their files
    aorg leftovers --propose                  # a changeset trashing the litter
    aorg leftovers --path Texas --propose --all   # also files that are not known litter

Only known litter is proposed (folder images, playlists, logs, system files); other
files, such as a PDF booklet, are listed and only proposed with `--all`. A playlist
(.m3u, .html, .json, ...) that names tracks is a playlist, not litter, and is kept, unless
it is dead (see below). A folder goes when its last file is trashed, and undoing the
changeset brings both back.

**Dead playlists.** Old tools left a playlist next to each album, or in the artist's folder,
naming files that have since been merged, renamed or trashed. Such a playlist is proposed
for the trash wherever it lies, also in folders that still hold music, when it names at
least one track and none of them is there (entries are looked for beside the playlist and
under the library root). `.m3u`, `.m3u8`, `.pls`, `.wpl`, and the JSON and HTML playlists
`aorg` and its predecessor wrote are read; so are small files without any extension whose
lines are all audio file names (old tools garbled "Album.m3u" into names such as
"Albu毭3u"). A playlist with even one track still there, one that names a URL or a Windows
path, one without tracks, the files in the library root and in the library's playlist
folder are left alone. Run it after trashing duplicates and merging albums, which make
more playlists dead.

The same run looks at album folders: one without a cover.jpg or cover.png, but with
another image (AlbumArtSmall.jpg, Folder.jpg), gets a proposal to rename the biggest
image to cover.jpg (or cover.png for a PNG), the name sync and the players look for.

### Editing metadata

Edits are stored in the catalog only, where they can be undone (see [Undoing
decisions](#undoing-decisions)). Nothing is written to your files until you write them,
usually an album or an artist at a time, together with everything else waiting for
those files (see [Writing tags](#writing-tags)):

    aorg edit "Texas/Texas_Halo.mp3" --set album="Greatest Hits" --set tracknumber=11
    aorg pending --path "Texas/Texas_Halo.mp3"            # what would change
    aorg pending --path "Texas/Texas_Halo.mp3" --write

`aorg` never writes to a file that has changed since it was last scanned; rescan first.
The tags each file had when it was first scanned are kept in the catalog.

To edit all tracks of an album at once, say to fix a typo in its name, use `aorg
edit-album`. It stores the edit for every track and tells you how many of them now
differ from their tags:

    aorg edit-album --album "Best of Foriegner" --set album="Best of Foreigner" --move-files
    aorg edit-album --album "Greatest Hits" --albumartist Texas --set date=2000
    aorg edit-album --folder "Foreigner/Best of Foreigner" --set genre=Rock

`--album` finds the album by name (ignoring case) in every folder, so a common name such
as "Greatest Hits" needs `--albumartist`; the command lists the folders it found.
`--folder` takes the tracks directly in one folder, not those in its subfolders.

If the folders have the typo too, `--move-files` also chooses moves for the folders: a
folder whose name holds the old album name as whole words ("1992 Best of Foriegner")
gets the new one ("1992 Best of Foreigner"), and a folder named exactly after an artist
or album artist being renamed gets the new name. File names stay as they are, and the
folder's other files (cover, booklet, `.cue`, Scans) go along with its last track. The
moves are chosen like the edit and written with the tags from the Pending page
(`aorg pending --only path` for the moves alone). A
track stays where it is if its new path is taken, or differs only in letter case (the
same folder on macOS and most NAS shares). To move files into your library's layout
instead, use `aorg reorganize` (see above).

In the web UI, an album's or folder's page has an *Edit all tracks* form with the fields
its tracks usually share and a checkbox to rename the folders too; a field left empty or
unchanged keeps each track's own value. Saving it opens the album on the Pending page.
The Albums page has a *Rename album* link on each album, which opens that form.

Besides title, artist, album, track and disc numbers, year and genre, these fields can
be read and edited: original year, album artist and artist sort names, composer,
conductor, grouping, subtitle, language, mood, a 1–5 star rating, the gnudb disc ID and
ReplayGain values. They are written in the usual tags for MP3 (ID3), M4A and FLAC/Ogg,
so that other players read them; moods also go to the standard MOOD tag rather than only
the MusicMatch comment they may have come from.

On a track's page in the web UI:

- **Measure ReplayGain** measures the track's loudness with ffmpeg and stores the track
  gain and peak (and the album's, once all its tracks are measured) in the catalog.
- Values that are not yet in the file are marked, and **Review and write** opens the
  file on the Pending page.
- **Cover** shows the cover embedded in the file and the image in its folder, and can
  embed the folder's image in a file that has none.

### Covers

Phones, car stereos and many players only show covers embedded in the files:

    aorg covers                               # tracks without a cover, and the cover
    aorg covers --path "Björk" --decide       # choose it for those tracks
    aorg covers --min-pixels 500 --decide     # also replace covers under 500 px
    aorg pending --only cover                 # review, then write the chosen covers
    aorg pending --only cover --write

The cover is the folder's image, `cover`, `folder` or `front` (`.jpg`, `.jpeg` or
`.png`), or its only image; when the folder has none at least `--min-pixels` (200 by
default) on a side, it is the cover found online (`aorg coverart`, or `aorg scan --with-covers`). Smaller images, such as
Windows Media Player's `AlbumArtSmall.jpg`, are thumbnails: listed but not embedded,
and an embedded thumbnail is replaced (undo puts it back). The image is copied into
every track, so mind large images. A file whose cover is big enough is never given
another one. The web UI's Covers page does the same, also for single folders, and a
track's page can embed its folder's cover into that one file. Listing folders is slow on a
network share, so the page remembers what each folder holds and lists a folder again only
when it is about to be shown and has changed; choosing covers always checks the disk.
`aorg scan` fills this memory as it walks the library (it sees every folder anyway), so
the page is fast from the first view. *Check the folders again* lists every folder again,
if the counts below the table are stale. Every folder on the page is ticked; untick the
ones you do not want, or use the box in the header to select or deselect all, and
choose covers for the ticked ones.

A cover found online is also saved as the folder's `cover.jpg` (`cover.png` for a PNG),
so that players that read folder images and [sync](#syncing-to-object-storage) have it
too; also for albums whose tracks already have it embedded. That happens only when the
folder holds just that album and has no image big enough. A smaller `cover.jpg` is
replaced (undo puts it back); other images are left alone, as `cover.jpg` comes first.
With `--no-save-images` covers are only embedded.

Catalogs made before `aorg` read the size of embedded covers know only that a file has
one; the next cover lookup reads their sizes (only the tags, not the whole files).

### Ratings from MusicMatch preferences

MusicMatch Jukebox kept a "preference" for tracks (Excellent, Very Good, Good, Fair,
Poor, Bad Taste). They can become 1–5 star ratings that other players read:

    aorg ratings                              # preferences, with the stars they map to
    aorg ratings --set "Good=" --set "Bad Taste=1" --apply
    aorg pending --only rating --artist Air --write

The default mapping is Excellent ★★★★★, Very Good ★★★★, Good ★★★, Fair ★★, Poor and Bad
Taste ★; an empty value gives no rating. A rating already in a file is kept. The web
UI's Ratings page does the same. Applying the mapping is a decision that `aorg history
undo` reverts. Smart playlists can then use rules such as `rating>=4`.

### Writing tags

Edits, measurements, lookups and ratings are kept in the catalog until written into the
files; values only inferred from file and folder names are not written. However many
changes wait for a file (an album renamed, a rating, ReplayGain), it is written once,
with all of them:

    aorg pending                              # the albums with changes waiting
    aorg pending --artist Texas               # what would change in each file
    aorg pending --artist Texas --album "Greatest Hits" --write
    aorg pending --only replaygain --path "Texas" --write
    aorg pending --write --limit 200 --id3-version 3      # the first 200 files
    aorg pending --artist Texas --discard     # drop your edits instead

`--artist` is the album artist, or the artist where there is none; `--album ""` takes
the tracks without an album. `--only` takes field names or their beginnings
(`replaygain` covers all ReplayGain fields), and `--id3-version` writes MP3 tags as
ID3v2.3 or 2.4 (by default each file keeps its own; ID3v2.3 is read by more players
and devices, and both keep all the fields above). What is listed is what is written:
`--write` applies it at once, as a changeset that `aorg changes undo` reverts. Undoing
restores the tags but keeps your edits, so the files are pending again; `--discard`
drops edits (measurements and lookups are kept; `aorg history undo` brings the edits
back, see [Undoing decisions](#undoing-decisions)). Files that a changeset waiting for
review would change are left to it.

The web UI's Pending page lists the albums with changes waiting; choose one (or an
artist) to see the old and new value of every field, untick the files you want to
leave, and write the rest or discard your edits in them.

By default only what the tags lack is written, so a gnudb value that differs from a tag
is left out: the tag is the stronger evidence. To review those, add `--prefer-gnudb`
(on the Pending page: *Tags that differ from gnudb's values*; the *Review what gnudb
found* link above an album's tracks opens it for that album). The listing then shows
each tag and the gnudb value that would replace it. Choose the fields you trust
(`--only genre,album`, or by clicking a field on the page), and the files (`--path` is
an album's folder, say; on the page, untick files), and accept them. Accepted values
become your edits, which are then written like any other:

    aorg pending --prefer-gnudb --path "Texas/Greatest Hits"            # what would change
    aorg pending --prefer-gnudb --only album,genre --path "Texas/Greatest Hits" --accept
    aorg pending --path "Texas/Greatest Hits" --write

Your own edits are never replaced, and neither are tag values that gnudb only spells
differently (case, punctuation, "02" for "2"). Once written, the tag and gnudb agree, and
the track is no longer disputed.

### Undoing decisions

Every edit, album edit, rating mapping applied, gnudb values accepted and edits
discarded is a decision kept in the catalog's history. Undoing one sets its values back,
so its files are pending again (or no longer), and the next write puts the tags right:

    aorg history                              # newest first, with how much each changed
    aorg history show 12                      # its values: file, field, before -> after
    aorg history undo 12

A value that a later decision has changed since is left as it is. An undo is itself a
decision, so it can be undone too. The web UI's History page lists the decisions, each
with its values and an *Undo* button. To undo what was written into files, undo the
changeset instead (`aorg changes undo`, or the Changes page).

### Playlists

*Smart* playlists are rules over the catalog and stay up to date as the library
changes; *static* playlists are lists you curate or import.

    aorg playlists create "90s Rock" --rule "genre=Rock year>=1990 year<2000"
    aorg playlists create "Best Of {mood}" --group-by mood --rule 'rating>=4'
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
default, in a new order each time a playlist is generated. `--sort` also takes
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

Tracks stay in static playlists when files are moved or renamed. When a duplicate copy is
trashed, its entries switch to the copy that is kept, so a playlist that had both now
names that track twice. `aorg playlists dedupe` finds these repeats (the same file, or
files with identical audio, also in an imported playlist that listed a song under two
names) and removes them, keeping the first entry of each track and the order of the rest:

    aorg playlists dedupe                        # list the repeats in every static playlist
    aorg playlists dedupe --name "Party" --apply # remove them from one playlist

Other recordings of a song (a live take, another edition) are left alone, and so are smart
playlists, which hold one copy of each song anyway. Only the catalog changes; a playlist
file already written changes when you write it again. A playlist's page in the web UI
offers the same when it has repeats. To write playlists
into a folder, with tracks referred to by relative paths:

    aorg playlists export ~/Music/Playlists --format m3u,json,html

To write playlists to the library's own disk, or with `--target` to a bucket, in the
place's playlist folder and formats (see Configuration):

    aorg playlists write "Party" --dry-run       # shows each file's full path and what happens
    aorg playlists write --target nas
    aorg playlists delete-files "Party"
    aorg playlists restore-files                 # lists deleted and replaced files
    aorg playlists restore-files 3

A file that someone else has edited is replaced too, but its old content is kept; so is
the content of a deleted file. Deleting a playlist's files leaves the playlist in the
catalog.

`aorg scan` also reads playlists: `.m3u` and `.m3u8` files directly in the library's
playlist folder (the library root by default; not the playlists some tools leave in album
folders) become static playlists. Files that `aorg` wrote itself are recognised and left
alone. A file that someone has edited since becomes a static playlist of its own, so a smart
and a static playlist can share a name; they are then listed as "Best Of Rock (smart)" and
"Best Of Rock (static)", and commands take the name with that suffix. A file whose name
matches a static playlist that differs from it is reported and not imported. A playlist
deleted from the catalog is imported again by the next scan while its file is there. Use
`aorg scan --no-playlists` to leave playlist files alone.

Besides `.m3u`, playlists can be written as schema.org `MusicPlaylist` JSON-LD and as an
HTML page.

### Syncing to object storage

    aorg target reconcile backup      # once, if the bucket already holds your music
    aorg sync backup --dry-run
    aorg sync backup

A sync can also be started from the web UI: open the target on the Sync page, review the
plan and press Start sync. The plan is made in the background (a progress bar shows how
many tracks and album folders have been checked, which takes minutes on a network share)
and Start sync runs that very plan; a plan older than ten minutes, or one made
before a server restart, is made again first. The sync runs in the background of
`aorg web` and its page shows the progress; if the files changed after the plan was
shown, it does not start and asks you to review the plan again. Only one sync of a target runs at a time, whether started from
the web UI or with `aorg sync`, and the web UI shows the progress of either. Stopping a
sync, or stopping the server or the command, loses nothing: each object is recorded as
it is synced, and the next sync carries on from there.

The bucket looks like this (below the target's prefix, if any):

    Artist/Album/01 Title.mp3        tracks, at their library paths
    Artist/Album/cover.jpg           the album's cover image
    playlists/Best Of Upbeat.m3u     playlists as .m3u, .json and .html

Tracks carry these object metadata (`x-amz-meta-*`), all optional, lowercase,
percent-encoded UTF-8, and left out when empty:

| Key | Value |
| --- | --- |
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

All playlists of the library are synced too, as files in the target's playlist folder
(the prefix itself by default; see `aorg target set`), referring to the tracks by relative
paths (`Artist/Album/01 Title.mp3`, or `../Artist/...` in a subfolder).

Only new and changed files are uploaded. Metadata changes and moved files are handled
with server-side copies, without uploading the audio again. Objects that are no longer
in the library, such as the files of a deleted playlist, are kept unless you add
`--delete`.

`reconcile` matches what is already in the bucket against your library, so music
uploaded by other tools is not uploaded again. It changes nothing in the bucket.

### Trash

Trashed files are moved to `.aorg-trash/` in the library folder, and undoing the
changeset brings them back. They are only deleted when you empty the trash:

    aorg trash                                # what is in it
    aorg trash empty

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
- **No file changes without review**: edits only change the catalog, as operations that
  record each value's before and after so they can be undone; writing them shows the
  diff of a selection first. Every retag or move is applied as a changeset that records
  how to undo it.
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
| `provenance.py` | Resolving field values from their sources; the effective metadata and pending field tables |
| `audiohash.py` | Audio-only content hash (sampled) |
| `blockfile.py` | Block-cached reading of a file, for few network round trips |
| `changes.py` | Changesets (each records the feature that proposed it): propose, approve, apply, undo; retag, move and trash |
| `duplicates.py` | Identical copies and what to keep |
| `encodings.py` | One track in several encodings, and which to keep |
| `album_merge.py` | Album folders holding the same album, and merging them |
| `neighbours.py` | Loose tracks that belong in an album folder, and moving them there |
| `leftovers.py` | Litter in folders without music and dead playlists anywhere, and trashing them |
| `layout.py` | The folder layout template, and choosing the moves into it |
| `artists.py` | Artists' sort names and suggestions for them |
| `loudness.py` | ReplayGain 2.0: measuring tracks with ffmpeg, album gains (`scan --deep`) |
| `covers.py` | Embedded cover images: reading, embedding, replacing thumbnails; which tracks get which cover |
| `gnudb.py` | Looking up album folders in gnudb (cddbp, pseudo-TOC from durations), `aorg gnudb`, `scan --with-gnudb` |
| `coverart.py` | Looking up album covers online (MusicBrainz, Cover Art Archive), `aorg coverart`, `scan --with-covers` |
| `images.py` | Size and type of JPEG and PNG images from their headers |
| `operations.py` | The decision log: operations that set field values, recording before and after; undo |
| `writing.py` | Pending changes (values not yet in the files) by artist and album; storing edits, accepting lookups, writing a selection |
| `album_edit.py` | Editing all tracks of an album or folder, and renaming its folders |
| `ratings.py` | Star ratings from MusicMatch preferences |
| `tracks.py` | Streaming tracks with resolved metadata, in batches |
| `playlists.py` | Playlist rules, generation in one pass, import and export |
| `playlist_formats.py` | M3U, JSON-LD and HTML playlist files |
| `sync.py` | Planning and carrying out syncs to object storage |
| `sync_jobs.py` | Sync runs as jobs: progress, one sync per target, background runs for the web UI |
| `objectstore.py` | Object storage backends (S3-compatible) |
| `cli.py` | The `aorg` command |
| `web/` | The web UI: FastAPI app (`app.py`), Jinja2 templates, stylesheet |
