# Music Recommendation Agent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a low-memory, self-hosted agent that records completed listens, ranks tracks and playlists, synchronizes listening history to ListenBrainz, and creates one idempotent discovery playlist every week.

**Architecture:** Keep Mirasonic as the playback and playlist server. Persist listen events and weekly-run state in its existing SQLite database, keep scoring deterministic in a pure Python module, isolate ListenBrainz HTTP calls behind an injected client, and run weekly orchestration in a second process built from the same Docker image. The agent creates or refreshes a private Mirasonic playlist; it never rewrites user-created playlists.

**Tech Stack:** Python 3.12, FastAPI, SQLite/WAL, httpx, Docker Compose, pytest; no local LLM and no new scheduler dependency.

## Global Constraints

- Preserve the current single-listener, anonymous-to-YouTube design.
- Keep SQLite as the only durable store and `/data` as the only writable container mount.
- Count only Subsonic `scrobble` requests whose `submission` value is true; `submission=false` is “playing now” and is not a completed listen.
- Deduplicate explicit client timestamps exactly; when no timestamp is supplied, suppress a retry of the same song within 30 seconds.
- Store timestamps as Unix milliseconds in SQLite and convert only at API boundaries.
- Keep all ranking deterministic and explainable; do not add an LLM or ML runtime.
- Make ListenBrainz optional for playback and ranking, but require `LISTENBRAINZ_USER` and `LISTENBRAINZ_TOKEN` for external discovery and listen synchronization.
- Limit each generated playlist to `AGENT_PLAYLIST_SIZE`, default 30 and hard maximum 50.
- Never modify, reorder, rename, or delete a playlist that was not created by the weekly agent.
- Use a deterministic weekly playlist name, `Discoveries — YYYY-MM-DD`, and update that playlist on retry rather than creating duplicates.
- All automated tests must be offline; external calls are mocked. Any new live test must use the existing `live` pytest marker.
- Reuse the existing YouTube Music search path and metadata fields; do not introduce Spotify Web API access in this version.
- Keep the existing security posture: bind Mirasonic to loopback, store tokens only in environment variables, and never log credentials or tokens.

## File Map

| File | Responsibility |
|---|---|
| `library.py` | SQLite schema and all persistence operations for listens, sync state, weekly runs, generated items, and playlist lookup. |
| `subsonic.py` | Parse Subsonic `scrobble` parameters, resolve song metadata, and record completed listens. |
| `ranking.py` | Pure scoring functions for tracks and playlists; no SQL, HTTP, environment, or wall-clock access. |
| `listenbrainz_client.py` | Typed async wrapper for listen submission, recommendations, recording metadata, fresh releases, and release tracks. |
| `music_agent.py` | Candidate filtering/matching, weekly orchestration, idempotency, CLI commands, and daemon loop. |
| `test_library_listening.py` | Persistence, deduplication, synchronization state, and weekly-run tests. |
| `test_ranking.py` | Fixed-clock unit tests for scoring and playlist-size normalization. |
| `test_listenbrainz_client.py` | MockTransport contract tests for every external endpoint. |
| `test_music_agent.py` | Candidate filtering, YouTube matching, idempotent playlist creation, failure, and daemon scheduling tests. |
| `test_subsonic.py` | Subsonic protocol tests for `scrobble`. |
| `Dockerfile` | Copy the agent modules into the image. |
| `compose.yaml` | Add the low-memory agent service sharing `/data`. |
| `.env.example` | Document agent configuration without real secrets. |
| `README.md` | User setup, commands, data flow, limitations, and recovery instructions. |

---

### Task 1: Persist completed listening history

**Files:**
- Modify: `library.py:18-57`
- Modify: `library.py` after `Library.get_starred`
- Create: `test_library_listening.py`

**Interfaces:**
- Produces: `Library.record_listen(song: dict, played_at_ms: Optional[int]) -> bool`
- Produces: `Library.get_listen_stats(since_ms: int) -> list[dict]`
- Produces: `Library.get_unsynced_listens(limit: int = 100) -> list[dict]`
- Produces: `Library.mark_listens_synced(event_ids: list[int], synced_at_ms: int) -> None`
- Produces: `Library.get_playlist_song_ids() -> list[dict]`
- Produces: `Library.get_playlist_by_name(name: str) -> Optional[dict]`

- [ ] **Step 1: Write failing persistence tests**

Create `test_library_listening.py` with a fixture that always uses a temporary database and tests insertion, exact-timestamp idempotency, retry suppression for missing timestamps, and raw statistics:

