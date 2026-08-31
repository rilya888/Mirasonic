"""The server's persistent library — playlists and starred songs
(docs/SUBSONIC.md §4, phase 2). The schema is copied from the contract as-is,
not reinvented here.

Neither InnerTube nor yt-dlp is mentioned in this file: the caller
(subsonic.py) resolves a track's metadata itself (from the search cache or
main.get_song_details) and passes finished values in. library.py knows only
about SQLite.
"""
import os
import secrets
import sqlite3
import threading
import time
from datetime import datetime, timezone
from typing import Optional

DEFAULT_DB_PATH = "/data/mirasonic.db"
WEEKLY_RUN_LEASE_MS = 30 * 60 * 1000

SCHEMA = """
CREATE TABLE IF NOT EXISTS songs (
  id             TEXT PRIMARY KEY,   -- videoId
  title          TEXT NOT NULL,
  artist         TEXT NOT NULL,
  album          TEXT,               -- NULL when InnerTube gave none
  duration       INTEGER,            -- seconds; NULL until resolved
  artwork_url    TEXT,
  added_at       TEXT NOT NULL       -- ISO 8601 with milliseconds, UTC
);

CREATE TABLE IF NOT EXISTS playlists (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  name       TEXT NOT NULL,
  created_at TEXT NOT NULL,
  changed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS playlist_items (
  playlist_id INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
  position    INTEGER NOT NULL,      -- 0-based, no gaps
  song_id     TEXT    NOT NULL REFERENCES songs(id),
  PRIMARY KEY (playlist_id, position)
);

CREATE TABLE IF NOT EXISTS starred (
  song_id    TEXT PRIMARY KEY REFERENCES songs(id),
  starred_at TEXT NOT NULL
);

-- What has already been matched to YouTube: Spotify playlists get refreshed
-- monthly, and without this table every re-import would search for all
-- hundred-odd tracks again. It also survives hand editing: a correction stays
-- corrected.
CREATE TABLE IF NOT EXISTS spotify_map (
  spotify_uri TEXT PRIMARY KEY,
  song_id     TEXT NOT NULL REFERENCES songs(id),
  mapped_at   TEXT NOT NULL
);

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
  error_message  TEXT,
  claim_token    TEXT,
  lease_until_ms INTEGER
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
"""


def _now_iso() -> str:
    """Same format as subsonic._iso_created — Amperfy parses `created` only
    with milliseconds (ISO8601DateFormatter.withFractionalSeconds)."""
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


