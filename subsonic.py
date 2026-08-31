"""Subsonic API layer for Amperfy (docs/SUBSONIC.md — that file is the contract,
not this docstring). Phase 1: auth, ping, search3, stream/download,
getCoverArt, the search-metadata cache, and the empty-success stubs from
section 9. Phase 2 adds the persistent library (library.py): playlists
and starred songs. Phase 3 derives artists and albums from those same library
rows, so Amperfy's browse tabs stop being empty.

No knowledge of yt-dlp or InnerTube lives here; everything YouTube-shaped is
reused from `main` (search, get_song_details, proxy_bytes, get_upstream_client).
No knowledge of SQL lives here either — that's library.py; this module only
resolves metadata and calls into it.
"""
import asyncio
import hashlib
import hmac
import logging
import os
import random
import time
import xml.etree.ElementTree as ET
from collections import OrderedDict
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import Response

import library
import main

logger = logging.getLogger("worker.subsonic")

NAMESPACE = "http://subsonic.org/restapi"
PROTOCOL_VERSION = "1.16.1"  # Amperfy: below 1.13.0 falls back to plaintext
                              # password, below 1.14.0 stops expecting an id
                              # in createPlaylist's response (docs/SUBSONIC.md §2).
SERVER_VERSION = "0.1.0"

# search3 → up to 3 InnerTube pages per request (docs/SUBSONIC.md §5).
MAX_SEARCH_CALLS = 3

# `size` estimate for <song> — docs/SUBSONIC.md §6 trap: Amperfy's
# excludeServerDeleteUncachedSongsFetchPredicate hides any song with size==0
# and no album relationship (SongMO+CoreDataClass.swift, found 2026-08-26 on
# iPhone). Resolving every search result through yt-dlp to get a real size
# would queue enough requests to trip YouTube's captcha (pitfall #1), so this
# is a duration-based approximation, not a measured size.
# Measured live: a 321s track came out of /stream at 5,235,198 bytes — itag 140
# repacked into ADTS (main.py: why not mp4) => ~16,300 bytes/sec at ~129kbps.
BYTES_PER_SECOND = 16_300
BIT_RATE = 129  # itag 140, the bitstream is copied as-is — no re-encoding
# Assumed length used only for the size estimate when duration is unknown.
# `duration="0"` itself is still honest (docs/SUBSONIC.md §4), but `size` must
# never be 0 or the song disappears again — the exact bug this file fixes.
FALLBACK_SIZE_SECONDS = 180


def _estimate_size(duration_seconds: int) -> int:
    seconds = duration_seconds if duration_seconds else FALLBACK_SIZE_SECONDS
    return seconds * BYTES_PER_SECOND


def _artist_id(artist: str) -> str:
    """docs/SUBSONIC.md §4: ar-{first 16 hex sha1(name)}."""
    return "ar-" + hashlib.sha1(artist.encode("utf-8")).hexdigest()[:16]


def _album_id(artist: str, album: str) -> str:
    """docs/SUBSONIC.md §4: al-{first 16 hex sha1(artist + "\\0" + album)}."""
    digest = hashlib.sha1(f"{artist}\0{album}".encode("utf-8")).hexdigest()
    return "al-" + digest[:16]


def _iso_created() -> str:
    """docs/SUBSONIC.md §6: Amperfy parses `created` with ISO8601DateFormatter's
    .withFractionalSeconds — a value without milliseconds silently parses to
    nil, so the fractional part is not optional here."""
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


# ---------------------------------------------------------------------------
# credentials — no defaults, ever. Checked per-request (cheap os.environ
# lookups) and again at ASGI startup so a real deployment refuses to come up
# without them; reading fresh each time means the check works from unit tests
# that never trigger the startup lifecycle at all (plain TestClient(app).get()
# calls, same style as test_worker.py, do not run lifespan).
# ---------------------------------------------------------------------------

def _get_credentials() -> tuple[str, str]:
    user = os.environ.get("SUBSONIC_USER")
    password = os.environ.get("SUBSONIC_PASSWORD")
    if not user or not password:
        raise RuntimeError(
            "SUBSONIC_USER and SUBSONIC_PASSWORD must be set — refusing to start"
        )
    return user, password


@asynccontextmanager
async def _lifespan(_router: APIRouter):
    _get_credentials()
    yield


router = APIRouter(lifespan=_lifespan)


# ---------------------------------------------------------------------------
# library (SQLite, phase 2) — one instance per process, created lazily so
# unit tests that never touch a library endpoint never touch a DB file. Tests
# that do exercise the library assign their own `library.Library(tmp_path)`
# here directly (see test_subsonic.py) instead of relying on the default
# MIRASONIC_DB path.
# ---------------------------------------------------------------------------

_library: Optional[library.Library] = None


def _get_library() -> library.Library:
    global _library
    if _library is None:
        _library = library.Library()
    return _library


