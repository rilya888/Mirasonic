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
