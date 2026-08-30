import logging
import hashlib
import os

os.environ["SUBSONIC_USER"] = "rilya"
os.environ["SUBSONIC_PASSWORD"] = "s3cr3t"

import pytest
from fastapi.testclient import TestClient

import library
import main
import subsonic

client = TestClient(main.app)

USER = "rilya"
PASSWORD = "s3cr3t"


def token_params(user=USER, password=PASSWORD, salt="abc123salt0000000"):
    t = hashlib.md5((password + salt).encode("utf-8")).hexdigest()
    return {"u": user, "t": t, "s": salt, "v": "1.16.1", "c": "Amperfy"}


def plain_params(user=USER, password=PASSWORD):
    return {"u": user, "p": password, "v": "1.13.0", "c": "Amperfy"}


def enc_params(user=USER, password=PASSWORD):
    return {"u": user, "p": "enc:" + password.encode("utf-8").hex(), "v": "1.13.0", "c": "Amperfy"}


# ---------------------------------------------------------------------------
# auth — token form, plaintext p, enc:<hex> p, rejection
# ---------------------------------------------------------------------------

def test_authenticate_accepts_valid_token():
    assert subsonic._authenticate(token_params())


def test_authenticate_accepts_plain_p():
    assert subsonic._authenticate(plain_params())


def test_authenticate_accepts_enc_hex_p():
    assert subsonic._authenticate(enc_params())


def test_authenticate_rejects_wrong_password_token():
    assert subsonic._authenticate(token_params())  # sanity: real one still ok
    bad = dict(token_params())
    bad["t"] = hashlib.md5(("wrong" + bad["s"]).encode("utf-8")).hexdigest()
    assert not subsonic._authenticate(bad)


def test_authenticate_rejects_wrong_user():
    params = token_params(user="someoneelse")
    assert not subsonic._authenticate(params)


def test_authenticate_rejects_wrong_plain_password():
    assert not subsonic._authenticate(plain_params(password="wrong"))


def test_authenticate_rejects_missing_credentials():
    assert not subsonic._authenticate({"u": USER})


def test_missing_env_credentials_refuse_to_start(monkeypatch):
    monkeypatch.delenv("SUBSONIC_USER", raising=False)
    monkeypatch.delenv("SUBSONIC_PASSWORD", raising=False)
    with pytest.raises(RuntimeError):
        subsonic._get_credentials()


def test_ping_rejects_bad_credentials_via_http():
    resp = client.get("/rest/ping.view", params={"u": USER, "t": "deadbeef", "s": "x"})
    assert resp.json is not None
    assert "<error code=\"40\"" in resp.text


# ---------------------------------------------------------------------------
# HTTP code is always 200, even for protocol errors — the single most
# expensive-to-miss rule in SUBSONIC.md §2 (Amperfy's Alamofire .validate()
# drops the body of any non-2xx response).
# ---------------------------------------------------------------------------

def test_error_response_http_status_is_always_200():
    resp = client.get("/rest/ping.view", params={"u": USER, "t": "deadbeef", "s": "x"})
    assert resp.status_code == 200


def test_unknown_action_is_also_http_200():
    resp = client.get("/rest/totallyMadeUpAction.view", params=token_params())
    assert resp.status_code == 200
    assert 'status="failed"' in resp.text
    assert 'code="0"' in resp.text


# ---------------------------------------------------------------------------
# .view suffix is mandatory
# ---------------------------------------------------------------------------

def test_path_with_view_suffix_works():
    resp = client.get("/rest/ping.view", params=token_params())
    assert resp.status_code == 200
    assert 'status="ok"' in resp.text


def test_path_without_view_suffix_is_not_routed():
    resp = client.get("/rest/ping", params=token_params())
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# response shape: version, root element, content type
# ---------------------------------------------------------------------------

def test_ping_response_declares_mirasonic_identity_and_versions():
    resp = client.get("/rest/ping.view", params=token_params())
    assert 'version="1.16.1"' in resp.text
    assert 'type="mirasonic"' in resp.text
    assert 'serverVersion="0.1.0"' in resp.text
    assert resp.headers["content-type"].startswith("text/xml")


def test_library_path_can_be_configured_with_mirasonic_env(monkeypatch, tmp_path):
    path = tmp_path / "mirasonic.db"
    monkeypatch.setenv("MIRASONIC_DB", str(path))

    instance = library.Library()

    assert instance.path == str(path)


# ---------------------------------------------------------------------------
# <song> element construction — escaping and duration="0" fallback
# ---------------------------------------------------------------------------

def test_song_element_escapes_ampersand_and_quotes():
    import xml.etree.ElementTree as ET

    root = ET.Element("root")
    subsonic._add_song_element(root, "song", "vid1", {
        "title": 'Rock & Roll "Anthem"',
        "artist": "AC/DC",
        "duration": 200,
    })
    xml_bytes = ET.tostring(root, encoding="utf-8")
    text = xml_bytes.decode("utf-8")
    # raw characters must not appear unescaped
    assert 'title="Rock & Roll' not in text
    assert "&amp;" in text
    assert "&quot;" in text
    # round-trips back to the original string
    parsed = ET.fromstring(xml_bytes)
    song = parsed.find("song")
    assert song.get("title") == 'Rock & Roll "Anthem"'


def test_song_element_unknown_duration_is_zero():
    import xml.etree.ElementTree as ET

    root = ET.Element("root")
    subsonic._add_song_element(root, "song", "vid2", {
        "title": "T", "artist": "A", "duration": None,
    })
    song = root.find("song")
    assert song.get("duration") == "0"


def test_song_element_is_never_a_directory():
    import xml.etree.ElementTree as ET

    root = ET.Element("root")
    subsonic._add_song_element(root, "song", "vid3", {"title": "T", "artist": "A"})
    song = root.find("song")
    assert song.get("isDir") == "false"
    assert song.get("contentType") == "audio/aac"
    assert song.get("suffix") == "aac"
    assert song.get("coverArt") == "vid3"


# ---------------------------------------------------------------------------
# Regression: Amperfy's excludeServerDeleteUncachedSongsFetchPredicate
# (SongMO+CoreDataClass.swift) hides any <song> unless `size > 0 AND
# album.remoteStatus == available`, or it has a downloaded file (offline
# caching is off by design, D-015). Before this fix <song> carried no album,
# no albumId, and size defaulted to duration*0 == "0" whenever duration was
# unknown — so search3 answered 200 with tracks, but Amperfy silently dropped
# every one of them from the results list. Found 2026-08-26 on iPhone.
# These three asserts are the actual bug: if any of them regresses, the songs
# vanish from Amperfy's search again while the HTTP response still looks fine.
# ---------------------------------------------------------------------------

def test_song_element_always_carries_nonempty_album_and_positive_size():
    import xml.etree.ElementTree as ET

    root = ET.Element("root")
    subsonic._add_song_element(root, "song", "vid4", {
        "title": "One More Time", "artist": "Daft Punk", "album": "Discovery",
        "duration": 320,
    })
    song = root.find("song")
    assert song.get("album") == "Discovery"
    assert song.get("albumId")  # non-empty, al- prefixed (checked below)
    assert song.get("albumId").startswith("al-")
    assert int(song.get("size")) > 0