class Library:
    """One connection per process; writes go through a shared lock.

    A second writer exists (`spotify_import.py`, a separate process), so the
    journal is set to WAL: in the default mode a writer locks the whole
    database, and importing a hundred tracks would break playback with
    `database is locked` mid-song. Under WAL, readers never wait for a writer.
    """

    def __init__(self, path: Optional[str] = None):
        self.path = path or os.environ.get("MIRASONIC_DB", DEFAULT_DB_PATH)
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")  # or ON DELETE CASCADE stays silent
        self._conn.execute("PRAGMA journal_mode = WAL")  # reader and writer stop blocking each other
        self._conn.execute("PRAGMA busy_timeout = 5000")  # wait for another transaction instead of failing
        self._conn.executescript(SCHEMA)
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            columns = {
                row["name"] for row in self._conn.execute("PRAGMA table_info(weekly_runs)")
            }
            if "claim_token" not in columns:
                self._conn.execute("ALTER TABLE weekly_runs ADD COLUMN claim_token TEXT")
            if "lease_until_ms" not in columns:
                self._conn.execute("ALTER TABLE weekly_runs ADD COLUMN lease_until_ms INTEGER")
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        self._lock = threading.Lock()

    # -- songs ---------------------------------------------------------

    def _insert_song_unlocked(self, song_id, title, artist, album, duration, artwork_url):
        """Only while self._lock is already held — Lock is not reentrant.

        Re-adding the same track does not overwrite what is already known, but
        fills in what is missing: duration and artwork may have been unknown
        when the track first entered the database (a search-cache miss), and
        this is the second chance to record them. INSERT OR IGNORE simply threw
        that chance away.
        """
        self._conn.execute(
            "INSERT INTO songs (id, title, artist, album, duration, artwork_url, added_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET "
            "  title       = COALESCE(NULLIF(songs.title, ''), excluded.title),"
            "  artist      = COALESCE(NULLIF(songs.artist, ''), excluded.artist),"
            "  album       = COALESCE(songs.album, excluded.album),"
            "  duration    = COALESCE(songs.duration, excluded.duration),"
            "  artwork_url = COALESCE(songs.artwork_url, excluded.artwork_url)",
            (song_id, title or "", artist or "", album, duration, artwork_url, _now_iso()),
        )

    def upsert_song(self, song_id: str, title: str, artist: str, album=None,
                    duration=None, artwork_url=None) -> None:
        """Record a track without attaching it to a playlist or a star."""
        with self._lock:
            self._insert_song_unlocked(song_id, title, artist, album, duration, artwork_url)
            self._conn.commit()

    def get_song(self, song_id: str) -> Optional[dict]:
        row = self._conn.execute("SELECT * FROM songs WHERE id = ?", (song_id,)).fetchone()
        return dict(row) if row else None

    def get_songs(self) -> list[dict]:
        """The whole library in one query — subsonic.py builds artists and
        albums out of it (phase 3). Three hundred rows; grouping that in SQL
        for savings nobody can measure is not worth it."""
        return [dict(row) for row in self._conn.execute("SELECT * FROM songs")]

    def get_random_songs(self, size: int) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM songs ORDER BY RANDOM() LIMIT ?", (size,)
        ).fetchall()
        return [dict(row) for row in rows]

    # -- playlists -------------------------------------------------------

    def get_playlists(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT p.id, p.name, p.created_at, p.changed_at, "
            "COUNT(pi.song_id) AS song_count, COALESCE(SUM(s.duration), 0) AS duration "
            "FROM playlists p "
            "LEFT JOIN playlist_items pi ON pi.playlist_id = p.id "
            "LEFT JOIN songs s ON s.id = pi.song_id "
            "GROUP BY p.id ORDER BY p.id"
        ).fetchall()
        return [dict(row) for row in rows]

    def get_playlist(self, playlist_id: int) -> Optional[dict]:
        prow = self._conn.execute(
            "SELECT id, name, created_at, changed_at FROM playlists WHERE id = ?",
            (playlist_id,),
        ).fetchone()
        if prow is None:
            return None
        items = self._conn.execute(
            "SELECT s.* FROM playlist_items pi JOIN songs s ON s.id = pi.song_id "
            "WHERE pi.playlist_id = ? ORDER BY pi.position",
            (playlist_id,),
        ).fetchall()
        songs = [dict(row) for row in items]
        duration = sum(song.get("duration") or 0 for song in songs)
        result = dict(prow)
        result["songs"] = songs
        result["song_count"] = len(songs)
        result["duration"] = duration
        return result

    def create_playlist(self, name: str) -> int:
        with self._lock:
            now = _now_iso()
            cur = self._conn.execute(
                "INSERT INTO playlists (name, created_at, changed_at) VALUES (?, ?, ?)",
                (name, now, now),
            )
            self._conn.commit()
            return cur.lastrowid

    def delete_playlist(self, playlist_id: int) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM playlists WHERE id = ?", (playlist_id,))
            self._conn.commit()
            return cur.rowcount > 0

    def update_playlist(self, playlist_id: int, name: str,
                        remove_indices: list[int],
                        add_songs: list[tuple]) -> bool:
        """`remove_indices` are positions in the list as it was BEFORE this call.
        `add_songs` is [(song_id, title, artist, album, duration, artwork_url), …],
        appended at the end in the given order.

        The order is mandatory (docs/SUBSONIC.md §5): every index is resolved
        at once against the pre-operation state, and only then do the additions
        happen. Amperfy reorders a playlist by removing `0..n-1` and re-adding
        the whole list — recomputing indices while deleting one at a time
        breaks that.
        """
        with self._lock:
            updated = self._update_playlist_unlocked(
                playlist_id, name, remove_indices, add_songs
            )
            if updated:
                self._conn.commit()
            return updated

    def _update_playlist_unlocked(self, playlist_id: int, name: str,
                                  remove_indices: list[int],
                                  add_songs: list[tuple]) -> bool:
        """Update a playlist while the caller owns ``self._lock`` and transaction."""
        prow = self._conn.execute(
            "SELECT id FROM playlists WHERE id = ?", (playlist_id,)
        ).fetchone()
        if prow is None:
            return False

        current = [r["song_id"] for r in self._conn.execute(
            "SELECT song_id FROM playlist_items WHERE playlist_id = ? ORDER BY position",
            (playlist_id,),
        ).fetchall()]
        remove_set = set(remove_indices)
        kept = [song_id for i, song_id in enumerate(current) if i not in remove_set]
        for song_id, title, artist, album, duration, artwork_url in add_songs:
            self._insert_song_unlocked(song_id, title, artist, album, duration, artwork_url)
            kept.append(song_id)
        self._conn.execute("DELETE FROM playlist_items WHERE playlist_id = ?", (playlist_id,))
        self._conn.executemany(
            "INSERT INTO playlist_items (playlist_id, position, song_id) VALUES (?, ?, ?)",
            [(playlist_id, position, song_id) for position, song_id in enumerate(kept)],
        )
        self._conn.execute(
            "UPDATE playlists SET name = ?, changed_at = ? WHERE id = ?",
            (name, _now_iso(), playlist_id),
        )
        return True

    # -- Spotify mappings -------------------------------------------------

    def get_spotify_map(self) -> dict[str, str]:
        return {r["spotify_uri"]: r["song_id"] for r in
                self._conn.execute("SELECT spotify_uri, song_id FROM spotify_map")}

    def put_spotify_map(self, pairs: list[tuple[str, str]]) -> None:
        """`pairs` is [(spotify_uri, song_id), …]. The track must already be in songs."""
        with self._lock:
            self._conn.executemany(
                "INSERT INTO spotify_map (spotify_uri, song_id, mapped_at) VALUES (?, ?, ?) "
                "ON CONFLICT(spotify_uri) DO UPDATE SET song_id = excluded.song_id, "
                "mapped_at = excluded.mapped_at",
                [(uri, song_id, _now_iso()) for uri, song_id in pairs],
            )
            self._conn.commit()

    # -- starred ---------------------------------------------------------

    def star(self, song_id: str, title: str, artist: str, album=None,
             duration=None, artwork_url=None) -> None:
        with self._lock:
            self._insert_song_unlocked(song_id, title, artist, album, duration, artwork_url)
            self._conn.execute(
                "INSERT OR REPLACE INTO starred (song_id, starred_at) VALUES (?, ?)",
                (song_id, _now_iso()),
            )
            self._conn.commit()

    def unstar(self, song_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM starred WHERE song_id = ?", (song_id,))
            self._conn.commit()

    def get_starred(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT s.*, st.starred_at FROM starred st JOIN songs s ON s.id = st.song_id "
            "ORDER BY st.starred_at"
        ).fetchall()
        return [dict(row) for row in rows]

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
                    "WHERE song_id = ? AND played_at_ms BETWEEN ? AND ? "
                    "ORDER BY played_at_ms DESC LIMIT 1",
                    (song["id"], event_ms - 30_000, event_ms),
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

    # -- weekly discovery runs -----------------------------------------

    def get_weekly_run(self, week_start: str) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT * FROM weekly_runs WHERE week_start = ?", (week_start,)
        ).fetchone()
        return None if row is None else dict(row)

    def begin_weekly_run(self, week_start: str) -> dict:
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                existing = self._conn.execute(
                    "SELECT * FROM weekly_runs WHERE week_start = ?", (week_start,)
                ).fetchone()
                now_ms = _now_ms()
                if existing is not None and (
                    existing["status"] == "completed"
                    or (existing["status"] == "running"
                        and (existing["lease_until_ms"] or 0) > now_ms)
                ):
                    result = dict(existing)
                    result["claimed"] = False
                    self._conn.commit()
                    return result
                now = _now_iso()
                claim_token = secrets.token_urlsafe(24)
                self._conn.execute(
                    "INSERT INTO weekly_runs "
                    "(week_start, status, started_at, claim_token, lease_until_ms) "
                    "VALUES (?, 'running', ?, ?, ?) "
                    "ON CONFLICT(week_start) DO UPDATE SET status='running', "
                    "started_at=excluded.started_at, finished_at=NULL, error_message=NULL, "
                    "claim_token=excluded.claim_token, lease_until_ms=excluded.lease_until_ms",
                    (week_start, now, claim_token, now_ms + WEEKLY_RUN_LEASE_MS),
                )
                result = dict(self._conn.execute(
                    "SELECT * FROM weekly_runs WHERE week_start = ?", (week_start,)
                ).fetchone())
                result["claimed"] = True
                self._conn.commit()
                return result
            except Exception:
                self._conn.rollback()
                raise

    def set_weekly_run_playlist(self, week_start: str, playlist_id: int) -> None:
        """Persist agent ownership immediately after allocating a playlist."""
        with self._lock:
            self._conn.execute(
                "UPDATE weekly_runs SET playlist_id = ? WHERE week_start = ?",
                (playlist_id, week_start),
            )
            self._conn.commit()

    def complete_weekly_run(self, week_start: str, playlist_id: Optional[int],
                            items: list[dict], claim_token: Optional[str] = None) -> bool:
        with self._lock:
            try:
                self._conn.execute("BEGIN")
                if claim_token is not None and self._conn.execute(
                    "SELECT 1 FROM weekly_runs WHERE week_start=? AND status='running' "
                    "AND claim_token=?", (week_start, claim_token)
                ).fetchone() is None:
                    self._conn.rollback()
                    raise RuntimeError("weekly run claim is no longer active")
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
                return True
            except Exception:
                self._conn.rollback()
                raise

    def finalize_weekly_playlist(self, week_start: str, name: str,
                                 add_songs: list[tuple], items: list[dict],
                                 claim_token: Optional[str] = None) -> int:
        """Atomically replace the agent-owned playlist and complete its run.

        Ownership is established exclusively by ``weekly_runs.playlist_id``;
        the supplied name is never used to select an existing playlist.
        """
        with self._lock:
            try:
                self._conn.execute("BEGIN")
                run = self._conn.execute(
                    "SELECT playlist_id, status, claim_token FROM weekly_runs WHERE week_start = ?",
                    (week_start,),
                ).fetchone()
                if run is None:
                    raise ValueError("weekly run does not exist")
                if claim_token is not None and (
                    run["status"] != "running" or run["claim_token"] != claim_token
                ):
                    self._conn.rollback()
                    raise RuntimeError("weekly run claim is no longer active")
                playlist_id = run["playlist_id"]
                if playlist_id is None:
                    created_at = _now_iso()
                    cursor = self._conn.execute(
                        "INSERT INTO playlists (name, created_at, changed_at) VALUES (?, ?, ?)",
                        (name, created_at, created_at),
                    )
                    playlist_id = cursor.lastrowid
                    self._conn.execute(
                        "UPDATE weekly_runs SET playlist_id = ? WHERE week_start = ?",
                        (playlist_id, week_start),
                    )
                current_count = self._conn.execute(
                    "SELECT COUNT(*) FROM playlist_items WHERE playlist_id = ?", (playlist_id,)
                ).fetchone()[0]
                if not self._update_playlist_unlocked(
                    playlist_id, name, list(range(current_count)), add_songs
                ):
                    raise RuntimeError("agent playlist is missing")
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
                    "UPDATE weekly_runs SET status='completed', finished_at=?, error_message=NULL "
                    "WHERE week_start=?",
                    (_now_iso(), week_start),
                )
                self._conn.commit()
                return playlist_id
            except Exception:
                self._conn.rollback()
                raise

    def get_weekly_recommendation_items(self, week_start: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT position, song_id, source, recording_mbid, score FROM recommendation_items "
            "WHERE week_start = ? ORDER BY position",
            (week_start,),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_weekly_recommendation_count(self, week_start: str) -> int:
        return self._conn.execute(
            "SELECT COUNT(*) FROM recommendation_items WHERE week_start = ?", (week_start,)
        ).fetchone()[0]

    def fail_weekly_run(self, week_start: str, message: str,
                        claim_token: Optional[str] = None) -> bool:
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE weekly_runs SET status='failed', finished_at=?, error_message=? "
                "WHERE week_start=? AND status='running' "
                "AND (? IS NULL OR claim_token=?)",
                (_now_iso(), message[:500], week_start, claim_token, claim_token),
            )
            self._conn.commit()
            return cursor.rowcount == 1

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
