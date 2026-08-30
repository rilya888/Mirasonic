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


def _recording_candidate(metadata: dict, scores: dict[str, float]) -> Optional[dict]:
    recording = metadata.get("recording") or {}
    artist = metadata.get("artist") or {}
    release = metadata.get("release") or {}
    title = recording.get("name") if isinstance(recording, dict) else None
    artist_name = artist.get("name") if isinstance(artist, dict) else None
    mbid = metadata.get("recording_mbid")
    if not title or not artist_name:
        return None
    return {
        "title": title,
        "artist": artist_name,
        "album": release.get("name") if isinstance(release, dict) else None,
        "duration_seconds": metadata.get("duration_seconds"),
        "recording_mbid": mbid,
        "source": "cf",
        "score": scores.get(mbid, 0.0),
    }


def _release_mbid(release: dict) -> Optional[str]:
    return release.get("release_mbid") or release.get("mbid") or release.get("id")


def _safe_error(exc: Exception, token: str | None) -> str:
    message = str(exc).splitlines()[0] if str(exc) else exc.__class__.__name__
    if token:
        message = message.replace(token, "[redacted]")
    message = re.sub(r"(?i)traceback.*", "", message).strip()
    return message[:500]


def _completed_result(lib, run: dict) -> dict:
    playlist_id = run.get("playlist_id")
    playlist = lib.get_playlist(playlist_id) if playlist_id is not None else None
    return {
        "status": "completed",
        "added": playlist["song_count"] if playlist else 0,
        "playlist_id": playlist_id,
    }


async def run_weekly(lib, lb, user: str, now: datetime, size: int) -> dict:
    now_utc = now.astimezone(timezone.utc)
    week_start = now_utc.date().fromordinal(now_utc.date().toordinal() - now_utc.weekday()).isoformat()
    existing = lib.get_weekly_run(week_start)
    if existing is not None and existing["status"] == "completed":
        return _completed_result(lib, existing)

    run = lib.begin_weekly_run(week_start)
    size = max(1, min(MAX_PLAYLIST_SIZE, int(size)))
    try:
        await sync_unsent_listens(lib, lb, int(now_utc.timestamp() * 1000))
        recommendations = await lb.get_recommendation_mbids(user, 50)
        scores = {
            row.get("recording_mbid"): float(row.get("score") or 0.0)
            for row in recommendations if row.get("recording_mbid")
        }
        metadata = await lb.get_recording_metadata(list(scores))
        candidates = [candidate for item in metadata
                      if (candidate := _recording_candidate(item, scores)) is not None]

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

        playlist_id = run.get("playlist_id")
        name = f"Discoveries — {week_start}"
        if playlist_id is None:
            playlist_id = lib.create_playlist(name)
            lib.set_weekly_run_playlist(week_start, playlist_id)
        playlist = lib.get_playlist(playlist_id)
        if playlist is None:
            raise RuntimeError("agent playlist is missing")
        add_songs = [matched for _, matched in accepted]
        if not lib.update_playlist(
            playlist_id, name, list(range(playlist["song_count"])), add_songs
        ):
            raise RuntimeError("agent playlist update failed")
        items = [
            {"song_id": matched[0], "source": candidate["source"],
             "recording_mbid": candidate.get("recording_mbid"), "score": candidate["score"]}
            for candidate, matched in accepted
        ]
        lib.complete_weekly_run(week_start, playlist_id, items)
        return {"status": "completed", "added": len(accepted), "playlist_id": playlist_id}
    except Exception as exc:
        lib.fail_weekly_run(week_start, _safe_error(exc, getattr(lb, "token", None)))
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
