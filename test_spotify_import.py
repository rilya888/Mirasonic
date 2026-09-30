"""Tests for the Spotify playlist import.

The numbers asserted here come from a live run on 2026-08-27 over 173 real
tracks: correct matches scored >= 8.0, and both mistakes scored 6.0.
"""
import asyncio

import pytest

import library
import spotify_import

HEADER = "Track URI,Track Name,Album Name,Artist Name(s),Release Date,Duration (ms)\n"


def write_csv(tmp_path, name, rows):
    path = tmp_path / name
    path.write_text(HEADER + "".join(rows), encoding="utf-8")
    return str(path)


def row(uri, title, album, artists, ms):
    return f'{uri},"{title}","{album}","{artists}",2024-01-01,{ms}\n'


def candidate(video_id, title, artist, duration, album=None, artwork=None):
    return {"id": video_id, "title": title, "artist": artist,
            "durationSeconds": duration, "album": album, "artworkURL": artwork}


@pytest.fixture
def lib(tmp_path):
    return library.Library(str(tmp_path / "test.db"))


def fake_search(pages):
    """pages: {query: [candidates]}. Records what was actually asked."""
    asked = []

    async def _search(q="", limit=20, continuation=""):
        asked.append(q)
        return {"tracks": pages.get(q, []), "continuation": None}

    return _search, asked


# ---------------------------------------------------------------------------
# candidate scoring
# ---------------------------------------------------------------------------

def test_score_accepts_the_same_song_under_a_localised_title():
    """JISOO — FLOWER is titled 꽃(FLOWER) in YouTube Music, and it is the same track."""
    value = spotify_import.score(
        "FLOWER", ["JISOO"], 176, candidate("v1", "꽃(FLOWER)", "JISOO", 177))
    assert value >= spotify_import.ACCEPT_SCORE


def test_score_rejects_another_artist_with_the_same_title():
    """A real mistake: "HWASA — Maria" matched "Pianella Piano — Maria" of the same length."""
    value = spotify_import.score(
        "Maria", ["HWASA"], 189, candidate("v2", "Maria", "Pianella Piano", 189))
    assert value < spotify_import.ACCEPT_SCORE


def test_score_rejects_another_song_of_the_same_artist():
    """The second real mistake: "MEOVV — DDI RO RI" matched "MEOVV — DROP TOP"."""
    value = spotify_import.score(
        "DDI RO RI", ["MEOVV"], 175, candidate("v3", "DROP TOP", "MEOVV", 175))
    assert value < spotify_import.ACCEPT_SCORE


def test_score_rejects_a_live_take_of_the_right_song():
    """Duration is the main signal: same title, different recording."""
    value = spotify_import.score(
        "Что сделал ты для своей мечты?", ["Элизиум"], 143,
        candidate("v4", "Что сделал ты для своей мечты?", "Элизиум", 204))
    assert value < spotify_import.ACCEPT_SCORE


# ---------------------------------------------------------------------------
# CSV parsing
# ---------------------------------------------------------------------------

def test_read_csv_skips_local_files(tmp_path):
    path = write_csv(tmp_path, "p.csv", [
        row("spotify:track:aaa", "Song", "Album", "Artist", 200000),
        row("spotify:local:x:y:z", "Local", "", "Someone", 100000),
    ])
    tracks = spotify_import.read_csv(path)
    assert [t["uri"] for t in tracks] == ["spotify:track:aaa"]
    assert tracks[0]["seconds"] == 200


def test_read_csv_rejects_a_file_that_is_not_an_exportify_dump(tmp_path):
    path = tmp_path / "junk.csv"
    path.write_text("a,b,c\n1,2,3\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Exportify"):
        spotify_import.read_csv(str(path))


def test_read_csv_splits_several_artists(tmp_path):
    path = write_csv(tmp_path, "p.csv", [
        row("spotify:track:aaa", "SPOT!", "SPOT!", "ZICO, JENNIE", 180000)])
    assert spotify_import.read_csv(path)[0]["artists"] == ["ZICO", "JENNIE"]


# ---------------------------------------------------------------------------
# import
# ---------------------------------------------------------------------------