def _decode_p(p: str) -> str:
    """`p` may be plaintext or `enc:<hex>` — same bytes, hex-encoded."""
    if p.startswith("enc:"):
        try:
            return bytes.fromhex(p[4:]).decode("utf-8")
        except ValueError:
            return ""
    return p


def _authenticate(params) -> bool:
    user, password = _get_credentials()
    if params.get("u") != user:
        return False
    t = params.get("t")
    s = params.get("s")
    if t and s:
        expected = hashlib.md5((password + s).encode("utf-8")).hexdigest()
        return hmac.compare_digest(expected, t)
    p = params.get("p")
    if p is not None:
        return hmac.compare_digest(_decode_p(p), password)
    return False


# ---------------------------------------------------------------------------
# search-metadata cache (docs/SUBSONIC.md §7) — videoId -> {title, artist,
# artworkURL, duration}. `duration` is None until resolved. Bridges search3
# (which has the metadata) and updatePlaylist (which gets only a videoId) —
# phase 2 will read it too, but it is populated starting in phase 1.
# ---------------------------------------------------------------------------

_SEARCH_CACHE_MAX = 2000
_search_cache: "OrderedDict[str, dict]" = OrderedDict()


def _cache_get(video_id: str) -> Optional[dict]:
    meta = _search_cache.get(video_id)
    if meta is not None:
        _search_cache.move_to_end(video_id)
    return meta


def _cache_put(video_id: str, meta: dict) -> None:
    _search_cache[video_id] = meta
    _search_cache.move_to_end(video_id)
    while len(_search_cache) > _SEARCH_CACHE_MAX:
        _search_cache.popitem(last=False)


# ---------------------------------------------------------------------------
# XML response building
# ---------------------------------------------------------------------------

def _root(status: str) -> ET.Element:
    return ET.Element("subsonic-response", {
        "xmlns": NAMESPACE,
        "status": status,
        "version": PROTOCOL_VERSION,
        "type": "mirasonic",
        "serverVersion": SERVER_VERSION,
    })


def _xml_response(root: ET.Element, status_code: int = 200) -> Response:
    # encoding="utf-8" makes ElementTree omit its own <?xml ...?> line (it
    # only emits one for encodings other than utf-8/us-ascii/unicode), so
    # prepending ours here does not produce a duplicate declaration.
    body = b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="utf-8")
    return Response(content=body, media_type="text/xml", status_code=status_code)


def _ok_response(build=None) -> Response:
    root = _root("ok")
    if build:
        build(root)
    return _xml_response(root)


def _error_response(code: int, message: str) -> Response:
    """HTTP is ALWAYS 200 here — Amperfy's Alamofire .validate() drops the
    body on any non-2xx and the real error would be lost (docs/SUBSONIC.md §2)."""
    root = _root("failed")
    ET.SubElement(root, "error", {"code": str(code), "message": message})
    return _xml_response(root, status_code=200)


def _add_song_element(parent: ET.Element, tag: str, video_id: str, meta: dict) -> None:
    """One shape for <song>/<entry>/<child> — Amperfy parses all three with
    the same delegate (docs/SUBSONIC.md §6). isDir="false" is load-bearing: without
    it the client discards the element as a folder.

    `album`/`albumId` and a non-zero `size` are equally load-bearing, just not
    obviously so: Amperfy only *shows* a song if it passes
    excludeServerDeleteUncachedSongsFetchPredicate — `size > 0 AND
    album.remoteStatus == available` (or a downloaded file, which offline
    caching being off means never). Without a real album in the listing this
    still needs *some* album relationship, so `album` falls back to the title
    for singles rather than staying empty. Found 2026-08-26 on iPhone; see
    docs/SUBSONIC.md §6 for the full trap writeup.
    """
    title = meta.get("title") or ""
    artist = meta.get("artist") or ""
    album = meta.get("album") or title
    duration = meta.get("duration") or 0
    attrib = {
        "id": video_id,
        "title": title,
        "artist": artist,
        "artistId": _artist_id(artist),
        "album": album,
        "albumId": _album_id(artist, album),
        "duration": str(duration),
        "size": str(_estimate_size(duration)),
        "coverArt": video_id,
        "contentType": main.ADTS_CONTENT_TYPE,
        "suffix": "aac",
        "bitRate": str(BIT_RATE),
        "isDir": "false",
        "type": "music",
        "created": _iso_created(),
    }
    # Presence of the attribute means starred (docs/SUBSONIC.md §6); getStarred2
    # is the only caller that puts this key into meta.
    if meta.get("starred"):
        attrib["starred"] = meta["starred"]
    ET.SubElement(parent, tag, attrib)


# ---------------------------------------------------------------------------
# search3.view
# ---------------------------------------------------------------------------

def _int_param(params, name: str, default: int = 0) -> int:
    try:
        return int(params.get(name, default))
    except (TypeError, ValueError):
        return default


