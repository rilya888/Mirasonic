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
