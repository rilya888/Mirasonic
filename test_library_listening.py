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