async def _resolve_durations(video_ids: list[str]) -> None:
    """Fills in cache entries whose duration is still unknown, concurrently.
    Concurrency at the HTTP level is bounded by main._player_semaphore, not
    here — this just fires the requests. main.get_song_duration never raises,
    but the try/except stays as a second line of defense: one bad track must
    never break the rest of a search3 page.
    """
    todo, seen = [], set()
    for video_id in video_ids:
        if video_id in seen:
            continue
        seen.add(video_id)
        meta = _cache_get(video_id)
        if meta is not None and meta.get("duration") is None:
            todo.append(video_id)
    if not todo:
        return

    async def resolve_one(video_id: str) -> None:
        try:
            duration = await main.get_song_duration(video_id)
        except Exception:
            logger.exception("resolve_durations video_id=%s failed", video_id)
            duration = 0
        meta = _cache_get(video_id)
        if meta is not None:
            meta["duration"] = duration

    await asyncio.gather(*(resolve_one(v) for v in todo))


async def _search_songs(query: str, offset: int, count: int) -> list[tuple[str, dict]]:
    needed = offset + count
    tracks: list[dict] = []
    seen: set[str] = set()  # duplicates occur across pages too, not only within one
    continuation = ""
    q = query
    for _ in range(MAX_SEARCH_CALLS):
        page = await main.search(q=q, limit=main.SEARCH_PAGE_SIZE, continuation=continuation)
        if not isinstance(page, dict):
            # main.search() returns a JSONResponse (not a dict) on upstream
            # failure — treat as "no more results" rather than crash search3.
            break
        for track in page.get("tracks") or []:
            if track["id"] not in seen:
                seen.add(track["id"])
                tracks.append(track)
        continuation = page.get("continuation") or ""
        q = ""  # only the first call carries the query; the rest ride the token
        if len(tracks) >= needed or not continuation:
            break

    window = tracks[offset:offset + count]
    for track in window:
        if _cache_get(track["id"]) is None:
            _cache_put(track["id"], {
                "title": track["title"],
                "artist": track["artist"],
                "album": track.get("album"),  # None for singles — _add_song_element
                                               # falls back to the title, never empty.
                "artworkURL": track.get("artworkURL"),
                # main.search() already resolves this from InnerTube's search
                # response (measured 2026-08-27: 1707/1707 candidates carried
                # it, D-014). Discarding it here was the actual bug: it forced
                # _resolve_durations to hit /youtubei/v1/player once per track
                # of every page, 20 requests where zero were needed. Left as
                # None only for the rare track that genuinely has no value,
                # so the fallback resolve below still has something to do.
                "duration": track.get("durationSeconds"),
            })

    await _resolve_durations([track["id"] for track in window])
    return [(track["id"], _cache_get(track["id"])) for track in window]


async def _search3(params, request: Request) -> Response:
    query = params.get("query", "")
    song_count = _int_param(params, "songCount", 0)
    song_offset = _int_param(params, "songOffset", 0)
    # artistCount/albumCount>0 → intentionally empty result: artist/album
    # search needs different InnerTube filters and is out of scope for phase 1
    # (docs/SUBSONIC.md §5).

    root = _root("ok")
    result = ET.SubElement(root, "searchResult3")

    if song_count > 0 and query.strip():
        for video_id, meta in await _search_songs(query, song_offset, song_count):
            _add_song_element(result, "song", video_id, meta)

    return _xml_response(root)


# ---------------------------------------------------------------------------
# stream.view / download.view — same handler, same code as /stream/{id}
# ---------------------------------------------------------------------------

async def _stream_or_download(params, request: Request) -> Response:
    video_id = params.get("id")
    if not video_id:
        return _error_response(10, "Required parameter 'id' is missing")
    return await main.proxy_bytes(video_id, request.headers.get("range"))


# ---------------------------------------------------------------------------
# getCoverArt.view — plain HTTP proxy, no yt-dlp/InnerTube involved
# ---------------------------------------------------------------------------

async def _proxy_get(url: str) -> Response:
    client = main.get_upstream_client()
    try:
        upstream = await client.get(url)
    except httpx.HTTPError:
        logger.info("getCoverArt upstream fetch failed url=%s", url)
        return _error_response(0, "Cover art fetch failed")
    if upstream.status_code >= 400:
        return _error_response(70, "Cover art not found")
    content_type = upstream.headers.get("content-type", "image/jpeg")
    return Response(content=upstream.content, media_type=content_type)


async def _get_cover_art(params, request: Request) -> Response:
    cover_id = params.get("id")
    if not cover_id:
        return _error_response(10, "Required parameter 'id' is missing")
    if cover_id.startswith("pl-"):
        # docs/SUBSONIC.md §5: a playlist cover is the cover of its first track.
        # An empty (or missing) playlist -> error 70.
        try:
            playlist_id = int(cover_id[len("pl-"):])
        except ValueError:
            return _error_response(70, "Playlist not found")
        playlist = _get_library().get_playlist(playlist_id)
        if not playlist or not playlist["songs"]:
            return _error_response(70, "Playlist not found")
        artwork_url = playlist["songs"][0].get("artwork_url")
        if not artwork_url:
            return _error_response(70, "Cover art not found")
        return await _proxy_get(artwork_url)

    meta = _cache_get(cover_id)
    artwork_url = (meta or {}).get("artworkURL")
    if not artwork_url:
        # Search-cache miss (worker restart, eviction) — second attempt against
        # the persistent library, where artwork_url lands when a track is added
        # to a playlist or starred.
        song = _get_library().get_song(cover_id)
        artwork_url = (song or {}).get("artwork_url")
    if not artwork_url:
        return _error_response(70, "Cover art not found")
    return await _proxy_get(artwork_url)


