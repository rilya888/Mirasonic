import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient
from yt_dlp.utils import DownloadError

import main


# ---------------------------------------------------------------------------
# fixture builders — compact, hand-built InnerTube search JSON shapes
# ---------------------------------------------------------------------------

def flex_column(runs):
    return {"musicResponsiveListItemFlexColumnRenderer": {"text": {"runs": runs}}}


def fixed_column(text):
    return {"musicResponsiveListItemFixedColumnRenderer": {"text": {"runs": [{"text": text}]}}}


def list_item(id_, title, artist, music_video_type="MUSIC_VIDEO_TYPE_ATV",
              artwork_url=None, duration=None, include_artist=True, album=None):
    columns = [
        flex_column([{
            "text": title,
            "navigationEndpoint": {
                "watchEndpoint": {
                    "videoId": id_,
                    "watchEndpointMusicSupportedConfigs": {
                        "watchEndpointMusicConfig": {"musicVideoType": music_video_type}
                    },
                }
            },
        }])
    ]
    artist_run = {"text": artist}
    if include_artist:
        artist_run["navigationEndpoint"] = {
            "browseEndpoint": {
                "browseEndpointContextSupportedConfigs": {
                    "browseEndpointContextMusicConfig": {"pageType": "MUSIC_PAGE_TYPE_ARTIST"}
                }
            }
        }
    row = [{"text": "Song"}, {"text": " • "}, artist_run]
    if album:
        album_run = {
            "text": album,
            "navigationEndpoint": {
                "browseEndpoint": {
                    "browseEndpointContextSupportedConfigs": {
                        "browseEndpointContextMusicConfig": {"pageType": "MUSIC_PAGE_TYPE_ALBUM"}
                    }
                }
            },
        }
        row += [{"text": " • "}, album_run]
    columns.append(flex_column(row))

    renderer = {"playlistItemData": {"videoId": id_}, "flexColumns": columns}
    if artwork_url:
        renderer["thumbnail"] = {
            "musicThumbnailRenderer": {
                "thumbnail": {
                    "thumbnails": [
                        {"url": "https://example.test/small.jpg", "width": 60, "height": 60},
                        {"url": artwork_url, "width": 120, "height": 120},
                    ]
                }
            }
        }
    if duration:
        renderer["fixedColumns"] = [fixed_column(duration)]
    return {"musicResponsiveListItemRenderer": renderer}


def search_fixture(items):
    return {
        "contents": {
            "tabbedSearchResultsRenderer": {
                "tabs": [{
                    "tabRenderer": {
                        "content": {
                            "sectionListRenderer": {
                                "contents": [{"musicShelfRenderer": {"contents": items}}]
                            }
                        }
                    }
                }]
            }
        }
    }


# ---------------------------------------------------------------------------
# duration parsing
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("5:20", 320),
    ("0:05", 5),
    ("1:02:03", 3723),
])
def test_parse_duration_valid(text, expected):
    assert main.parse_duration(text) == expected


@pytest.mark.parametrize("text", [
    "abc", "1:60", "12:34:99", "", "125", "1:2:3:4", "-1:20", "1:2:",
])
def test_parse_duration_rejects_garbage(text):
    assert main.parse_duration(text) is None


# ---------------------------------------------------------------------------
# search response parsing
# ---------------------------------------------------------------------------

def test_parse_search_response_extracts_full_track():
    item = list_item(
        "track-1", "One More Time", "Daft Punk", album="Discovery",
        artwork_url="https://example.test/art.jpg", duration="3:20",
    )
    tracks = main.parse_search_response(search_fixture([item]), limit=10)
    assert tracks == [{
        "id": "track-1",
        "title": "One More Time",
        "artist": "Daft Punk",
        "album": "Discovery",
        "artworkURL": "https://example.test/art.jpg",
        "durationSeconds": 200,
    }]


def test_parse_search_response_album_is_none_for_singles():
    """No MUSIC_PAGE_TYPE_ALBUM run at all (a single) — _parse_item itself
    stays honest and reports None; subsonic.py is the one that must not let
    an empty album reach Amperfy (see test_subsonic.py)."""
    item = list_item("track-2", "Loner", "Artist")  # no album= passed
    tracks = main.parse_search_response(search_fixture([item]), limit=10)
    assert tracks[0]["album"] is None


