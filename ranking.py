import math


DAY_MS = 86_400_000
RECENCY_HALF_LIFE_DAYS = 30.0


def track_score(row: dict, now_ms: int) -> float:
    count = max(0, int(row.get("listen_count") or 0))
    last_ms = int(row.get("last_played_ms") or 0)
    age_days = max(0.0, (now_ms - last_ms) / DAY_MS)
    frequency = math.log1p(count) / math.log1p(30)
    recency = math.pow(0.5, age_days / RECENCY_HALF_LIFE_DAYS)
    starred = 1.0 if row.get("starred") else 0.0
    return round(0.60 * frequency + 0.25 * recency + 0.15 * starred, 6)


def rank_tracks(rows: list[dict], now_ms: int) -> list[dict]:
    scored = [{**row, "score": track_score(row, now_ms)} for row in rows]
    return sorted(scored, key=lambda item: (-item["score"], item["song_id"]))


def rank_playlists(playlists: list[dict], scores: dict[str, float]) -> list[dict]:
    ranked = []
    for playlist in playlists:
        song_ids = playlist.get("song_ids") or []
        values = [scores.get(song_id, 0.0) for song_id in song_ids]
        coverage = sum(value > 0 for value in values) / len(values) if values else 0.0
        mean = sum(values) / len(values) if values else 0.0
        ranked.append(
            {
                **playlist,
                "score": round(0.80 * mean + 0.20 * coverage, 6),
                "listened_coverage": round(coverage, 6),
            }
        )
    return sorted(ranked, key=lambda item: (-item["score"], item["id"]))