# ---------------------------------------------------------------------------
# getSong / getArtists / getAlbumList2 / getArtist / getAlbum — phase 3.
#
# The source is the SQLite library. Measured 2026-08-27 before this work
# started: of 303 tracks, 302 carry a real album name, across 111 artists and
# 239 albums — so the tabs come out meaningful rather than a list of three
# hundred one-song "albums".
#
# The lists (getArtists, getAlbumList2) read ONLY the library. Mixing the
# search cache into them is not allowed: a list that swells with recent
# queries and shrinks after a restart reads to Amperfy as objects being
# deleted, and it purges them on its side (§2). Point lookups (getArtist,
# getAlbum, getSong) do consult the cache — a track just found by search and
# not yet added to the library must not vanish from the results.
#
# None of them answers error 70 for an unknown id. `_not_found` used to stand
# here, and it cost a live listening session on 2026-08-26: Amperfy calls
# getAlbum for every song whose album is not synced yet, and after an error
# the album never becomes synced. Half an hour of log held 684 calls, peaking
# at 292 a minute, interleaved with audio dropouts and a frozen app. An empty
# but valid response the client remembers and stops asking; an error it does not.
# ---------------------------------------------------------------------------

def _album_name(meta: dict) -> str:
    """For a single with no album — the track title, exactly as in
    _add_song_element. The grouping key must match whatever went into the
    song's `albumId`, otherwise the album in the list and the album reached
    from the track end up as two different entities."""
    return meta.get("album") or meta.get("title") or ""


def _library_songs() -> list[tuple[str, dict]]:
    return [(song["id"], song) for song in _get_library().get_songs()]


def _known_songs() -> list[tuple[str, dict]]:
    """Library plus search cache; the library record wins — its metadata has
    already been resolved, while the cache may still hold a None duration."""
    songs: "OrderedDict[str, dict]" = OrderedDict(_library_songs())
    for video_id, meta in _search_cache.items():
        songs.setdefault(video_id, meta)
    return list(songs.items())


def _group_by_album(songs: list[tuple[str, dict]]) -> "OrderedDict[str, list]":
    groups: "OrderedDict[str, list]" = OrderedDict()
    for video_id, meta in songs:
        album_id = _album_id(meta.get("artist") or "", _album_name(meta))
        groups.setdefault(album_id, []).append((video_id, meta))
    return groups


def _sort_key(name: Optional[str]) -> str:
    return (name or "").lower()


def _index_letter(name: str) -> str:
    """Letter for <index>. Digits, punctuation and empty names go to "#", the
    convention the other Subsonic servers follow."""
    first = (name or "").strip()[:1].upper()
    return first if first.isalpha() else "#"


def _is_single(songs: list[tuple[str, dict]]) -> bool:
    """A single-song release named after the song itself.

    On the Albums tab that is a duplicate of the track, not an album: 73 such
    tiles out of 240 in a real library (measured 2026-08-27), because the
    library was assembled from Spotify playlists and most releases in it hold
    exactly one track. They are dropped from the list but stay resolvable: the
    song's `albumId` is unchanged, `getAlbum` still answers for it, and
    navigating from a track to its album works. Only browsing is hidden.
    """
    if len(songs) != 1:
        return False
    meta = songs[0][1]
    return _album_name(meta) == (meta.get("title") or "")


def _album_created(songs: list[tuple[str, dict]]) -> str:
    """An album's `created` is when its most recent track entered the library.
    This used to say "now", which broke sorting by newest entirely: every album
    had the same timestamp and it changed on every request."""
    stamps = [meta.get("added_at") for _, meta in songs if meta.get("added_at")]
    return max(stamps) if stamps else _iso_created()


def _add_album_element(parent: ET.Element, album_id: str,
                       songs: list[tuple[str, dict]], with_songs: bool) -> ET.Element:
    first = songs[0][1] if songs else {}
    artist = first.get("artist") or ""
    attrib = {
        "id": album_id,
        "name": _album_name(first),
        "artist": artist,
        "artistId": _artist_id(artist),
        "songCount": str(len(songs)),
        "duration": str(sum(meta.get("duration") or 0 for _, meta in songs)),
        "created": _album_created(songs),
    }
    if songs:
        # coverArt resolves through _get_cover_art, which looks up by videoId —
        # so an album's cover is the cover of its first track, not al-…
        attrib["coverArt"] = songs[0][0]
    album = ET.SubElement(parent, "album", attrib)
    if with_songs:
        for video_id, meta in songs:
            _add_song_element(album, "song", video_id, meta)
    return album


