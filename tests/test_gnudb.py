import socketserver
import threading

import pytest
from sqlalchemy import select
from typer.testing import CliRunner

from audio_organizer import coverart, gnudb, provenance, tags
from audio_organizer.cli import app
from audio_organizer.models import AudioFile, FieldValue, GnudbLookup, Library
from audio_organizer.scanner import scan

XMCD = """# xmcd
DISCID=940aac0b
DTITLE=Texas / Greatest Hits
DYEAR=2000
DGENRE=Pop
TTITLE0=Halo
TTITLE1=Insane
EXTD=
PLAYORDER=
"""


class Handler(socketserver.StreamRequestHandler):
    """A cddbp server that knows one album."""

    def send(self, text):
        self.wfile.write(text.encode() + b"\r\n")

    def handle(self):
        server = self.server
        self.send("201 gnudb CDDBP server ready")
        for raw in self.rfile:
            line = raw.decode().strip()
            server.commands.append(line)
            if line.startswith("cddb hello"):
                if server.refuse:
                    self.send("500 Unknown application, developer email for audio-organizer")
                    return
                self.send("200 Hello and welcome")
            elif line.startswith("proto"):
                self.send("201 OK, CDDB protocol level now: 6")
            elif line.startswith("cddb query"):
                if server.known:
                    self.send("211 Found inexact matches, list follows (until terminating `.')")
                    self.send("rock 11111111 Texas / Greatest Hits (Live)")
                    self.send("pop 940aac0b Texas / Greatest Hits")
                    self.send(".")
                else:
                    self.send("202 No match found")
            elif line.startswith("cddb read pop 940aac0b"):
                self.send("210 pop 940aac0b CD database entry follows (until terminating `.')")
                self.wfile.write(XMCD.replace("\n", "\r\n").encode() + b".\r\n")
            elif line.startswith("cddb read"):
                self.send("401 Specified CDDB entry not found.")
            elif line == "quit":
                return
            else:
                self.send("500 Command syntax error")