```python
import library


def song(song_id="vid-1", title="One", artist="Artist"):
    return {
        "id": song_id,
        "title": title,
        "artist": artist,
        "album": "Album",
        "duration": 180,
        "artwork_url": "https://example.invalid/cover.jpg",
    }


def test_record_listen_is_idempotent_for_explicit_timestamp(tmp_path):
    lib = library.Library(str(tmp_path / "library.db"))
    assert lib.record_listen(song(), 1_700_000_000_000) is True
    assert lib.record_listen(song(), 1_700_000_000_000) is False
    assert lib.get_listen_stats(0)[0]["listen_count"] == 1


def test_record_listen_suppresses_untimed_retry(monkeypatch, tmp_path):
    lib = library.Library(str(tmp_path / "library.db"))
    moments = iter((1_700_000_000_000, 1_700_000_010_000, 1_700_000_031_000))
    monkeypatch.setattr(library, "_now_ms", lambda: next(moments))
    assert lib.record_listen(song(), None) is True
    assert lib.record_listen(song(), None) is False
    assert lib.record_listen(song(), None) is True


def test_unsynced_listens_include_track_metadata(tmp_path):
    lib = library.Library(str(tmp_path / "library.db"))
    lib.record_listen(song(), 1_700_000_000_000)
    event = lib.get_unsynced_listens()[0]
    assert event["title"] == "One"
    assert event["artist"] == "Artist"
    assert event["played_at_ms"] == 1_700_000_000_000
```

- [ ] **Step 2: Run the focused tests and verify the missing-interface failure**

Run: `python -m pytest -q test_library_listening.py`

Expected: FAIL because `Library.record_listen` and the new query methods do not exist.

- [ ] **Step 3: Extend the schema with events and weekly state**

Add these statements to the existing `SCHEMA` string. `CREATE TABLE IF NOT EXISTS` keeps existing databases upgradeable without a separate migration runner:

```sql
CREATE TABLE IF NOT EXISTS listening_events (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  song_id        TEXT NOT NULL REFERENCES songs(id),
  played_at_ms   INTEGER NOT NULL,
  external_sent_at_ms INTEGER,
  created_at     TEXT NOT NULL,
  UNIQUE(song_id, played_at_ms)
);
CREATE INDEX IF NOT EXISTS listening_events_played_at_idx
  ON listening_events(played_at_ms);
CREATE INDEX IF NOT EXISTS listening_events_unsynced_idx
  ON listening_events(external_sent_at_ms, id);

CREATE TABLE IF NOT EXISTS weekly_runs (
  week_start     TEXT PRIMARY KEY,
  status         TEXT NOT NULL CHECK(status IN ('running', 'completed', 'failed')),
  playlist_id    INTEGER REFERENCES playlists(id) ON DELETE SET NULL,
  started_at     TEXT NOT NULL,
  finished_at    TEXT,
  error_message  TEXT
);

CREATE TABLE IF NOT EXISTS recommendation_items (
  week_start     TEXT NOT NULL REFERENCES weekly_runs(week_start) ON DELETE CASCADE,
  position       INTEGER NOT NULL,
  song_id        TEXT NOT NULL REFERENCES songs(id),
  source         TEXT NOT NULL,
  recording_mbid TEXT,
  score          REAL NOT NULL,
  PRIMARY KEY (week_start, position)
);
```

- [ ] **Step 4: Add the clock helper and persistence methods**

Add `import time`, then add the following helper and methods. Keep SQL inside `Library` and use the existing lock for every write transaction:

```python
def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def record_listen(self, song: dict, played_at_ms: Optional[int]) -> bool:
    with self._lock:
        self._insert_song_unlocked(
            song["id"], song["title"], song["artist"], song.get("album"),
            song.get("duration"), song.get("artwork_url"),
        )
        event_ms = played_at_ms
        if event_ms is None:
            event_ms = _now_ms()
            duplicate = self._conn.execute(
                "SELECT 1 FROM listening_events "
                "WHERE song_id = ? AND played_at_ms >= ? ORDER BY played_at_ms DESC LIMIT 1",
                (song["id"], event_ms - 30_000),
            ).fetchone()
            if duplicate is not None:
                self._conn.commit()
                return False
        cursor = self._conn.execute(
            "INSERT OR IGNORE INTO listening_events "
            "(song_id, played_at_ms, created_at) VALUES (?, ?, ?)",
            (song["id"], event_ms, _now_iso()),
        )
        self._conn.commit()
        return cursor.rowcount == 1


def get_listen_stats(self, since_ms: int) -> list[dict]:
    rows = self._conn.execute(
        "SELECT s.id AS song_id, s.title, s.artist, s.album, s.duration, "
        "COUNT(e.id) AS listen_count, MAX(e.played_at_ms) AS last_played_ms, "
        "CASE WHEN st.song_id IS NULL THEN 0 ELSE 1 END AS starred "
        "FROM songs s JOIN listening_events e ON e.song_id = s.id "
        "LEFT JOIN starred st ON st.song_id = s.id "
        "WHERE e.played_at_ms >= ? GROUP BY s.id ORDER BY s.id",
        (since_ms,),
    ).fetchall()
    return [dict(row) for row in rows]


def get_unsynced_listens(self, limit: int = 100) -> list[dict]:
    rows = self._conn.execute(
        "SELECT e.id AS event_id, e.played_at_ms, s.id AS song_id, "
        "s.title, s.artist, s.album, s.duration "
        "FROM listening_events e JOIN songs s ON s.id = e.song_id "
        "WHERE e.external_sent_at_ms IS NULL ORDER BY e.id LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(row) for row in rows]


def mark_listens_synced(self, event_ids: list[int], synced_at_ms: int) -> None:
    if not event_ids:
        return
    with self._lock:
        self._conn.executemany(
            "UPDATE listening_events SET external_sent_at_ms = ? WHERE id = ?",
            [(synced_at_ms, event_id) for event_id in event_ids],
        )
        self._conn.commit()
```