async def _get_song(params, request: Request) -> Response:
    """docs/SUBSONIC.md §5: "one track from the library" — the source is now
    SQLite. But the search cache stays a second source, and that is not a
    detail: error 70 tells Amperfy "this is gone", and it silently drops the
    object from its own database (§2). A track found by search and not yet
    added to the library is an everyday case, and it must not vanish from the
    results. That very same "honest" 70 from getAlbum earned phase 1 some 684
    repeat calls in half an hour and audio dropouts — see the comment above."""
    video_id = params.get("id")
    if not video_id:
        return _error_response(10, "Required parameter 'id' is missing")
    song = _get_library().get_song(video_id) or _cache_get(video_id)
    if song is None:
        return _error_response(70, "Not found")
    root = _root("ok")
    _add_song_element(root, "song", video_id, song)
    return _xml_response(root)


async def _get_artists(params, request: Request) -> Response:
    """The Artists tab. Subsonic has no flat list — only <index> entries by
    first letter, and those are what Amperfy parses."""
    by_artist: "OrderedDict[str, list]" = OrderedDict()
    for video_id, meta in _library_songs():
        name = meta.get("artist") or ""
        if name:  # an unnamed artist has nothing to show and no reason to appear
            by_artist.setdefault(name, []).append((video_id, meta))

    indexes: "OrderedDict[str, list[str]]" = OrderedDict()
    for name in sorted(by_artist, key=_sort_key):
        indexes.setdefault(_index_letter(name), []).append(name)

    root = _root("ok")
    container = ET.SubElement(root, "artists", {"ignoredArticles": ""})
    for letter, names in indexes.items():
        index = ET.SubElement(container, "index", {"name": letter})
        for name in names:
            songs = by_artist[name]
            ET.SubElement(index, "artist", {
                "id": _artist_id(name),
                "name": name,
                "albumCount": str(len(_group_by_album(songs))),
                "coverArt": songs[0][0],
            })
    return _xml_response(root)


async def _get_album_list2(params, request: Request) -> Response:
    """The Albums tab. Amperfy pages through it until a response comes back
    empty, so offset/size must slice the list honestly — otherwise the client
    either never reaches the end or never stops."""
    list_type = params.get("type") or "alphabeticalByName"
    size = _int_param(params, "size", 10)
    offset = _int_param(params, "offset", 0)

    groups = [item for item in _group_by_album(_library_songs()).items()
              if not _is_single(item[1])]
    if list_type == "starred":
        # Stars live on songs here (the starred table), so a "starred album"
        # is an album that contains a starred song.
        starred = {song["id"] for song in _get_library().get_starred()}
        groups = [g for g in groups if any(v in starred for v, _ in g[1])]

    if list_type == "random":
        random.shuffle(groups)
    elif list_type in ("newest", "recent", "frequent"):
        # This server keeps no play counts and will not start keeping them for
        # two tabs: "frequent" and "recent" answer the same as "newest".
        groups.sort(key=lambda item: _album_created(item[1]), reverse=True)
    elif list_type == "alphabeticalByArtist":
        groups.sort(key=lambda item: (_sort_key(item[1][0][1].get("artist")),
                                      _sort_key(_album_name(item[1][0][1]))))
    else:
        groups.sort(key=lambda item: _sort_key(_album_name(item[1][0][1])))

    root = _root("ok")
    container = ET.SubElement(root, "albumList2")
    for album_id, songs in groups[offset:offset + size]:
        _add_album_element(container, album_id, songs, with_songs=False)
    return _xml_response(root)


async def _get_album(params, request: Request) -> Response:
    album_id = params.get("id")
    if not album_id:
        return _error_response(10, "Required parameter 'id' is missing")
    songs = [(video_id, meta) for video_id, meta in _known_songs()
             if _album_id(meta.get("artist") or "", _album_name(meta)) == album_id]
    root = _root("ok")
    _add_album_element(root, album_id, songs, with_songs=True)
    return _xml_response(root)


async def _get_artist(params, request: Request) -> Response:
    """An artist's albums. An empty response is still a success, for the same
    reason as in _get_album."""
    artist_id = params.get("id")
    if not artist_id:
        return _error_response(10, "Required parameter 'id' is missing")

    songs = [(video_id, meta) for video_id, meta in _known_songs()
             if _artist_id(meta.get("artist") or "") == artist_id]
    by_album = _group_by_album(songs)

    root = _root("ok")
    element = ET.SubElement(root, "artist", {
        "id": artist_id,
        "name": (songs[0][1].get("artist") or "") if songs else "",
        "albumCount": str(len(by_album)),
    })
    if songs:
        element.set("coverArt", songs[0][0])
    for album_id, album_songs in by_album.items():
        _add_album_element(element, album_id, album_songs, with_songs=False)
    return _xml_response(root)


