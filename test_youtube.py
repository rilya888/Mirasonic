"""The single upstream layer and the endpoints built on it.

InnerTube fixtures are cut down from live responses captured 2026-11-05
(docs/PITFALLS.md #3); `-m live` runs the same paths against the real service.
"""

import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient
from yt_dlp.utils import DownloadError

import main
import subsonic
from test_subsonic import client, lib, token_params  # noqa: F401  (lib is a fixture)

CONFIG_HTML = ('"INNERTUBE_API_KEY":"K","INNERTUBE_CLIENT_NAME":"WEB_REMIX",'
               '"INNERTUBE_CLIENT_VERSION":"1.0"')


@pytest.fixture(autouse=True)
def _config_and_no_sleep(monkeypatch, request):
    pauses = []
    if request.node.get_closest_marker("live"):
        return pauses
    subsonic._search_cache.clear()
    monkeypatch.setattr(main, "_config_cache", {
        "INNERTUBE_API_KEY": "K", "INNERTUBE_CLIENT_NAME": "WEB_REMIX",
        "INNERTUBE_CLIENT_VERSION": "1.0"})

    async def sleep(seconds):
        pauses.append(seconds)

    monkeypatch.setattr(main.asyncio, "sleep", sleep)
    return pauses


def mock(handler):
    """Routes the shared pool through `handler`; returns the request log."""
    seen = []

    def wrapped(request):
        seen.append(request)
        return handler(request)

    main._upstream_client = httpx.AsyncClient(transport=httpx.MockTransport(wrapped))
    return seen


def scripted(*statuses):
    remaining = list(statuses)
    return lambda request: httpx.Response(remaining.pop(0), json={"ok": True})


# --- upstream(): retries ------------------------------------------------------

@pytest.mark.parametrize("status", [429, 500, 503])
def test_upstream_retries_with_a_pause(status, _config_and_no_sleep):
    seen = mock(scripted(status, 200))
    response = asyncio.run(main.upstream("get", "https://x/"))
    assert response.status_code == 200
    assert len(seen) == 2
    assert _config_and_no_sleep == [main.UPSTREAM_PAUSE]


def test_upstream_backs_off_and_gives_up(_config_and_no_sleep):
    seen = mock(scripted(503, 503, 503))
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(main.upstream("get", "https://x/"))
    assert len(seen) == main.UPSTREAM_ATTEMPTS
    assert _config_and_no_sleep == [main.UPSTREAM_PAUSE, main.UPSTREAM_PAUSE * 2]


def test_upstream_does_not_retry_other_client_errors():
    seen = mock(scripted(404))
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(main.upstream("get", "https://x/"))
    assert len(seen) == 1


def test_retry_after_is_honoured_up_to_the_cap(_config_and_no_sleep):
    answers = [httpx.Response(429, headers={"Retry-After": "1"}),
               httpx.Response(429, headers={"Retry-After": "3600"}),
               httpx.Response(200)]
    mock(lambda request: answers.pop(0))
    asyncio.run(main.upstream("get", "https://x/"))
    assert _config_and_no_sleep == [1.0, main.UPSTREAM_MAX_PAUSE]


def test_upstream_sends_the_timeout():
    seen = mock(scripted(200))
    asyncio.run(main.upstream("get", "https://x/"))
    assert seen[0].extensions["timeout"]["read"] == main.UPSTREAM_TIMEOUT.read


def test_upstream_deadline_covers_a_hung_call():
    class Hung:
        async def get(self, url, **kwargs):
            await asyncio.Event().wait()

    with pytest.raises(TimeoutError):
        asyncio.run(main.upstream("get", "https://x/", client=Hung(), deadline=0.01))


# --- innertube(): cache and coalescing ----------------------------------------

def test_innertube_builds_the_request():
    seen = mock(lambda request: httpx.Response(200, json={}))
    asyncio.run(main.innertube("browse", browseId="B"))
    assert seen[0].url.path == "/youtubei/v1/browse"
    assert seen[0].url.params["key"] == "K"
    body = json.loads(seen[0].content)
    assert body["browseId"] == "B"
    assert body["context"]["client"]["clientName"] == "WEB_REMIX"


