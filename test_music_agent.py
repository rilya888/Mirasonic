from datetime import datetime, timezone

import httpx
import pytest

import library
import main
from listenbrainz_client import ListenBrainzClient


class FakeListenBrainz:
    def __init__(self, recommendations=None, metadata=None, releases=None, release_tracks=None):
        self.recommendations = recommendations or []
        self.metadata = metadata or []
        self.releases = releases or []
        self.release_tracks = release_tracks or {}
        self.submitted = []
        self.token = "test-token"

    async def submit_listens(self, events):
        self.submitted.append(list(events))

    async def get_recommendation_mbids(self, user, count):
        assert count == 50
        return self.recommendations

    async def get_recording_metadata(self, mbids):
        return self.metadata

    async def get_fresh_releases(self, user, days=14):
        assert days == 14
        return self.releases

    async def get_release_tracks(self, release_mbid):
        return self.release_tracks.get(release_mbid, [])


def recording(mbid="mbid-1", title="New Song", artist="New Artist", album="New Album"):
    return {
        "recording_mbid": mbid,
        "recording": {"name": title},
        "artist": {"name": artist},
        "release": {"name": album},
    }


def track(video_id="yt-1", title="New Song", artist="New Artist", album="New Album", duration=180):
    return {
        "id": video_id,
        "title": title,
        "artist": artist,
        "album": album,
        "durationSeconds": duration,
        "artworkURL": None,
    }


@pytest.mark.asyncio
async def test_weekly_run_creates_one_deterministic_playlist(tmp_path, monkeypatch):
    import music_agent

    lib = library.Library(str(tmp_path / "library.db"))
    lb = FakeListenBrainz(
        recommendations=[{"recording_mbid": "mbid-1", "score": 9.0}],
        metadata=[recording()],
    )

    async def search(q="", limit=20, continuation=""):
        return {"tracks": [track()], "continuation": None}

    async def no_sleep(_):
        return None

    monkeypatch.setattr(main, "search", search)
    monkeypatch.setattr(music_agent.asyncio, "sleep", no_sleep)
    now = datetime(2026, 8, 24, tzinfo=timezone.utc)
    first = await music_agent.run_weekly(lib, lb, "listener", now, 30)
    second = await music_agent.run_weekly(lib, lb, "listener", now, 30)

    assert first["playlist_id"] == second["playlist_id"]
    assert len(lib.get_playlists()) == 1
    assert lib.get_playlist(first["playlist_id"])["songs"][0]["id"] == "yt-1"


@pytest.mark.asyncio
async def test_weekly_run_skips_known_library_song_including_playlist_only_song(tmp_path, monkeypatch):
    import music_agent

    lib = library.Library(str(tmp_path / "library.db"))
    playlist_id = lib.create_playlist("Manual")
    lib.update_playlist(playlist_id, "Manual", [], [("old", "New Song", "New Artist", None, 180, None)])
    lb = FakeListenBrainz([{"recording_mbid": "mbid-1", "score": 9.0}], [recording()])
    searched = []

    async def search(**kwargs):
        searched.append(kwargs)
        return {"tracks": [track()], "continuation": None}

    monkeypatch.setattr(main, "search", search)
    result = await music_agent.run_weekly(
        lib, lb, "listener", datetime(2026, 8, 24, tzinfo=timezone.utc), 30
    )

    assert result == {"status": "completed", "added": 0, "playlist_id": None}
    assert searched == []
    assert [p["name"] for p in lib.get_playlists()] == ["Manual"]


@pytest.mark.asyncio
async def test_weekly_run_rejects_title_or_artist_mismatch_and_allows_fewer_than_size(tmp_path, monkeypatch):
    import music_agent

    lib = library.Library(str(tmp_path / "library.db"))
    lb = FakeListenBrainz(
        [{"recording_mbid": "one", "score": 2}, {"recording_mbid": "two", "score": 1}],
        [recording("one", "One", "Artist"), recording("two", "Two", "Artist")],
    )

    async def search(q="", **kwargs):
        if "One" in q:
            return {"tracks": [track("bad", "One Other", "Artist")], "continuation": None}
        return {"tracks": [track("good", "Two", "Artist")], "continuation": None}

    async def no_sleep(_):
        return None

    monkeypatch.setattr(main, "search", search)
    monkeypatch.setattr(music_agent.asyncio, "sleep", no_sleep)
    result = await music_agent.run_weekly(
        lib, lb, "listener", datetime(2026, 8, 24, tzinfo=timezone.utc), 30
    )

    assert result["added"] == 1
    assert lib.get_playlist(result["playlist_id"])["songs"][0]["id"] == "good"


