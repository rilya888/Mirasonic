"""Mirasonic worker: anonymous YouTube Music search + stream proxy.

GET /search?q=...&limit=..  -> {"tracks": [...]}
GET /stream/{video_id}      -> proxied audio bytes (Range-aware)

No auth, no DB, no disk writes. Everything cached is a plain in-memory dict.
"""
import asyncio
import json
import logging
import os
import re
import time
import unicodedata
from collections import OrderedDict
from typing import Any, Awaitable, Callable, Optional
from urllib.parse import urlparse, parse_qs

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

logging.basicConfig(level=logging.INFO)
# httpx/httpcore log every request URL at INFO level by default, which would
# leak the signed (ip+sig) googlevideo URL straight into the logs. Only our
# own "worker" logger (videoId + status only) may run at INFO.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger("worker")


class _RedactRestQuery(logging.Filter):
    """Uvicorn's access log prints the full request line, query string and all.

    Subsonic sends the username and password with every single request as
    `u`/`p`/`t`/`s`, so without this filter the credentials end up in
    `docker logs` in plain text — which is exactly what happened on the very
    first deployment. Our own `worker.subsonic` logger only ever prints
    parameter names, but uvicorn keeps its own log and knows nothing about
    that discipline.

    Only `/rest` is redacted: `/search?q=…` and `/stream/{id}` carry no
    secrets, and every measurement in this project was taken against them.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str):
            path = args[2]
            if path.startswith("/rest/") and "?" in path:
                record.args = args[:2] + (path.split("?", 1)[0] + "?<redacted>",) + args[3:]
        return True


logging.getLogger("uvicorn.access").addFilter(_RedactRestQuery())

# Search region. Must match the country the server reaches YouTube from:
# stream resolution happens from the server's real IP, and a mismatch makes
# search return tracks that then fail to resolve.
REGION = os.environ.get("REGION", "US")

HOME_URL = "https://music.youtube.com/"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36"
)
STREAM_EXPIRE_MARGIN_SECONDS = 600  # refresh 10 min before the signed URL expires
# The "Songs" filter for /youtubei/v1/search. Without it a query like
# "green day" comes back as an artist card plus a jumble of clips and albums —
# a 2026-08-26 measurement returned 5 songs. With it: 20 songs per page and a
# continuation token. Same value ytmusicapi uses.
SEARCH_PARAMS_SONGS = "EgWKAQIIAWoMEA4QChADEAQQCRAF"
SEARCH_PARAMS_ARTISTS = "EgWKAQIgAWoMEA4QChADEAQQCRAF"
SEARCH_PARAMS_ALBUMS = "EgWKAQIYAWoMEA4QChADEAQQCRAF"
SEARCH_PAGE_SIZE = 20
YDL_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "skip_download": True,
    "format": "bestaudio[ext=m4a]/bestaudio",
    "socket_timeout": 20,
}
RETRYABLE_RESOLVE = re.compile(r"HTTP Error (429|5\d\d)")
UNAVAILABLE_MARKERS = (
    "unavailable",
    "private",
    "not available",
    "removed",
    "restricted",
    "region",
    "no longer",
    # Age gate: never resolves anonymously, and retrying will not help, so
    # this is a 404 rather than a 502. Deliberately NOT matching "sign in" as
    # a whole — the phrase "Sign in to confirm you're not a bot" means the
    # anti-bot system tripped, which is temporary and must stay a 502.
    "confirm your age",
    "age-restricted",
    "age restricted",
)

app = FastAPI()

# ponytail: plain module-level dicts, no eviction — fine for a single-owner
# personal server; add an LRU cap if this ever runs long enough to matter.
_config_cache: Optional[dict] = None
_stream_cache: dict[str, dict] = {}

# Bounds concurrent /youtubei/v1/player calls (track-duration lookups for the
# Subsonic layer). Pitfall #1 in docs/PITFALLS.md: a queue of requests from one
# address reads as a bot and earns the whole server a captcha.
_player_semaphore = asyncio.Semaphore(8)

# One client for the whole process rather than a fresh one per request. A
# player pulls a track as a dozen separate range requests, and with a
# client-per-request each one paid for a new TLS handshake to googlevideo:
# measured 2026-08-26 at 0.22 s on a cold connection against 0.035 s on a
# reused one. The pool removes that handshake.
_upstream_client: Optional[httpx.AsyncClient] = None


def get_upstream_client() -> httpx.AsyncClient:
    global _upstream_client
    if _upstream_client is None or _upstream_client.is_closed:
        _upstream_client = httpx.AsyncClient(
            timeout=None,
            limits=httpx.Limits(max_keepalive_connections=20, keepalive_expiry=300),
        )
    return _upstream_client


# ---------------------------------------------------------------------------
# The one way out to YouTube Music. Every HTTP call goes through upstream()
# (timeout, overall deadline, retry on 429/5xx with a pause); InnerTube JSON
# additionally goes through innertube() (short cache, identical concurrent calls
# merged). Failures always propagate and are never cached: a remembered outage
# would read as "nothing found", and spotify_import tells the two apart to
# protect --replace. Turning a failure into an empty answer is the caller's call.
# ---------------------------------------------------------------------------
UPSTREAM_TIMEOUT = httpx.Timeout(10)  # per operation, the way httpx counts it
UPSTREAM_DEADLINE = 20  # seconds for one call, retries and pauses included
UPSTREAM_ATTEMPTS = 3
UPSTREAM_PAUSE = 0.5  # doubles per retry
# Low on purpose: _download pauses while holding _adts_lock, and no client
# waits out a Retry-After of a minute anyway.
UPSTREAM_MAX_PAUSE = 2.0
RESOLVE_DEADLINE = 60  # one yt-dlp resolve, ~2 s when healthy
CACHE_TTL_SECONDS = 300
CACHE_SIZE = 32  # a /browse payload is ~1 MB of JSON

_response_cache: "OrderedDict[tuple, tuple[float, Any]]" = OrderedDict()
_inflight: dict[tuple, asyncio.Future] = {}


def _retry_pause(attempt: int, retry_after: Optional[str] = None) -> float:
    try:
        pause = float(retry_after)
    except (TypeError, ValueError):
        pause = UPSTREAM_PAUSE * 2 ** attempt
    return min(max(pause, 0), UPSTREAM_MAX_PAUSE)


async def upstream(method: str, url: str, *, client: Optional[httpx.AsyncClient] = None,
                   timeout=UPSTREAM_TIMEOUT, deadline: Optional[float] = UPSTREAM_DEADLINE,
                   **kwargs) -> httpx.Response:
    """A 2xx response or an exception: httpx.HTTPStatusError once 429/5xx
    retries run out (any other status at once), httpx.TransportError, or
    TimeoutError past the deadline."""
    http = client or get_upstream_client()

    async def attempts() -> httpx.Response:
        for attempt in range(UPSTREAM_ATTEMPTS):
            response = await getattr(http, method)(url, timeout=timeout, **kwargs)
            try:
                response.raise_for_status()
                return response
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                if attempt + 1 == UPSTREAM_ATTEMPTS or (status != 429 and status < 500):
                    raise
                pause = _retry_pause(attempt, exc.response.headers.get("retry-after"))
                logger.info("upstream status=%s, retrying in %.1fs", status, pause)
                await asyncio.sleep(pause)
        raise AssertionError("unreachable")

    return await asyncio.wait_for(attempts(), deadline)


def _forget_inflight(key: tuple, task: asyncio.Future) -> None:
    _inflight.pop(key, None)
    if not task.cancelled():
        task.exception()  # marks it retrieved even if every waiter was cancelled


async def coalesce(key: tuple, fetch: Callable[[], Awaitable]):
    """Concurrent calls with one key share one fetch. It runs as its own task,
    so a client hanging up cannot fail the others waiting on the same result."""
    task = _inflight.get(key)
    if task is None:
        task = _inflight[key] = asyncio.ensure_future(fetch())
        task.add_done_callback(lambda done: _forget_inflight(key, done))
    return await asyncio.shield(task)


async def cached(key: tuple, fetch: Callable[[], Awaitable]):
    """coalesce() plus a short TTL. Only successes are stored."""
    hit = _response_cache.get(key)
    if hit is not None and hit[0] > time.monotonic():
        _response_cache.move_to_end(key)
        return hit[1]

    async def fill():
        value = await fetch()
        _response_cache[key] = (time.monotonic() + CACHE_TTL_SECONDS, value)
        _response_cache.move_to_end(key)
        while len(_response_cache) > CACHE_SIZE:
            _response_cache.popitem(last=False)
        return value

    return await coalesce(key, fill)


async def innertube(endpoint: str, url_params: Optional[dict] = None, **payload) -> dict:
    """POST /youtubei/v1/{endpoint}. `url_params` land in the query string (the
    continuation token of /search), `payload` in the body beside the context."""
    async def fetch() -> dict:
        client = get_upstream_client()
        config = await get_innertube_config(client)
        response = await upstream(
            "post", f"{HOME_URL}youtubei/v1/{endpoint}",
            params={"key": config["INNERTUBE_API_KEY"], "prettyPrint": "false",
                    **(url_params or {})},
            json={
                "context": {"client": {
                    "clientName": config["INNERTUBE_CLIENT_NAME"],
                    "clientVersion": config["INNERTUBE_CLIENT_VERSION"],
                    "hl": "en", "gl": REGION,
                }},
                **payload,
            },
            headers={
                "Content-Type": "application/json",
                "X-YouTube-Client-Name": "67",
                "X-YouTube-Client-Version": config["INNERTUBE_CLIENT_VERSION"],
                "User-Agent": USER_AGENT,
            },
        )
        return response.json()

    return await cached(
        ("innertube", endpoint, json.dumps([url_params, payload], sort_keys=True)), fetch)



async def get_innertube_config(client: httpx.AsyncClient) -> dict:
    """Fetch + cache INNERTUBE_API_KEY / CLIENT_NAME / CLIENT_VERSION from the
    YT Music homepage. Cached forever in-process — the page is 547 KB and the
    values live for hours, so re-fetching per search is wasteful."""
    if _config_cache is not None:
        return _config_cache

    async def fetch() -> dict:
        global _config_cache
        resp = await upstream(
            "get", HOME_URL,
            client=client,
            headers={
                "User-Agent": USER_AGENT,
                "Accept-Language": "en-US,en;q=0.9",
                # ponytail: EU-geolocated IPs get redirected to a GDPR consent
                # page instead of the homepage; this is yt-dlp's own bypass cookie.
                "Cookie": "SOCS=CAI",
            },
            follow_redirects=True,
        )
        html = resp.text
        config = {}
        for key in ("INNERTUBE_API_KEY", "INNERTUBE_CLIENT_NAME", "INNERTUBE_CLIENT_VERSION"):
            match = re.search(rf'"{key}":"([^"]+)"', html)
            if not match:
                raise ValueError(f"missing {key} in homepage")
            config[key] = match.group(1)
        _config_cache = config
        return config

    return await coalesce(("config",), fetch)


def parse_duration(text: str) -> Optional[int]:
    """Parse 'M:SS' or 'H:MM:SS'. All parts but the first must be 0..59."""
    parts = text.strip().split(":")
    if len(parts) not in (2, 3) or not all(p.isdigit() for p in parts):
        return None
    nums = [int(p) for p in parts]
    if any(not (0 <= n <= 59) for n in nums[1:]):
        return None
    total = 0
    for n in nums:
        total = total * 60 + n
    return total


def _runs(column: dict, renderer_key: str) -> list:
    return column.get(renderer_key, {}).get("text", {}).get("runs", [])


def _best_thumbnail(thumbnails: list) -> Optional[str]:
    """Largest thumbnail in the list. Same shape in search results and in the
    videoDetails of a /player response."""
    if not thumbnails:
        return None
    return max(thumbnails, key=lambda t: t.get("width", 0) * t.get("height", 0)).get("url")


def _page_type(node: dict) -> Optional[str]:
    """What a run or a row links to: MUSIC_PAGE_TYPE_ARTIST, _ALBUM…"""
    return (
        node.get("navigationEndpoint", {})
        .get("browseEndpoint", {})
        .get("browseEndpointContextSupportedConfigs", {})
        .get("browseEndpointContextMusicConfig", {})
        .get("pageType")
    )


def _parse_item(renderer: dict) -> Optional[dict]:
    flex_columns = renderer.get("flexColumns", [])
    if not flex_columns:
        return None

    title_runs = _runs(flex_columns[0], "musicResponsiveListItemFlexColumnRenderer")
    if not title_runs:
        return None
    title_run = title_runs[0]
    title = title_run.get("text")
    watch = title_run.get("navigationEndpoint", {}).get("watchEndpoint", {})
    music_video_type = (
        watch.get("watchEndpointMusicSupportedConfigs", {})
        .get("watchEndpointMusicConfig", {})
        .get("musicVideoType")
    )
    if music_video_type != "MUSIC_VIDEO_TYPE_ATV":
        return None

    video_id = renderer.get("playlistItemData", {}).get("videoId") or watch.get("videoId")
    if not video_id or not title:
        return None

    artist = None
    album = None
    for column in flex_columns[1:]:
        for run in _runs(column, "musicResponsiveListItemFlexColumnRenderer"):
            page_type = _page_type(run)
            if page_type == "MUSIC_PAGE_TYPE_ARTIST" and artist is None:
                artist = run.get("text")
            elif page_type == "MUSIC_PAGE_TYPE_ALBUM" and album is None:
                album = run.get("text")
        if artist and album:
            break
    if not artist:
        return None
    # album stays None for singles (no MUSIC_PAGE_TYPE_ALBUM run at all) — callers
    # that need a non-empty album (subsonic.py) fall back to the title themselves.

    artwork_url = _best_thumbnail(
        renderer.get("thumbnail", {})
        .get("musicThumbnailRenderer", {})
        .get("thumbnail", {})
        .get("thumbnails", [])
    )

    fixed_runs = [
        r for c in renderer.get("fixedColumns", [])
        for r in _runs(c, "musicResponsiveListItemFixedColumnRenderer")
    ]
    flex_runs = [
        r for c in flex_columns for r in _runs(c, "musicResponsiveListItemFlexColumnRenderer")
    ]
    duration_seconds = None
    for run in fixed_runs + flex_runs:
        duration_seconds = parse_duration(run.get("text", ""))
        if duration_seconds is not None:
            break

    return {
        "id": video_id,
        "title": title,
        "artist": artist,
        "album": album,
        "artworkURL": artwork_url,
        "durationSeconds": duration_seconds,
    }


def _shelves(data: dict) -> list:
    """Response shelves. A continuation arrives in a different wrapper than the
    first page does."""
    continuation = data.get("continuationContents", {}).get("musicShelfContinuation")
    if continuation is not None:
        return [continuation]

    shelves = []
    tabs = data.get("contents", {}).get("tabbedSearchResultsRenderer", {}).get("tabs", [])
    for tab in tabs:
        sections = (
            tab.get("tabRenderer", {})
            .get("content", {})
            .get("sectionListRenderer", {})
            .get("contents", [])
        )
        for section in sections:
            for key in ("musicShelfRenderer", "musicCardShelfRenderer", "itemSectionRenderer"):
                shelf = section.get(key)
                if shelf:
                    shelves.append(shelf)
    return shelves


def parse_search_page(data: dict, limit: int) -> tuple[list, Optional[str]]:
    """Tracks on this page plus the token for the next one.

    The token is only handed back when the page fit inside `limit` entirely:
    otherwise a client following it would skip over the truncated tail.
    """
    tracks = []
    # The same track arrives in several shelves at once (the "top result" card
    # duplicates a row from the song list), and every duplicate ate a slot in
    # the window the client asked for. Measured 2026-08-27 on one query: 78
    # positions across 5 pages against 54 unique ones — a quarter wasted.
    seen: set[str] = set()
    token = None
    for shelf in _shelves(data):
        for item in shelf.get("contents") or []:
            renderer = item.get("musicResponsiveListItemRenderer")
            if not renderer:
                continue
            track = _parse_item(renderer)
            if track and track["id"] not in seen:
                seen.add(track["id"])
                tracks.append(track)
        if token is None:
            continuations = shelf.get("continuations") or []
            if continuations:
                token = continuations[0].get("nextContinuationData", {}).get("continuation")

    if len(tracks) > limit:
        return tracks[:limit], None
    return tracks, token


def parse_search_response(data: dict, limit: int) -> list:
    return parse_search_page(data, limit)[0]


@app.get("/")
async def root():
    """The server has no web UI, and without this route the address opened in
    a browser answers a bare 404 — which reads as "the server is broken" twice
    out of two. This page does nothing except confirm the process is alive."""
    return Response(
        "Mirasonic worker.\n\n"
        "There is no web UI and there never will be: playback happens in a\n"
        "Subsonic client. The API lives at /rest/{action}.view — point your\n"
        "client at this address.\n",
        media_type="text/plain; charset=utf-8",
    )


@app.get("/search")
async def search(q: str = "", limit: int = SEARCH_PAGE_SIZE, continuation: str = ""):
    """A page of songs. `continuation` from a previous response returns the next.

    When following a continuation, `q` is not needed — the token already
    carries the query.
    """
    if not continuation and not q.strip():
        return JSONResponse({"error": "empty_query"}, status_code=400)
    limit = max(1, min(50, limit))
    try:
        if continuation:
            data = await innertube("search", url_params={"continuation": continuation})
        else:
            data = await innertube("search", query=q, params=SEARCH_PARAMS_SONGS)
        tracks, next_token = parse_search_page(data, limit)
    except Exception:
        logger.exception("search upstream failure query=%r", q)
        return JSONResponse({"error": "upstream"}, status_code=502)
    return {"tracks": tracks, "continuation": next_token}


# ---------------------------------------------------------------------------
# /browse and /next, for the Subsonic artist and album pages. Shapes taken from
# live responses, 2026-11-05. All of these raise on upstream failure.
# ---------------------------------------------------------------------------
PLAYS_RE = re.compile(r"([\d.,]+)\s*([KMB]?)\s+plays?")
PLAYS_SCALE = {"": 1, "K": 1e3, "M": 1e6, "B": 1e9}


def _find_all(node, key: str) -> list:
    """Every value under `key` anywhere in the tree. The same renderer sits at
    different depths on artist, album and playlist pages."""
    found = []
    if isinstance(node, dict):
        for name, value in node.items():
            if name == key:
                found.append(value)
            else:
                found.extend(_find_all(value, key))
    elif isinstance(node, list):
        for value in node:
            found.extend(_find_all(value, key))
    return found


def _text(node: dict) -> str:
    return "".join(run.get("text", "") for run in (node or {}).get("runs", []))


def _fold(name: str) -> str:
    """Comparison key: "Beyonce" finds "Beyoncé", "Guns N' Roses" its curly
    apostrophe. Names of nothing but punctuation stay as they are."""
    decomposed = unicodedata.normalize("NFKD", name)
    return "".join(c for c in decomposed if c.isalnum()).casefold() or name


def _flex_runs(row: dict) -> list:
    return [run for column in row.get("flexColumns", [])
            for run in _runs(column, "musicResponsiveListItemFlexColumnRenderer")]


def _plays(row: dict) -> int:
    for run in _flex_runs(row):
        match = PLAYS_RE.fullmatch(run.get("text", "").strip())
        if match:
            return int(float(match[1].replace(",", "")) * PLAYS_SCALE[match[2]])
    return 0


def parse_top_songs(rows: list, artist: str) -> list:
    """Most played first, one version of each title.

    The "Top songs" list is an editorial mix, not a chart. Measured 2026-11-05:
    Beyonce's 4th row has 31M plays above rows with 1.2B, and Kino's list holds
    "Группа крови" three times. Rows with no play count sort last in the order
    YouTube gave them, so if the count ever vanishes the mix is what is left.

    Rows not credited to this artist go too (Destiny's Child's "Say My Name",
    804M, sits in Beyonce's list), unless that is every row: then the page
    spells the name differently from the caller.
    """
    credited = [row for row in rows if any(
        _page_type(run) == "MUSIC_PAGE_TYPE_ARTIST"
        and _fold(run.get("text", "")) == _fold(artist) for run in _flex_runs(row))]
    ranked = sorted(credited or rows, key=_plays, reverse=True)
    best: dict[str, dict] = {}
    for row in ranked:
        track = _parse_item(row)
        if track:
            best.setdefault(_fold(track["title"]), track)
    return list(best.values())


def parse_radio(data: dict, seed: str) -> list:
    tracks: dict[str, dict] = {}
    for item in _find_all(data, "playlistPanelVideoRenderer"):
        video_id = item.get("videoId")
        title = _text(item.get("title"))
        runs = item.get("longBylineText", {}).get("runs", [])
        artist = next((r.get("text") for r in runs
                       if _page_type(r) == "MUSIC_PAGE_TYPE_ARTIST"), None)
        artist = artist or (runs[0].get("text") if runs else None)
        if not video_id or video_id == seed or not title or not artist:
            continue
        tracks.setdefault(video_id, {
            "id": video_id,
            "title": title,
            "artist": artist,
            "album": next((r.get("text") for r in runs
                           if _page_type(r) == "MUSIC_PAGE_TYPE_ALBUM"), None),
            "artworkURL": _best_thumbnail(item.get("thumbnail", {}).get("thumbnails", [])),
            "durationSeconds": parse_duration(_text(item.get("lengthText"))),
        })
    return list(tracks.values())


def _artist_browse_id(data: dict, name: str) -> Optional[str]:
    for row in _find_all(data, "musicResponsiveListItemRenderer"):
        runs = _flex_runs(row)
        if (_page_type(row) == "MUSIC_PAGE_TYPE_ARTIST" and runs
                and _fold(runs[0].get("text", "")) == _fold(name)):
            return row["navigationEndpoint"]["browseEndpoint"].get("browseId")
        for run in runs:
            if (_page_type(run) == "MUSIC_PAGE_TYPE_ARTIST"
                    and _fold(run.get("text", "")) == _fold(name)):
                return run["navigationEndpoint"]["browseEndpoint"].get("browseId")
    return None


async def _artist_page(name: str) -> Optional[dict]:
    """The artist page for exactly this name, or None: the artist filter returns
    lookalikes when there is no such artist, and someone else's songs under this
    name are worse than none. Two artists sharing a name: YouTube's first wins.

    Song rows are the fallback because that is where the names we hand out come
    from, and they can differ from the artist card: measured 2026-11-05, the card
    says "Kino", every one of its 100 tracks says "Кино"."""
    for params in (SEARCH_PARAMS_ARTISTS, SEARCH_PARAMS_SONGS):
        browse_id = _artist_browse_id(await innertube("search", query=name, params=params), name)
        if browse_id:
            return await innertube("browse", browseId=browse_id)
    return None


async def artist_top_songs(name: str) -> list:
    page = await _artist_page(name)
    shelves = _find_all(page, "musicShelfRenderer")
    if not shelves:
        return []
    # The page shows five rows; bottomEndpoint is its "See all" playlist. An
    # artist with few songs has none, and the five are the whole list.
    rows = _find_all(shelves[0], "musicResponsiveListItemRenderer")
    endpoint = shelves[0].get("bottomEndpoint", {}).get("browseEndpoint")
    if endpoint and endpoint.get("browseId"):
        playlist = await innertube(
            "browse", browseId=endpoint["browseId"], params=endpoint.get("params"))
        rows = _find_all(playlist, "musicResponsiveListItemRenderer") or rows
    return parse_top_songs(rows, name)


async def similar_tracks(video_id: str) -> list:
    """The radio YouTube Music would start from this track, without the track."""
    data = await innertube(
        "next", videoId=video_id, playlistId=f"RDAMVM{video_id}", isAudioOnly=True)
    return parse_radio(data, video_id)


async def artist_info(name: str) -> dict:
    """{"biography": str, "similar": [names]}; empty for an unknown artist."""
    page = await _artist_page(name)
    biography = "".join(
        _text(shelf.get("description")) for shelf in _find_all(page, "musicDescriptionShelfRenderer")[:1])
    # "Fans might also like" is the carousel of artists, whatever its title says.
    similar = [_text(item.get("title")) for item in _find_all(page, "musicTwoRowItemRenderer")
               if _page_type(item) == "MUSIC_PAGE_TYPE_ARTIST"]
    return {"biography": biography, "similar": [name for name in similar if name]}


async def album_notes(artist: str, album: str) -> str:
    """The album's description, "" when YouTube has none or no such album.

    An exact title wins. Otherwise a row counts when `album` is its title plus
    an edition suffix: "OK Computer OKNOTOK 1997 2017" is listed as "OK Computer".
    """
    results = await innertube(
        "search", query=f"{album} {artist}", params=SEARCH_PARAMS_ALBUMS)
    albums = []
    for row in _find_all(results, "musicResponsiveListItemRenderer"):
        runs = _flex_runs(row)
        if (_page_type(row) == "MUSIC_PAGE_TYPE_ALBUM" and runs
                and any(_page_type(r) == "MUSIC_PAGE_TYPE_ARTIST"
                        and _fold(r.get("text", "")) == _fold(artist) for r in runs)):
            albums.append((runs[0].get("text", ""), row))
    row = next((row for title, row in albums if _fold(title) == _fold(album)), None) or next(
        (row for title, row in albums
         if title and album.casefold().startswith(title.casefold() + " ")), None)
    if row is None:
        return ""
    page = await innertube(
        "browse", browseId=row["navigationEndpoint"]["browseEndpoint"]["browseId"])
    shelves = _find_all(page, "musicDescriptionShelfRenderer")
    return _text(shelves[0].get("description")) if shelves else ""


def _extract_expire(url: str) -> Optional[int]:
    values = parse_qs(urlparse(url).query).get("expire")
    if not values:
        return None
    try:
        return int(values[0])
    except ValueError:
        return None


def _resolve_stream_sync(video_id: str) -> str:
    with YoutubeDL(YDL_OPTS) as ydl:
        info = ydl.extract_info(
            f"https://music.youtube.com/watch?v={video_id}", download=False
        )
    url = info.get("url")
    if not url:
        raise DownloadError(f"no direct url resolved for {video_id}")
    return url


async def get_stream_url(video_id: str) -> str:
    cached = _stream_cache.get(video_id)
    now = time.time()
    if cached and cached["expire"] - STREAM_EXPIRE_MARGIN_SECONDS > now:
        return cached["url"]
    return await coalesce(("stream", video_id), lambda: _resolve_stream(video_id))


async def _resolve_stream(video_id: str) -> str:
    """yt-dlp under the upstream() rules. It speaks exceptions rather than
    status codes. The deadline frees the caller; the thread itself ends at
    socket_timeout, it cannot be killed."""
    for attempt in range(UPSTREAM_ATTEMPTS):
        try:
            url = await asyncio.wait_for(
                asyncio.to_thread(_resolve_stream_sync, video_id), RESOLVE_DEADLINE)
            break
        except DownloadError as exc:
            # "Sign in to confirm you're not a bot" never matches: it is the
            # captcha, and retrying feeds it (docs/PITFALLS.md #1).
            if attempt + 1 == UPSTREAM_ATTEMPTS or not RETRYABLE_RESOLVE.search(str(exc)):
                raise
            await asyncio.sleep(_retry_pause(attempt))
    expire = _extract_expire(url) or (time.time() + 3600)
    _stream_cache[video_id] = {"url": url, "expire": expire}
    return url


async def get_song_details(video_id: str) -> dict:
    """Best-effort title/artist/length via the anonymous /youtubei/v1/player
    endpoint. Returns {"title": str|None, "artist": str|None, "duration": int}.

    Measured 2026-08-26 (docs/DESIGN-NOTES.md, D-014): videoDetails is populated even
    when playabilityStatus is UNPLAYABLE, so this is cheap — one JSON request,
    no yt-dlp, none of the age-gate/captcha risk of a full stream resolve.

    videoDetails.title/author come from the same response as lengthSeconds, so
    reading them costs nothing extra. The Subsonic layer needs them when a
    track is added to the library without having passed through search3 (a
    worker restart empties the in-memory metadata cache) — without them the
    row would be written with the videoId as its title, permanently.

    Never raises: on any failure (network, missing field, upstream shape
    change) the fields come back None/0 rather than let one bad track break a
    search page or a playlist edit.
    """
    async with _player_semaphore:
        try:
            data = await innertube("player", videoId=video_id)
        except Exception:
            logger.exception("get_song_details video_id=%s failed", video_id)
            return {"title": None, "artist": None, "duration": 0, "artwork": None}

    details = data.get("videoDetails", {}) if isinstance(data, dict) else {}
    try:
        duration = int(details.get("lengthSeconds"))
    except (TypeError, ValueError):
        duration = 0
    return {
        "title": details.get("title") or None,
        "artist": details.get("author") or None,
        "duration": duration,
        # Measured 2026-08-27: videoDetails carries a square cover up to
        # 544x544 — same request, zero extra cost. Without it a track added to
        # a playlist outside of search (a restart emptied the metadata cache,
        # or it was mapped by hand with --map) stayed in the library with no
        # artwork forever: a second INSERT will not fill it in.
        "artwork": _best_thumbnail(details.get("thumbnail", {}).get("thumbnails", [])),
    }


async def get_song_duration(video_id: str) -> int:
    """Just the length — same one request as get_song_details, kept as its own
    name because the search path wants nothing else."""
    return (await get_song_details(video_id))["duration"]


def _is_unavailable(exc: DownloadError) -> bool:
    message = str(exc).lower()
    return any(marker in message for marker in UNAVAILABLE_MARKERS)


def _resolve_error_response(exc: DownloadError, video_id: str, endpoint: str) -> JSONResponse:
    """404 — unavailable anonymously and retrying will not help; 502 — everything else."""
    if _is_unavailable(exc):
        logger.info("%s video_id=%s status=404 (unavailable)", endpoint, video_id)
        return JSONResponse({"error": "not_found"}, status_code=404)
    logger.info("%s video_id=%s status=502 (resolve failed)", endpoint, video_id)
    return JSONResponse({"error": "upstream"}, status_code=502)


@app.get("/prefetch/{video_id}")
async def prefetch(video_id: str):
    """Warms the signed-URL cache without returning anything.

    A yt-dlp resolve takes ~2 s and accounts for nearly all the delay before
    sound (measured: cold /stream 1.99 s, warm 0.035 s). The client calls this
    for the NEXT track while the current one is already playing, which hides
    the resolve behind the music.

    ponytail: a depth of 1 is the client's responsibility. Warming a whole
    playlist as a queue reads as a bot to YouTube and earns the IP a captcha.
    """
    try:
        await get_stream_url(video_id)
    except DownloadError as exc:
        return _resolve_error_response(exc, video_id, "prefetch")
    except Exception:
        logger.exception("prefetch video_id=%s resolve error", video_id)
        return JSONResponse({"error": "upstream"}, status_code=502)
    logger.info("prefetch video_id=%s status=204", video_id)
    return Response(status_code=204)


# What /stream actually returns, and why it is not what came in from upstream
# ---------------------------------------------------------------------------
# Investigated 2026-08-27. itag 140 arrives from googlevideo NOT as a plain
# mp4 but as a fragmented one (DASH):
# [ftyp][moov 735 bytes][sidx][moof][mdat][moof][mdat]…
# There are no sample tables in moov; they are spread across the moof boxes
# along the whole file.
#
# Amperfy plays through AudioStreaming, which plays through AudioFileStream,
# and on a seek it calls AudioFileStreamSeek. Measured against a real track:
# when only the beginning of the file has arrived, for a fragmented mp4 that
# function returns kAudioFileStreamError_DataUnavailable — the moof covering
# the target has not been seen yet, so there is no way to know the offset.
# On that error AudioStreaming silently falls back to the linear estimate
# `dataOffset + time/duration*length`, which lands in the middle of an mdat.
# It then feeds those bytes to the parser with the discontinuity flag, and out
# of 400 KB the parser recovers 129 packets instead of a thousand — garbage
# with invented sizes. That is the crash on seek.
#
# The same track repackaged as ADTS: AudioFileStreamSeek returns noErr, and
# after a seek the same 400 KB yields 1055 packets. ADTS is a self-
# synchronising stream of frames, like mp3: any offset is a valid entry point,
# and the parser resyncs to the next header. That is exactly the case
# AudioStreaming handles, and exactly what ordinary Subsonic servers serve.
#
# Repackaging is a bitstream copy (`-c:a copy`), not a re-encode: the same AAC
# frames plus a 7-byte header per frame (+0.7% in size).
ADTS_CONTENT_TYPE = "audio/aac"
DOWNLOAD_ATTEMPTS = 3
# Per-operation (connect/read gap), not total. Measured 2026-09-21: one stalled
# googlevideo read held _adts_lock for hours and every stream queued behind it.
DOWNLOAD_TIMEOUT = httpx.Timeout(20)
ADTS_CACHE_SIZE = 3

# Finished tracks. Repackaging 5 MB costs about a second, and a client makes at
# least two requests per track plus one for every seek.
_adts_cache: dict[str, bytes] = {}
# ponytail: one shared lock instead of a per-track lock — this server has one
# listener. Without the lock, the probe `Range: bytes=0-1` and the main request
# (50 ms apart) downloaded and repackaged the same track twice.
_adts_lock = asyncio.Lock()


class UpstreamError(Exception):
    """The URL resolved, but no bytes came out of it."""


class UpstreamRefused(UpstreamError):
    """googlevideo answered with an HTTP error: the signed URL itself is bad."""


async def _download(url: str) -> bytes:
    """Pulls the whole track.

    Range is mandatory even when the whole file is wanted. Measured
    2026-08-27: without Range, googlevideo serves at ~32 KB/s; with
    `Range: bytes=0-` the same 5.2 MB file arrives in 0.15 s. Four orders of
    magnitude, all of it resting on one header.
    """
    last: Optional[Exception] = None
    for _ in range(DOWNLOAD_ATTEMPTS):
        try:
            response = await upstream(
                "get", url, headers={"Range": "bytes=0-"}, timeout=DOWNLOAD_TIMEOUT,
                deadline=None)
        except httpx.HTTPStatusError as exc:
            raise UpstreamRefused(f"upstream {exc.response.status_code}")
        except httpx.HTTPError as exc:
            last = exc  # cut off mid-transfer — just refetch, it costs 0.15 s
            continue
        return response.content
    raise UpstreamError(f"upstream unreachable: {last!r}")


async def _remux_to_adts(data: bytes) -> bytes:
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-i", "pipe:0", "-vn", "-c:a", "copy", "-f", "adts", "pipe:1",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate(data)
    if proc.returncode != 0 or not out:
        raise UpstreamError(
            f"ffmpeg rc={proc.returncode}: {err[:200].decode('utf-8', 'replace')}")
    return out


_silence: Optional[bytes] = None


async def get_silence() -> bytes:
    """One second of silent ADTS, made once.

    Served in place of a track that will never resolve (age gate, removed).
    Measured 2026-10-01: on a 404 Amperfy restarts the current track, so one
    age-gated song looped every ~13 s for good and nothing else played. A
    short silent track ends normally and the player moves to the next one.
    """
    global _silence
    if _silence is None:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-t", "1",
            "-c:a", "aac", "-f", "adts", "pipe:1",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
        if proc.returncode != 0 or not out:
            raise UpstreamError(f"ffmpeg silence rc={proc.returncode}: {err[:200]!r}")
        _silence = out
    return _silence


async def get_adts(video_id: str) -> bytes:
    async with _adts_lock:
        cached = _adts_cache.get(video_id)
        if cached is not None:
            return cached
        try:
            data = await _download(await get_stream_url(video_id))
        except DownloadError as exc:
            if not _is_unavailable(exc):
                raise
            logger.info("stream video_id=%s unavailable, serving silence to skip", video_id)
            data = None
            audio = await get_silence()
        except UpstreamRefused:
            # A URL googlevideo refused stays refused until it expires (~6 h).
            # Measured 2026-09-29: without eviction every retry hit the same
            # dead URL, the client got 502 after 502, and Amperfy restarted the
            # current track each time. One fresh resolve usually plays.
            _stream_cache.pop(video_id, None)
            data = await _download(await get_stream_url(video_id))
        if data is not None:
            try:
                audio = await _remux_to_adts(data)
            except UpstreamError as exc:
                # A cached URL can also go bad without an HTTP error: measured
                # 2026-11-04, googlevideo answered 2xx with bytes ffmpeg could not
                # read, and the same track 502'd five to seven times in a row.
                # The same fresh resolve as UpstreamRefused plays it.
                logger.info("stream video_id=%s unplayable bytes (%s, %d B, head %r), resolving again",
                            video_id, exc, len(data), data[:16])
                _stream_cache.pop(video_id, None)
                audio = await _remux_to_adts(await _download(await get_stream_url(video_id)))
        _adts_cache[video_id] = audio
        while len(_adts_cache) > ADTS_CACHE_SIZE:
            _adts_cache.pop(next(iter(_adts_cache)))  # dict keeps insertion order
        return audio


_RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")


def _range_response(audio: bytes, range_header: Optional[str]) -> Response:
    """Serves a slice of the track per the Range header. The bytes are already
    in memory, so the bounds are exact: both content-length and content-range
    are computed from the real size, not from whatever upstream promised."""
    total = len(audio)
    headers = {"accept-ranges": "bytes"}
    match = _RANGE_RE.match((range_header or "").strip())
    if match is None:
        # No Range, or one in a shape we do not understand — RFC 9110 §14.2
        # allows serving the whole file.
        return Response(audio, media_type=ADTS_CONTENT_TYPE, headers=headers)

    first, last = match.group(1), match.group(2)
    if first:
        start = int(first)
        end = min(int(last), total - 1) if last else total - 1
    elif last:
        start, end = max(0, total - int(last)), total - 1  # suffix Range
    else:
        start, end = 0, -1  # "bytes=-" — not a range at all

    if start > end or start >= total:
        # 416, not 502: "there is nothing further" is a normal answer.
        # AudioStreaming calls errorOccurred on any code >= 300, Amperfy runs
        # handleError -> restartPlayer -> triggerReinsertPlayable, and the
        # track starts over. On a 416 the same client simply records the end.
        return Response(status_code=416, headers={**headers,
                                                  "content-range": f"bytes */{total}"})

    headers["content-range"] = f"bytes {start}-{end}/{total}"
    return Response(audio[start:end + 1], status_code=206,
                    media_type=ADTS_CONTENT_TYPE, headers=headers)


async def proxy_bytes(video_id: str, range_header: Optional[str]):
    """Resolves video_id, fetches the track, repackages it as ADTS and serves
    the requested range. Shared entry point for /stream/{id} and the Subsonic
    stream.view/download.view — one resolve, one cache, one error mapping.
    """
    try:
        audio = await get_adts(video_id)
    except DownloadError as exc:
        return _resolve_error_response(exc, video_id, "stream")
    except UpstreamError as exc:
        logger.info("stream video_id=%s status=502 (%s)", video_id, exc)
        return JSONResponse({"error": "upstream"}, status_code=502)
    except Exception:
        logger.exception("stream video_id=%s failed", video_id)
        return JSONResponse({"error": "upstream"}, status_code=502)

    response = _range_response(audio, range_header)
    logger.info("stream video_id=%s status=%s", video_id, response.status_code)
    return response


@app.get("/stream/{video_id}")
async def stream(video_id: str, request: Request):
    return await proxy_bytes(video_id, request.headers.get("range"))


# Imported last: subsonic.py does `import main` and reaches back into this
# module's functions (proxy_bytes, get_song_duration, search, ...) only at
# request time, never at import time, so the circular import is safe as long
# as everything it needs is already defined above this line.
import subsonic  # noqa: E402

app.include_router(subsonic.router, prefix="/rest")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8080")),
    )