Add `get_playlist_song_ids()` and `get_playlist_by_name()` using the existing playlist result shape:

```python
def get_playlist_song_ids(self) -> list[dict]:
    playlists = self.get_playlists()
    for playlist in playlists:
        rows = self._conn.execute(
            "SELECT song_id FROM playlist_items WHERE playlist_id = ? ORDER BY position",
            (playlist["id"],),
        ).fetchall()
        playlist["song_ids"] = [row["song_id"] for row in rows]
    return playlists


def get_playlist_by_name(self, name: str) -> Optional[dict]:
    row = self._conn.execute(
        "SELECT id FROM playlists WHERE name = ? ORDER BY id LIMIT 1", (name,)
    ).fetchone()
    return None if row is None else self.get_playlist(row["id"])
```

- [ ] **Step 5: Add and test synchronization marking and playlist lookup**

Add tests that mark only selected events, verify the others remain unsynced, and verify duplicate playlist names resolve to the lowest ID so the result is deterministic.

Run: `python -m pytest -q test_library_listening.py`

Expected: PASS.

- [ ] **Step 6: Run the full suite and commit**

Run: `python -m pytest -q`

Expected: `149 passed, 5 deselected` plus the new passing tests.

```bash
git add library.py test_library_listening.py
git commit -m "feat: persist completed listening history"
```

---

### Task 2: Turn the Subsonic scrobble stub into real history

**Files:**
- Modify: `subsonic.py:799-875`
- Modify: `test_subsonic.py` near the playlist/star tests

**Interfaces:**
- Consumes: `Library.record_listen(song: dict, played_at_ms: Optional[int]) -> bool`
- Consumes: existing `async _resolve_song_meta(video_id: str) -> dict`
- Produces: `async _scrobble(params, request: Request) -> Response`

- [ ] **Step 1: Write failing protocol tests**

Add these cases to `test_subsonic.py` using its existing `client`, `lib`, and `token_params` fixtures:

```python
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
    assert sum(row["listen_count"] for row in lib.get_listen_stats(0)) == 2
```

- [ ] **Step 2: Verify that the old no-op behavior fails the first test**

Run: `python -m pytest -q test_subsonic.py -k scrobble`

Expected: FAIL because `_HANDLERS["scrobble"]` still points at `_noop_ok`.

- [ ] **Step 3: Implement strict Subsonic parameter parsing**

Add helpers and the handler:

```python
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


async def _scrobble(params, request: Request) -> Response:
    if not _submission_is_true(params.get("submission")):
        return _ok_response()
    ids = params.getlist("id")
    if not ids:
        return _error_response(10, "Required parameter 'id' is missing")
    raw_times = params.getlist("time")
    for index, video_id in enumerate(ids):
        played_at_ms = _parse_scrobble_time(
            raw_times[index] if index < len(raw_times) else None
        )
        meta = await _resolve_song_meta(video_id)
        _get_library().record_listen({"id": video_id, **meta}, played_at_ms)
    return _ok_response()
```

Replace `_HANDLERS["scrobble"] = _noop_ok` with `_HANDLERS["scrobble"] = _scrobble`.

- [ ] **Step 4: Test malformed values and retry idempotency**

Add cases for missing `id`, negative/non-numeric time, default `submission=true`, and a repeated explicit `(id, time)` pair. A malformed time must be treated as absent and must never crash the request.

Run: `python -m pytest -q test_subsonic.py -k scrobble`

Expected: PASS.

- [ ] **Step 5: Run the full suite and commit**

Run: `python -m pytest -q`

Expected: all tests pass.

```bash
git add subsonic.py test_subsonic.py
git commit -m "feat: record Subsonic scrobbles"
```

---

### Task 3: Add deterministic track and playlist ranking

**Files:**
- Create: `ranking.py`
- Create: `test_ranking.py`
- Modify: `library.py` to expose playlist membership if Task 1 did not already do so

**Interfaces:**
- Consumes: raw rows from `Library.get_listen_stats()` and `Library.get_playlist_song_ids()`
- Produces: `track_score(row: dict, now_ms: int) -> float`
- Produces: `rank_tracks(rows: list[dict], now_ms: int) -> list[dict]`
- Produces: `rank_playlists(playlists: list[dict], scores: dict[str, float]) -> list[dict]`