@pytest.mark.asyncio
async def test_weekly_run_empty_upstream_results_completes_without_playlist(tmp_path, monkeypatch):
    import music_agent

    lib = library.Library(str(tmp_path / "library.db"))
    lb = FakeListenBrainz()

    async def search(**kwargs):
        return {"tracks": [], "continuation": None}

    monkeypatch.setattr(main, "search", search)
    result = await music_agent.run_weekly(
        lib, lb, "listener", datetime(2026, 8, 24, tzinfo=timezone.utc), 30
    )

    assert result == {"status": "completed", "added": 0, "playlist_id": None}
    assert lib.get_playlists() == []


@pytest.mark.asyncio
async def test_weekly_run_preserves_user_playlist_after_atomic_finalization_failure(tmp_path, monkeypatch):
    import music_agent

    lib = library.Library(str(tmp_path / "library.db"))
    name = "Discoveries — 2026-08-24"
    user_playlist = lib.create_playlist(name)
    lib.update_playlist(user_playlist, name, [], [("user", "Keep", "User", None, 180, None)])
    lb = FakeListenBrainz([{"recording_mbid": "mbid", "score": 1}], [recording("mbid")])

    async def search(**kwargs):
        return {"tracks": [track()], "continuation": None}

    async def no_sleep(_):
        return None

    monkeypatch.setattr(main, "search", search)
    monkeypatch.setattr(music_agent.asyncio, "sleep", no_sleep)
    original_finalize = lib.finalize_weekly_playlist
    calls = 0

    def fail_once(week_start, playlist_name, add_songs, items):
        nonlocal calls
        calls += 1
        if calls == 1:
            return original_finalize(
                week_start, playlist_name, add_songs,
                [{"song_id": "missing", "source": "cf", "score": 1.0}],
            )
        return original_finalize(week_start, playlist_name, add_songs, items)

    monkeypatch.setattr(lib, "finalize_weekly_playlist", fail_once)
    now = datetime(2026, 8, 24, tzinfo=timezone.utc)
    with pytest.raises(Exception):
        await music_agent.run_weekly(lib, lb, "listener", now, 30)
    assert lib.get_weekly_run("2026-08-24")["playlist_id"] is None
    retry = await music_agent.run_weekly(lib, lb, "listener", now, 30)

    assert retry["playlist_id"] == lib.get_weekly_run("2026-08-24")["playlist_id"]
    assert len(lib.get_playlists()) == 2
    assert [song["id"] for song in lib.get_playlist(user_playlist)["songs"]] == ["user"]


@pytest.mark.asyncio
async def test_failed_run_redacts_listenbrainz_token_and_refreshes_on_retry(tmp_path, monkeypatch):
    import music_agent

    lib = library.Library(str(tmp_path / "library.db"))
    lb = FakeListenBrainz()
    lb.token = "super-secret-token"

    async def fail_recommendations(*args):
        raise RuntimeError("network error: super-secret-token Traceback details")

    lb.get_recommendation_mbids = fail_recommendations
    now = datetime(2026, 8, 24, tzinfo=timezone.utc)
    with pytest.raises(RuntimeError):
        await music_agent.run_weekly(lib, lb, "listener", now, 30)
    assert "super-secret-token" not in lib.get_weekly_run("2026-08-24")["error_message"]
    assert "Traceback" not in lib.get_weekly_run("2026-08-24")["error_message"]

    async def empty_recommendations(*args):
        return []

    lb.get_recommendation_mbids = empty_recommendations
    result = await music_agent.run_weekly(lib, lb, "listener", now, 30)
    assert result["status"] == "completed"


@pytest.mark.asyncio
async def test_match_candidate_requires_normalized_identity_and_close_duration(monkeypatch):
    import music_agent

    candidate = {"title": "Beyoncé's Song!", "artist": "The Artist", "duration_seconds": 180}

    async def search(**kwargs):
        return {"tracks": [
            track("wrong-duration", "Beyonce's Song", "The Artist", duration=193),
            track("right", "Beyonce's Song", "The Artist", duration=180),
        ]}

    monkeypatch.setattr(main, "search", search)
    assert await music_agent.match_candidate(candidate) == (
        "right", "Beyonce's Song", "The Artist", "New Album", 180, None
    )


