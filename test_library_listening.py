import sqlite3

import pytest

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


def test_opening_legacy_library_preserves_playlist_data_and_adds_agent_tables(tmp_path):
    """Agent tables must be an additive upgrade for existing Library databases."""
    path = tmp_path / "legacy.db"
    legacy = sqlite3.connect(path)
    legacy.executescript(
        """
        CREATE TABLE songs (
          id TEXT PRIMARY KEY, title TEXT NOT NULL, artist TEXT NOT NULL,
          album TEXT, duration INTEGER, artwork_url TEXT, added_at TEXT NOT NULL
        );
        CREATE TABLE playlists (
          id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
          created_at TEXT NOT NULL, changed_at TEXT NOT NULL
        );
        CREATE TABLE playlist_items (
          playlist_id INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
          position INTEGER NOT NULL, song_id TEXT NOT NULL REFERENCES songs(id),
          PRIMARY KEY (playlist_id, position)
        );
        CREATE TABLE starred (
          song_id TEXT PRIMARY KEY REFERENCES songs(id), starred_at TEXT NOT NULL
        );
        CREATE TABLE spotify_map (
          spotify_uri TEXT PRIMARY KEY, song_id TEXT NOT NULL REFERENCES songs(id),
          mapped_at TEXT NOT NULL
        );
        """
    )
    expected_rows = [
        ("old-1", "First — unchanged", "Artist A", "Album A", 101,
         "https://example.invalid/first?raw=1", "2024-01-02T03:04:05.006Z"),
        ("old-2", "Second", "Artist B", None, None, None, "2024-01-02T03:04:06.007Z"),
    ]
    legacy.executemany("INSERT INTO songs VALUES (?, ?, ?, ?, ?, ?, ?)", expected_rows)
    legacy.execute(
        "INSERT INTO playlists (id, name, created_at, changed_at) VALUES (7, ?, ?, ?)",
        ("Old ordered mix", "2024-01-02T03:04:05.006Z", "2024-01-02T03:04:05.006Z"),
    )
    legacy.executemany(
        "INSERT INTO playlist_items (playlist_id, position, song_id) VALUES (7, ?, ?)",
        [(0, "old-2"), (1, "old-1")],
    )
    legacy.commit()
    legacy.close()

    lib = library.Library(str(path))

    # Compare every legacy column verbatim and retain the established order.
    rows = lib._conn.execute(
        "SELECT id, title, artist, album, duration, artwork_url, added_at FROM songs ORDER BY id"
    ).fetchall()
    assert [tuple(row) for row in rows] == expected_rows
    assert [entry["id"] for entry in lib.get_playlist(7)["songs"]] == ["old-2", "old-1"]
    for table in ("listening_events", "weekly_runs", "recommendation_items"):
        # Preparing and fetching this query proves the upgraded table is usable,
        # not merely listed in sqlite_master.
        assert lib._conn.execute(f"SELECT * FROM {table} LIMIT 0").fetchall() == []


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


def test_record_listen_does_not_suppress_untimed_event_for_future_explicit_event(
    monkeypatch, tmp_path
):
    lib = library.Library(str(tmp_path / "library.db"))
    assert lib.record_listen(song(), 1_700_000_010_000) is True
    monkeypatch.setattr(library, "_now_ms", lambda: 1_700_000_000_000)
    assert lib.record_listen(song(), None) is True


def test_unsynced_listens_include_track_metadata(tmp_path):
    lib = library.Library(str(tmp_path / "library.db"))
    lib.record_listen(song(), 1_700_000_000_000)
    event = lib.get_unsynced_listens()[0]
    assert event["title"] == "One"
    assert event["artist"] == "Artist"
    assert event["played_at_ms"] == 1_700_000_000_000