def test_song_element_size_is_never_zero_even_with_unknown_duration():
    import xml.etree.ElementTree as ET

    root = ET.Element("root")
    subsonic._add_song_element(root, "song", "vid5", {
        "title": "T", "artist": "A", "album": "Al", "duration": None,
    })
    song = root.find("song")
    assert song.get("duration") == "0"  # still honest, per SUBSONIC.md §4
    assert int(song.get("size")) > 0  # but size must never be 0


def test_song_element_album_falls_back_to_title_when_missing():
    """meta["album"] is None (single, no album run in the listing) — the
    element must still get a non-empty album, or the predicate above hides
    the song exactly as it did before this fix."""
    import xml.etree.ElementTree as ET

    root = ET.Element("root")
    subsonic._add_song_element(root, "song", "vid6", {
        "title": "Loner", "artist": "A", "album": None, "duration": 180,
    })
    song = root.find("song")
    assert song.get("album") == "Loner"


def test_song_element_bitrate_and_created_are_present():
    import xml.etree.ElementTree as ET
    from datetime import datetime

    root = ET.Element("root")
    subsonic._add_song_element(root, "song", "vid7", {
        "title": "T", "artist": "A", "album": "Al", "duration": 200,
    })
    song = root.find("song")
    assert song.get("bitRate") == "129"
    # Amperfy parses `created` via ISO8601DateFormatter with
    # .withFractionalSeconds — without milliseconds it silently fails.
    created = song.get("created")
    assert created.endswith("Z")
    parsed = datetime.fromisoformat(created.replace("Z", "+00:00"))
    assert parsed.microsecond % 1000 == 0  # millisecond precision, not micro-noise


# ---------------------------------------------------------------------------
# search-metadata cache — LRU eviction on overflow
# ---------------------------------------------------------------------------

def test_search_cache_evicts_oldest_on_overflow():
    subsonic._search_cache.clear()
    original_max = subsonic._SEARCH_CACHE_MAX
    try:
        subsonic._SEARCH_CACHE_MAX = 3
        for i in range(4):
            subsonic._cache_put(f"id{i}", {"title": f"T{i}", "artist": "A", "duration": 0})
        assert subsonic._cache_get("id0") is None  # evicted, oldest
        assert subsonic._cache_get("id1") is not None
        assert subsonic._cache_get("id3") is not None
        assert len(subsonic._search_cache) == 3
    finally:
        subsonic._SEARCH_CACHE_MAX = original_max
        subsonic._search_cache.clear()


def test_search_cache_get_refreshes_lru_order():
    subsonic._search_cache.clear()
    original_max = subsonic._SEARCH_CACHE_MAX
    try:
        subsonic._SEARCH_CACHE_MAX = 3
        subsonic._cache_put("a", {"title": "A", "artist": "X", "duration": 0})
        subsonic._cache_put("b", {"title": "B", "artist": "X", "duration": 0})
        subsonic._cache_put("c", {"title": "C", "artist": "X", "duration": 0})
        subsonic._cache_get("a")  # touch a, so b becomes the oldest
        subsonic._cache_put("d", {"title": "D", "artist": "X", "duration": 0})
        assert subsonic._cache_get("b") is None
        assert subsonic._cache_get("a") is not None
        assert subsonic._cache_get("c") is not None
        assert subsonic._cache_get("d") is not None
    finally:
        subsonic._SEARCH_CACHE_MAX = original_max
        subsonic._search_cache.clear()


# ---------------------------------------------------------------------------
# stubs from §9 — empty success
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("action,tag", [
    ("getGenres", "genres"),
    ("getIndexes", "indexes"),
    ("getMusicFolders", "musicFolders"),
    ("getPodcasts", "podcasts"),
    ("getNewestPodcasts", "newestPodcasts"),
    ("getInternetRadioStations", "internetRadioStations"),
    ("getSimilarSongs2", "similarSongs2"),
    # getRandomSongs/getPlaylists moved to phase 2, getArtists/getAlbumList2 to
    # phase 3 (all backed by SQLite, see the tests using the `lib` fixture
    # below) — they answer empty here only because the test library is empty,
    # not because they are stubs.
])
def test_stub_endpoints_return_empty_success(action, tag):
    resp = client.get(f"/rest/{action}.view", params=token_params())
    assert resp.status_code == 200
    assert 'status="ok"' in resp.text
    assert f"<{tag}" in resp.text


@pytest.mark.parametrize("action", ["setRating", "deletePodcastEpisode",
                                     "getOpenSubsonicExtensions"])
def test_noop_endpoints_return_empty_success(action):
    resp = client.get(f"/rest/{action}.view", params=token_params())
    assert resp.status_code == 200
    assert 'status="ok"' in resp.text


def test_get_music_directory_returns_not_found():
    """getArtist/getAlbum used to answer 70 here too — and that turned out to be
    the very bug that had Amperfy asking for an album up to 292 times a minute.
    getMusicDirectory Amperfy never calls at all, so 70 is safe for it."""
    resp = client.get("/rest/getMusicDirectory.view", params={**token_params(), "id": "whatever"})
    assert resp.status_code == 200
    assert 'code="70"' in resp.text


# ---------------------------------------------------------------------------
# getCoverArt.view
# ---------------------------------------------------------------------------

def test_get_cover_art_missing_id_returns_error_10():
    resp = client.get("/rest/getCoverArt.view", params=token_params())
    assert 'code="10"' in resp.text


def test_get_cover_art_unknown_video_id_returns_error_70(lib):
    subsonic._search_cache.clear()
    resp = client.get("/rest/getCoverArt.view", params={**token_params(), "id": "nope"})
    assert 'code="70"' in resp.text


def test_get_cover_art_playlist_id_returns_error_70_phase1(lib):
    """No playlist with that id exists (the test library is empty) — the same
    70 that was an honest stub answer back in phase 1."""
    resp = client.get("/rest/getCoverArt.view", params={**token_params(), "id": "pl-1"})
    assert 'code="70"' in resp.text


# ---------------------------------------------------------------------------
# search3.view — empty when songCount is 0, uses metadata cache, calls into
# main.search/main.get_song_duration without touching yt-dlp/InnerTube.
# ---------------------------------------------------------------------------

def test_search3_with_zero_song_count_returns_empty_result():
    resp = client.get("/rest/search3.view", params={
        **token_params(), "query": "whatever", "songCount": "0",
        "artistCount": "0", "albumCount": "0",
    })
    assert resp.status_code == 200
    assert "<searchResult3" in resp.text
    assert "<song" not in resp.text


def test_search3_builds_songs_from_cached_search_and_duration(monkeypatch):
    subsonic._search_cache.clear()

    async def fake_search(q="", limit=20, continuation=""):
        return {
            "tracks": [
                {"id": "vidA", "title": "Song A", "artist": "Artist A",
                 "artworkURL": "https://example.test/a.jpg", "durationSeconds": None},
            ],
            "continuation": None,
        }

    async def fake_duration(video_id):
        assert video_id == "vidA"
        return 217

    monkeypatch.setattr(main, "search", fake_search)
    monkeypatch.setattr(main, "get_song_duration", fake_duration)

    resp = client.get("/rest/search3.view", params={
        **token_params(), "query": "test query", "songCount": "10", "songOffset": "0",
        "artistCount": "0", "albumCount": "0",
    })
    assert resp.status_code == 200
    assert 'id="vidA"' in resp.text
    assert 'title="Song A"' in resp.text
    assert 'duration="217"' in resp.text
    assert 'isDir="false"' in resp.text
    # metadata now cached for a later updatePlaylist lookup
    cached = subsonic._cache_get("vidA")
    assert cached["title"] == "Song A"
    assert cached["duration"] == 217