@pytest.mark.asyncio
async def test_weekly_searches_sequentially_and_sleeps_only_between_searches(tmp_path, monkeypatch):
    import music_agent

    lib = library.Library(str(tmp_path / "library.db"))
    lb = FakeListenBrainz(
        [{"recording_mbid": "one", "score": 2}, {"recording_mbid": "two", "score": 1}],
        [recording("one", "One", "Artist"), recording("two", "Two", "Artist")],
    )
    sleeps = []

    async def search(q="", **kwargs):
        return {"tracks": [track("one" if "One" in q else "two", "Mismatch" if "One" in q else "Two", "Artist")]}

    async def sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(main, "search", search)
    monkeypatch.setattr(music_agent.asyncio, "sleep", sleep)
    result = await music_agent.run_weekly(
        lib, lb, "listener", datetime(2026, 8, 24, tzinfo=timezone.utc), 30
    )

    assert result["added"] == 1
    assert sleeps == [0.3]


@pytest.mark.asyncio
async def test_sync_unsent_listens_marks_batches_only_after_success(tmp_path):
    import music_agent

    lib = library.Library(str(tmp_path / "library.db"))
    for index in range(101):
        lib.record_listen(
            {"id": f"song-{index}", "title": str(index), "artist": "Artist"},
            1_700_000_000_000 + index,
        )
    lb = FakeListenBrainz()

    assert await music_agent.sync_unsent_listens(lib, lb, 1_700_000_100_000) == 101
    assert [len(batch) for batch in lb.submitted] == [100, 1]
    assert lib.get_unsynced_listens() == []


@pytest.mark.asyncio
async def test_sync_unsent_listens_does_not_mark_failed_batch(tmp_path):
    import music_agent

    lib = library.Library(str(tmp_path / "library.db"))
    lib.record_listen({"id": "song", "title": "One", "artist": "Artist"}, 1_700_000_000_000)
    lb = FakeListenBrainz()

    async def fail(events):
        raise RuntimeError("listen submit failed")

    lb.submit_listens = fail
    with pytest.raises(RuntimeError, match="listen submit failed"):
        await music_agent.sync_unsent_listens(lib, lb, 1_700_000_100_000)
    assert len(lib.get_unsynced_listens()) == 1


def test_build_rankings_returns_public_json_shape_without_reordering_playlists(tmp_path):
    import music_agent

    lib = library.Library(str(tmp_path / "library.db"))
    first = lib.create_playlist("First")
    second = lib.create_playlist("Second")
    lib.update_playlist(first, "First", [], [("one", "One", "Artist", None, 180, None)])
    lib.update_playlist(second, "Second", [], [("two", "Two", "Artist", None, 180, None)])
    lib.record_listen({"id": "two", "title": "Two", "artist": "Artist"}, 1_700_000_000_000)

    rankings = music_agent.build_rankings(lib, 1_700_000_000_000)

    assert rankings["tracks"] == [{"song_id": "two", "score": 0.371109, "listen_count": 1}]
    assert set(rankings["playlists"][0]) == {"id", "name", "score", "listened_coverage"}
    assert [song["id"] for song in lib.get_playlist(first)["songs"]] == ["one"]
    assert [song["id"] for song in lib.get_playlist(second)["songs"]] == ["two"]


@pytest.mark.asyncio
async def test_weekly_fresh_releases_are_limited_to_ten_releases_and_two_tracks_each(tmp_path, monkeypatch):
    import music_agent

    lib = library.Library(str(tmp_path / "library.db"))
    releases = [{"release_mbid": f"release-{index}"} for index in range(11)]
    release_tracks = {
        f"release-{index}": [
            {"title": f"{index}-{track_index}", "artist": "Fresh", "duration_seconds": 180}
            for track_index in range(3)
        ] for index in range(11)
    }
    lb = FakeListenBrainz(releases=releases, release_tracks=release_tracks)
    searched = []

    async def search(q="", **kwargs):
        searched.append(q)
        return {"tracks": []}

    async def no_sleep(_):
        return None

    monkeypatch.setattr(main, "search", search)
    monkeypatch.setattr(music_agent.asyncio, "sleep", no_sleep)
    await music_agent.run_weekly(
        lib, lb, "listener", datetime(2026, 8, 24, tzinfo=timezone.utc), 50
    )

    assert searched == [f"Fresh {release}-{position}" for release in range(10) for position in range(2)]


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [0, -1, 51, "bad", 1.5])
async def test_invalid_weekly_size_is_rejected_before_creating_run(tmp_path, size):
    import music_agent

    lib = library.Library(str(tmp_path / "library.db"))
    with pytest.raises(ValueError):
        await music_agent.run_weekly(
            lib, FakeListenBrainz(), "listener", datetime(2026, 8, 24, tzinfo=timezone.utc), size
        )
    assert lib.get_weekly_run("2026-08-24") is None