- [ ] **Step 1: Write fixed-clock score tests**

Create `test_ranking.py`:

```python
import ranking

NOW = 1_800_000_000_000
DAY = 86_400_000


def row(song_id, count, age_days, starred=0):
    return {"song_id": song_id, "listen_count": count,
            "last_played_ms": NOW - age_days * DAY, "starred": starred}


def test_more_recent_repeat_listens_rank_higher():
    ranked = ranking.rank_tracks(
        [row("old", 5, 80), row("recent", 5, 2)], NOW
    )
    assert [item["song_id"] for item in ranked] == ["recent", "old"]


def test_star_is_a_bonus_not_an_override():
    assert ranking.track_score(row("star", 1, 10, 1), NOW) < \
           ranking.track_score(row("habit", 20, 10, 0), NOW)


def test_large_playlist_does_not_win_only_because_it_is_large():
    ranked = ranking.rank_playlists(
        [{"id": 1, "name": "Focused", "song_ids": ["a", "b"]},
         {"id": 2, "name": "Huge", "song_ids": ["a", "x", "y", "z"]}],
        {"a": 10.0, "b": 10.0, "x": 0.0, "y": 0.0, "z": 0.0},
    )
    assert ranked[0]["name"] == "Focused"
```

- [ ] **Step 2: Verify the module is missing**

Run: `python -m pytest -q test_ranking.py`

Expected: collection FAIL with `ModuleNotFoundError: ranking`.

- [ ] **Step 3: Implement the pure scoring module**

Use logarithmic frequency so a single heavily repeated track cannot dominate indefinitely, and exponential decay with a 30-day half-life:

```python
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
        ranked.append({**playlist, "score": round(0.80 * mean + 0.20 * coverage, 6),
                       "listened_coverage": round(coverage, 6)})
    return sorted(ranked, key=lambda item: (-item["score"], item["id"]))
```

- [ ] **Step 4: Add boundary tests**

Test an empty playlist, a future timestamp, zero listens, equal-score deterministic ordering, and a 30-day decay value. Keep all tests free of `time.time()`.

Run: `python -m pytest -q test_ranking.py`

Expected: PASS.

- [ ] **Step 5: Run the full suite and commit**

```bash
python -m pytest -q
git add ranking.py test_ranking.py library.py
git commit -m "feat: rank tracks and playlists"
```

---

### Task 4: Isolate the ListenBrainz API

**Files:**
- Create: `listenbrainz_client.py`
- Create: `test_listenbrainz_client.py`

**Interfaces:**
- Produces: `ListenBrainzClient(token: str, client: httpx.AsyncClient)`
- Produces: `submit_listens(events: list[dict]) -> None`
- Produces: `get_recommendation_mbids(user: str, count: int) -> list[dict]`
- Produces: `get_recording_metadata(mbids: list[str]) -> list[dict]`
- Produces: `get_fresh_releases(user: str, days: int = 14) -> list[dict]`
- Produces: `get_release_tracks(release_mbid: str) -> list[dict]`

- [ ] **Step 1: Write MockTransport contract tests**

Create tests that inspect method, URL, authorization header, and JSON body. The listen-submission test must assert seconds rather than milliseconds:

```python
import httpx
import pytest
from listenbrainz_client import ListenBrainzClient


@pytest.mark.asyncio
async def test_submit_listens_converts_timestamp_and_metadata():
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"status": "ok"})
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://api.listenbrainz.org"
    ) as http:
        client = ListenBrainzClient("secret", http)
        await client.submit_listens([{
            "played_at_ms": 1_700_000_000_000, "title": "One",
            "artist": "Artist", "album": "Album",
        }])
    body = __import__("json").loads(seen[0].content)
    assert body["listen_type"] == "import"
    assert body["payload"][0]["listened_at"] == 1_700_000_000
    assert seen[0].headers["Authorization"] == "Token secret"
```

Add separate tests for 204 recommendations, metadata batching, personalized fresh releases, release-track JSPF parsing, HTTP 429, and malformed JSON.

- [ ] **Step 2: Verify the module is missing**

Run: `python -m pytest -q test_listenbrainz_client.py`

Expected: collection FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement the async wrapper with injected transport**

Use one base URL and one authorization helper. Call `raise_for_status()` on every non-204 response, return an empty recommendation list on 204, and consume only documented response keys:

```python
import httpx


class ListenBrainzClient:
    def __init__(self, token: str, client: httpx.AsyncClient):
        self.token = token
        self.client = client

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Token {self.token}"}

    async def submit_listens(self, events: list[dict]) -> None:
        payload = []
        for event in events:
            metadata = {"track_name": event["title"], "artist_name": event["artist"]}
            if event.get("album"):
                metadata["release_name"] = event["album"]
            payload.append({"listened_at": event["played_at_ms"] // 1000,
                            "track_metadata": metadata})
        response = await self.client.post(
            "/1/submit-listens", headers=self.headers,
            json={"listen_type": "import", "payload": payload},
        )
        response.raise_for_status()

    async def get_recommendation_mbids(self, user: str, count: int) -> list[dict]:
        response = await self.client.get(
            f"/1/cf/recommendation/user/{user}/recording",
            params={"count": count, "offset": 0},
        )
        if response.status_code == 204:
            return []
        response.raise_for_status()
        return response.json().get("payload", {}).get("mbids", [])

    async def get_recording_metadata(self, mbids: list[str]) -> list[dict]:
        if not mbids:
            return []
        response = await self.client.post(
            "/1/metadata/recording/", json={"recording_mbids": mbids,
                                             "inc": "artist release"}
        )
        response.raise_for_status()
        return response.json()

    async def get_fresh_releases(self, user: str, days: int = 14) -> list[dict]:
        response = await self.client.get(
            f"/1/user/{user}/fresh_releases",
            params={"days": days, "past": "true", "future": "false",
                    "sort": "release_date"},
        )
        response.raise_for_status()
        body = response.json()
        return body.get("payload", {}).get("releases", body.get("releases", []))
```

Implement `get_release_tracks()` against `/player/release/{release_mbid}/` and normalize JSPF results to dictionaries containing `title`, `artist`, `album`, `duration_seconds`, and `recording_mbid`.

- [ ] **Step 4: Enforce conservative batching**

The orchestration layer will send at most 100 listens and at most 50 recording MBIDs per call. Add explicit `ValueError` guards above those values so a future caller cannot accidentally create oversized requests.

Run: `python -m pytest -q test_listenbrainz_client.py`

Expected: PASS.

- [ ] **Step 5: Document source contracts in module comments and commit**

Reference the official endpoints used by this module:

- https://listenbrainz.readthedocs.io/en/latest/users/api/core.html
- https://listenbrainz.readthedocs.io/en/latest/users/api/recommendation.html
- https://listenbrainz.readthedocs.io/en/latest/users/api/metadata.html
- https://listenbrainz.readthedocs.io/en/latest/users/api/misc.html
- https://listenbrainz.readthedocs.io/en/latest/users/api/player.html

```bash
python -m pytest -q
git add listenbrainz_client.py test_listenbrainz_client.py
git commit -m "feat: add ListenBrainz client"
```

---

### Task 5: Build idempotent weekly playlist generation

**Files:**
- Create: `music_agent.py`
- Create: `test_music_agent.py`
- Modify: `library.py` with weekly-run and recommendation-item operations

**Interfaces:**
- Consumes: `ranking.rank_tracks`, `ranking.rank_playlists`
- Consumes: all `ListenBrainzClient` methods from Task 4
- Consumes: `main.search(q: str, limit: int, continuation: str = "")`
- Consumes: `Library.create_playlist`, `Library.update_playlist`, and Task 1 persistence methods
- Produces: `async sync_unsent_listens(lib, lb, now_ms) -> int`
- Produces: `async match_candidate(candidate: dict) -> Optional[tuple]`
- Produces: `async run_weekly(lib, lb, user: str, now: datetime, size: int) -> dict`
- Produces: `build_rankings(lib, now_ms: int) -> dict`

- [ ] **Step 1: Add weekly-run persistence tests and methods**

Add tests proving that `begin_weekly_run("2026-08-24")` creates `running`, a second begin returns the existing row, `complete_weekly_run` stores the playlist and items atomically, and `fail_weekly_run` preserves a short error message without a token or traceback.

Implement these exact methods in `Library`:

```python
def get_weekly_run(self, week_start: str) -> Optional[dict]:
    row = self._conn.execute(
        "SELECT * FROM weekly_runs WHERE week_start = ?", (week_start,)
    ).fetchone()
    return None if row is None else dict(row)


def begin_weekly_run(self, week_start: str) -> dict:
    with self._lock:
        existing = self._conn.execute(
            "SELECT * FROM weekly_runs WHERE week_start = ?", (week_start,)
        ).fetchone()
        if existing is not None and existing["status"] == "completed":
            return dict(existing)
        now = _now_iso()
        self._conn.execute(
            "INSERT INTO weekly_runs (week_start, status, started_at) VALUES (?, 'running', ?) "
            "ON CONFLICT(week_start) DO UPDATE SET status='running', started_at=excluded.started_at, "
            "finished_at=NULL, error_message=NULL",
            (week_start, now),
        )
        self._conn.commit()
        return dict(self._conn.execute(
            "SELECT * FROM weekly_runs WHERE week_start = ?", (week_start,)
        ).fetchone())


def complete_weekly_run(self, week_start: str, playlist_id: Optional[int],
                        items: list[dict]) -> None:
    with self._lock:
        self._conn.execute(
            "DELETE FROM recommendation_items WHERE week_start = ?", (week_start,)
        )
        self._conn.executemany(
            "INSERT INTO recommendation_items "
            "(week_start, position, song_id, source, recording_mbid, score) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [(week_start, position, item["song_id"], item["source"],
              item.get("recording_mbid"), item["score"])
             for position, item in enumerate(items)],
        )
        self._conn.execute(
            "UPDATE weekly_runs SET status='completed', playlist_id=?, finished_at=?, "
            "error_message=NULL WHERE week_start=?",
            (playlist_id, _now_iso(), week_start),
        )
        self._conn.commit()


def fail_weekly_run(self, week_start: str, message: str) -> None:
    with self._lock:
        self._conn.execute(
            "UPDATE weekly_runs SET status='failed', finished_at=?, error_message=? "
            "WHERE week_start=?",
            (_now_iso(), message[:500], week_start),
        )
        self._conn.commit()
```