def test_innertube_cache_holds_until_the_ttl(monkeypatch):
    seen = mock(lambda request: httpx.Response(200, json={"n": 1}))
    now = [1000.0]
    monkeypatch.setattr(main.time, "monotonic", lambda: now[0])

    async def twice():
        await main.innertube("browse", browseId="B")
        await main.innertube("browse", browseId="B")

    asyncio.run(twice())
    assert len(seen) == 1
    asyncio.run(main.innertube("browse", browseId="other"))
    assert len(seen) == 2
    now[0] += main.CACHE_TTL_SECONDS + 1
    asyncio.run(main.innertube("browse", browseId="B"))
    assert len(seen) == 3


def test_innertube_cache_is_bounded():
    mock(lambda request: httpx.Response(200, json={}))

    async def fill():
        for n in range(main.CACHE_SIZE + 5):
            await main.innertube("browse", browseId=str(n))

    asyncio.run(fill())
    assert len(main._response_cache) == main.CACHE_SIZE


def test_failures_are_not_cached():
    seen = mock(scripted(404, 200))
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(main.innertube("browse", browseId="B"))
    assert main._response_cache == {}
    assert asyncio.run(main.innertube("browse", browseId="B")) == {"ok": True}
    assert len(seen) == 2


def test_concurrent_identical_calls_share_one_request():
    seen = mock(lambda request: httpx.Response(200, json={"n": 1}))

    async def many():
        return await asyncio.gather(*(main.innertube("browse", browseId="B") for _ in range(10)))

    assert asyncio.run(many()) == [{"n": 1}] * 10
    assert len(seen) == 1
    assert main._inflight == {}


def test_a_failure_reaches_every_waiter_and_clears():
    seen = mock(scripted(404, 200))

    async def many():
        return await asyncio.gather(*(main.innertube("browse", browseId="B") for _ in range(3)),
                                    return_exceptions=True)

    results = asyncio.run(many())
    assert all(isinstance(result, httpx.HTTPStatusError) for result in results)
    assert len(seen) == 1
    assert main._inflight == {}


def test_a_cancelled_caller_does_not_fail_the_others():
    release_event = {}

    async def fetch():
        await release_event["go"].wait()
        return "value"

    async def scenario():
        release_event["go"] = asyncio.Event()
        leader = asyncio.ensure_future(main.coalesce(("k",), fetch))
        follower = asyncio.ensure_future(main.coalesce(("k",), fetch))
        await asyncio.wait([leader], timeout=0)
        leader.cancel()
        await asyncio.wait([leader], timeout=0)
        release_event["go"].set()
        return await follower

    assert asyncio.run(scenario()) == "value"


def test_cold_start_fetches_the_homepage_once(monkeypatch):
    monkeypatch.setattr(main, "_config_cache", None)
    seen = mock(lambda request: httpx.Response(200, text=CONFIG_HTML)
                if request.method == "GET" else httpx.Response(200, json={}))

    async def many():
        await asyncio.gather(*(main.innertube("browse", browseId=str(n)) for n in range(5)))

    asyncio.run(many())
    assert sum(request.method == "GET" for request in seen) == 1


# --- failure semantics of the existing callers --------------------------------

def test_search_still_reports_an_outage():
    """spotify_import tells an outage from an empty result by this."""
    mock(lambda request: httpx.Response(503))
    resp = TestClient(main.app).get("/search?q=x")
    assert resp.status_code == 502


def test_get_song_details_still_never_raises():
    mock(lambda request: httpx.Response(503))
    details = asyncio.run(main.get_song_details("v"))
    assert details["title"] is None and details["duration"] == 0


def test_cover_art_retries_and_keeps_its_errors(lib):
    subsonic._cache_put("v1", {"title": "T", "artist": "A", "artworkURL": "https://lh3/x"})
    seen = mock(scripted(503, 200))
    assert client.get("/rest/getCoverArt.view", params={**token_params(), "id": "v1"}
                      ).status_code == 200
    assert len(seen) == 2

    main._response_cache.clear()
    mock(lambda request: httpx.Response(404))
    resp = client.get("/rest/getCoverArt.view", params={**token_params(), "id": "v1"})
    assert 'code="70"' in resp.text