def test_mark_listens_synced_only_marks_selected_events(tmp_path):
    lib = library.Library(str(tmp_path / "library.db"))
    for index in range(3):
        lib.record_listen(song(f"vid-{index}"), 1_700_000_000_000 + index)

    events = lib.get_unsynced_listens()
    lib.mark_listens_synced([events[0]["event_id"], events[2]["event_id"]], 1_700_000_100_000)

    unsynced = lib.get_unsynced_listens()
    assert [event["event_id"] for event in unsynced] == [events[1]["event_id"]]


def test_playlist_lookup_by_name_uses_lowest_id_and_includes_song_ids(tmp_path):
    lib = library.Library(str(tmp_path / "library.db"))
    first_id = lib.create_playlist("Mix")
    second_id = lib.create_playlist("Mix")
    lib.update_playlist(first_id, "Mix", [], [("vid-1", "One", "Artist", "Album", 180, None)])
    lib.update_playlist(second_id, "Mix", [], [("vid-2", "Two", "Artist", "Album", 180, None)])

    selected = lib.get_playlist_by_name("Mix")
    assert selected["id"] == first_id
    assert [song["id"] for song in selected["songs"]] == ["vid-1"]

    playlists = lib.get_playlist_song_ids()
    assert playlists[0]["song_ids"] == ["vid-1"]
    assert playlists[1]["song_ids"] == ["vid-2"]


def test_begin_weekly_run_creates_running_and_retry_preserves_playlist_id(tmp_path):
    lib = library.Library(str(tmp_path / "library.db"))

    first = lib.begin_weekly_run("2026-08-24")
    playlist_id = lib.create_playlist("Discoveries — 2026-08-24")
    lib.set_weekly_run_playlist("2026-08-24", playlist_id)
    retry = lib.begin_weekly_run("2026-08-24")

    assert first["status"] == "running"
    assert retry["status"] == "running"
    assert retry["playlist_id"] == playlist_id


def test_weekly_claim_blocks_parallel_run_and_late_failure_cannot_overwrite_completion(tmp_path):
    path = tmp_path / "library.db"
    first_lib = library.Library(str(path))
    second_lib = library.Library(str(path))
    week_start = "2026-08-24"

    first_claim = first_lib.begin_weekly_run(week_start)
    blocked_claim = second_lib.begin_weekly_run(week_start)

    assert first_claim["claimed"] is True
    assert blocked_claim["claimed"] is False

    # A crashed worker becomes retryable after its lease expires.
    first_lib._conn.execute(
        "UPDATE weekly_runs SET lease_until_ms = 0 WHERE week_start = ?", (week_start,)
    )
    first_lib._conn.commit()
    retry_claim = second_lib.begin_weekly_run(week_start)
    assert retry_claim["claimed"] is True
    assert retry_claim["claim_token"] != first_claim["claim_token"]

    second_lib.complete_weekly_run(
        week_start, None, [], claim_token=retry_claim["claim_token"]
    )
    first_lib.fail_weekly_run(
        week_start, "late failure", claim_token=first_claim["claim_token"]
    )

    run = first_lib.get_weekly_run(week_start)
    assert run["status"] == "completed"
    assert run["error_message"] is None


def test_complete_weekly_run_replaces_items_and_completes_atomically(tmp_path):
    lib = library.Library(str(tmp_path / "library.db"))
    playlist_id = lib.create_playlist("Discoveries — 2026-08-24")
    lib.upsert_song("old", "Old", "Artist")
    lib.upsert_song("new", "New", "Artist")
    lib.begin_weekly_run("2026-08-24")
    lib.complete_weekly_run(
        "2026-08-24", playlist_id,
        [{"song_id": "old", "source": "cf", "score": 1.0}],
    )
    lib.begin_weekly_run("2026-08-24")
    lib.complete_weekly_run(
        "2026-08-24", playlist_id,
        [{"song_id": "new", "source": "fresh", "recording_mbid": "mbid", "score": 2.0}],
    )

    run = lib.get_weekly_run("2026-08-24")
    items = lib._conn.execute(
        "SELECT position, song_id, source, recording_mbid, score FROM recommendation_items "
        "WHERE week_start = ? ORDER BY position", ("2026-08-24",)
    ).fetchall()
    assert run["status"] == "completed"
    assert run["playlist_id"] == playlist_id
    assert [tuple(row) for row in items] == [(0, "new", "fresh", "mbid", 2.0)]


