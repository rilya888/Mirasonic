"""Mirasonic worker: anonymous YouTube Music search + stream proxy.

GET /search?q=...&limit=..  -> {"tracks": [...]}
GET /stream/{video_id}      -> proxied audio bytes (Range-aware)

No auth, no DB, no disk writes. Everything cached is a plain in-memory dict.
"""
import asyncio
import logging
import os
import re
import time
from typing import Optional
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
SEARCH_PAGE_SIZE = 20
YDL_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "skip_download": True,
    "format": "bestaudio[ext=m4a]/bestaudio",
    "socket_timeout": 20,
}
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



async def get_innertube_config(client: httpx.AsyncClient) -> dict:
    """Fetch + cache INNERTUBE_API_KEY / CLIENT_NAME / CLIENT_VERSION from the
    YT Music homepage. Cached forever in-process — the page is 547 KB and the
    values live for hours, so re-fetching per search is wasteful."""
    global _config_cache
    if _config_cache is not None:
        return _config_cache
    resp = await client.get(
        HOME_URL,
        headers={
            "User-Agent": USER_AGENT,
            "Accept-Language": "en-US,en;q=0.9",
            # ponytail: EU-geolocated IPs get redirected to a GDPR consent
            # page instead of the homepage; this is yt-dlp's own bypass cookie.
            "Cookie": "SOCS=CAI",
        },
        follow_redirects=True,
        timeout=20,
    )
    resp.raise_for_status()
    html = resp.text
    config = {}
    for key in ("INNERTUBE_API_KEY", "INNERTUBE_CLIENT_NAME", "INNERTUBE_CLIENT_VERSION"):
        match = re.search(rf'"{key}":"([^"]+)"', html)
        if not match:
            raise ValueError(f"missing {key} in homepage")
        config[key] = match.group(1)
    _config_cache = config
    return config


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
            page_type = (
                run.get("navigationEndpoint", {})
                .get("browseEndpoint", {})
                .get("browseEndpointContextSupportedConfigs", {})
                .get("browseEndpointContextMusicConfig", {})
                .get("pageType")
            )
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
        async with httpx.AsyncClient(timeout=20) as client:
            config = await get_innertube_config(client)
            params = {"key": config["INNERTUBE_API_KEY"], "prettyPrint": "false"}
            body = {
                "context": {
                    "client": {
                        "clientName": config["INNERTUBE_CLIENT_NAME"],
                        "clientVersion": config["INNERTUBE_CLIENT_VERSION"],
                        "hl": "en",
                        "gl": REGION,
                    }
                }
            }
            if continuation:
                params["continuation"] = continuation
            else:
                body["query"] = q
                body["params"] = SEARCH_PARAMS_SONGS
            resp = await client.post(
                f"{HOME_URL}youtubei/v1/search",
                params=params,
                json=body,
                headers={
                    "Content-Type": "application/json",
                    "X-YouTube-Client-Name": "67",
                    "X-YouTube-Client-Version": config["INNERTUBE_CLIENT_VERSION"],
                    "User-Agent": USER_AGENT,
                },
            )
            resp.raise_for_status()
            data = resp.json()
        tracks, next_token = parse_search_page(data, limit)
    except Exception:
        logger.exception("search upstream failure query=%r", q)
        return JSONResponse({"error": "upstream"}, status_code=502)
    return {"tracks": tracks, "continuation": next_token}


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
    url = await asyncio.to_thread(_resolve_stream_sync, video_id)
    expire = _extract_expire(url) or (now + 3600)
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
            client = get_upstream_client()
            config = await get_innertube_config(client)
            params = {"key": config["INNERTUBE_API_KEY"], "prettyPrint": "false"}
            body = {
                "context": {"client": {
                    "clientName": config["INNERTUBE_CLIENT_NAME"],
                    "clientVersion": config["INNERTUBE_CLIENT_VERSION"],
                    "hl": "en", "gl": REGION,
                }},
                "videoId": video_id,
            }
            resp = await client.post(
                f"{HOME_URL}youtubei/v1/player", params=params, json=body,
                headers={
                    "Content-Type": "application/json",
                    "X-YouTube-Client-Name": "67",
                    "X-YouTube-Client-Version": config["INNERTUBE_CLIENT_VERSION"],
                    "User-Agent": USER_AGENT,
                },
                timeout=20,
            )
            resp.raise_for_status()
            data = resp.json()
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
    client = get_upstream_client()
    last: Optional[Exception] = None
    for _ in range(DOWNLOAD_ATTEMPTS):
        try:
            response = await client.get(
                url, headers={"Range": "bytes=0-"}, timeout=DOWNLOAD_TIMEOUT)
        except httpx.HTTPError as exc:
            last = exc  # cut off mid-transfer — just refetch, it costs 0.15 s
            continue
        if response.status_code >= 400:
            raise UpstreamRefused(f"upstream {response.status_code}")
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
            audio = await _remux_to_adts(data)
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