def test_download_retries_a_server_error_then_refuses():
    seen = mock(scripted(503, 503, 503))
    with pytest.raises(main.UpstreamRefused):
        asyncio.run(main._download("https://googlevideo/x"))
    assert len(seen) == main.UPSTREAM_ATTEMPTS


# --- yt-dlp -------------------------------------------------------------------

def resolver(monkeypatch, *outcomes):
    outcomes = list(outcomes)
    calls = []

    def resolve(video_id):
        calls.append(video_id)
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(main, "_resolve_stream_sync", resolve)
    monkeypatch.setattr(main, "_stream_cache", {})
    return calls


def test_resolve_retries_throttling(monkeypatch):
    calls = resolver(monkeypatch, DownloadError("ERROR: HTTP Error 429: Too Many Requests"),
                     "https://g/?expire=9999999999")
    assert asyncio.run(main.get_stream_url("v")) == "https://g/?expire=9999999999"
    assert len(calls) == 2


def test_resolve_never_retries_the_captcha(monkeypatch):
    calls = resolver(monkeypatch, DownloadError("Sign in to confirm you're not a bot"))
    with pytest.raises(DownloadError):
        asyncio.run(main.get_stream_url("v"))
    assert len(calls) == 1


def test_concurrent_resolves_of_one_track_are_merged(monkeypatch):
    calls = resolver(monkeypatch, "https://g/?expire=9999999999")

    async def both():
        return await asyncio.gather(main.get_stream_url("v"), main.get_stream_url("v"))

    assert len(set(asyncio.run(both()))) == 1
    assert len(calls) == 1


# --- fixtures in the live shape ------------------------------------------------

def artist_run(name):
    return {"text": name, "navigationEndpoint": {"browseEndpoint": {
        "browseId": "UC" + name,
        "browseEndpointContextSupportedConfigs": {
            "browseEndpointContextMusicConfig": {"pageType": "MUSIC_PAGE_TYPE_ARTIST"}}}}}


def album_run(name):
    return {"text": name, "navigationEndpoint": {"browseEndpoint": {
        "browseId": "MPREb_" + name,
        "browseEndpointContextSupportedConfigs": {
            "browseEndpointContextMusicConfig": {"pageType": "MUSIC_PAGE_TYPE_ALBUM"}}}}}


def column(*runs):
    return {"musicResponsiveListItemFlexColumnRenderer": {"text": {"runs": list(runs)}}}


def song_row(video_id, title, artist, plays, album="Album"):
    return {"musicResponsiveListItemRenderer": {
        "flexColumns": [
            column({"text": title, "navigationEndpoint": {"watchEndpoint": {
                "videoId": video_id,
                "watchEndpointMusicSupportedConfigs": {"watchEndpointMusicConfig": {
                    "musicVideoType": "MUSIC_VIDEO_TYPE_ATV"}}}}}),
            column(artist_run(artist)),
            column({"text": plays}) if plays else column(),
            column(album_run(album)),
        ],
        "fixedColumns": [{"musicResponsiveListItemFixedColumnRenderer": {
            "text": {"runs": [{"text": "3:59"}]}}}],
        "playlistItemData": {"videoId": video_id},
        "thumbnail": {"musicThumbnailRenderer": {"thumbnail": {"thumbnails": [
            {"url": "https://lh3/a", "width": 60, "height": 60}]}}},
    }}


def entity_row(name, page_type, browse_id, *more_runs):
    return {"musicResponsiveListItemRenderer": {
        "flexColumns": [column({"text": name}), column(*more_runs)],
        "navigationEndpoint": {"browseEndpoint": {
            "browseId": browse_id,
            "browseEndpointContextSupportedConfigs": {
                "browseEndpointContextMusicConfig": {"pageType": page_type}}}},
    }}


def search_page(*rows):
    return {"contents": {"tabbedSearchResultsRenderer": {"tabs": [{"tabRenderer": {"content": {
        "sectionListRenderer": {"contents": [{"musicShelfRenderer": {"contents": list(rows)}}]}}}}]}}}