def test_fail_weekly_run_keeps_short_safe_message(tmp_path):
    lib = library.Library(str(tmp_path / "library.db"))
    lib.begin_weekly_run("2026-08-24")
    lib.fail_weekly_run("2026-08-24", "upstream request failed")

    run = lib.get_weekly_run("2026-08-24")
    assert run["status"] == "failed"
    assert run["error_message"] == "upstream request failed"
    assert run["finished_at"]


def test_complete_weekly_run_rolls_back_old_items_and_status_on_foreign_key_error(tmp_path):
    lib = library.Library(str(tmp_path / "library.db"))
    week_start = "2026-08-24"
    playlist_id = lib.create_playlist("Discoveries — 2026-08-24")
    lib.upsert_song("old", "Old", "Artist")
    lib.begin_weekly_run(week_start)
    lib._conn.execute(
        "INSERT INTO recommendation_items (week_start, position, song_id, source, recording_mbid, score) "
        "VALUES (?, 0, 'old', 'cf', 'old-mbid', 3.0)",
        (week_start,),
    )
    lib._conn.commit()

    try:
        lib.complete_weekly_run(
            week_start, playlist_id,
            [{"song_id": "missing", "source": "fresh", "score": 1.0}],
        )
    except Exception:
        pass
    else:
        raise AssertionError("expected foreign-key failure")

    assert lib.get_weekly_run(week_start)["status"] == "running"
    assert lib.get_weekly_recommendation_items(week_start) == [
        {"position": 0, "song_id": "old", "source": "cf", "recording_mbid": "old-mbid", "score": 3.0}
    ]


def test_finalize_weekly_playlist_rolls_back_new_playlist_and_ownership_on_failure(tmp_path):
    lib = library.Library(str(tmp_path / "library.db"))
    week_start = "2026-08-24"
    lib.begin_weekly_run(week_start)

    try:
        lib.finalize_weekly_playlist(
            week_start, "Discoveries — 2026-08-24",
            [("yt-1", "New", "Artist", None, 180, None)],
            [{"song_id": "missing", "source": "cf", "score": 1.0}],
        )
    except Exception:
        pass
    else:
        raise AssertionError("expected foreign-key failure")

    assert lib.get_playlists() == []
    assert lib.get_weekly_run(week_start)["playlist_id"] is None
    assert lib.get_weekly_run(week_start)["status"] == "running"


def test_finalize_retry_uses_one_agent_playlist_and_preserves_same_named_user_playlist(tmp_path):
    lib = library.Library(str(tmp_path / "library.db"))
    week_start = "2026-08-24"
    name = "Discoveries — 2026-08-24"
    user_id = lib.create_playlist(name)
    lib.update_playlist(user_id, name, [], [("user", "Keep", "User", None, 180, None)])
    lib.begin_weekly_run(week_start)
    with pytest.raises(Exception):
        lib.finalize_weekly_playlist(
            week_start, name, [("yt-1", "New", "Artist", None, 180, None)],
            [{"song_id": "missing", "source": "cf", "score": 1.0}],
        )

    agent_id = lib.finalize_weekly_playlist(
        week_start, name, [("yt-1", "New", "Artist", None, 180, None)],
        [{"song_id": "yt-1", "source": "cf", "recording_mbid": "mbid", "score": 2.0}],
    )
    assert len(lib.get_playlists()) == 2
    assert agent_id == lib.get_weekly_run(week_start)["playlist_id"]
    assert [entry["song_id"] for entry in lib.get_weekly_recommendation_items(week_start)] == ["yt-1"]
    assert [entry["id"] for entry in lib.get_playlist(user_id)["songs"]] == ["user"]