def test_search3_uses_album_from_innertube_when_present(monkeypatch):
    subsonic._search_cache.clear()

    async def fake_search(q="", limit=20, continuation=""):
        return {
            "tracks": [
                {"id": "vidAlbum", "title": "One More Time", "artist": "Daft Punk",
                 "album": "Discovery", "artworkURL": None, "durationSeconds": None},
            ],
            "continuation": None,
        }

    async def fake_duration(video_id):
        return 320

    monkeypatch.setattr(main, "search", fake_search)
    monkeypatch.setattr(main, "get_song_duration", fake_duration)

    resp = client.get("/rest/search3.view", params={
        **token_params(), "query": "one more time", "songCount": "10",
        "artistCount": "0", "albumCount": "0",
    })
    assert 'album="Discovery"' in resp.text


def test_search3_falls_back_album_to_title_for_singles(monkeypatch):
    """InnerTube gave no album run (a single) — search3 must not let an empty
    album through, or the song disappears from Amperfy (see the regression
    tests on _add_song_element above)."""
    subsonic._search_cache.clear()

    async def fake_search(q="", limit=20, continuation=""):
        return {
            "tracks": [
                {"id": "vidSingle", "title": "Loner", "artist": "Some Artist",
                 "album": None, "artworkURL": None, "durationSeconds": None},
            ],
            "continuation": None,
        }

    async def fake_duration(video_id):
        return 180

    monkeypatch.setattr(main, "search", fake_search)
    monkeypatch.setattr(main, "get_song_duration", fake_duration)

    resp = client.get("/rest/search3.view", params={
        **token_params(), "query": "loner", "songCount": "10",
        "artistCount": "0", "albumCount": "0",
    })
    assert 'album="Loner"' in resp.text  # fell back to the title, never empty


def test_search3_upstream_search_failure_does_not_crash(monkeypatch):
    from fastapi.responses import JSONResponse

    async def failing_search(q="", limit=20, continuation=""):
        return JSONResponse({"error": "upstream"}, status_code=502)

    monkeypatch.setattr(main, "search", failing_search)

    resp = client.get("/rest/search3.view", params={
        **token_params(), "query": "test query", "songCount": "10",
        "artistCount": "0", "albumCount": "0",
    })
    assert resp.status_code == 200
    assert "<searchResult3" in resp.text


def test_search3_one_bad_duration_lookup_does_not_break_the_page(monkeypatch):
    subsonic._search_cache.clear()

    async def fake_search(q="", limit=20, continuation=""):
        return {
            "tracks": [
                {"id": "vidGood", "title": "Good", "artist": "A", "artworkURL": None, "durationSeconds": None},
                {"id": "vidBad", "title": "Bad", "artist": "A", "artworkURL": None, "durationSeconds": None},
            ],
            "continuation": None,
        }

    async def flaky_duration(video_id):
        if video_id == "vidBad":
            raise RuntimeError("boom")
        return 123

    monkeypatch.setattr(main, "search", fake_search)
    monkeypatch.setattr(main, "get_song_duration", flaky_duration)

    resp = client.get("/rest/search3.view", params={
        **token_params(), "query": "q", "songCount": "10",
        "artistCount": "0", "albumCount": "0",
    })
    assert resp.status_code == 200
    assert 'id="vidGood"' in resp.text
    assert 'id="vidBad"' in resp.text
    assert 'duration="123"' in resp.text
    assert 'duration="0"' in resp.text  # the failed one falls back to 0


# ---------------------------------------------------------------------------
# stream.view / download.view — reuse main.proxy_bytes, missing id -> 10
# ---------------------------------------------------------------------------

def test_stream_view_missing_id_returns_error_10():
    resp = client.get("/rest/stream.view", params=token_params())
    assert 'code="10"' in resp.text


def test_stream_view_and_download_view_share_the_same_proxy(monkeypatch):
    calls = []

    async def fake_proxy_bytes(video_id, range_header):
        calls.append((video_id, range_header))
        from fastapi.responses import Response as PlainResponse
        return PlainResponse(content=b"bytes", media_type="audio/aac")

    monkeypatch.setattr(main, "proxy_bytes", fake_proxy_bytes)

    r1 = client.get("/rest/stream.view", params={**token_params(), "id": "vid1"},
                     headers={"Range": "bytes=0-9"})
    r2 = client.get("/rest/download.view", params={**token_params(), "id": "vid1"})

    assert r1.status_code == 200
    assert r2.status_code == 200
    assert calls == [("vid1", "bytes=0-9"), ("vid1", None)]


# ---------------------------------------------------------------------------
# library (phase 2) — playlists, star/unstar, getSong/getRandomSongs on
# SQLite. Every test below gets its own on-disk DB via tmp_path — never the
# default /data path.
# ---------------------------------------------------------------------------

@pytest.fixture
def lib(tmp_path, monkeypatch):
    instance = library.Library(str(tmp_path / "test.db"))
    monkeypatch.setattr(subsonic, "_library", instance)
    yield instance


def test_get_playlist_unknown_id_is_not_found(lib):
    resp = client.get("/rest/getPlaylist.view", params={**token_params(), "id": "999"})
    assert resp.status_code == 200
    assert 'code="70"' in resp.text


def test_create_playlist_returns_its_id(lib):
    resp = client.get("/rest/createPlaylist.view", params={**token_params(), "name": "Morning"})
    assert resp.status_code == 200
    assert 'status="ok"' in resp.text
    assert 'name="Morning"' in resp.text

    playlists = lib.get_playlists()
    assert len(playlists) == 1
    assert f'id="{playlists[0]["id"]}"' in resp.text


def test_update_playlist_removes_multiple_indices_at_once(lib):
    playlist_id = lib.create_playlist("Queue")
    songs = [(f"vid{i}", f"Title {i}", "Artist", "Album", 100 + i, None) for i in range(5)]
    lib.update_playlist(playlist_id, "Queue", [], songs)

    resp = client.get("/rest/updatePlaylist.view", params={
        **token_params(), "playlistId": str(playlist_id), "name": "Queue",
        "songIndexToRemove": ["1", "3"],
    })
    assert resp.status_code == 200
    assert 'status="ok"' in resp.text

    remaining = [s["id"] for s in lib.get_playlist(playlist_id)["songs"]]
    assert remaining == ["vid0", "vid2", "vid4"]


def test_update_playlist_adds_songs_in_order(lib):
    playlist_id = lib.create_playlist("Queue")
    subsonic._search_cache.clear()
    subsonic._cache_put("vidNew1", {"title": "New One", "artist": "A", "album": "Al",
                                    "duration": 111, "artworkURL": None})
    subsonic._cache_put("vidNew2", {"title": "New Two", "artist": "A", "album": "Al",
                                    "duration": 222, "artworkURL": None})

    resp = client.get("/rest/updatePlaylist.view", params={
        **token_params(), "playlistId": str(playlist_id), "name": "Queue",
        "songIdToAdd": ["vidNew1", "vidNew2"],
    })
    assert 'status="ok"' in resp.text

    songs = lib.get_playlist(playlist_id)["songs"]
    assert [s["id"] for s in songs] == ["vidNew1", "vidNew2"]
    assert songs[0]["title"] == "New One"
    assert songs[0]["duration"] == 111