def test_parse_search_response_drops_non_atv_items():
    atv = list_item("song-1", "Song", "Artist")
    omv = list_item("video-1", "Video", "Artist", music_video_type="MUSIC_VIDEO_TYPE_OMV")
    tracks = main.parse_search_response(search_fixture([omv, atv]), limit=10)
    assert [t["id"] for t in tracks] == ["song-1"]


def test_parse_search_response_drops_items_without_artist():
    item = list_item("song-2", "Song", "Artist", include_artist=False)
    tracks = main.parse_search_response(search_fixture([item]), limit=10)
    assert tracks == []


def test_parse_search_response_respects_limit():
    items = [list_item(f"song-{i}", f"Song {i}", "Artist") for i in range(5)]
    tracks = main.parse_search_response(search_fixture(items), limit=2)
    assert len(tracks) == 2


def test_parse_search_response_reads_item_section_shelf():
    item = list_item("song-3", "Song", "Artist")
    data = {
        "contents": {
            "tabbedSearchResultsRenderer": {
                "tabs": [{
                    "tabRenderer": {
                        "content": {
                            "sectionListRenderer": {
                                "contents": [{"itemSectionRenderer": {"contents": [item]}}]
                            }
                        }
                    }
                }]
            }
        }
    }
    tracks = main.parse_search_response(data, limit=10)
    assert [t["id"] for t in tracks] == ["song-3"]


# ---------------------------------------------------------------------------
# /search endpoint
# ---------------------------------------------------------------------------

def test_search_rejects_empty_query():
    resp = TestClient(main.app).get("/search", params={"q": "   "})
    assert resp.status_code == 400
    assert resp.json() == {"error": "empty_query"}


def test_search_rejects_missing_query():
    resp = TestClient(main.app).get("/search")
    assert resp.status_code == 400
    assert resp.json() == {"error": "empty_query"}


# ---------------------------------------------------------------------------
# InnerTube config cache
# ---------------------------------------------------------------------------

def test_innertube_config_is_cached(monkeypatch):
    monkeypatch.setattr(main, "_config_cache", None)
    html = (
        '"INNERTUBE_API_KEY":"KEY123",'
        '"INNERTUBE_CLIENT_NAME":"WEB_REMIX",'
        '"INNERTUBE_CLIENT_VERSION":"1.20260101.00.00"'
    )
    calls = {"count": 0}

    class FakeResponse:
        text = html

        def raise_for_status(self):
            pass

    class FakeConfigClient:
        async def get(self, *args, **kwargs):
            calls["count"] += 1
            return FakeResponse()

    client = FakeConfigClient()
    config1 = asyncio.run(main.get_innertube_config(client))
    config2 = asyncio.run(main.get_innertube_config(client))

    assert config1 == config2 == {
        "INNERTUBE_API_KEY": "KEY123",
        "INNERTUBE_CLIENT_NAME": "WEB_REMIX",
        "INNERTUBE_CLIENT_VERSION": "1.20260101.00.00",
    }
    assert calls["count"] == 1  # second call must not hit the network


# /search — the songs filter and paging
# /search — the songs filter and paging
# ---------------------------------------------------------------------------

def shelf_with_continuation(items, token):
    return {
        "contents": {"tabbedSearchResultsRenderer": {"tabs": [{"tabRenderer": {"content": {
            "sectionListRenderer": {"contents": [{"musicShelfRenderer": {
                "contents": items,
                "continuations": [{"nextContinuationData": {"continuation": token}}],
            }}]}
        }}}]}}
    }


def test_parse_search_page_returns_continuation_token():
    data = shelf_with_continuation([list_item("a1", "T", "A")], "TOKEN123")
    tracks, token = main.parse_search_page(data, 20)
    assert [t["id"] for t in tracks] == ["a1"]
    assert token == "TOKEN123"


def test_parse_search_page_reads_continuation_shape():
    """A continuation arrives in a different wrapper than the first page."""
    data = {"continuationContents": {"musicShelfContinuation": {
        "contents": [list_item("b1", "T", "A")],
        "continuations": [{"nextContinuationData": {"continuation": "NEXT"}}],
    }}}
    tracks, token = main.parse_search_page(data, 20)
    assert [t["id"] for t in tracks] == ["b1"]
    assert token == "NEXT"


