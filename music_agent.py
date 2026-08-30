"""Idempotent weekly discovery-playlist orchestration."""
import asyncio
import re
import unicodedata
from datetime import datetime, timezone
from typing import Optional

import main
from ranking import rank_playlists, rank_tracks


MAX_PLAYLIST_SIZE = 50


def _norm(value: str) -> str:
    value = unicodedata.normalize("NFKD", value or "").casefold()
    return " ".join(re.findall(r"[\w]+", value))


def _candidate_matches(candidate: dict, track: dict) -> bool:
    if _norm(candidate["title"]) != _norm(track.get("title", "")):
        return False
    if _norm(candidate["artist"]) != _norm(track.get("artist", "")):
        return False
    expected = candidate.get("duration_seconds")
    actual = track.get("durationSeconds")
    return expected is None or actual is None or abs(expected - actual) <= 12


async def match_candidate(candidate: dict) -> Optional[tuple]:
    page = await main.search(q=f"{candidate['artist']} {candidate['title']}", limit=20)
    if not isinstance(page, dict):
        return None
    for track in page.get("tracks") or []:
        if _candidate_matches(candidate, track):
            return (
                track["id"], track["title"], track["artist"], track.get("album"),
                track.get("durationSeconds"), track.get("artworkURL"),
            )
    return None


async def sync_unsent_listens(lib, lb, now_ms: int) -> int:
    sent = 0
    while True:
        events = lib.get_unsynced_listens(limit=100)
        if not events:
            return sent
        await lb.submit_listens(events)
        lib.mark_listens_synced([event["event_id"] for event in events], now_ms)
        sent += len(events)
        if len(events) < 100:
            return sent


def _recording_candidate(metadata: dict, score: float) -> Optional[dict]:
    recording = metadata.get("recording") or {}
    artist = metadata.get("artist") or {}
    release = metadata.get("release") or {}
    title = (recording.get("name") if isinstance(recording, dict) else None) or metadata.get("recording_name")
    artist_name = (artist.get("name") if isinstance(artist, dict) else None) or metadata.get("artist_name")
    if isinstance(artist, str):
        artist_name = artist
    album = (release.get("name") if isinstance(release, dict) else None) or metadata.get("release_name")
    if isinstance(release, str):
        album = release
    mbid = metadata.get("recording_mbid")
    if not title or not artist_name:
        return None
    return {
        "title": title,
        "artist": artist_name,
        "album": album,
        "duration_seconds": metadata.get("duration_seconds"),
        "recording_mbid": mbid,
        "source": "cf",
        "score": score,
    }


def _release_mbid(release: dict) -> Optional[str]:
    return release.get("release_mbid") or release.get("mbid") or release.get("id")


def _safe_error(exc: Exception) -> str:
    """A stable, credential-free persisted failure summary."""
    return f"weekly discovery failed ({exc.__class__.__name__})"[:500]


def _completed_result(lib, run: dict) -> dict:
    playlist_id = run.get("playlist_id")
    return {
        "status": "completed",
        "added": lib.get_weekly_recommendation_count(run["week_start"]),
        "playlist_id": playlist_id,
    }


def _validate_size(size: object) -> int:
    if isinstance(size, bool):
        raise ValueError("size must be an integer from 1 to 50")
    if isinstance(size, int):
        value = size
    elif isinstance(size, str) and re.fullmatch(r"[0-9]+", size.strip()):
        value = int(size)
    else:
        raise ValueError("size must be an integer from 1 to 50")
    if not 1 <= value <= MAX_PLAYLIST_SIZE:
        raise ValueError("size must be an integer from 1 to 50")
    return value


async def run_weekly(lib, lb, user: str, now: datetime, size: int) -> dict:
    size = _validate_size(size)
    now_utc = now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now.astimezone(timezone.utc)
    week_start = now_utc.date().fromordinal(now_utc.date().toordinal() - now_utc.weekday()).isoformat()
    existing = lib.get_weekly_run(week_start)
    if existing is not None and existing["status"] == "completed":
        return _completed_result(lib, existing)

    lib.begin_weekly_run(week_start)
    try:
        await sync_unsent_listens(lib, lb, int(now_utc.timestamp() * 1000))
        recommendations = await lb.get_recommendation_mbids(user, 50) or []
        ordered_recommendations = [
            row for row in recommendations[:50] if row.get("recording_mbid")
        ]
        mbids = list(dict.fromkeys(row["recording_mbid"] for row in ordered_recommendations))
        metadata = await lb.get_recording_metadata(mbids)
        metadata_by_mbid = {
            item.get("recording_mbid"): item for item in metadata if item.get("recording_mbid")
        }
        candidates = []
        for recommendation in ordered_recommendations:
            item = metadata_by_mbid.get(recommendation["recording_mbid"])
            if item is None:
                continue
            candidate = _recording_candidate(item, float(recommendation.get("score") or 0.0))
            if candidate is not None:
                candidates.append(candidate)

        releases = await lb.get_fresh_releases(user, days=14)
        for release in (releases or [])[:10]:
            release_mbid = _release_mbid(release)
            if not release_mbid:
                continue
            for item in (await lb.get_release_tracks(release_mbid))[:2]:
                if item.get("title") and item.get("artist"):
                    candidates.append({
                        "title": item["title"], "artist": item["artist"],
                        "album": item.get("album"),
                        "duration_seconds": item.get("duration_seconds"),
                        "recording_mbid": item.get("recording_mbid"),
                        "source": "fresh", "score": 0.0,
                    })

        unique = []
        identities = set()
        for candidate in candidates:
            identity = (_norm(candidate["artist"]), _norm(candidate["title"]))
            if identity not in identities:
                identities.add(identity)
                unique.append(candidate)
        local = {(_norm(song["artist"]), _norm(song["title"])) for song in lib.get_songs()}
        candidates = [candidate for candidate in unique
                      if (_norm(candidate["artist"]), _norm(candidate["title"])) not in local]

        accepted = []
        for index, candidate in enumerate(candidates):
            matched = await match_candidate(candidate)
            if matched is not None:
                accepted.append((candidate, matched))
                if len(accepted) >= size:
                    break
            if index < len(candidates) - 1:
                await asyncio.sleep(0.3)

        if not accepted:
            lib.complete_weekly_run(week_start, None, [])
            return {"status": "completed", "added": 0, "playlist_id": None}

        name = f"Discoveries — {week_start}"
        add_songs = [matched for _, matched in accepted]
        items = [
            {"song_id": matched[0], "source": candidate["source"],
             "recording_mbid": candidate.get("recording_mbid"), "score": candidate["score"]}
            for candidate, matched in accepted
        ]
        playlist_id = lib.finalize_weekly_playlist(week_start, name, add_songs, items)
        return {"status": "completed", "added": len(accepted), "playlist_id": playlist_id}
    except Exception as exc:
        lib.fail_weekly_run(week_start, _safe_error(exc))
        raise


def build_rankings(lib, now_ms: int) -> dict:
    tracks = rank_tracks(lib.get_listen_stats(0), now_ms)
    scores = {track["song_id"]: track["score"] for track in tracks}
    playlists = rank_playlists(lib.get_playlist_song_ids(), scores)
    return {
        "tracks": [
            {"song_id": row["song_id"], "score": row["score"],
             "listen_count": row["listen_count"]}
            for row in tracks
        ],
        "playlists": [
            {"id": row["id"], "name": row["name"], "score": row["score"],
             "listened_coverage": row["listened_coverage"]}
            for row in playlists
        ],
    }