def test_update_playlist_full_reorder_matches_amperfy_pattern(lib):
    """Amperfy reorders a playlist by removing indices 0..n-1 and re-adding the
    whole list in the new order (syncUpload(playlistToUpdateOrder:)). The
    result is compared against the entire expected list at once rather than
    element by element — this is exactly where recomputing indices one removal
    at a time would break the order."""
    playlist_id = lib.create_playlist("Queue")
    songs = [(f"vid{i}", f"T{i}", "A", "Al", 100, None) for i in range(4)]
    lib.update_playlist(playlist_id, "Queue", [], songs)

    new_order = ["vid3", "vid1", "vid0", "vid2"]
    resp = client.get("/rest/updatePlaylist.view", params={
        **token_params(), "playlistId": str(playlist_id), "name": "Queue",
        "songIndexToRemove": ["0", "1", "2", "3"],
        "songIdToAdd": new_order,
    })
    assert 'status="ok"' in resp.text

    assert [s["id"] for s in lib.get_playlist(playlist_id)["songs"]] == new_order


def test_delete_playlist_cascades_to_items(lib):
    playlist_id = lib.create_playlist("Gone")
    lib.update_playlist(playlist_id, "Gone", [], [("vidX", "T", "A", None, 10, None)])

    resp = client.get("/rest/deletePlaylist.view", params={**token_params(), "id": str(playlist_id)})
    assert 'status="ok"' in resp.text

    remaining_items = lib._conn.execute(
        "SELECT COUNT(*) FROM playlist_items WHERE playlist_id = ?", (playlist_id,)
    ).fetchone()[0]
    assert remaining_items == 0
    assert lib.get_playlist(playlist_id) is None


def test_star_unstar_and_get_starred2_round_trip(lib):
    subsonic._search_cache.clear()
    subsonic._cache_put("vidStar", {"title": "Starred Song", "artist": "A", "album": "Al",
                                    "duration": 200, "artworkURL": None})

    resp = client.get("/rest/star.view", params={**token_params(), "id": "vidStar"})
    assert 'status="ok"' in resp.text

    resp = client.get("/rest/getStarred2.view", params=token_params())
    assert 'id="vidStar"' in resp.text
    assert 'starred="' in resp.text

    resp = client.get("/rest/unstar.view", params={**token_params(), "id": "vidStar"})
    assert 'status="ok"' in resp.text

    resp = client.get("/rest/getStarred2.view", params=token_params())
    assert 'id="vidStar"' not in resp.text


def test_star_without_song_id_is_accepted_and_ignored(lib):
    """§5: `id` is a song; albumId/artistId are accepted and ignored."""
    resp = client.get("/rest/star.view", params={**token_params(), "albumId": "al-deadbeef"})
    assert resp.status_code == 200
    assert 'status="ok"' in resp.text


# ---------------------------------------------------------------------------
# scrobble.view — completed listens are persisted; playing-now notifications
# remain an empty success. The protocol permits repeated id/time parameters,
# and Library.record_listen supplies idempotency for an explicit pair.
# ---------------------------------------------------------------------------

def test_scrobble_records_completed_listen(lib, monkeypatch):
    async def details(_video_id):
        return {"title": "One", "artist": "Artist", "duration": 180,
                "artwork": "https://example.invalid/cover.jpg"}

    monkeypatch.setattr(main, "get_song_details", details)
    response = client.get("/rest/scrobble.view", params={
        **token_params(), "id": "vid-1", "time": "1700000000000",
        "submission": "true",
    })
    assert response.status_code == 200
    assert lib.get_listen_stats(0)[0]["listen_count"] == 1


@pytest.mark.parametrize("submission, should_count", [
    ("TRUE", True),
    ("1", True),
    ("FALSE", False),
    ("0", False),
])
def test_scrobble_submission_values_follow_protocol(lib, monkeypatch, submission,
                                                    should_count):
    async def meta(video_id):
        return {"title": video_id, "artist": "Artist", "duration": 180,
                "artwork_url": None}

    monkeypatch.setattr(subsonic, "_resolve_song_meta", meta)
    response = client.get("/rest/scrobble.view", params={
        **token_params(), "id": f"vid-submission-{submission}",
        "time": "1700000000000", "submission": submission,
    })
    assert response.status_code == 200
    assert bool(lib.get_listen_stats(0)) is should_count


def test_scrobble_playing_now_does_not_count(lib):
    response = client.get("/rest/scrobble.view", params={
        **token_params(), "id": "vid-1", "submission": "false",
    })
    assert response.status_code == 200
    assert lib.get_listen_stats(0) == []


def test_scrobble_accepts_repeated_ids_and_times(lib, monkeypatch):
    async def meta(video_id):
        return {"title": video_id, "artist": "Artist", "album": None,
                "duration": 180, "artwork_url": None}

    monkeypatch.setattr(subsonic, "_resolve_song_meta", meta)
    response = client.get("/rest/scrobble.view", params=[
        *token_params().items(), ("id", "vid-1"), ("id", "vid-2"),
        ("time", "1700000000000"), ("time", "1700000180000"),
        ("submission", "true"),
    ])
    assert response.status_code == 200
    stats = {row["song_id"]: row["last_played_ms"]
             for row in lib.get_listen_stats(0)}
    assert stats == {
        "vid-1": 1700000000000,
        "vid-2": 1700000180000,
    }


def test_scrobble_missing_id_returns_error(lib):
    response = client.get("/rest/scrobble.view", params={
        **token_params(), "submission": "true",
    })
    assert response.status_code == 200
    assert 'code="10"' in response.text


@pytest.mark.parametrize("raw_time", ["-1", "not-a-number"])
def test_scrobble_malformed_time_is_treated_as_absent(lib, monkeypatch, raw_time):
    async def meta(video_id):
        return {"title": video_id, "artist": "Artist", "duration": 180,
                "artwork_url": None}

    monkeypatch.setattr(subsonic, "_resolve_song_meta", meta)
    response = client.get("/rest/scrobble.view", params={
        **token_params(), "id": "vid-malformed", "time": raw_time,
    })
    assert response.status_code == 200
    assert 'status="ok"' in response.text
    assert lib.get_listen_stats(0)[0]["listen_count"] == 1


def test_scrobble_defaults_submission_to_true(lib, monkeypatch):
    async def meta(video_id):
        return {"title": video_id, "artist": "Artist", "duration": 180,
                "artwork_url": None}

    monkeypatch.setattr(subsonic, "_resolve_song_meta", meta)
    response = client.get("/rest/scrobble.view", params={
        **token_params(), "id": "vid-default", "time": "1700000000000",
    })
    assert response.status_code == 200
    assert lib.get_listen_stats(0)[0]["listen_count"] == 1


def test_scrobble_repeated_explicit_pair_is_idempotent(lib, monkeypatch):
    async def meta(video_id):
        return {"title": video_id, "artist": "Artist", "duration": 180,
                "artwork_url": None}

    monkeypatch.setattr(subsonic, "_resolve_song_meta", meta)
    params = [
        *token_params().items(), ("id", "vid-repeat"), ("id", "vid-repeat"),
        ("time", "1700000000000"), ("time", "1700000000000"),
    ]
    response = client.get("/rest/scrobble.view", params=params)
    assert response.status_code == 200
    assert lib.get_listen_stats(0)[0]["listen_count"] == 1


