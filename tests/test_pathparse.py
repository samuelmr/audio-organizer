import pytest

from audio_organizer.pathparse import normalize_name, parse, unflip


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (
            "Darude/Before The Storm/05 Touch Me Feel Me.mp3",
            {"artist": "Darude", "albumartist": "Darude", "album": "Before The Storm",
             "tracknumber": "5", "title": "Touch Me Feel Me"},
        ),
        (
            "Compilations/Kaikkien aikojen paras lastenlevy CD2/2-14 Hei Muumit.mp3",
            {"albumartist": "Various Artists", "compilation": "1",
             "album": "Kaikkien aikojen paras lastenlevy", "discnumber": "2", "tracknumber": "14",
             "title": "Hei Muumit"},
        ),
        (
            "Compilations/Best Of Christmas Songs [Disc 1]/05 Love On Layaway.mp3",
            {"albumartist": "Various Artists", "compilation": "1",
             "album": "Best Of Christmas Songs", "discnumber": "1", "tracknumber": "5",
             "title": "Love On Layaway"},
        ),
        (
            "Frank Sinatra/A Fine Romance - The Love Songs of Frank - Disk 1/"
            "12 Love's Been Good To Me.mp3",
            {"artist": "Frank Sinatra", "albumartist": "Frank Sinatra",
             "album": "A Fine Romance - The Love Songs of Frank", "discnumber": "1",
             "tracknumber": "12", "title": "Love's Been Good To Me"},
        ),
        (
            "Kitaro/Ten Years CD2/06 - God Of Thunder.mp3",
            {"artist": "Kitaro", "albumartist": "Kitaro", "album": "Ten Years", "discnumber": "2",
             "tracknumber": "6", "title": "God Of Thunder"},
        ),
        (
            "The Cardigans/Long Gone Before Daylight/04 The Cardigans - And Then You Kissed Me.mp3",
            {"artist": "The Cardigans", "albumartist": "The Cardigans",
             "album": "Long Gone Before Daylight", "tracknumber": "4",
             "title": "And Then You Kissed Me"},
        ),
        (
            "Elfman, Danny/Danny Elfman_End Title.mp3",
            {"artist": "Danny Elfman", "title": "End Title"},
        ),
        (
            "Kill Bill Vol.2/Malcolm Mclaren_About Her.mp3",
            {"artist": "Malcolm Mclaren", "title": "About Her"},
        ),
        (
            "Texas/Texas_In Our Lifetime_0702163216.mp3",
            {"artist": "Texas", "title": "In Our Lifetime", "copy_suffix": "0702163216"},
        ),
        (
            "Red Hot Chili Peppers/Californication/03 Scar Tissue 1.mp3",
            {"artist": "Red Hot Chili Peppers", "albumartist": "Red Hot Chili Peppers",
             "album": "Californication", "tracknumber": "3", "title": "Scar Tissue 1",
             "copy_suffix": "1"},
        ),
        (
            "Unknown Artist/Unknown Album/12 Track 12 2.mp3",
            {"tracknumber": "12", "copy_suffix": "2"},
        ),
        (
            "Kontra/Unknown Album/32 Jerry Cotton.mp3",
            {"artist": "Kontra", "albumartist": "Kontra", "tracknumber": "32",
             "title": "Jerry Cotton"},
        ),
        ("Topley, Martina/Martina Topley Bird Baby Blue.mp3", {"artist": "Martina Topley"}),
        ("Eläinlaulut2/21 Audio Track.aiff", {"artist": "Eläinlaulut2", "tracknumber": "21"}),
        (
            "Limp Bizkit/Unknown Album/My Way (dj Lethal Remix).mp3",
            {"artist": "Limp Bizkit", "albumartist": "Limp Bizkit",
             "title": "My Way (dj Lethal Remix)"},
        ),
    ],
)  # fmt: skip
def test_parse(path, expected):
    assert parse(path) == expected


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Adams, Bryan", "Bryan Adams"),
        ("Topley, Martina", "Martina Topley"),
        ("Earth, Wind & Fire", "Earth, Wind & Fire"),
        ("Crosby, Stills, Nash", "Crosby, Stills, Nash"),
        ("Peter, Paul and Mary", "Peter, Paul and Mary"),
        ("Madonna", "Madonna"),
    ],
)
def test_unflip(name, expected):
    assert unflip(name) == expected


def test_normalize_name():
    assert normalize_name("The Cardigans") == normalize_name("cardigans")
    assert normalize_name("Pepe Deluxé") == normalize_name("pepe deluxe")
    assert normalize_name("Guns'n'Roses") == normalize_name("Gunsn Roses".replace(" ", ""))