def test_import_creates_the_playlist_and_keeps_the_file_order(lib, tmp_path, monkeypatch):
    path = write_csv(tmp_path, "Upward.csv", [
        row("spotify:track:a", "First", "Alb", "Artist", 200000),
        row("spotify:track:b", "Second", "Alb", "Artist", 300000),
    ])
    search, _ = fake_search({
        "Artist First": [candidate("vA", "First", "Artist", 200)],
        "Artist Second": [candidate("vB", "Second", "Artist", 300)],
    })
    monkeypatch.setattr(spotify_import.main, "search", search)

    report = asyncio.run(spotify_import.import_file(lib, path))

    assert report["playlist"] == "Upward"
    assert (report["matched"], report["added"]) == (2, 2)
    playlist = lib.get_playlist(report["playlist_id"])
    assert [s["id"] for s in playlist["songs"]] == ["vA", "vB"]
    assert playlist["songs"][0]["duration"] == 200


def test_import_puts_a_doubtful_match_in_the_report_not_in_the_playlist(lib, tmp_path, monkeypatch):
    path = write_csv(tmp_path, "K-pop.csv", [
        row("spotify:track:a", "Maria", "Maria", "HWASA", 189000)])
    search, _ = fake_search({
        "HWASA Maria": [candidate("vX", "Maria", "Pianella Piano", 189)]})
    monkeypatch.setattr(spotify_import.main, "search", search)

    report = asyncio.run(spotify_import.import_file(lib, path))

    assert report["added"] == 0
    assert len(report["unmatched"]) == 1
    assert "Pianella Piano" in report["unmatched"][0]["closest"]
    assert lib.get_playlist(report["playlist_id"])["songs"] == []
    assert lib.get_spotify_map() == {}  # a wrong match is not remembered


def test_second_import_of_the_same_file_searches_nothing_and_adds_nothing(lib, tmp_path, monkeypatch):
    path = write_csv(tmp_path, "K-pop.csv", [
        row("spotify:track:a", "First", "Alb", "Artist", 200000)])
    search, asked = fake_search({"Artist First": [candidate("vA", "First", "Artist", 200)]})
    monkeypatch.setattr(spotify_import.main, "search", search)

    asyncio.run(spotify_import.import_file(lib, path))
    assert len(asked) == 1

    report = asyncio.run(spotify_import.import_file(lib, path))
    assert len(asked) == 1  # the mapping is already known — nothing to search for
    assert (report["from_map"], report["added"], report["already_in_playlist"]) == (1, 0, 1)
    assert len(lib.get_playlist(report["playlist_id"])["songs"]) == 1


def test_monthly_reimport_appends_only_what_is_new(lib, tmp_path, monkeypatch):
    """A month later the playlist grew — the old entries stay where they were."""
    first = write_csv(tmp_path, "K-pop.csv", [
        row("spotify:track:a", "First", "Alb", "Artist", 200000)])
    search, _ = fake_search({
        "Artist First": [candidate("vA", "First", "Artist", 200)],
        "Artist Second": [candidate("vB", "Second", "Artist", 300)],
    })
    monkeypatch.setattr(spotify_import.main, "search", search)
    asyncio.run(spotify_import.import_file(lib, first))

    second = write_csv(tmp_path, "K-pop.csv", [
        row("spotify:track:a", "First", "Alb", "Artist", 200000),
        row("spotify:track:b", "Second", "Alb", "Artist", 300000),
    ])
    report = asyncio.run(spotify_import.import_file(lib, second))

    assert (report["from_map"], report["added"]) == (1, 1)
    assert [s["id"] for s in lib.get_playlist(report["playlist_id"])["songs"]] == ["vA", "vB"]


def test_import_does_not_lose_a_track_the_owner_added_by_hand(lib, tmp_path, monkeypatch):
    """A repeat import appends rather than rebuilds: other entries survive."""
    path = write_csv(tmp_path, "K-pop.csv", [
        row("spotify:track:a", "First", "Alb", "Artist", 200000)])
    search, _ = fake_search({"Artist First": [candidate("vA", "First", "Artist", 200)]})
    monkeypatch.setattr(spotify_import.main, "search", search)
    report = asyncio.run(spotify_import.import_file(lib, path))

    playlist_id = report["playlist_id"]
    lib.update_playlist(playlist_id, "K-pop", [], [("manual", "By hand", "Someone", None, 100, None)])

    asyncio.run(spotify_import.import_file(lib, path))
    assert [s["id"] for s in lib.get_playlist(playlist_id)["songs"]] == ["vA", "manual"]