def test_scrobble_untimed_same_song_is_deduplicated_within_30_seconds(
        lib, monkeypatch):
    import asyncio
    from starlette.datastructures import QueryParams

    async def meta(video_id):
        return {"title": video_id, "artist": "Artist", "duration": 180,
                "artwork_url": None}

    now_values = iter([1700000000000, 1700000010000])
    monkeypatch.setattr(subsonic, "_resolve_song_meta", meta)
    monkeypatch.setattr(library, "_now_ms", lambda: next(now_values))

    first = asyncio.run(subsonic._scrobble(
        QueryParams([("id", "vid-untimed"), ("time", "broken")]),
        None,
    ))
    second = asyncio.run(subsonic._scrobble(
        QueryParams([("id", "vid-untimed")]), None
    ))

    assert first.status_code == second.status_code == 200
    assert lib.get_listen_stats(0)[0]["listen_count"] == 1


def test_update_playlist_add_song_missing_from_cache_falls_back_to_player(lib, monkeypatch):
    """A track missing from the search cache (worker restart, eviction) — its
    metadata must not be lost, and yt-dlp must not be called under any
    circumstances (docs/SUBSONIC.md §7, §8).

    Title and artist come from the same /youtubei/v1/player response as the
    duration: without them the playlist would hold a videoId instead of a name.
    """
    subsonic._search_cache.clear()
    playlist_id = lib.create_playlist("Fresh")

    async def fake_details(video_id):
        assert video_id == "vidUncached"
        return {"title": "Uncached Song", "artist": "Some Artist", "duration": 250,
                "artwork": "https://example.invalid/uncached.jpg"}

    def yt_dlp_must_not_be_called(*args, **kwargs):
        raise AssertionError("yt-dlp must never be called from updatePlaylist")

    monkeypatch.setattr(main, "get_song_details", fake_details)
    monkeypatch.setattr(main, "_resolve_stream_sync", yt_dlp_must_not_be_called)

    resp = client.get("/rest/updatePlaylist.view", params={
        **token_params(), "playlistId": str(playlist_id), "name": "Fresh",
        "songIdToAdd": ["vidUncached"],
    })
    assert 'status="ok"' in resp.text

    song = lib.get_song("vidUncached")
    assert song is not None
    assert (song["title"], song["artist"], song["duration"]) == (
        "Uncached Song", "Some Artist", 250)
    # Artwork arrives in that same /player response. Without it a track added
    # outside of search stayed in the library with no picture forever: a repeat
    # INSERT will not fill it in.
    assert song["artwork_url"] == "https://example.invalid/uncached.jpg"


def test_add_song_twice_fills_in_what_was_unknown_the_first_time(lib):
    """The first add knew only the duration, the second brought artwork and an
    album — those must appear, while what was already known stays untouched."""
    playlist_id = lib.create_playlist("Heal")
    lib.update_playlist(playlist_id, "Heal", [],
                        [("vidHeal", "Real Title", "Real Artist", None, 200, None)])
    lib.star("vidHeal", "Other Title", "Other Artist", "Album", 999,
             "https://lh3.googleusercontent.com/x")

    song = lib.get_song("vidHeal")
    assert (song["title"], song["artist"], song["duration"]) == (
        "Real Title", "Real Artist", 200)          # known values not overwritten
    assert song["album"] == "Album"                 # missing values filled in
    assert song["artwork_url"] == "https://lh3.googleusercontent.com/x"


def test_get_playlist_escapes_ampersand_and_quotes_in_name_and_song_title(lib):
    playlist_id = lib.create_playlist('Rock & Roll "Anthem"')
    lib.update_playlist(playlist_id, 'Rock & Roll "Anthem"', [],
                        [("vidEsc", 'Song & "Title"', "AC/DC", "Album", 100, None)])

    resp = client.get("/rest/getPlaylist.view", params={**token_params(), "id": str(playlist_id)})
    assert resp.status_code == 200
    assert "&amp;" in resp.text
    assert "&quot;" in resp.text

    import xml.etree.ElementTree as ET
    parsed = ET.fromstring(resp.text)
    playlist_el = parsed.find("{*}playlist")  # root has the subsonic xmlns
    assert playlist_el.get("name") == 'Rock & Roll "Anthem"'
    entry = playlist_el.find("{*}entry")
    assert entry.get("title") == 'Song & "Title"'


def test_get_random_songs_returns_library_songs(lib):
    lib.star("vidR1", "R1", "A", "Al", 100, None)
    lib.star("vidR2", "R2", "A", "Al", 100, None)

    resp = client.get("/rest/getRandomSongs.view", params={**token_params(), "size": "10"})
    assert resp.status_code == 200
    assert 'id="vidR1"' in resp.text
    assert 'id="vidR2"' in resp.text


def test_get_song_prefers_the_library_but_still_answers_from_search_cache(lib):
    """The library is the first source, the search cache the second. Error 70
    would be expensive here: Amperfy reads it as "the track is gone" and drops
    it from its own database, while a track found by search and not yet added
    is a perfectly normal state."""
    subsonic._search_cache.clear()
    subsonic._cache_put("vidCacheOnly", {"title": "Cache Only", "artist": "A", "duration": 100})

    resp = client.get("/rest/getSong.view", params={**token_params(), "id": "vidCacheOnly"})
    assert 'status="ok"' in resp.text
    assert 'title="Cache Only"' in resp.text

    lib.star("vidCacheOnly", "From Library", "A", "Al", 100, None)
    resp = client.get("/rest/getSong.view", params={**token_params(), "id": "vidCacheOnly"})
    assert 'title="From Library"' in resp.text


def test_get_song_unknown_everywhere_is_not_found(lib):
    subsonic._search_cache.clear()
    resp = client.get("/rest/getSong.view", params={**token_params(), "id": "vidNowhere"})
    assert resp.status_code == 200
    assert 'code="70"' in resp.text


def test_get_playlists_on_an_empty_library_is_empty_success(lib):
    """syncInitial aborts on an error, but an empty response passes (§5)."""
    resp = client.get("/rest/getPlaylists.view", params=token_params())
    assert resp.status_code == 200
    assert 'status="ok"' in resp.text
    assert "<playlists" in resp.text
    assert "<playlist " not in resp.text


# ---------------------------------------------------------------------------
# bug fix: search3 must not throw away durationSeconds from main.search() —
# doing so forced a /youtubei/v1/player round trip per track of every page
# where the search response already carried the duration (measured
# 2026-08-27: 1707/1707 candidates had it). _resolve_durations stays as the
# fallback path for the rare track that genuinely lacks it.
# ---------------------------------------------------------------------------

def test_search3_does_not_resolve_duration_when_already_present(monkeypatch):
    subsonic._search_cache.clear()

    async def fake_search(q="", limit=20, continuation=""):
        return {
            "tracks": [
                {"id": "vidKnown", "title": "Known", "artist": "A", "album": "Al",
                 "artworkURL": None, "durationSeconds": 250},
            ],
            "continuation": None,
        }

    def player_must_not_be_called(video_id):
        raise AssertionError("duration was already known — /player must not be called")

    monkeypatch.setattr(main, "search", fake_search)
    monkeypatch.setattr(main, "get_song_duration", player_must_not_be_called)

    resp = client.get("/rest/search3.view", params={
        **token_params(), "query": "known track", "songCount": "10",
        "artistCount": "0", "albumCount": "0",
    })
    assert resp.status_code == 200
    assert 'duration="250"' in resp.text
    assert subsonic._cache_get("vidKnown")["duration"] == 250


# ---------------------------------------------------------------------------
# live tests — real network, real YouTube Music. Run with `pytest -m live`.
# ---------------------------------------------------------------------------