# ---------------------------------------------------------------------------
# library (phase 2) — playlists and starred songs in SQLite (library.py).
# ---------------------------------------------------------------------------

async def _resolve_song_meta(video_id: str) -> dict:
    """A track's title/artist/album/duration/artwork_url before it first lands
    in library.songs. Amperfy sends only a videoId when adding, so the search
    cache (§7) is usually the only place the rich metadata still lives. A miss
    (cache evicted, worker restarted, the track never came through search3) is
    not fatal: main.get_song_details pulls title, artist and duration from an
    anonymous /youtubei/v1/player, a safe resolve behind
    main._player_semaphore. yt-dlp is never called here under any
    circumstances — that would be a queue of resolves and a captcha for the
    whole server (docs/SUBSONIC.md §8).
    """
    meta = _cache_get(video_id)
    if meta is not None:
        return {
            "title": meta.get("title") or video_id,
            "artist": meta.get("artist") or "",
            "album": meta.get("album"),
            "duration": meta.get("duration"),
            "artwork_url": meta.get("artworkURL"),
        }
    details = await main.get_song_details(video_id)  # never raises
    # A videoId as the title is what would be left here without title/author
    # from that same /player response: a string like "wU26xVT_vBU" sitting in
    # the playlist forever, because songs is written with INSERT and adding the
    # track again will not fix it.
    return {
        "title": details["title"] or video_id,
        "artist": details["artist"] or "",
        "album": None,
        "duration": details["duration"] or None,
        "artwork_url": details["artwork"],
    }


def _playlist_element(parent: ET.Element, playlist: dict, with_songs: bool) -> ET.Element:
    attrib = {
        "id": str(playlist["id"]),
        "name": playlist["name"],
        "comment": "",
        "owner": _get_credentials()[0],
        "public": "false",
        "songCount": str(playlist.get("song_count", 0)),
        "duration": str(playlist.get("duration") or 0),
        "created": playlist["created_at"],
        "changed": playlist["changed_at"],
        "coverArt": f"pl-{playlist['id']}",
    }
    element = ET.SubElement(parent, "playlist", attrib)
    if with_songs:
        for song in playlist.get("songs", []):
            _add_song_element(element, "entry", song["id"], song)
    return element


def _parse_playlist_id(params) -> Optional[int]:
    raw = params.get("id") or params.get("playlistId")
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


async def _get_playlists(params, request: Request) -> Response:
    root = _root("ok")
    container = ET.SubElement(root, "playlists")
    for playlist in _get_library().get_playlists():
        _playlist_element(container, playlist, with_songs=False)
    return _xml_response(root)


async def _get_playlist(params, request: Request) -> Response:
    playlist_id = _parse_playlist_id(params)
    if playlist_id is None:
        return _error_response(10, "Required parameter 'id' is missing")
    playlist = _get_library().get_playlist(playlist_id)
    if playlist is None:
        return _error_response(70, "Playlist not found")
    root = _root("ok")
    _playlist_element(root, playlist, with_songs=True)
    return _xml_response(root)


async def _create_playlist(params, request: Request) -> Response:
    name = params.get("name")
    if not name:
        return _error_response(10, "Required parameter 'name' is missing")
    playlist_id = _get_library().create_playlist(name)
    # The response must carry the new id — otherwise Amperfy goes looking for
    # the playlist by name in getPlaylists and risks binding to the wrong one
    # (docs/SUBSONIC.md §5, createPlaylist).
    playlist = _get_library().get_playlist(playlist_id)
    root = _root("ok")
    _playlist_element(root, playlist, with_songs=True)
    return _xml_response(root)


async def _delete_playlist(params, request: Request) -> Response:
    playlist_id = _parse_playlist_id(params)
    if playlist_id is None:
        return _error_response(10, "Required parameter 'id' is missing")
    if not _get_library().delete_playlist(playlist_id):
        return _error_response(70, "Playlist not found")
    return _ok_response()


async def _update_playlist(params, request: Request) -> Response:
    playlist_id = _parse_playlist_id(params)
    if playlist_id is None:
        return _error_response(10, "Required parameter 'playlistId' is missing")

    lib = _get_library()
    current = lib.get_playlist(playlist_id)
    if current is None:
        return _error_response(70, "Playlist not found")

    remove_indices = []
    for raw in params.getlist("songIndexToRemove"):
        try:
            remove_indices.append(int(raw))
        except ValueError:
            continue
    add_ids = params.getlist("songIdToAdd")

    add_songs = []
    for video_id in add_ids:
        meta = await _resolve_song_meta(video_id)
        add_songs.append((video_id, meta["title"], meta["artist"],
                          meta["album"], meta["duration"], meta["artwork_url"]))

    name = params.get("name") or current["name"]  # Amperfy always sends name,
                                                  # but do not lose the old one
    if not lib.update_playlist(playlist_id, name, remove_indices, add_songs):
        return _error_response(70, "Playlist not found")
    return _ok_response()