def test_import_retries_without_the_version_tail(lib, tmp_path, monkeypatch):
    """"- Stadium Live" exists in Spotify and not in YouTube Music."""
    path = write_csv(tmp_path, "Upward.csv", [
        row("spotify:track:a", "Мечта - Stadium Live", "Alb", "Элизиум", 143000)])
    search, asked = fake_search({
        "Элизиум Мечта - Stadium Live": [],
        "Элизиум Мечта": [candidate("vA", "Мечта", "Элизиум", 143)],
    })
    monkeypatch.setattr(spotify_import.main, "search", search)

    report = asyncio.run(spotify_import.import_file(lib, path))

    assert asked == ["Элизиум Мечта - Stadium Live", "Элизиум Мечта"]
    assert report["added"] == 1


def test_import_asks_the_other_way_round_before_giving_up(lib, tmp_path, monkeypatch):
    """Search results are not deterministic — a different phrasing returns a
    different page.

    A real case from 2026-08-27: "aespa Armageddon" returned a top twenty
    without the track, though a minute later it stood first in it.
    """
    path = write_csv(tmp_path, "K-pop.csv", [
        row("spotify:track:a", "Armageddon", "Alb", "aespa", 196000)])
    search, asked = fake_search({
        "aespa Armageddon": [candidate("vWrong", "Spicy", "aespa", 196)],
        "Armageddon aespa": [candidate("vRight", "Armageddon", "aespa", 197)],
    })
    monkeypatch.setattr(spotify_import.main, "search", search)

    report = asyncio.run(spotify_import.import_file(lib, path))

    assert asked == ["aespa Armageddon", "Armageddon aespa"]
    assert [s["id"] for s in lib.get_playlist(report["playlist_id"])["songs"]] == ["vRight"]


def test_dry_run_writes_nothing(lib, tmp_path, monkeypatch):
    path = write_csv(tmp_path, "K-pop.csv", [
        row("spotify:track:a", "First", "Alb", "Artist", 200000)])
    search, _ = fake_search({"Artist First": [candidate("vA", "First", "Artist", 200)]})
    monkeypatch.setattr(spotify_import.main, "search", search)

    report = asyncio.run(spotify_import.import_file(lib, path, dry_run=True))

    assert report["added"] == 1
    assert lib.get_playlists() == []
    assert lib.get_spotify_map() == {}


def test_upstream_failure_does_not_import_garbage(lib, tmp_path, monkeypatch):
    """main.search returns a JSONResponse when upstream fails, not a dict."""
    path = write_csv(tmp_path, "K-pop.csv", [
        row("spotify:track:a", "First", "Alb", "Artist", 200000)])

    async def failing(q="", limit=20, continuation=""):
        return spotify_import.main.JSONResponse({"error": "upstream"}, status_code=502)

    monkeypatch.setattr(spotify_import.main, "search", failing)
    report = asyncio.run(spotify_import.import_file(lib, path))

    assert report["added"] == 0
    assert report["unmatched"][0]["closest"] is None


def test_manual_mapping_lets_the_next_import_pick_the_track_up(lib, tmp_path, monkeypatch):
    """An artist name in a different alphabet: «Уматурман» vs «Uma2rman».

    The automation deliberately refuses this — a different song sharing the
    title scores exactly the same — so the mapping is set by hand.
    """
    path = write_csv(tmp_path, "Mix.csv", [
        row("spotify:track:u", "Кажется", "Alb", "Уматурман", 239000)])
    search, _ = fake_search({})  # search finds nothing
    monkeypatch.setattr(spotify_import.main, "search", search)

    async def details(video_id):
        assert video_id == "vUma"
        return {"title": "Кажется", "artist": "Uma2rman", "duration": 240,
                "artwork": "https://example.invalid/uma.jpg"}

    monkeypatch.setattr(spotify_import.main, "get_song_details", details)

    assert asyncio.run(spotify_import.import_file(lib, path))["added"] == 0

    asyncio.run(spotify_import.add_mapping(lib, [("spotify:track:u", "vUma")]))
    report = asyncio.run(spotify_import.import_file(lib, path))

    assert (report["from_map"], report["added"]) == (1, 1)
    song = lib.get_playlist(report["playlist_id"])["songs"][0]
    assert (song["id"], song["artist"], song["duration"]) == ("vUma", "Uma2rman", 240)
    # The first four hand-mapped tracks ended up without artwork precisely
    # because add_mapping did not request it.
    assert song["artwork_url"] == "https://example.invalid/uma.jpg"