@pytest.mark.live
def test_live_ping_returns_version():
    resp = client.get("/rest/ping.view", params=token_params())
    assert resp.status_code == 200
    assert 'version="1.16.1"' in resp.text
    assert 'status="ok"' in resp.text


@pytest.mark.live
def test_live_search3_returns_nonempty_songs():
    resp = client.get("/rest/search3.view", params={
        **token_params(), "query": "Daft Punk One More Time",
        "songCount": "10", "songOffset": "0",
        "artistCount": "0", "artistOffset": "0",
        "albumCount": "0", "albumOffset": "0",
    })
    assert resp.status_code == 200
    assert "<searchResult3" in resp.text
    assert "<song " in resp.text
    assert 'isDir="false"' in resp.text


@pytest.mark.live
def test_live_stream_view_range_request_returns_206_audio_aac():
    search_resp = client.get("/rest/search3.view", params={
        **token_params(), "query": "Daft Punk One More Time",
        "songCount": "5", "songOffset": "0", "artistCount": "0", "albumCount": "0",
    })
    import re
    video_id = re.search(r'<song id="([^"]+)"', search_resp.text).group(1)

    stream_resp = client.get(
        f"/rest/stream.view", params={**token_params(), "id": video_id},
        headers={"Range": "bytes=0-262143"},
    )
    assert stream_resp.status_code == 206
    assert stream_resp.headers["content-type"] == "audio/aac"


# --- uvicorn access log: credentials must never reach it -------------------
# Regression from the 2026-08-26 deployment: our own logger printed only
# parameter names, while uvicorn printed the whole request line in parallel,
# `p=<password>` included. The argument shape is taken from
# uvicorn.protocols.http:
# (client_addr, method, full_path, http_version, status_code, phrase).

def _access_record(full_path: str) -> logging.LogRecord:
    return logging.LogRecord(
        name="uvicorn.access", level=logging.INFO, pathname=__file__, lineno=0,
        msg='%s - "%s %s HTTP/%s" %d %s',
        args=("172.23.0.1:1", "GET", full_path, "1.1", 200, "OK"),
        exc_info=None,
    )


def test_access_log_redacts_subsonic_credentials():
    record = _access_record("/rest/ping.view?u=rilya&p=hunter2&v=1.13.0&c=Amperfy")
    main._RedactRestQuery().filter(record)
    rendered = record.getMessage()
    assert "hunter2" not in rendered
    assert "u=rilya" not in rendered
    assert "/rest/ping.view?<redacted>" in rendered


def test_access_log_keeps_non_rest_paths_intact():
    record = _access_record("/search?q=daft+punk")
    main._RedactRestQuery().filter(record)
    assert "/search?q=daft+punk" in record.getMessage()


def test_root_answers_200_not_404():
    """A bare 404 at the root was twice mistaken for a broken server."""
    with TestClient(main.app) as client:
        response = client.get("/")
    assert response.status_code == 200
    assert "/rest/" in response.text


# ---------------------------------------------------------------------------
# getSong / getAlbum / getArtist — regression test for the loop of 684 calls in
# half an hour (log of 2026-08-26 19:00–19:30). Error 70 made Amperfy ask again.
# ---------------------------------------------------------------------------

@pytest.fixture
def cached_song(lib):
    subsonic._search_cache.clear()
    meta = {
        "title": "Get Lucky",
        "artist": "Daft Punk",
        "album": "Random Access Memories",
        "artworkURL": "https://example.invalid/art.jpg",
        "duration": 369,
    }
    subsonic._cache_put("vid00000001", meta)
    # getSong (phase 2) reads the library, not the search cache — the track has
    # to be in both so this fixture works for getAlbum/getArtist (cache) and
    # getSong (database) at the same time.
    lib.star("vid00000001", meta["title"], meta["artist"], meta["album"],
             meta["duration"], meta["artworkURL"])
    yield "vid00000001", meta
    subsonic._search_cache.clear()


def test_get_album_answers_ok_for_unknown_id(lib):
    """Never an error: an error makes Amperfy retry forever."""
    subsonic._search_cache.clear()
    response = client.get("/rest/getAlbum.view", params={**token_params(), "id": "al-deadbeefdeadbeef"})
    assert response.status_code == 200
    assert 'status="ok"' in response.text
    assert 'id="al-deadbeefdeadbeef"' in response.text
    assert 'songCount="0"' in response.text


def test_get_album_returns_cached_songs(cached_song):
    video_id, meta = cached_song
    album_id = subsonic._album_id(meta["artist"], meta["album"])
    response = client.get("/rest/getAlbum.view", params={**token_params(), "id": album_id})
    assert 'status="ok"' in response.text
    assert 'songCount="1"' in response.text
    assert f'id="{video_id}"' in response.text
    assert 'name="Random Access Memories"' in response.text
    # an album's cover comes from a track: _get_cover_art looks up by videoId
    assert f'coverArt="{video_id}"' in response.text


def test_get_artist_lists_albums_without_songs(cached_song):
    video_id, meta = cached_song
    artist_id = subsonic._artist_id(meta["artist"])
    response = client.get("/rest/getArtist.view", params={**token_params(), "id": artist_id})
    assert 'status="ok"' in response.text
    assert 'name="Daft Punk"' in response.text
    assert 'albumCount="1"' in response.text
    assert f'id="{video_id}"' not in response.text


def test_get_song_returns_cached_song(cached_song):
    video_id, _ = cached_song
    response = client.get("/rest/getSong.view", params={**token_params(), "id": video_id})
    assert 'status="ok"' in response.text
    assert 'title="Get Lucky"' in response.text
    assert 'duration="369"' in response.text


def test_get_song_unknown_id_is_not_found(lib):
    subsonic._search_cache.clear()
    response = client.get("/rest/getSong.view", params={**token_params(), "id": "nope"})
    assert 'status="failed"' in response.text
    assert 'code="70"' in response.text


# What /stream returns — investigated 2026-08-27, "the app crashes on seek".
# googlevideo serves itag 140 as a fragmented mp4; AudioFileStreamSeek returns
# DataUnavailable inside one, AudioStreaming falls back to a linear offset
# estimate, lands in the middle of an mdat and feeds the parser garbage. Hence
# the server repackages the track as ADTS and slices ranges itself, from bytes
# held in memory.
# ---------------------------------------------------------------------------

import httpx


def _mock_upstream(monkeypatch, handler, remux=None):
    monkeypatch.setattr(main, "get_stream_url",
                        lambda video_id: _completed("https://example.invalid/media"))
    main._upstream_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def _remux(data):
        return data if remux is None else remux(data)
    monkeypatch.setattr(main, "_remux_to_adts", _remux)


async def _completed(value):
    return value


def _whole_file(body):
    def handler(request):
        return httpx.Response(206, content=body,
                              headers={"content-range": f"bytes 0-{len(body) - 1}/{len(body)}"})
    return handler


def test_stream_always_asks_upstream_for_a_range(monkeypatch):
    """Without Range googlevideo throttles to ~32 KB/s and the player starves."""
    seen = []

    def handler(request):
        seen.append(request.headers.get("range"))
        return httpx.Response(206, content=b"0123456789",
                              headers={"content-range": "bytes 0-9/10"})

    _mock_upstream(monkeypatch, handler)
    response = client.get("/stream/vid00000001")

    assert seen == ["bytes=0-"]
    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/aac"
    assert response.headers["content-length"] == "10"
    assert response.headers["accept-ranges"] == "bytes"
    assert "content-range" not in response.headers
    assert response.content == b"0123456789"