async def _get_starred2(params, request: Request) -> Response:
    root = _root("ok")
    container = ET.SubElement(root, "starred2")
    for song in _get_library().get_starred():
        meta = dict(song)
        meta["starred"] = song["starred_at"]  # see _add_song_element
        _add_song_element(container, "song", song["id"], meta)
    return _xml_response(root)


async def _star(params, request: Request) -> Response:
    """`id` is a song; albumId/artistId are accepted and ignored
    (docs/SUBSONIC.md §5). Phase 3 did not change that: the `starred` table
    holds songs, and starring an album would need a table of its own for a tab
    nobody asked for. `getAlbumList2?type=starred` still works — an album
    counts as starred when it contains a starred song."""
    song_id = params.get("id")
    if not song_id:
        return _ok_response()
    meta = await _resolve_song_meta(song_id)
    _get_library().star(song_id, meta["title"], meta["artist"],
                        meta["album"], meta["duration"], meta["artwork_url"])
    return _ok_response()


async def _unstar(params, request: Request) -> Response:
    song_id = params.get("id")
    if song_id:
        _get_library().unstar(song_id)
    return _ok_response()


async def _get_random_songs(params, request: Request) -> Response:
    size = _int_param(params, "size", 10)
    root = _root("ok")
    container = ET.SubElement(root, "randomSongs")
    for song in _get_library().get_random_songs(size):
        _add_song_element(container, "song", song["id"], song)
    return _xml_response(root)


# ---------------------------------------------------------------------------
# stubs — docs/SUBSONIC.md §9, empty success.
# ---------------------------------------------------------------------------

async def _ping(params, request: Request) -> Response:
    return _ok_response()


async def _noop_ok(params, request: Request) -> Response:
    return _ok_response()


def _submission_is_true(raw: Optional[str]) -> bool:
    return raw is None or raw.lower() in ("true", "1")


def _parse_scrobble_time(raw: Optional[str]) -> Optional[int]:
    if raw is None:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value >= 0 else None


def _now_ms() -> int:
    return int(time.time() * 1000)


# The client's current "playing now" track, waiting for the ping that ends it.
# One listener, one worker process, so a module-level slot is the whole store.
_playing_now: dict = {}

# How long an unknown-length track has to play before it counts.
_UNKNOWN_DURATION_THRESHOLD_SECONDS = 120


def _listen_threshold_ms(duration) -> Optional[int]:
    """Last.fm's rule: half the track, capped at four minutes. None means the
    track can never count — anything under thirty seconds."""
    if duration is None:
        return _UNKNOWN_DURATION_THRESHOLD_SECONDS * 1000
    if duration < 30:
        return None
    return int(min(duration / 2, 240) * 1000)


async def _note_playing_now(video_id: str, now_ms: int) -> None:
    """Count the previous track, then remember this one.

    Amperfy sends only submission=false: one ping three seconds into every
    track and nothing at all when it ends — verified over 23 minutes of
    uninterrupted playback, 8 tracks, not one submission=true. Taking the ping
    itself as a listen would count every skip, so a track is credited when the
    *next* ping arrives and it had been playing long enough.

    ponytail: the pending track lives in memory. A worker restart drops at
    most the one currently playing, and the last track of a session is
    credited only when the next one starts. Persisting it would buy one
    listen at the cost of a table and a migration.
    """
    previous = _playing_now.get("current")
    if previous is not None:
        elapsed = now_ms - previous["started_ms"]
        threshold = _listen_threshold_ms(previous["meta"].get("duration"))
        if not previous["counted"] and threshold is not None and elapsed >= threshold:
            _get_library().record_listen(
                {"id": previous["id"], **previous["meta"]}, previous["started_ms"]
            )
            previous["counted"] = True
        if previous["id"] == video_id and not _restarted(previous, elapsed):
            # The same track still playing: Amperfy re-pings it about once a
            # minute. Keep the original start and the counted flag, or one play
            # is credited again on every ping past the threshold.
            return
    _playing_now["current"] = {
        "id": video_id,
        "meta": await _resolve_song_meta(video_id),
        "started_ms": now_ms,
        "counted": False,
    }


def _restarted(previous: dict, elapsed_ms: int) -> bool:
    """Whether a repeated ping for the same track is a second play rather than
    the client repeating itself: it can only be one once the track has had time
    to finish.

    ponytail: an unknown duration never counts as a restart, so a track with no
    metadata on repeat-one is credited once per session. Fixing that needs real
    playback position, which the Subsonic ping does not carry."""
    duration = previous["meta"].get("duration")
    return duration is not None and elapsed_ms >= duration * 1000