ARTIST_PAGE = {"contents": {"singleColumnBrowseResultsRenderer": {"tabs": [{"tabRenderer": {
    "content": {"sectionListRenderer": {"contents": [
        {"musicShelfRenderer": {
            "contents": [song_row("low", "Low", "Band", "1M plays")],
            "bottomEndpoint": {"browseEndpoint": {"browseId": "VLTOP", "params": "ggMCCAI%3D"}},
        }},
        {"musicCarouselShelfRenderer": {"contents": [{"musicTwoRowItemRenderer": {
            "title": {"runs": [{"text": "An Album"}]},
            "navigationEndpoint": {"browseEndpoint": {
                "browseId": "MPREb_x",
                "browseEndpointContextSupportedConfigs": {
                    "browseEndpointContextMusicConfig": {
                        "pageType": "MUSIC_PAGE_TYPE_ALBUM"}}}},
        }}]}},
        {"musicCarouselShelfRenderer": {"contents": [{"musicTwoRowItemRenderer": {
            "title": {"runs": [{"text": name}]},
            "navigationEndpoint": {"browseEndpoint": {
                "browseId": "UC" + name,
                "browseEndpointContextSupportedConfigs": {
                    "browseEndpointContextMusicConfig": {
                        "pageType": "MUSIC_PAGE_TYPE_ARTIST"}}}},
        }} for name in ("Known Band", "Stranger")]}},
        {"musicDescriptionShelfRenderer": {"description": {"runs": [{"text": "Bio\x0b text."}]}}},
    ]}}}}]}}}

TOP_PLAYLIST = {"contents": {"singleColumnBrowseResultsRenderer": {"tabs": [{"tabRenderer": {
    "content": {"sectionListRenderer": {"contents": [{"musicPlaylistShelfRenderer": {"contents": [
        song_row("mid", "Mid", "Band", "31M plays"),
        song_row("big", "Big", "Band", "1.2B plays"),
        song_row("big2", "BIG", "Band", "40K plays"),
        song_row("kilo", "Small", "Band", "1,500 plays"),
        song_row("none", "Mystery", "Band", None),
        song_row("huge", "Huge", "Band", "2.3B plays"),
        song_row("foreign", "Foreign", "Another Act", "9B plays"),
    ]}}]}}}}]}}}

ALBUM_PAGE = {"contents": {"twoColumnBrowseResultsRenderer": {"tabs": [{"tabRenderer": {"content": {
    "sectionListRenderer": {"contents": [{"musicResponsiveHeaderRenderer": {"description": {
        "musicDescriptionShelfRenderer": {"description": {"runs": [{"text": "Album notes."}]}}}}}]}}}}]}}}


def radio_item(video_id, title, artist, album="Radio Album"):
    return {"playlistPanelVideoRenderer": {
        "videoId": video_id,
        "title": {"runs": [{"text": title}]},
        "longBylineText": {"runs": [artist_run(artist), {"text": " • "}, album_run(album),
                                    {"text": " • "}, {"text": "1993"}]},
        "lengthText": {"runs": [{"text": "3:45"}]},
        "thumbnail": {"thumbnails": [{"url": "https://lh3/r", "width": 60, "height": 60}]},
    }}


RADIO = {"contents": {"singleColumnMusicWatchNextResultsRenderer": {"tabbedRenderer": {
    "watchNextTabbedResultsRenderer": {"tabs": [{"tabRenderer": {"content": {"musicQueueRenderer": {
        "content": {"playlistPanelRenderer": {"contents": [
            radio_item("huge", "Huge", "Band"),
            radio_item("r1", "One", "Other"),
            radio_item("r2", "Two", "Another"),
            radio_item("r1", "One", "Other"),
        ]}}}}}}]}}}}}