def test_replace_swaps_the_mix_for_the_new_file(lib, tmp_path, monkeypatch):
    """A Spotify mix changes its tracks rather than growing: the playlist mirrors it."""
    search, asked = fake_search({
        "Artist First": [candidate("vA", "First", "Artist", 200)],
        "Artist Second": [candidate("vB", "Second", "Artist", 300)],
        "Artist Third": [candidate("vC", "Third", "Artist", 250)],
    })
    monkeypatch.setattr(spotify_import.main, "search", search)
    asyncio.run(spotify_import.import_file(lib, write_csv(tmp_path, "Mix.csv", [
        row("spotify:track:a", "First", "Alb", "Artist", 200000),
        row("spotify:track:b", "Second", "Alb", "Artist", 300000),
    ]), replace=True))

    report = asyncio.run(spotify_import.import_file(lib, write_csv(tmp_path, "Mix.csv", [
        row("spotify:track:c", "Third", "Alb", "Artist", 250000),
        row("spotify:track:a", "First", "Alb", "Artist", 200000),
    ]), replace=True))

    assert [s["id"] for s in lib.get_playlist(report["playlist_id"])["songs"]] == ["vC", "vA"]
    assert (report["removed"], report["added"], report["from_map"]) == (2, 2, 1)
    assert asked.count("Artist First") == 1  # a known track is not searched again


def test_replace_does_not_wipe_the_playlist_when_nothing_matched(lib, tmp_path, monkeypatch):
    path = write_csv(tmp_path, "Mix.csv", [
        row("spotify:track:a", "First", "Alb", "Artist", 200000)])
    search, _ = fake_search({"Artist First": [candidate("vA", "First", "Artist", 200)]})
    monkeypatch.setattr(spotify_import.main, "search", search)
    playlist_id = asyncio.run(spotify_import.import_file(lib, path, replace=True))["playlist_id"]

    async def failing(q="", limit=20, continuation=""):
        return spotify_import.main.JSONResponse({"error": "upstream"}, status_code=502)

    monkeypatch.setattr(spotify_import.main, "search", failing)
    asyncio.run(spotify_import.import_file(lib, write_csv(tmp_path, "Mix.csv", [
        row("spotify:track:z", "New", "Alb", "Artist", 100000)]), replace=True))

    assert [s["id"] for s in lib.get_playlist(playlist_id)["songs"]] == ["vA"]


def test_read_csv_accepts_the_2026_exportify_duration_column(tmp_path):
    path = tmp_path / "Mix.csv"
    path.write_text("Track URI,Track Name,Artist Name(s),Track Duration (ms)\n"
                    'spotify:track:a,"First","Artist",200000\n', encoding="utf-8")
    assert spotify_import.read_csv(str(path))[0]["seconds"] == 200


def test_score_accepts_an_artist_spelled_in_latin():
    """Spotify gives `Monetochka`, YouTube Music `Монеточка` — the same person."""
    value = spotify_import.score("Каждый раз", ["Monetochka"], 200,
                                 candidate("v", "Каждый раз", "монеточка", 200))
    assert value >= spotify_import.ACCEPT_SCORE


def test_transliteration_does_not_make_another_artist_match():
    value = spotify_import.score("Maria", ["HWASA"], 189,
                                 candidate("v", "Maria", "Мария Пианелла", 189))
    assert value < spotify_import.ACCEPT_SCORE


def test_read_csv_splits_artists_on_semicolons(tmp_path):
    path = tmp_path / "Mix.csv"
    path.write_text("Track URI,Track Name,Artist Name(s),Duration (ms)\n"
                    'spotify:track:a,"Люди с автоматами","Swanky Tunes;Monetochka;Noize MC",192490\n',
                    encoding="utf-8")
    assert spotify_import.read_csv(str(path))[0]["artists"] == ["Swanky Tunes", "Monetochka", "Noize MC"]