def test_parse_search_page_drops_repeats_of_the_same_track():
    """YouTube puts one track in the "top result" card and in the song list.

    Measured 2026-08-27 on one query: 78 positions across 5 pages against 54
    unique ones — every duplicate ate a slot in the window the client asked for.
    """
    items = [list_item("a1", "T", "A"), list_item("a1", "T", "A"), list_item("a2", "T2", "A")]
    tracks, _ = main.parse_search_page(shelf_with_continuation(items, "TOKEN"), 20)
    assert [t["id"] for t in tracks] == ["a1", "a2"]


def test_parse_search_page_drops_token_when_page_is_truncated():
    """Otherwise a client following the token would skip the truncated tail."""
    items = [list_item(f"id{i}", f"T{i}", "A") for i in range(5)]
    tracks, token = main.parse_search_page(shelf_with_continuation(items, "TOKEN"), 2)
    assert len(tracks) == 2
    assert token is None


class FakeSearchClient:
    """Captures the body and query parameters of the InnerTube request."""
    captured_body = None
    captured_params = None
    payload = {"contents": {}}

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, *args, **kwargs):
        class R:
            text = ('"INNERTUBE_API_KEY":"K","INNERTUBE_CLIENT_NAME":"WEB_REMIX",'
                    '"INNERTUBE_CLIENT_VERSION":"1.0"')

            def raise_for_status(self):
                pass
        return R()

    async def post(self, url, params=None, json=None, headers=None):
        FakeSearchClient.captured_body = json
        FakeSearchClient.captured_params = params
        payload = FakeSearchClient.payload

        class R:
            def raise_for_status(self):
                pass

            def json(self):
                return payload
        return R()


def test_search_sends_songs_filter(monkeypatch):
    monkeypatch.setattr(main, "_config_cache", None)
    monkeypatch.setattr(main.httpx, "AsyncClient", FakeSearchClient)
    FakeSearchClient.payload = shelf_with_continuation([list_item("a1", "T", "A")], "TOK")

    resp = TestClient(main.app).get("/search?q=green+day")

    assert resp.status_code == 200
    assert FakeSearchClient.captured_body["params"] == main.SEARCH_PARAMS_SONGS
    assert FakeSearchClient.captured_body["query"] == "green day"
    assert resp.json()["continuation"] == "TOK"


def test_search_with_continuation_omits_query(monkeypatch):
    monkeypatch.setattr(main, "_config_cache", None)
    monkeypatch.setattr(main.httpx, "AsyncClient", FakeSearchClient)
    FakeSearchClient.payload = {"continuationContents": {"musicShelfContinuation": {
        "contents": [list_item("b1", "T", "A")]}}}

    resp = TestClient(main.app).get("/search?continuation=TOK")

    assert resp.status_code == 200
    assert FakeSearchClient.captured_params["continuation"] == "TOK"
    assert "query" not in FakeSearchClient.captured_body
    assert resp.json()["tracks"][0]["id"] == "b1"
    assert resp.json()["continuation"] is None


def test_search_still_rejects_empty_query_without_continuation():
    resp = TestClient(main.app).get("/search?q=+")
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# /stream endpoint — Range forwarding and error mapping
# ---------------------------------------------------------------------------

class FakeAsyncClient:
    """Serves the whole "file" in one response — which is how the server fetches now."""
    captured_headers = None
    body = b"0123456789"

    def __init__(self, *args, **kwargs):
        pass

    async def get(self, url, headers=None, timeout=None):
        FakeAsyncClient.captured_headers = headers
        return httpx.Response(206, content=FakeAsyncClient.body,
                              headers={"content-range": "bytes 0-9/10"})

    async def aclose(self):
        pass


class FailingAsyncClient(FakeAsyncClient):
    async def get(self, url, headers=None, timeout=None):
        raise httpx.ConnectError("boom")


def _no_remux(monkeypatch):
    """ffmpeg is not needed in unit tests: the live test covers repackaging."""
    async def identity(data):
        return data
    monkeypatch.setattr(main, "_remux_to_adts", identity)


def test_upstream_client_is_reused_across_requests():
    """The point of the pool: a player pulls a track as a dozen range requests,
    and a fresh client for each would pay for a TLS handshake every time."""
    main._upstream_client = None
    first = main.get_upstream_client()
    second = main.get_upstream_client()
    assert first is second
    asyncio.run(first.aclose())
    main._upstream_client = None