def youtube(request):
    """A tiny YouTube Music: artist "Band", album "Disc"."""
    body = json.loads(request.content)
    endpoint = request.url.path.rsplit("/", 1)[1]
    if endpoint == "search" and body["params"] == main.SEARCH_PARAMS_ARTISTS:
        return httpx.Response(200, json=search_page(
            entity_row("Band Tribute", "MUSIC_PAGE_TYPE_ARTIST", "UCfake"),
            entity_row("Bánd", "MUSIC_PAGE_TYPE_ARTIST", "UCband")))
    if endpoint == "search" and body["params"] == main.SEARCH_PARAMS_SONGS:
        return httpx.Response(200, json=search_page(song_row("c1", "Song", "Кино", "1M plays")))
    if endpoint == "search" and body["params"] == main.SEARCH_PARAMS_ALBUMS:
        return httpx.Response(200, json=search_page(
            entity_row("Disc", "MUSIC_PAGE_TYPE_ALBUM", "MPREwrong", artist_run("Somebody")),
            entity_row("Disc", "MUSIC_PAGE_TYPE_ALBUM", "MPREdisc", artist_run("Band"))))
    if endpoint == "browse":
        pages = {"UCband": ARTIST_PAGE, "UCКино": ARTIST_PAGE,
                 "VLTOP": TOP_PLAYLIST, "MPREdisc": ALBUM_PAGE}
        return httpx.Response(200, json=pages[body["browseId"]])
    if endpoint == "next":
        return httpx.Response(200, json=RADIO)
    return httpx.Response(404)


def known(video_id="k1", artist="Band", album="Disc", title="Song"):
    subsonic._cache_put(video_id, {"title": title, "artist": artist, "album": album,
                                   "duration": 100, "artworkURL": "https://lh3/k"})


def get(action, **params):
    resp = client.get(f"/rest/{action}.view", params={**token_params(), "f": "json", **params})
    assert resp.status_code == 200
    body = resp.json()["subsonic-response"]
    assert body["status"] == "ok"
    return body


# --- parsers ------------------------------------------------------------------

@pytest.mark.parametrize("text,plays", [
    ("2.3B plays", 2_300_000_000), ("634M plays", 634_000_000), ("56K plays", 56_000),
    ("1,500 plays", 1500), ("1 play", 1), ("Album", 0)])
def test_plays(text, plays):
    row = {"flexColumns": [column({"text": text})]}
    assert main._plays(row) == pytest.approx(plays)


def test_top_songs_are_ranked_by_plays_not_youtube_order():
    rows = main._find_all(TOP_PLAYLIST, "musicResponsiveListItemRenderer")
    assert [t["id"] for t in main.parse_top_songs(rows, "Band")] == [
        "huge", "big", "mid", "kilo", "none"]


def test_top_songs_keep_everything_when_no_row_credits_the_name():
    rows = main._find_all(TOP_PLAYLIST, "musicResponsiveListItemRenderer")
    assert main.parse_top_songs(rows, "Kino")[0]["id"] == "foreign"


def test_radio_drops_seed_and_repeats_and_reads_the_byline():
    tracks = main.parse_radio(RADIO, "huge")
    assert [t["id"] for t in tracks] == ["r1", "r2"]
    assert tracks[0] == {
        "id": "r1", "title": "One", "artist": "Other", "album": "Radio Album",
        "artworkURL": "https://lh3/r", "durationSeconds": 225}


def test_artist_card_spelled_differently_is_found_through_song_rows():
    seen = mock(youtube)
    assert asyncio.run(main.artist_top_songs("Кино"))
    assert any(json.loads(r.content).get("browseId") == "UCКино" for r in seen)


def test_unknown_artist_gets_nothing_rather_than_a_lookalike():
    mock(youtube)
    assert asyncio.run(main.artist_top_songs("Band Tribut")) == []


# --- endpoints ----------------------------------------------------------------

def test_get_top_songs_returns_the_most_played(lib):
    mock(youtube)
    songs = get("getTopSongs", artist="Band", count="2")["topSongs"]["song"]
    assert [s["id"] for s in songs] == ["huge", "big"]
    assert songs[0]["duration"] == 239 and songs[0]["size"] > 0 and songs[0]["album"] == "Album"


def test_returned_songs_are_resolvable_afterwards(lib):
    mock(youtube)
    song = get("getTopSongs", artist="Band")["topSongs"]["song"][0]
    assert subsonic._cache_get(song["id"])["artworkURL"] == "https://lh3/a"
    assert get("getSong", id=song["id"])["song"]["title"] == "Huge"


def test_top_songs_xml_matches_the_schema(lib):
    mock(youtube)
    resp = client.get("/rest/getTopSongs.view", params={**token_params(), "artist": "Band"})
    assert "<topSongs><song " in resp.text.replace(' xmlns="http://subsonic.org/restapi"', "")


