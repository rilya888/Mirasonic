"""Idempotent weekly discovery-playlist orchestration."""
import asyncio
import argparse
import json
import logging
import os
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Callable, Mapping, Optional

import httpx
import main
import library
from listenbrainz_client import ListenBrainzClient
from ranking import rank_playlists, rank_tracks


MAX_PLAYLIST_SIZE = 50
LISTENBRAINZ_BASE_URL = "https://api.listenbrainz.org"
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AgentConfig:
    db_path: str
    user: str | None
    token: str | None = field(repr=False)
    weekday: int
    hour_utc: int
    playlist_size: int


def _schedule_value(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError("invalid weekly schedule")
    if isinstance(value, str) and not re.fullmatch(r"-?[0-9]+", value.strip()):
        raise ValueError("invalid weekly schedule")
    return int(value)


def scheduled_week(now: datetime, weekday: int, hour_utc: int) -> tuple[str, datetime]:
    """Return the latest UTC schedule occurrence, including a missed run."""
    if weekday not in range(7) or hour_utc not in range(24):
        raise ValueError("invalid weekly schedule")
    now_utc = now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now.astimezone(timezone.utc)
    days_since = (now_utc.weekday() - weekday) % 7
    target_date = now_utc.date() - timedelta(days=days_since)
    target = datetime.combine(target_date, time(hour_utc), tzinfo=timezone.utc)
    if target > now_utc:
        target -= timedelta(days=7)
    week_start = target.date() - timedelta(days=target.weekday())
    return week_start.isoformat(), target


def agent_config(
    environ: Mapping[str, str] | None = None, *, require_listenbrainz: bool = False
) -> AgentConfig:
    """Read operational settings without ever placing credentials in errors."""
    env = os.environ if environ is None else environ
    weekday = _schedule_value(env.get("AGENT_WEEKDAY", "0"))
    hour_utc = _schedule_value(env.get("AGENT_HOUR_UTC", "6"))
    scheduled_week(datetime(2000, 1, 3, tzinfo=timezone.utc), weekday, hour_utc)
    playlist_size = _validate_size(env.get("AGENT_PLAYLIST_SIZE", "30"))
    user = env.get("LISTENBRAINZ_USER") or None
    token = env.get("LISTENBRAINZ_TOKEN") or None
    if require_listenbrainz and (not user or not token):
        raise ValueError("ListenBrainz user and token are required for weekly discovery")
    return AgentConfig(
        env.get("MIRASONIC_DB", library.DEFAULT_DB_PATH), user, token,
        weekday, hour_utc, playlist_size,
    )


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


def _weekly_run_start(now: datetime, week_start: str | None) -> str:
    if week_start is None:
        return (now.date() - timedelta(days=now.weekday())).isoformat()
    try:
        parsed = date.fromisoformat(week_start)
    except (TypeError, ValueError) as exc:
        raise ValueError("week_start must be an ISO Monday date") from exc
    if parsed.weekday() != 0:
        raise ValueError("week_start must be an ISO Monday date")
    return parsed.isoformat()


async def run_weekly(
    lib, lb, user: str, now: datetime, size: int, *, week_start: str | None = None
) -> dict:
    size = _validate_size(size)
    now_utc = now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now.astimezone(timezone.utc)
    week_start = _weekly_run_start(now_utc, week_start)
    existing = lib.get_weekly_run(week_start)
    if existing is not None and existing["status"] == "completed":
        return _completed_result(lib, existing)

    claim = lib.begin_weekly_run(week_start)
    if not claim["claimed"]:
        if claim["status"] == "completed":
            return _completed_result(lib, claim)
        return {"status": "running", "added": 0, "playlist_id": claim["playlist_id"]}
    claim_token = claim["claim_token"]
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
            lib.complete_weekly_run(week_start, None, [], claim_token=claim_token)
            return {"status": "completed", "added": 0, "playlist_id": None}

        name = f"Discoveries — {week_start}"
        add_songs = [matched for _, matched in accepted]
        items = [
            {"song_id": matched[0], "source": candidate["source"],
             "recording_mbid": candidate.get("recording_mbid"), "score": candidate["score"]}
            for candidate, matched in accepted
        ]
        playlist_id = lib.finalize_weekly_playlist(
            week_start, name, add_songs, items, claim_token=claim_token
        )
        return {"status": "completed", "added": len(accepted), "playlist_id": playlist_id}
    except Exception as exc:
        lib.fail_weekly_run(week_start, _safe_error(exc), claim_token=claim_token)
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


async def daemon(
    config: AgentConfig,
    lib,
    lb,
    *,
    now_fn: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    sleep: Callable[[float], object] = asyncio.sleep,
) -> None:
    """Check hourly and retry the latest scheduled run until it completes."""
    while True:
        week_start = None
        now = now_fn()
        # Hourly, not once a week inside run_weekly: a completed week returns
        # early there, so scrobbles played after it would sit unsent for days.
        # Its own try — a ListenBrainz outage must not also skip discovery.
        try:
            # The only line the healthy loop prints: without it a daemon that has
            # nothing to send is indistinguishable from one that died.
            sent = await sync_unsent_listens(lib, lb, int(now.timestamp() * 1000))
            logger.info("listen sync ok sent=%d", sent)
        except Exception as exc:
            logger.error("listen sync failed error_type=%s", type(exc).__name__)
        try:
            week_start, _scheduled = scheduled_week(now, config.weekday, config.hour_utc)
            run = lib.get_weekly_run(week_start)
            if run is None or run["status"] != "completed":
                await run_weekly(
                    lib, lb, config.user or "", now, config.playlist_size, week_start=week_start
                )
        except Exception as exc:
            # Never include raw exceptions: HTTP clients can contain a token in them.
            if week_start is None:
                logger.error("weekly agent iteration failed error_type=%s", type(exc).__name__)
            else:
                logger.error(
                    "weekly agent iteration failed week_start=%s error_type=%s",
                    week_start, type(exc).__name__,
                )
        await sleep(3600)


async def _run_weekly_command(config: AgentConfig, lib) -> dict:
    """Run one discovery pass with a short-lived injected HTTP client."""
    async with httpx.AsyncClient(base_url=LISTENBRAINZ_BASE_URL) as client:
        lb = ListenBrainzClient(config.token or "", client)
        return await run_weekly(
            lib, lb, config.user or "", datetime.now(timezone.utc), config.playlist_size
        )


async def _run_daemon_command(config: AgentConfig, lib) -> None:
    """Keep the daemon's HTTP client alive for its complete service lifetime."""
    async with httpx.AsyncClient(base_url=LISTENBRAINZ_BASE_URL) as client:
        lb = ListenBrainzClient(config.token or "", client)
        await daemon(config, lib, lb)


def cli(argv: list[str] | None = None) -> int:
    """Command-line entry point for local rankings and scheduled discovery."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("rankings", "weekly", "daemon"))
    args = parser.parse_args(argv)
    # Without a handler the daemon's logger drops everything below WARNING, so
    # a container that is syncing listens hourly looks identical to one that
    # has silently stopped. rankings prints JSON to stdout — keep it clean.
    if args.command != "rankings":
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s %(message)s",
            stream=os.sys.stderr,
        )
    requires_listenbrainz = args.command in {"weekly", "daemon"}
    try:
        config = agent_config(require_listenbrainz=requires_listenbrainz)
    except ValueError as exc:
        print(f"configuration error: {exc}", file=os.sys.stderr)
        return 2

    lib = library.Library(config.db_path)
    if args.command == "rankings":
        print(json.dumps(build_rankings(lib, int(datetime.now(timezone.utc).timestamp() * 1000))))
        return 0
    try:
        if args.command == "weekly":
            asyncio.run(_run_weekly_command(config, lib))
        else:
            asyncio.run(_run_daemon_command(config, lib))
    except Exception:
        print("weekly agent run failed", file=os.sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(cli())