def test_stream_slices_locally_and_always_pulls_the_whole_file(monkeypatch):
    """The client's range is sliced from bytes already in memory, while upstream
    always gets `bytes=0-`: without Range googlevideo serves ~32 KB/s instead
    of 5 MB in 0.15 s."""
    async def fake_get_stream_url(video_id):
        return "https://googlevideo.example/videoplayback?expire=9999999999"

    monkeypatch.setattr(main, "get_stream_url", fake_get_stream_url)
    monkeypatch.setattr(main, "get_upstream_client", lambda: FakeAsyncClient())
    _no_remux(monkeypatch)

    resp = TestClient(main.app).get("/stream/abc123", headers={"Range": "bytes=4-6"})

    assert resp.status_code == 206
    assert resp.headers["content-type"] == "audio/aac"
    assert resp.headers["content-range"] == "bytes 4-6/10"
    assert resp.headers["accept-ranges"] == "bytes"
    assert resp.content == b"456"
    assert FakeAsyncClient.captured_headers == {"Range": "bytes=0-"}


def test_stream_maps_unavailable_download_error_to_404(monkeypatch):
    async def fake_get_stream_url(video_id):
        raise DownloadError("ERROR: [youtube] abc123: Video unavailable")

    monkeypatch.setattr(main, "get_stream_url", fake_get_stream_url)
    resp = TestClient(main.app).get("/stream/abc123")
    assert resp.status_code == 404
    assert resp.json() == {"error": "not_found"}


def test_stream_maps_other_download_error_to_502(monkeypatch):
    async def fake_get_stream_url(video_id):
        raise DownloadError("ERROR: no formats found")

    monkeypatch.setattr(main, "get_stream_url", fake_get_stream_url)
    resp = TestClient(main.app).get("/stream/abc123")
    assert resp.status_code == 502
    assert resp.json() == {"error": "upstream"}


def test_stream_maps_upstream_connect_failure_to_502(monkeypatch):
    async def fake_get_stream_url(video_id):
        return "https://googlevideo.example/videoplayback?expire=9999999999"

    monkeypatch.setattr(main, "get_stream_url", fake_get_stream_url)
    monkeypatch.setattr(main, "get_upstream_client", lambda: FailingAsyncClient())
    _no_remux(monkeypatch)

    resp = TestClient(main.app).get("/stream/abc123")
    assert resp.status_code == 502
    assert resp.json() == {"error": "upstream"}


# ---------------------------------------------------------------------------
# live tests — real network, real YouTube Music. Run with `pytest -m live`.
# ---------------------------------------------------------------------------

def test_stream_maps_age_gate_to_404(monkeypatch):
    async def fake_get_stream_url(video_id):
        raise DownloadError(
            "ERROR: [youtube] abc123: Sign in to confirm your age. "
            "Use --cookies-from-browser or --cookies for the authentication."
        )

    monkeypatch.setattr(main, "get_stream_url", fake_get_stream_url)
    resp = TestClient(main.app).get("/stream/abc123")
    assert resp.status_code == 404
    assert resp.json() == {"error": "not_found"}


def test_stream_maps_bot_check_to_502_not_404(monkeypatch):
    """The anti-bot check is temporary: a retry may work, a 404 would lie."""
    async def fake_get_stream_url(video_id):
        raise DownloadError(
            "ERROR: [youtube] abc123: Sign in to confirm you're not a bot."
        )

    monkeypatch.setattr(main, "get_stream_url", fake_get_stream_url)
    resp = TestClient(main.app).get("/stream/abc123")
    assert resp.status_code == 502
    assert resp.json() == {"error": "upstream"}


def test_prefetch_warms_cache_and_returns_204(monkeypatch):
    calls = []

    async def fake_get_stream_url(video_id):
        calls.append(video_id)
        return "https://example.test/media"

    monkeypatch.setattr(main, "get_stream_url", fake_get_stream_url)
    resp = TestClient(main.app).get("/prefetch/abc123")
    assert resp.status_code == 204
    assert resp.content == b""
    assert calls == ["abc123"]