@pytest.mark.asyncio
async def test_weekly_size_fifty_accepts_exactly_fifty_of_more_than_fifty_matches(tmp_path, monkeypatch):
    import music_agent

    lib = library.Library(str(tmp_path / "library.db"))
    recommendations = [{"recording_mbid": f"cf-{index}", "score": 100 - index} for index in range(50)]
    metadata = [recording(f"cf-{index}", f"title{index}", f"artist{index}") for index in range(50)]
    releases = [{"release_mbid": f"release-{index}"} for index in range(10)]
    release_tracks = {
        release["release_mbid"]: [
            {"title": f"fresh{index}-{track_index}", "artist": f"new{index}", "duration_seconds": 180}
            for track_index in range(2)
        ] for index, release in enumerate(releases)
    }
    lb = FakeListenBrainz(recommendations, metadata, releases, release_tracks)
    searched = []

    async def search(q="", **kwargs):
        artist, title = q.split()
        searched.append(q)
        return {"tracks": [track(f"yt-{len(searched)}", title, artist)]}

    async def no_sleep(_):
        return None

    monkeypatch.setattr(main, "search", search)
    monkeypatch.setattr(music_agent.asyncio, "sleep", no_sleep)
    result = await music_agent.run_weekly(
        lib, lb, "listener", datetime(2026, 8, 24, tzinfo=timezone.utc), 50
    )

    assert result["added"] == 50
    assert len(searched) == 50
    assert len(lib.get_playlist(result["playlist_id"])["songs"]) == 50


@pytest.mark.asyncio
async def test_flat_listenbrainz_metadata_response_flows_into_weekly_orchestration(tmp_path, monkeypatch):
    import music_agent

    async def handler(request):
        assert request.url.path == "/1/metadata/recording/"
        return httpx.Response(200, json={
            "flat-mbid": {
                "recording_name": "Flat Song", "artist_name": "Flat Artist",
                "release_name": "Flat Album", "duration_seconds": 180,
            }
        })

    lib = library.Library(str(tmp_path / "library.db"))
    lb = FakeListenBrainz(recommendations=[{"recording_mbid": "flat-mbid", "score": 7}])
    async with httpx.AsyncClient(
        base_url="https://listenbrainz.invalid", transport=httpx.MockTransport(handler)
    ) as client:
        lb.get_recording_metadata = ListenBrainzClient("token", client).get_recording_metadata

        async def search(**kwargs):
            return {"tracks": [track("flat-yt", "Flat Song", "Flat Artist", "Flat Album")]}

        monkeypatch.setattr(main, "search", search)
        result = await music_agent.run_weekly(
            lib, lb, "listener", datetime(2026, 8, 24, tzinfo=timezone.utc), 30
        )

    assert result["added"] == 1
    assert lib.get_weekly_recommendation_items("2026-08-24") == [
        {"position": 0, "song_id": "flat-yt", "source": "cf", "recording_mbid": "flat-mbid", "score": 7.0}
    ]


@pytest.mark.asyncio
async def test_metadata_response_order_cannot_change_recommendation_order_or_scores(tmp_path, monkeypatch):
    import music_agent

    lib = library.Library(str(tmp_path / "library.db"))
    lb = FakeListenBrainz(
        [{"recording_mbid": "first", "score": 9.0}, {"recording_mbid": "second", "score": 3.0}],
        [recording("second", "Second", "Artist"), recording("first", "First", "Artist")],
    )

    async def search(q="", **kwargs):
        title = "First" if "First" in q else "Second"
        return {"tracks": [track(f"yt-{title.lower()}", title, "Artist")]}

    async def no_sleep(_):
        return None

    monkeypatch.setattr(main, "search", search)
    monkeypatch.setattr(music_agent.asyncio, "sleep", no_sleep)
    result = await music_agent.run_weekly(
        lib, lb, "listener", datetime(2026, 8, 24, tzinfo=timezone.utc), 2
    )

    assert [song["id"] for song in lib.get_playlist(result["playlist_id"])["songs"]] == [
        "yt-first", "yt-second"
    ]
    assert lib.get_weekly_recommendation_items("2026-08-24") == [
        {"position": 0, "song_id": "yt-first", "source": "cf", "recording_mbid": "first", "score": 9.0},
        {"position": 1, "song_id": "yt-second", "source": "cf", "recording_mbid": "second", "score": 3.0},
    ]