@pytest.fixture
def server():
    class Server(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True

    with Server(("127.0.0.1", 0), Handler) as srv:
        srv.commands, srv.known, srv.refuse = [], True, False
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        yield srv
        srv.shutdown()


@pytest.fixture
def client(server):
    with gnudb.Client(
        "127.0.0.1", server.server_address[1], pause=0, contact="me@example.org"
    ) as c:
        yield c


def test_disc_id_of_a_known_disc():
    # start seconds 2, 195, 385: digit sums 2 + 15 + 16 = 33; playing time 400 - 2 s
    assert gnudb.disc_id([150, 14675, 28912], 400) == f"{33 << 24 | 398 << 8 | 3:08x}"


def test_pseudo_toc_lays_the_tracks_end_to_end():
    offsets, seconds = gnudb.pseudo_toc([60.0, 120.5])
    assert offsets == [150, 150 + 4500]
    assert seconds == (150 + 4500 + round(120.5 * 75)) // 75


def test_parse_entry_joins_long_titles_and_splits_compilations():
    entry = gnudb.parse_entry(
        "misc",
        "1",
        ["DTITLE=Various / Hits", "TTITLE0=A / One", "TTITLE0= more", "TTITLE1=B / Two"],
    )
    assert entry.artists == ["A", "B"] and entry.titles == ["One more", "Two"]
    assert gnudb.split_title("Solo") == ("Solo", "Solo")


def test_query_and_read(client, server):
    candidates = client.query([150, 5000], 80)
    assert [c.discid for c in candidates] == ["11111111", "940aac0b"]
    entry = client.read(candidates[1])
    assert (entry.artist, entry.album, entry.year, entry.titles) == (
        "Texas", "Greatest Hits", "2000", ["Halo", "Insane"]
    )  # fmt: skip
    assert server.commands[0] == "cddb hello me example.org audio-organizer " + gnudb.__version__
    assert server.commands[1] == "proto 6"


def test_refused_application_stops(server):
    server.refuse = True
    with gnudb.Client("127.0.0.1", server.server_address[1], pause=0, contact="a@b.c") as c:
        with pytest.raises(gnudb.GnudbRefused, match="Unknown application"):
            c.query([150], 10)


def test_refused_connection_stops():
    with gnudb.Client("127.0.0.1", 1, pause=0, contact="a@b.c") as c:  # nothing listens there
        with pytest.raises(gnudb.GnudbRefused, match="blocked"):
            c.query([150], 10)


def test_email_is_needed(monkeypatch):
    monkeypatch.delenv("AORG_GNUDB_EMAIL", raising=False)
    monkeypatch.delenv("AORG_CONTACT", raising=False)
    with pytest.raises(gnudb.GnudbRefused, match="AORG_GNUDB_EMAIL"):
        gnudb.email()


@pytest.fixture
def library(Session, copy_audio, tmp_path):
    root = tmp_path / "music"
    first = copy_audio("mp3", root / "Texas/Greatest Hits/01 Halo.mp3")
    second = copy_audio("mp3b", root / "Texas/Greatest Hits/02 Insane.mp3")
    tags.write(first, {"album": "Greatest Hits", "tracknumber": "1", "date": "2000-03-02"})
    tags.write(second, {"album": "Greatest Hits", "tracknumber": "2"})
    session = Session()
    lib = Library(name="music", root=str(root))
    session.add(lib)
    session.commit()
    scan(session, lib)
    yield session, lib
    session.close()


def _values(session, field):
    return {
        row.path: row.value
        for row in session.execute(
            select(AudioFile.path, FieldValue.value)
            .join(FieldValue)
            .where(FieldValue.source == "gnudb", FieldValue.field == field)
        )
    }


def test_lookup_stores_gnudb_values_once(library, client, server):
    session, lib = library
    stats = gnudb.lookup_library(session, lib, client=client)
    assert (stats.albums, stats.found, stats.not_found) == (1, 1, 0), (stats.errors, stats.stopped)
    assert _values(session, "discid") == {
        "Texas/Greatest Hits/01 Halo.mp3": "940aac0b",
        "Texas/Greatest Hits/02 Insane.mp3": "940aac0b",
    }
    assert _values(session, "genre")["Texas/Greatest Hits/02 Insane.mp3"] == "Pop"
    # the first file's full date is not replaced by the year; the second has none
    assert set(_values(session, "date")) == {"Texas/Greatest Hits/02 Insane.mp3"}
    [lookup] = session.scalars(select(GnudbLookup))
    assert (lookup.status, lookup.discid, lookup.tracks) == ("found", "940aac0b", 2)

    queries = len([c for c in server.commands if c.startswith("cddb query")])
    again = gnudb.lookup_library(session, lib, client=client)
    assert again.albums == 0
    assert len([c for c in server.commands if c.startswith("cddb query")]) == queries


def test_no_match_is_remembered(library, client, server):
    session, lib = library
    server.known = False
    stats = gnudb.lookup_library(session, lib, client=client)
    assert (stats.found, stats.not_found) == (0, 1)
    assert gnudb.lookup_library(session, lib, client=client).albums == 0


def test_entry_with_other_titles_is_not_accepted(library, client):
    session, lib = library
    for path in ("01 Halo", "02 Insane"):
        file = session.scalar(select(AudioFile).where(AudioFile.path.contains(path)))
        provenance.set_value(session, file, "manual", "title", "Something else")
    stats = gnudb.lookup_library(session, lib, client=client)
    assert (stats.found, stats.not_found) == (0, 1)
    assert not _values(session, "album")


def test_refusal_stops_the_lookups(library, server):
    session, lib = library
    server.refuse = True
    client = gnudb.Client("127.0.0.1", server.server_address[1], pause=0, contact="a@b.c")
    stats = gnudb.lookup_library(session, lib, client=client)
    client.close()
    assert "Unknown application" in (stats.stopped or "")
    assert session.scalar(select(GnudbLookup)) is None


def test_incomplete_albums_are_not_looked_up(library):
    session, lib = library
    file = session.scalar(select(AudioFile).where(AudioFile.path.contains("02 Insane")))
    provenance.set_value(session, file, "manual", "tracknumber", "3")
    assert gnudb.folders(session, lib) == []


def test_cli_scan_deep_names_gnudb():
    result = CliRunner().invoke(app, ["scan", "--help"], terminal_width=200)
    assert "gnudb" in result.output


def test_cli_gnudb_needs_an_email(library, monkeypatch):
    session, lib = library
    monkeypatch.delenv("AORG_GNUDB_EMAIL", raising=False)
    monkeypatch.delenv("AORG_CONTACT", raising=False)
    result = CliRunner().invoke(app, ["--db", str(session.get_bind().url), "gnudb"])
    assert result.exit_code != 0
    assert "AORG_GNUDB_EMAIL" in result.output


def test_cli_gnudb_limit_and_no_gnudb(library, monkeypatch):
    session, lib = library
    db = str(session.get_bind().url)
    monkeypatch.setenv("AORG_GNUDB_EMAIL", "me@example.org")
    asked = []

    def fake_lookup(session, library, *, limit=None, **kwargs):
        asked.append(limit)
        return gnudb.LookupStats(albums=3, found=1, not_found=1)

    monkeypatch.setattr(gnudb, "lookup_library", fake_lookup)
    monkeypatch.setattr("audio_organizer.loudness.measure_library", _no_measuring)
    monkeypatch.setattr(
        "audio_organizer.coverart.lookup_library", lambda *a, **k: coverart.LookupStats()
    )

    result = CliRunner().invoke(app, ["--db", db, "gnudb", "--limit", "7"])
    assert result.exit_code == 0, result.output
    assert "gnudb: 1 found, 1 not found" in result.output and asked == [7]

    result = CliRunner().invoke(app, ["--db", db, "scan", "--deep", "--no-gnudb"])
    assert "gnudb" not in result.output and asked == [7]
    CliRunner().invoke(app, ["--db", db, "scan", "--deep", "--deep-limit", "2"])
    assert asked == [7, 2]

    monkeypatch.delenv("AORG_GNUDB_EMAIL")
    monkeypatch.delenv("AORG_CONTACT", raising=False)
    result = CliRunner().invoke(app, ["--db", db, "scan", "--deep"])
    assert "Skipping gnudb" in result.output and asked == [7, 2]


def _no_measuring(session, lib, **kwargs):
    from audio_organizer.loudness import DeepScanStats

    return DeepScanStats()