`complete_weekly_run` must delete old `recommendation_items` for that week, insert the supplied order, and set `status='completed'` in one locked transaction.

- [ ] **Step 2: Write failing orchestration tests**

Create fakes for ListenBrainz and YouTube search. Required cases:

```python
@pytest.mark.asyncio
async def test_weekly_run_creates_one_deterministic_playlist(tmp_path, monkeypatch):
    lib = library.Library(str(tmp_path / "library.db"))
    lb = FakeListenBrainz(
        recommendations=[{"recording_mbid": "mbid-1", "score": 9.0}],
        metadata=[{"recording_mbid": "mbid-1", "recording": {"name": "New Song"},
                   "artist": {"name": "New Artist"}, "release": {"name": "New Album"}}],
    )
    async def search(q="", limit=20, continuation=""):
        return {"tracks": [{"id": "yt-1", "title": "New Song",
                            "artist": "New Artist", "album": "New Album",
                            "durationSeconds": 180, "artworkURL": None}],
                "continuation": None}
    monkeypatch.setattr(main, "search", search)
    first = await music_agent.run_weekly(
        lib, lb, "listener", datetime(2026, 8, 24, tzinfo=timezone.utc), 30
    )
    second = await music_agent.run_weekly(
        lib, lb, "listener", datetime(2026, 8, 24, tzinfo=timezone.utc), 30
    )
    assert first["playlist_id"] == second["playlist_id"]
    assert len(lib.get_playlists()) == 1
    assert lib.get_playlist(first["playlist_id"])["songs"][0]["id"] == "yt-1"
```

Also test: already-known track filtering, a title/artist mismatch, an upstream 204/empty result, fewer than 30 matches, refresh after a failed run, and no modification to a user-created playlist.

- [ ] **Step 3: Implement candidate normalization and conservative YouTube matching**

Matching must require normalized title and artist agreement. When duration is available, reject a gap greater than 12 seconds. When it is absent, require exact normalized title and artist rather than guessing:

```python
import re
import unicodedata


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
    page = await main.search(
        q=f"{candidate['artist']} {candidate['title']}", limit=20
    )
    if not isinstance(page, dict):
        return None
    for track in page.get("tracks") or []:
        if _candidate_matches(candidate, track):
            return (track["id"], track["title"], track["artist"],
                    track.get("album"), track.get("durationSeconds"),
                    track.get("artworkURL"))
    return None
```

- [ ] **Step 4: Implement safe listen synchronization**

Read at most 100 unsynced rows, submit them, then mark exactly those event IDs. Marking must happen only after a successful HTTP response. Repeat until the batch is smaller than 100. If ListenBrainz fails, propagate the exception so the weekly run is marked failed and no events are falsely marked sent.

- [ ] **Step 5: Implement the weekly transaction boundary**

`run_weekly` must perform these operations in order:

1. Derive Monday `week_start` in UTC.
2. Return the existing completed run immediately.
3. Insert or reset the run to `running`.
4. Synchronize pending listens.
5. Fetch up to 50 collaborative-filtering MBIDs and their metadata.
6. Fetch personalized fresh releases for the previous 14 days and expand at most 10 releases, at most 2 tracks per release.
7. Normalize candidates and remove duplicate `(artist, title)` pairs.
8. Remove songs already present locally by normalized `(artist, title)` identity.
9. Search YouTube sequentially, sleeping 300 ms between searches, until `size` accepted matches are collected.
10. Find or create `Discoveries — YYYY-MM-DD`; update only that exact agent-owned name.
11. Replace the generated playlist contents in one `update_playlist` call.
12. Persist recommendation items and mark the run completed.
13. On exception, mark the run failed with `str(exc)[:500]` and re-raise.

Do not create an empty playlist: if zero candidates match, complete the run with `playlist_id=None` by making the schema and method accept NULL, and return `{"status": "completed", "added": 0}`.

- [ ] **Step 6: Add the rankings command data shape**

`build_rankings()` must return JSON-serializable output:

```python
{
    "tracks": [{"song_id": "vid-1", "score": 0.91, "listen_count": 12}],
    "playlists": [{"id": 4, "name": "Morning", "score": 0.78,
                   "listened_coverage": 0.64}],
}
```

This reports rankings without changing user playlist order.

- [ ] **Step 7: Run orchestration tests and commit**

```bash
python -m pytest -q test_music_agent.py test_library_listening.py
python -m pytest -q
git add music_agent.py test_music_agent.py library.py test_library_listening.py
git commit -m "feat: generate weekly discovery playlists"
```

---

### Task 6: Add CLI, daemon scheduling, and Docker deployment

**Files:**
- Modify: `music_agent.py`
- Modify: `Dockerfile:13-21`
- Modify: `compose.yaml`
- Modify: `.env.example`
- Modify: `README.md`
- Modify: `test_music_agent.py`

**Interfaces:**
- Produces: `python music_agent.py rankings`
- Produces: `python music_agent.py weekly`
- Produces: `python music_agent.py daemon`
- Produces: `scheduled_week(now: datetime, weekday: int, hour_utc: int) -> tuple[str, datetime]`

- [ ] **Step 1: Write scheduler and configuration tests**

Use fixed UTC datetimes and test: before the target hour, after the target hour, exact boundary, Sunday-to-Monday rollover, invalid weekday, invalid playlist size, and missing ListenBrainz configuration for `weekly`/`daemon`. `rankings` must work without external configuration. `scheduled_week` returns the most recent scheduled occurrence, allowing a service that was offline at the exact target hour to catch up later.

```python
def test_scheduled_week_catches_up_after_monday():
    now = datetime(2026, 8, 29, 12, tzinfo=timezone.utc)  # Saturday
    week_start, scheduled = music_agent.scheduled_week(now, weekday=0, hour_utc=6)
    assert week_start == "2026-08-24"
    assert scheduled == datetime(2026, 8, 24, 6, tzinfo=timezone.utc)
```

- [ ] **Step 2: Implement argparse commands and an hourly-bounded daemon loop**

The daemon checks once an hour. It runs the most recent scheduled week whenever that week is not completed, so restarts and temporary network failures cannot permanently miss the narrow scheduling boundary:

```python
def scheduled_week(now: datetime, weekday: int, hour_utc: int) -> tuple[str, datetime]:
    if weekday not in range(7) or hour_utc not in range(24):
        raise ValueError("invalid weekly schedule")
    days_since = (now.weekday() - weekday) % 7
    target_date = now.date() - timedelta(days=days_since)
    target = datetime.combine(target_date, time(hour_utc), tzinfo=timezone.utc)
    if target > now:
        target -= timedelta(days=7)
    return target.date().isoformat(), target


async def daemon(config, lib, lb):
    while True:
        now = datetime.now(timezone.utc)
        week_start, _scheduled = scheduled_week(
            now, config.weekday, config.hour_utc
        )
        run = lib.get_weekly_run(week_start)
        if run is None or run["status"] != "completed":
            try:
                await run_weekly(lib, lb, config.user, now, config.playlist_size)
            except Exception:
                logger.exception("weekly agent run failed week_start=%s", week_start)
        await asyncio.sleep(3600)
```

Let `weekly` exit non-zero on failure so operators and cron can detect it.

- [ ] **Step 3: Copy the new modules into the image**

Change the Dockerfile copy line to:

```dockerfile
COPY main.py subsonic.py library.py spotify_import.py \
     ranking.py listenbrainz_client.py music_agent.py ./
```

- [ ] **Step 4: Add an agent service sharing the database**

Add a second Compose service built from the same image. Do not publish a port for it:

```yaml
  agent:
    image: mirasonic:0.1.0
    build:
      context: .
    command: ["python", "music_agent.py", "daemon"]
    environment:
      MIRASONIC_DB: /data/mirasonic.db
      REGION: ${REGION:?set REGION in .env, e.g. US}
      LISTENBRAINZ_USER: ${LISTENBRAINZ_USER:?set LISTENBRAINZ_USER in .env}
      LISTENBRAINZ_TOKEN: ${LISTENBRAINZ_TOKEN:?set LISTENBRAINZ_TOKEN in .env}
      AGENT_WEEKDAY: ${AGENT_WEEKDAY:-0}
      AGENT_HOUR_UTC: ${AGENT_HOUR_UTC:-6}
      AGENT_PLAYLIST_SIZE: ${AGENT_PLAYLIST_SIZE:-30}
      HOME: /tmp
    restart: unless-stopped
    security_opt:
      - no-new-privileges:true
    read_only: true
    tmpfs:
      - /tmp
    volumes:
      - ${LIBRARY_PATH:-./data}:/data
```

Do not add `depends_on`: both processes open the same WAL database independently, and the agent does not need the HTTP worker to rank or persist. YouTube matching imports `main.search` directly.

- [ ] **Step 5: Document environment variables and operations**

Add empty placeholders to `.env.example`:

```dotenv
LISTENBRAINZ_USER=
LISTENBRAINZ_TOKEN=
AGENT_WEEKDAY=0
AGENT_HOUR_UTC=6
AGENT_PLAYLIST_SIZE=30
```

Add README commands for:

```bash
docker compose run --rm agent python music_agent.py rankings
docker compose run --rm agent python music_agent.py weekly
docker compose logs -f agent
```

Document that Monday is `0`, the hour is UTC, external discovery needs ListenBrainz, local history/ranking does not, and removing the agent service leaves playback unaffected. Include token-rotation instructions: update `.env`, then run `docker compose up -d --force-recreate agent`.

- [ ] **Step 6: Verify rendered Compose configuration**

Run with temporary non-secret values:

```bash
LISTENBRAINZ_USER=test LISTENBRAINZ_TOKEN=test REGION=US docker compose config
```

Expected: both `worker` and `agent` are present; only `worker` publishes port 8094; both mount the same `/data` target.

- [ ] **Step 7: Run all tests and commit**

```bash
python -m pytest -q
git add music_agent.py test_music_agent.py Dockerfile compose.yaml .env.example README.md
git commit -m "feat: deploy weekly music agent"
```

---

### Task 7: Verify upgrade safety and resource behavior

**Files:**
- Modify: `README.md` only if verification reveals an operational caveat
- Modify: tests only if a missing regression case is found

**Interfaces:**
- Verifies all interfaces produced by Tasks 1–6; produces no new runtime interface.

- [ ] **Step 1: Verify an existing database upgrades in place**

Create a database with only the pre-agent tables, insert one playlist and song, reopen it through the new `Library`, and assert that the old playlist still exists and all three new tables are queryable. Encode this as a permanent regression test in `test_library_listening.py`.

- [ ] **Step 2: Verify the complete offline suite**

Run: `python -m pytest -q`

Expected: every test passes and the existing five live tests remain deselected.

- [ ] **Step 3: Build the production image**

Run:

```bash
docker compose build
```

Expected: one `mirasonic:0.1.0` image builds and contains `music_agent.py`, `ranking.py`, and `listenbrainz_client.py`.

- [ ] **Step 4: Run an offline smoke test against a temporary database**

Run `rankings` with an empty temporary `/data` mount and confirm it prints valid JSON containing empty `tracks` and `playlists`. Then start both services with test configuration and confirm the worker answers `/` while the agent remains running.

- [ ] **Step 5: Measure memory rather than relying on estimates**

After both containers have been idle for at least one minute, run:

```bash
docker stats --no-stream
```

Acceptance criteria:

- combined idle usage is below 512 MiB;
- the agent daemon is below 128 MiB idle;
- one manual weekly run completes without either container exceeding 800 MiB;
- no container restarts or receives an OOM kill.

If the combined peak exceeds 800 MiB on the 1 GiB server, replace the persistent `agent` service with a host cron invoking `docker compose run --rm agent python music_agent.py weekly`; do not reduce SQLite safety settings or remove error handling to save memory.

- [ ] **Step 6: Perform one controlled live weekly run**

Use the real ListenBrainz credentials and a backup of `data/mirasonic.db`. Run `python music_agent.py weekly`, then verify:

- unsynced listens were marked only after successful submission;
- at most 30 songs were added;
- the generated playlist name contains the Monday date;
- rerunning the same command preserves the playlist ID and does not create a duplicate;
- user-created playlists are byte-for-byte unchanged in ordered song IDs;
- logs contain no Subsonic password, ListenBrainz token, or signed Google media URL.

- [ ] **Step 7: Final regression run and commit any verification-only fixes**

```bash
python -m pytest -q
git status --short
```

Expected: all tests pass. Commit only files changed to address an observed verification failure; otherwise leave no verification commit.

## Definition of Done

- A completed Subsonic listen appears once in `listening_events`; “playing now” does not.
- Existing Mirasonic databases upgrade without data loss.
- `music_agent.py rankings` reports deterministic track and playlist scores without external credentials.
- Pending listens synchronize to ListenBrainz in bounded batches and are marked sent only on success.
- A weekly run combines ListenBrainz recommendations and fresh releases, matches conservatively against YouTube Music, and creates at most one dated playlist.
- Repeating a weekly run is idempotent.
- User-created playlists are never modified.
- Playback continues when the agent is stopped or external services fail.
- Offline tests, Compose validation, image build, live idempotency check, secret-log audit, and memory acceptance criteria all pass.

## Explicitly Deferred

- Spotify OAuth and direct Spotify playlist creation.
- A web dashboard or mobile notifications.
- Local LLM-generated playlist names or descriptions.
- Multi-user history and recommendations.
- Automatic negative feedback inferred from skips; Subsonic `scrobble` does not provide enough context for reliable skip detection.
- Reordering user-created playlists.
- Self-hosting the complete ListenBrainz stack.