@pytest.mark.asyncio
async def test_weekly_error_persistence_never_keeps_credential_like_text(tmp_path):
    import music_agent

    lib = library.Library(str(tmp_path / "library.db"))
    lb = FakeListenBrainz()
    lb.token = ""

    async def fail(*args):
        raise RuntimeError("Bearer abc api_key=xyz password=hunter2 secret=oops")

    lb.get_recommendation_mbids = fail
    with pytest.raises(RuntimeError):
        await music_agent.run_weekly(
            lib, lb, "listener", datetime(2026, 8, 24, tzinfo=timezone.utc), 30
        )
    message = lib.get_weekly_run("2026-08-24")["error_message"].lower()
    assert len(message) <= 500
    assert all(word not in message for word in ("bearer", "api_key", "password", "secret", "abc", "xyz"))
    assert "runtimeerror" in message


@pytest.mark.asyncio
async def test_completed_retry_counts_persisted_recommendations_not_later_playlist_edits(tmp_path, monkeypatch):
    import music_agent

    lib = library.Library(str(tmp_path / "library.db"))
    lb = FakeListenBrainz([{"recording_mbid": "mbid", "score": 1}], [recording("mbid")])

    async def search(**kwargs):
        return {"tracks": [track()]}

    monkeypatch.setattr(main, "search", search)
    now = datetime(2026, 8, 24, tzinfo=timezone.utc)
    first = await music_agent.run_weekly(lib, lb, "listener", now, 30)
    lib.update_playlist(first["playlist_id"], "Discoveries — 2026-08-24", [], [
        ("manual", "Manual", "User", None, 180, None)
    ])
    retry = await music_agent.run_weekly(lib, lb, "listener", now, 30)

    assert retry == {"status": "completed", "added": 1, "playlist_id": first["playlist_id"]}


@pytest.mark.asyncio
async def test_naive_datetime_is_treated_as_utc_at_monday_boundary(tmp_path):
    import music_agent

    sunday = library.Library(str(tmp_path / "sunday.db"))
    monday = library.Library(str(tmp_path / "monday.db"))
    await music_agent.run_weekly(sunday, FakeListenBrainz(), "listener", datetime(2026, 8, 30, 23, 30), 30)
    await music_agent.run_weekly(monday, FakeListenBrainz(), "listener", datetime(2026, 8, 31, 0, 30), 30)

    assert sunday.get_weekly_run("2026-08-24")["status"] == "completed"
    assert monday.get_weekly_run("2026-08-31")["status"] == "completed"


@pytest.mark.asyncio
async def test_match_candidate_allows_exact_identity_when_duration_is_absent(monkeypatch):
    import music_agent

    async def search(**kwargs):
        return {"tracks": [track("no-duration", "One", "Artist", duration=None)]}

    monkeypatch.setattr(main, "search", search)
    assert await music_agent.match_candidate(
        {"title": "One", "artist": "Artist", "duration_seconds": None}
    ) == ("no-duration", "One", "Artist", "New Album", None, None)


@pytest.mark.asyncio
async def test_weekly_uses_atomic_finalize_once(tmp_path, monkeypatch):
    import music_agent

    lib = library.Library(str(tmp_path / "library.db"))
    lb = FakeListenBrainz([{"recording_mbid": "mbid", "score": 1}], [recording("mbid")])
    calls = 0
    original = lib.finalize_weekly_playlist

    def finalize(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    async def search(**kwargs):
        return {"tracks": [track()]}

    monkeypatch.setattr(lib, "finalize_weekly_playlist", finalize)
    monkeypatch.setattr(main, "search", search)
    await music_agent.run_weekly(
        lib, lb, "listener", datetime(2026, 8, 24, tzinfo=timezone.utc), 30
    )
    assert calls == 1