def test_stream_serves_the_requested_slice(monkeypatch):
    _mock_upstream(monkeypatch, _whole_file(b"0123456789"))

    response = client.get("/stream/vid00000001", headers={"Range": "bytes=4-"})

    assert response.status_code == 206
    assert response.headers["content-range"] == "bytes 4-9/10"
    assert response.content == b"456789"


def test_stream_slices_from_the_repacked_length_not_the_upstream_one(monkeypatch):
    """Bounds are computed from the ADTS, which is longer than the original by
    its per-frame headers. Taking upstream's content-length would leave the
    tail of the track unreachable."""
    _mock_upstream(monkeypatch, _whole_file(b"0123456789"),
                   remux=lambda data: data + b"abcd")

    response = client.get("/stream/vid00000001", headers={"Range": "bytes=8-"})

    assert response.status_code == 206
    assert response.headers["content-range"] == "bytes 8-13/14"
    assert response.content == b"89abcd"


def test_stream_answers_416_past_the_end(monkeypatch):
    """416 means "there is nothing further", not a failure. Through a 502
    AudioStreaming went to errorOccurred, Amperfy to handleError, and the track
    started over."""
    _mock_upstream(monkeypatch, _whole_file(b"0123456789"))

    response = client.get("/stream/vid00000001", headers={"Range": "bytes=99999-"})

    assert response.status_code == 416
    assert response.headers["content-range"] == "bytes */10"


def test_stream_ignores_a_malformed_range(monkeypatch):
    _mock_upstream(monkeypatch, _whole_file(b"0123456789"))

    response = client.get("/stream/vid00000001", headers={"Range": "seconds=1-2"})

    assert response.status_code == 200
    assert response.content == b"0123456789"


def test_stream_retries_a_cut_download(monkeypatch):
    """A download cut in the middle is fixed by refetching: the file takes 0.15 s."""
    attempts = []

    def handler(request):
        attempts.append(1)
        if len(attempts) == 1:
            raise httpx.ReadError("googlevideo closed the connection")
        return httpx.Response(206, content=b"0123456789",
                              headers={"content-range": "bytes 0-9/10"})

    _mock_upstream(monkeypatch, handler)
    response = client.get("/stream/vid00000001")

    assert len(attempts) == 2
    assert response.content == b"0123456789"


def test_stream_gives_up_instead_of_looping(monkeypatch):
    attempts = []

    def handler(request):
        attempts.append(1)
        raise httpx.ReadError("googlevideo closed the connection")

    _mock_upstream(monkeypatch, handler)
    response = client.get("/stream/vid00000001")

    assert len(attempts) == main.DOWNLOAD_ATTEMPTS
    assert response.status_code == 502


def test_stream_repacks_each_track_once(monkeypatch):
    """A client sends at least two requests per track plus one per seek — no
    reason to download and repackage five megabytes for each of them."""
    downloads = []

    def handler(request):
        downloads.append(1)
        return httpx.Response(206, content=b"0123456789",
                              headers={"content-range": "bytes 0-9/10"})

    _mock_upstream(monkeypatch, handler)
    client.get("/stream/vid00000001", headers={"Range": "bytes=0-1"})
    client.get("/stream/vid00000001")
    client.get("/stream/vid00000001", headers={"Range": "bytes=5-"})

    assert len(downloads) == 1


def test_search3_does_not_repeat_a_track_that_spans_two_pages(monkeypatch):
    """The same videoId arrives on both the first page and the second.

    Per-page dedup does not catch this: pages are parsed separately, so the
    window the client asked for was half duplicates.
    """
    subsonic._search_cache.clear()
    pages = [
        {"tracks": [{"id": "v1", "title": "One", "artist": "A", "durationSeconds": 100},
                    {"id": "v2", "title": "Two", "artist": "A", "durationSeconds": 200}],
         "continuation": "TOKEN"},
        {"tracks": [{"id": "v2", "title": "Two", "artist": "A", "durationSeconds": 200},
                    {"id": "v3", "title": "Three", "artist": "A", "durationSeconds": 300}],
         "continuation": None},
    ]
    calls = []

    async def fake_search(q="", limit=20, continuation=""):
        calls.append(continuation)
        return pages[len(calls) - 1]

    monkeypatch.setattr(main, "search", fake_search)

    resp = client.get("/rest/search3.view", params={
        **token_params(), "query": "band", "songCount": "3", "songOffset": "0",
        "artistCount": "0", "albumCount": "0",
    })
    assert resp.status_code == 200
    assert [m for m in ("v1", "v2", "v3") if f'id="{m}"' in resp.text] == ["v1", "v2", "v3"]
    assert resp.text.count('id="v2"') == 1


# ---------------------------------------------------------------------------
# phase 3 — artists and albums derived from the library's tracks.
#
# Measured against a real database before this work started (2026-08-27): 303
# tracks, 302 of them with a real album name, 111 artists, 239 albums. The tabs
# only make sense with numbers like that — on bare singles they would degrade
# into a list of three hundred one-song "albums".
#
# The Cyrillic names below are deliberate: <index> letters and sorting for
# non-Latin artists are real cases this server has to get right.
# ---------------------------------------------------------------------------

import xml.etree.ElementTree as ET

_NS = {"s": subsonic.NAMESPACE}


def _xml(response):
    assert response.status_code == 200
    return ET.fromstring(response.text)


def _fill(lib, *rows):
    """rows — (id, title, artist, album, duration)."""
    for song_id, title, artist, album, duration in rows:
        lib.upsert_song(song_id, title, artist, album, duration,
                        f"https://example.invalid/{song_id}.jpg")


@pytest.fixture
def library_songs(lib):
    subsonic._search_cache.clear()
    _fill(lib,
          ("vidAAA1", "One More Time", "Daft Punk", "Discovery", 320),
          ("vidAAA2", "Aerodynamic", "Daft Punk", "Discovery", 212),
          ("vidAAA3", "Get Lucky", "Daft Punk", "Random Access Memories", 369),
          ("vidBBB1", "Все хорошо", "Монеточка", "Раскраски для взрослых", 180),
          ("vidCCC1", "Одинокая песня", "Кто-то", None, 200))
    yield lib
    subsonic._search_cache.clear()


def test_get_artists_groups_the_library_by_first_letter(library_songs):
    root = _xml(client.get("/rest/getArtists.view", params=token_params()))
    indexes = root.findall(".//s:artists/s:index", _NS)
    assert [i.get("name") for i in indexes] == ["D", "К", "М"]

    daft = root.find(".//s:artist[@name='Daft Punk']", _NS)
    assert daft.get("albumCount") == "2"   # Discovery + Random Access Memories
    assert daft.get("id") == subsonic._artist_id("Daft Punk")
    assert daft.get("coverArt") == "vidAAA1"


def test_get_artists_ignores_the_search_cache(library_songs):
    """The list has to be stable. If the search cache were mixed into it,
    artists would appear from recent queries and disappear after a restart —
    and Amperfy reads a disappearance as a deletion and purges its database."""
    subsonic._cache_put("vidZZZ1", {"title": "Nope", "artist": "Random Query",
                                    "album": "Nope", "duration": 100})
    root = _xml(client.get("/rest/getArtists.view", params=token_params()))
    assert root.find(".//s:artist[@name='Random Query']", _NS) is None