@pytest.mark.parametrize("action,container", [
    ("getSimilarSongs", "similarSongs"), ("getSimilarSongs2", "similarSongs2")])
def test_similar_songs_accepts_song_artist_and_album_ids(action, container, lib):
    known()
    seen = mock(youtube)
    assert [s["id"] for s in get(action, id="huge")[container]["song"]] == ["r1", "r2"]
    assert [s["id"] for s in get(action, id="huge", count="1")[container]["song"]] == ["r1"]

    main._response_cache.clear()
    seen.clear()
    assert get(action, id=subsonic._artist_id("Band"))[container]["song"]
    radio = [json.loads(r.content) for r in seen if r.url.path.endswith("/next")]
    assert radio[0]["videoId"] == "huge"  # the artist's top song, not a random one

    seen.clear()
    main._response_cache.clear()
    assert get(action, id=subsonic._album_id("Band", "Disc"))[container]["song"]
    radio = [json.loads(r.content) for r in seen if r.url.path.endswith("/next")]
    assert radio[0]["videoId"] == "k1"


def test_artist_info_filters_to_the_library_by_default(lib):
    known()
    known("k2", artist="Known Band", album="Other Disc", title="Hit")
    mock(youtube)
    info = get("getArtistInfo2", id=subsonic._artist_id("Band"))["artistInfo2"]
    assert info["biography"] == "Bio text."
    assert info["similarArtist"] == [{
        "id": subsonic._artist_id("Known Band"), "name": "Known Band",
        "albumCount": 1, "coverArt": "k2"}]


def test_artist_info_include_not_present_and_count(lib):
    known()
    mock(youtube)
    info = get("getArtistInfo2", id=subsonic._artist_id("Band"),
               includeNotPresent="true")["artistInfo2"]
    assert [a["name"] for a in info["similarArtist"]] == ["Known Band", "Stranger"]
    assert info["similarArtist"][0]["albumCount"] == 0
    assert "coverArt" not in info["similarArtist"][0]
    info = get("getArtistInfo2", id=subsonic._artist_id("Band"), includeNotPresent="true",
               count="1")["artistInfo2"]
    assert len(info["similarArtist"]) == 1


def test_artist_info_xml_is_valid_despite_control_characters(lib):
    import xml.etree.ElementTree as ET
    known()
    mock(youtube)
    resp = client.get("/rest/getArtistInfo2.view",
                      params={**token_params(), "id": subsonic._artist_id("Band")})
    root = ET.fromstring(resp.content)
    assert root[0][0].tag.endswith("biography") and root[0][0].text == "Bio text."


def test_album_info_matches_artist_as_well_as_title(lib):
    known()
    mock(youtube)
    assert get("getAlbumInfo2", id=subsonic._album_id("Band", "Disc"))["albumInfo"] == {
        "notes": "Album notes."}
    assert get("getAlbumInfo2", id="k1")["albumInfo"] == {"notes": "Album notes."}


def albums_page(*rows):
    def handler(request):
        if request.url.path.endswith("/search"):
            return httpx.Response(200, json=search_page(*rows))
        browse_id = json.loads(request.content)["browseId"]
        return httpx.Response(200, json=ALBUM_PAGE if browse_id == "MPREdisc" else {})
    return handler


def test_album_info_finds_an_edition_listed_under_the_plain_title(lib):
    known(album="Disc OKNOTOK 1997 2017")
    mock(albums_page(
        entity_row("Disc", "MUSIC_PAGE_TYPE_ALBUM", "MPREwrong", artist_run("Somebody")),
        entity_row("Discography", "MUSIC_PAGE_TYPE_ALBUM", "MPREwrong", artist_run("Band")),
        entity_row("Disc", "MUSIC_PAGE_TYPE_ALBUM", "MPREdisc", artist_run("Band"))))
    assert get("getAlbumInfo2", id="k1")["albumInfo"] == {"notes": "Album notes."}


def test_album_info_prefers_the_exact_title_over_an_edition(lib):
    known(album="Disc Two")
    mock(albums_page(
        entity_row("Disc", "MUSIC_PAGE_TYPE_ALBUM", "MPREwrong", artist_run("Band")),
        entity_row("Disc Two", "MUSIC_PAGE_TYPE_ALBUM", "MPREdisc", artist_run("Band"))))
    assert get("getAlbumInfo2", id="k1")["albumInfo"] == {"notes": "Album notes."}