def test_prefetch_maps_age_gate_to_404(monkeypatch):
    async def fake_get_stream_url(video_id):
        raise DownloadError("ERROR: [youtube] abc123: Sign in to confirm your age.")

    monkeypatch.setattr(main, "get_stream_url", fake_get_stream_url)
    resp = TestClient(main.app).get("/prefetch/abc123")
    assert resp.status_code == 404


def test_prefetch_maps_other_download_error_to_502(monkeypatch):
    async def fake_get_stream_url(video_id):
        raise DownloadError("ERROR: no formats found")

    monkeypatch.setattr(main, "get_stream_url", fake_get_stream_url)
    resp = TestClient(main.app).get("/prefetch/abc123")
    assert resp.status_code == 502


def test_prefetch_then_stream_does_not_resolve_twice(monkeypatch):
    """The point of prefetch: the second call is served from cache, no yt-dlp."""
    main._stream_cache.clear()
    resolves = []

    def fake_resolve_sync(video_id):
        resolves.append(video_id)
        return "https://example.test/media?expire=99999999999"

    monkeypatch.setattr(main, "_resolve_stream_sync", fake_resolve_sync)
    client = TestClient(main.app)
    assert client.get("/prefetch/abc123").status_code == 204
    # the second call is served from cache: yt-dlp is not invoked again
    assert client.get("/prefetch/abc123").status_code == 204
    assert resolves == ["abc123"]
    main._stream_cache.clear()


def test_stream_refused_url_is_evicted_and_resolved_again(monkeypatch):
    """2026-09-29: googlevideo 403'd a cached URL and every retry reused it."""
    main._stream_cache.clear()
    main._adts_cache.clear()
    urls = iter(["https://gv.example/dead?expire=99999999999",
                 "https://gv.example/good?expire=99999999999"])
    monkeypatch.setattr(main, "_resolve_stream_sync", lambda video_id: next(urls))

    class RefusesDead(FakeAsyncClient):
        async def get(self, url, headers=None, timeout=None):
            if "dead" in url:
                return httpx.Response(403)
            return await super().get(url, headers, timeout)

    monkeypatch.setattr(main, "get_upstream_client", lambda: RefusesDead())
    _no_remux(monkeypatch)

    resp = TestClient(main.app).get("/stream/abc123")

    assert resp.status_code == 200
    assert main._stream_cache["abc123"]["url"].endswith("good?expire=99999999999")
    main._stream_cache.clear()
    main._adts_cache.clear()


@pytest.mark.live
def test_live_search_returns_tracks_with_artist():
    resp = TestClient(main.app).get("/search", params={"q": "Daft Punk One More Time"})
    assert resp.status_code == 200
    tracks = resp.json()["tracks"]
    assert len(tracks) > 0
    assert all(t["id"] and t["title"] and t["artist"] for t in tracks)


@pytest.mark.live
def test_live_stream_range_returns_adts_frames():
    """Live check of the whole chain: resolve, download, ffmpeg, slicing.

    0xFFF is the ADTS frame sync word. If a fragmented mp4 had survived, the
    response would start with an ftyp atom and clients would crash on seek.
    """
    search_resp = TestClient(main.app).get("/search", params={"q": "Daft Punk One More Time"})
    video_id = search_resp.json()["tracks"][0]["id"]

    stream_resp = TestClient(main.app).get(
        f"/stream/{video_id}", headers={"Range": "bytes=0-262143"}
    )
    assert stream_resp.status_code == 206
    assert stream_resp.headers["content-type"] == "audio/aac"
    assert len(stream_resp.content) == 262144
    head = stream_resp.content
    assert head[0] == 0xFF and head[1] & 0xF0 == 0xF0
    assert b"ftyp" not in head[:64]


def test_download_gives_up_after_stalled_reads(monkeypatch):
    class StalledClient:
        calls = 0

        async def get(self, url, **kwargs):
            self.calls += 1
            assert kwargs["timeout"] is main.DOWNLOAD_TIMEOUT
            raise httpx.ReadTimeout("stalled")

    client = StalledClient()
    monkeypatch.setattr(main, "get_upstream_client", lambda: client)
    with pytest.raises(main.UpstreamError):
        asyncio.run(main._download("https://example.invalid/track"))
    assert client.calls == main.DOWNLOAD_ATTEMPTS