async def _scrobble(params, request: Request) -> Response:
    if not _submission_is_true(params.get("submission")):
        ids = params.getlist("id")
        if ids:
            await _note_playing_now(ids[0], _now_ms())
        return _ok_response()
    ids = params.getlist("id")
    if not ids:
        return _error_response(10, "Required parameter 'id' is missing")
    raw_times = params.getlist("time")
    for index, video_id in enumerate(ids):
        played_at_ms = _parse_scrobble_time(
            raw_times[index] if index < len(raw_times) else None
        )
        pending = _playing_now.get("current")
        already_counted = (
            pending is not None
            and pending["id"] == video_id
            and pending["counted"]
        )
        # Dropping the pending track is not enough on its own: by the time the
        # client submits, the re-pings have usually already credited the play.
        # The two rows carry different timestamps and would survive the
        # UNIQUE(song_id, played_at_ms) as one play recorded twice.
        if not already_counted:
            meta = await _resolve_song_meta(video_id)
            _get_library().record_listen({"id": video_id, **meta}, played_at_ms)
        if pending is not None and pending["id"] == video_id:
            _playing_now.pop("current", None)
    return _ok_response()


async def _not_found(params, request: Request) -> Response:
    return _error_response(70, "Not found")


_HANDLERS = {
    "ping": _ping,
    "search3": _search3,
    "stream": _stream_or_download,
    "download": _stream_or_download,
    "getCoverArt": _get_cover_art,
}


def _register_stub(action: str, tag: str, attrib: Optional[dict] = None) -> None:
    async def handler(params, request: Request, _tag=tag, _attrib=attrib) -> Response:
        return _ok_response(lambda root: ET.SubElement(root, _tag, _attrib or {}))
    _HANDLERS[action] = handler


_register_stub("getGenres", "genres")
_register_stub("getIndexes", "indexes", {"lastModified": "0", "ignoredArticles": ""})
_register_stub("getMusicFolders", "musicFolders")
_register_stub("getPodcasts", "podcasts")
_register_stub("getNewestPodcasts", "newestPodcasts")
_register_stub("getInternetRadioStations", "internetRadioStations")
_register_stub("getSimilarSongs2", "similarSongs2")

_HANDLERS["getOpenSubsonicExtensions"] = _noop_ok
_HANDLERS["scrobble"] = _scrobble
_HANDLERS["setRating"] = _noop_ok
_HANDLERS["deletePodcastEpisode"] = _noop_ok

# phase 3 — artists and albums derived from the library's tracks
_HANDLERS["getSong"] = _get_song
_HANDLERS["getArtists"] = _get_artists
_HANDLERS["getAlbumList2"] = _get_album_list2
_HANDLERS["getAlbum"] = _get_album
_HANDLERS["getArtist"] = _get_artist
# getMusicDirectory — folder browsing, which Amperfy never calls at all: it
# browses by ID3 (getArtists/getAlbumList2). An error here is safe, there is
# no retry loop.
_HANDLERS["getMusicDirectory"] = _not_found

# phase 2 — the SQLite library (library.py)
_HANDLERS["getPlaylists"] = _get_playlists
_HANDLERS["getPlaylist"] = _get_playlist
_HANDLERS["createPlaylist"] = _create_playlist
_HANDLERS["updatePlaylist"] = _update_playlist
_HANDLERS["deletePlaylist"] = _delete_playlist
_HANDLERS["getStarred2"] = _get_starred2
_HANDLERS["star"] = _star
_HANDLERS["unstar"] = _unstar
_HANDLERS["getRandomSongs"] = _get_random_songs


# ---------------------------------------------------------------------------
# dispatch
# ---------------------------------------------------------------------------

_LOGGED_VALUES = {
    "search3": ("songCount", "songOffset"),
    "getAlbumList2": ("type", "size", "offset"),
    # An album id is a hash of the name, not a secret. It is the only way to
    # tell which albums the client is asking about: the ones the server serves,
    # or ghosts of its own.
    "getAlbum": ("id",),
    # A scrobble the server answers "ok" and drops is otherwise invisible:
    # submission=false is a now-playing ping, not a listen. Without the value
    # in the log, a client that never sends the real submission looks exactly
    # like one that scrobbles fine.
    "scrobble": ("id", "submission", "time"),
}


@router.get("/{action}.view")
async def rest_dispatch(action: str, request: Request) -> Response:
    params = request.query_params
    # Never log t/s/p — that would leak credentials the same way an unfiltered
    # httpx logger would leak signed media URLs (docs/SUBSONIC.md §3, main.py's own
    # httpx/httpcore log suppression follows the same rule).
    # These values are not secrets either, and they are the only way to see
    # what the client is actually asking for: both the ceiling on search
    # results and the traversal order of the Albums tab are set by the client,
    # not by the server.
    extra = "".join(f" {name}={params.get(name)}"
                    for name in _LOGGED_VALUES.get(action, ()))
    logger.info("rest action=%s params=%s%s", action, sorted(params.keys()), extra)

    if not _authenticate(params):
        return _error_response(40, "Wrong username or password")

    handler = _HANDLERS.get(action)
    if handler is None:
        logger.warning("rest unknown action=%s params=%s", action, sorted(params.keys()))
        return _error_response(0, "Unsupported action")
    return await handler(params, request)