ACTIONS = [("getTopSongs", "topSongs", {"song": []}),
           ("getSimilarSongs", "similarSongs", {"song": []}),
           ("getSimilarSongs2", "similarSongs2", {"song": []}),
           ("getArtistInfo2", "artistInfo2", {"similarArtist": []}),
           ("getAlbumInfo2", "albumInfo", {})]


@pytest.mark.parametrize("action,container,empty", ACTIONS)
@pytest.mark.parametrize("failure", [
    lambda request: httpx.Response(503),
    lambda request: httpx.Response(200, text="not json"),
    lambda request: (_ for _ in ()).throw(httpx.ConnectError("down")),
])
def test_upstream_failure_is_an_empty_success(action, container, empty, failure, lib):
    known()
    mock(failure)
    ids = {"getTopSongs": {"artist": "Band"},
           "getArtistInfo2": {"id": subsonic._artist_id("Band")},
           "getAlbumInfo2": {"id": subsonic._album_id("Band", "Disc")}}
    assert get(action, **ids.get(action, {"id": "k1"}))[container] == empty


@pytest.mark.parametrize("action,container,empty", ACTIONS)
def test_changed_page_shape_is_an_empty_success(action, container, empty, lib):
    known()
    mock(lambda request: httpx.Response(200, json={"contents": ["surprise", 1, None]}))
    ids = {"getTopSongs": {"artist": "Band"},
           "getArtistInfo2": {"id": subsonic._artist_id("Band")},
           "getAlbumInfo2": {"id": subsonic._album_id("Band", "Disc")}}
    assert get(action, **ids.get(action, {"id": "k1"}))[container] == empty


@pytest.mark.parametrize("action,container,empty", ACTIONS)
def test_unknown_ids_never_reach_youtube(action, container, empty, lib):
    seen = mock(youtube)
    if action == "getTopSongs":
        return  # takes a name, not an id
    bad = "ar-0000000000000000" if "Album" not in action else "al-0000000000000000"
    assert get(action, id=bad)[container] == empty
    assert seen == []


def test_negative_count_is_empty_not_a_slice_from_the_end(lib):
    mock(youtube)
    assert get("getTopSongs", artist="Band", count="-1")["topSongs"]["song"] == []


def test_a_slow_upstream_is_an_empty_success_within_budget(lib, monkeypatch):
    monkeypatch.setattr(subsonic, "UPSTREAM_BUDGET_SECONDS", 0.01)

    async def hang(name):
        await asyncio.Event().wait()

    monkeypatch.setattr(main, "artist_top_songs", hang)
    assert get("getTopSongs", artist="Band")["topSongs"] == {"song": []}


# --- live ---------------------------------------------------------------------

@pytest.mark.live
def test_live_top_songs_are_the_most_played():
    async def fetch():  # one loop: the pool dies with it (conftest.py)
        return [await main.artist_top_songs(name) for name in (
            "Radiohead", "Beyoncé", "Кино", "zzqx nonexistent artist 1234")]

    radiohead, beyonce, kino, nobody = asyncio.run(fetch())
    assert radiohead[0]["title"] == "Creep"
    assert len(radiohead) > 20
    # YouTube's own order has 1.2B below 31M here, and "Say My Name" is not hers
    titles = [track["title"] for track in beyonce]
    assert titles[0] == "Halo" and "Say My Name" not in titles
    assert kino
    assert nobody == []


@pytest.mark.live
def test_live_radio_and_info():
    async def fetch():
        return (await main.similar_tracks("zFYEYRcjK2g"), await main.artist_info("Radiohead"),
                await main.album_notes("Radiohead", "Pablo Honey"),
                await main.album_notes("Radiohead", "OK Computer OKNOTOK 1997 2017"))

    radio, info, notes, edition = asyncio.run(fetch())
    assert len(radio) > 10
    assert "Abingdon" in info["biography"] and info["similar"]
    assert "Radiohead" in notes
    assert "Radiohead" in edition