def test_album_list2_pages_honestly_and_ends(library_songs):
    """Amperfy pages until a page comes back empty. Lying about offset means
    either losing the tail of the library or looping requests forever."""
    first = _xml(client.get("/rest/getAlbumList2.view", params={
        **token_params(), "type": "alphabeticalByName", "size": "2", "offset": "0"}))
    second = _xml(client.get("/rest/getAlbumList2.view", params={
        **token_params(), "type": "alphabeticalByName", "size": "2", "offset": "2"}))
    past_end = _xml(client.get("/rest/getAlbumList2.view", params={
        **token_params(), "type": "alphabeticalByName", "size": "2", "offset": "99"}))

    def names(root):
        return [a.get("name") for a in root.findall(".//s:album", _NS)]

    assert names(first) == ["Discovery", "Random Access Memories"]
    assert names(second) == ["Раскраски для взрослых"]
    assert names(past_end) == []


def test_album_list2_newest_sorts_by_when_the_track_arrived(library_songs):
    library_songs._conn.execute(
        "UPDATE songs SET added_at = ? WHERE id = ?",
        ("2020-01-01T00:00:00.000Z", "vidAAA1"))
    library_songs._conn.execute(
        "UPDATE songs SET added_at = ? WHERE id = ?",
        ("2020-01-01T00:00:00.000Z", "vidAAA2"))
    library_songs._conn.execute(
        "UPDATE songs SET added_at = ? WHERE id = ?",
        ("2026-08-27T12:00:00.000Z", "vidBBB1"))
    library_songs._conn.commit()

    root = _xml(client.get("/rest/getAlbumList2.view", params={
        **token_params(), "type": "newest", "size": "10"}))
    assert root.findall(".//s:album", _NS)[-1].get("name") == "Discovery"


def test_album_list2_starred_keeps_albums_with_a_starred_song(library_songs):
    library_songs.star("vidAAA3", "Get Lucky", "Daft Punk", "Random Access Memories", 369)
    root = _xml(client.get("/rest/getAlbumList2.view", params={
        **token_params(), "type": "starred", "size": "10"}))
    assert [a.get("name") for a in root.findall(".//s:album", _NS)] == \
        ["Random Access Memories"]


def test_album_id_of_a_song_matches_the_album_in_the_list(library_songs):
    """The one pairing that must never drift apart: a <song>'s albumId is
    computed from (artist, album), and the grouping has to compute it the same
    way. Otherwise the album on the tab and the album reached from the track
    are two different entities, and song-to-album navigation leads nowhere."""
    song = _xml(client.get("/rest/getSong.view",
                           params={**token_params(), "id": "vidAAA1"}))
    album_id = song.find("s:song", _NS).get("albumId")

    listed = _xml(client.get("/rest/getAlbumList2.view",
                             params={**token_params(), "size": "50"}))
    assert album_id in [a.get("id") for a in listed.findall(".//s:album", _NS)]

    opened = _xml(client.get("/rest/getAlbum.view",
                             params={**token_params(), "id": album_id}))
    assert opened.find("s:album", _NS).get("songCount") == "2"


def test_single_without_an_album_falls_back_to_its_title(library_songs):
    """A single has album NULL in the database, and without an album
    relationship Amperfy hides the track entirely (§6). The substitution must
    be identical in both places: such an album is not listed, but it must still
    open by its own id."""
    song = _xml(client.get("/rest/getSong.view",
                           params={**token_params(), "id": "vidCCC1"}))
    assert song.find("s:song", _NS).get("albumId") == \
        subsonic._album_id("Кто-то", "Одинокая песня")

    opened = _xml(client.get("/rest/getAlbum.view", params={
        **token_params(), "id": subsonic._album_id("Кто-то", "Одинокая песня")}))
    assert opened.find("s:album", _NS).get("songCount") == "1"


def test_get_album_returns_library_songs(library_songs):
    """Phase 1 knew only the search cache, so after a restart the album came
    back empty. The source now survives a restart."""
    album_id = subsonic._album_id("Daft Punk", "Discovery")
    root = _xml(client.get("/rest/getAlbum.view",
                           params={**token_params(), "id": album_id}))
    album = root.find("s:album", _NS)
    assert album.get("songCount") == "2"
    assert album.get("duration") == str(320 + 212)
    assert {s.get("id") for s in album.findall("s:song", _NS)} == {"vidAAA1", "vidAAA2"}


def test_a_song_in_both_library_and_cache_is_counted_once(library_songs):
    subsonic._cache_put("vidAAA1", {"title": "One More Time", "artist": "Daft Punk",
                                    "album": "Discovery", "duration": 320})
    album_id = subsonic._album_id("Daft Punk", "Discovery")
    root = _xml(client.get("/rest/getAlbum.view",
                           params={**token_params(), "id": album_id}))
    assert root.find("s:album", _NS).get("songCount") == "2"


def test_get_artist_lists_its_albums_without_songs(library_songs):
    artist_id = subsonic._artist_id("Daft Punk")
    root = _xml(client.get("/rest/getArtist.view",
                           params={**token_params(), "id": artist_id}))
    artist = root.find("s:artist", _NS)
    assert artist.get("name") == "Daft Punk"
    assert artist.get("albumCount") == "2"
    assert artist.findall("s:album/s:song", _NS) == []


def test_get_artist_answers_ok_for_unknown_id(lib):
    """Same reason as getAlbum: an error makes Amperfy retry."""
    subsonic._search_cache.clear()
    resp = client.get("/rest/getArtist.view",
                      params={**token_params(), "id": "ar-deadbeefdeadbeef"})
    assert resp.status_code == 200
    assert 'status="ok"' in resp.text
    assert 'albumCount="0"' in resp.text


def test_album_list2_hides_a_single_named_after_its_own_song(library_songs):
    """A single-song release named after the song itself is a duplicate of the
    track on the tab. 73 such tiles out of 240 in a real library."""
    _fill(library_songs, ("vidDDD1", "Шприц", "Йорш", "Шприц", 202))

    listed = _xml(client.get("/rest/getAlbumList2.view",
                             params={**token_params(), "size": "50"}))
    assert "Шприц" not in [a.get("name") for a in listed.findall(".//s:album", _NS)]


def test_a_hidden_single_is_still_reachable_from_its_song(library_songs):
    """Only browsing is hidden. Break albumId resolution and Amperfy stops
    showing the track itself: with no album relationship it fails the
    predicate (§6)."""
    _fill(library_songs, ("vidDDD1", "Шприц", "Йорш", "Шприц", 202))

    song = _xml(client.get("/rest/getSong.view",
                           params={**token_params(), "id": "vidDDD1"}))
    album_id = song.find("s:song", _NS).get("albumId")
    opened = _xml(client.get("/rest/getAlbum.view",
                             params={**token_params(), "id": album_id}))
    assert opened.find("s:album", _NS).get("songCount") == "1"


def test_album_list2_keeps_a_single_track_album_with_a_different_name(library_songs):
    """"One song" on its own is no reason to hide: 302 tracks out of 303 carry
    a real release name from YouTube, and only name duplicates are meant to go,
    not every single-track album."""
    listed = _xml(client.get("/rest/getAlbumList2.view",
                             params={**token_params(), "size": "50"}))
    names = [a.get("name") for a in listed.findall(".//s:album", _NS)]
    assert "Раскраски для взрослых" in names   # 1 song, different name — stays
    assert "Одинокая песня" not in names       # single with no album — substituted
