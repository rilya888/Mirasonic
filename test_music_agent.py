import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

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
async def test_parallel_weekly_run_does_not_repeat_external_calls(tmp_path):
    import music_agent

    path = tmp_path / "library.db"
    active_lib = library.Library(str(path))
    competing_lib = library.Library(str(path))
    active_lib.begin_weekly_run("2026-08-24")

    class NoExternalCalls(FakeListenBrainz):
        async def get_recommendation_mbids(self, user, count):
            raise AssertionError("competing run must stop before external calls")

    lb = NoExternalCalls()

    result = await music_agent.run_weekly(
        competing_lib, lb, "listener", datetime(2026, 8, 24, tzinfo=timezone.utc), 30
    )

    assert result == {"status": "running", "added": 0, "playlist_id": None}
    assert lb.submitted == []


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

    def fail_once(week_start, playlist_name, add_songs, items, claim_token=None):
        nonlocal calls
        calls += 1
        if calls == 1:
            return original_finalize(
                week_start, playlist_name, add_songs,
                [{"song_id": "missing", "source": "cf", "score": 1.0}],
                claim_token=claim_token,
            )
        return original_finalize(
            week_start, playlist_name, add_songs, items, claim_token=claim_token
        )

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


@pytest.mark.parametrize(
    ("now", "expected_week", "expected_scheduled"),
    [
        (
            datetime(2026, 8, 24, 5, 59, tzinfo=timezone.utc),
            "2026-08-17", datetime(2026, 8, 17, 6, tzinfo=timezone.utc),
        ),
        (
            datetime(2026, 8, 24, 6, tzinfo=timezone.utc),
            "2026-08-24", datetime(2026, 8, 24, 6, tzinfo=timezone.utc),
        ),
        (
            datetime(2026, 8, 24, 6, 1, tzinfo=timezone.utc),
            "2026-08-24", datetime(2026, 8, 24, 6, tzinfo=timezone.utc),
        ),
        (
            datetime(2026, 8, 30, 12, tzinfo=timezone.utc),
            "2026-08-24", datetime(2026, 8, 24, 6, tzinfo=timezone.utc),
        ),
    ],
)
def test_scheduled_week_uses_most_recent_occurrence(now, expected_week, expected_scheduled):
    import music_agent

    assert music_agent.scheduled_week(now, weekday=0, hour_utc=6) == (
        expected_week, expected_scheduled,
    )


@pytest.mark.parametrize("weekday,hour", [(-1, 6), (7, 6), (0, -1), (0, 24)])
def test_scheduled_week_rejects_invalid_schedule(weekday, hour):
    import music_agent

    with pytest.raises(ValueError, match="invalid weekly schedule"):
        music_agent.scheduled_week(datetime(2026, 8, 24, tzinfo=timezone.utc), weekday, hour)


@pytest.mark.parametrize("size", ["0", "51", "bad", "1.5"])
def test_agent_config_rejects_invalid_playlist_size(size):
    import music_agent

    with pytest.raises(ValueError, match="size must be an integer from 1 to 50"):
        music_agent.agent_config({"AGENT_PLAYLIST_SIZE": size})


@pytest.mark.parametrize("variable", ["LISTENBRAINZ_USER", "LISTENBRAINZ_TOKEN"])
def test_agent_config_requires_listenbrainz_only_for_discovery(variable):
    import music_agent

    env = {"MIRASONIC_DB": "/tmp/music.db", "AGENT_PLAYLIST_SIZE": "30"}
    assert music_agent.agent_config(env).user is None
    with pytest.raises(ValueError, match="ListenBrainz"):
        music_agent.agent_config(env, require_listenbrainz=True)
    env[variable] = "configured"
    with pytest.raises(ValueError, match="ListenBrainz"):
        music_agent.agent_config(env, require_listenbrainz=True)


@pytest.mark.parametrize("command", ["weekly", "daemon"])
def test_discovery_commands_fail_clearly_without_listenbrainz(command, tmp_path, monkeypatch, capsys):
    import music_agent

    monkeypatch.setenv("MIRASONIC_DB", str(tmp_path / "library.db"))
    monkeypatch.delenv("LISTENBRAINZ_USER", raising=False)
    monkeypatch.delenv("LISTENBRAINZ_TOKEN", raising=False)

    assert music_agent.cli([command]) == 2
    assert "ListenBrainz user and token are required" in capsys.readouterr().err


def test_rankings_cli_outputs_json_without_listenbrainz(tmp_path, monkeypatch, capsys):
    import music_agent

    monkeypatch.setenv("MIRASONIC_DB", str(tmp_path / "library.db"))
    monkeypatch.delenv("LISTENBRAINZ_USER", raising=False)
    monkeypatch.delenv("LISTENBRAINZ_TOKEN", raising=False)

    assert music_agent.cli(["rankings"]) == 0
    assert set(json.loads(capsys.readouterr().out)) == {"tracks", "playlists"}


def test_weekly_cli_returns_nonzero_when_run_fails(tmp_path, monkeypatch, capsys):
    import music_agent

    async def fail(*args):
        raise RuntimeError("upstream unavailable")

    monkeypatch.setenv("MIRASONIC_DB", str(tmp_path / "library.db"))
    monkeypatch.setenv("LISTENBRAINZ_USER", "listener")
    monkeypatch.setenv("LISTENBRAINZ_TOKEN", "not-a-real-token")
    monkeypatch.setattr(music_agent, "_run_weekly_command", fail)

    assert music_agent.cli(["weekly"]) == 1
    assert "weekly agent run failed" in capsys.readouterr().err


@pytest.mark.asyncio
@pytest.mark.parametrize("run", [None, {"status": "running"}, {"status": "failed"}])
async def test_daemon_catches_up_incomplete_week_and_skips_completed(monkeypatch, run):
    import music_agent

    config = music_agent.AgentConfig("/tmp/music.db", "listener", "token", 0, 6, 30)
    now = datetime(2026, 8, 29, 12, tzinfo=timezone.utc)
    calls = []

    class IncompleteLibrary:
        def get_weekly_run(self, week_start):
            assert week_start == "2026-08-24"
            return run

    class CompletedLibrary:
        def get_weekly_run(self, week_start):
            return {"status": "completed"}

    async def fake_weekly(lib, lb, user, run_now, size, *, week_start=None):
        calls.append((lib, user, run_now, size, week_start))

    async def stop_after_one_hour(_):
        raise asyncio.CancelledError

    monkeypatch.setattr(music_agent, "run_weekly", fake_weekly)
    with pytest.raises(asyncio.CancelledError):
        await music_agent.daemon(config, IncompleteLibrary(), object(), now_fn=lambda: now, sleep=stop_after_one_hour)
    with pytest.raises(asyncio.CancelledError):
        await music_agent.daemon(config, CompletedLibrary(), object(), now_fn=lambda: now, sleep=stop_after_one_hour)

    assert len(calls) == 1
    assert calls[0][1:] == ("listener", now, 30, "2026-08-24")


@pytest.mark.asyncio
async def test_daemon_logs_generic_failure_and_continues_without_token(monkeypatch, caplog):
    import music_agent

    secret = "definitely-not-logged"
    config = music_agent.AgentConfig("/tmp/music.db", "listener", secret, 0, 6, 30)

    class FailedLibrary:
        def get_weekly_run(self, week_start):
            return {"status": "running"}

    async def fail(*args, **kwargs):
        raise RuntimeError(secret)

    async def stop_after_one_hour(_):
        raise asyncio.CancelledError

    monkeypatch.setattr(music_agent, "run_weekly", fail)
    caplog.set_level(logging.ERROR)
    with pytest.raises(asyncio.CancelledError):
        await music_agent.daemon(
            config, FailedLibrary(), object(),
            now_fn=lambda: datetime(2026, 8, 29, 12, tzinfo=timezone.utc), sleep=stop_after_one_hour,
        )

    assert "weekly agent iteration failed week_start=2026-08-24 error_type=RuntimeError" in caplog.text
    assert secret not in caplog.text


def test_scheduled_non_monday_occurrence_uses_its_calendar_monday():
    import music_agent

    week_start, scheduled = music_agent.scheduled_week(
        datetime(2026, 8, 27, 7, tzinfo=timezone.utc), weekday=2, hour_utc=6
    )

    assert week_start == "2026-08-24"
    assert scheduled == datetime(2026, 8, 26, 6, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_daemon_uses_scheduled_week_identity_and_skips_completed_week(tmp_path):
    import music_agent

    lib = library.Library(str(tmp_path / "library.db"))
    config = music_agent.AgentConfig(str(tmp_path / "library.db"), "listener", "token", 0, 6, 30)
    now = datetime(2026, 8, 24, 5, 59, tzinfo=timezone.utc)
    sleeps = 0

    async def stop_after_second_iteration(_):
        nonlocal sleeps
        sleeps += 1
        if sleeps == 2:
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await music_agent.daemon(
            config, lib, FakeListenBrainz(), now_fn=lambda: now, sleep=stop_after_second_iteration
        )

    assert lib.get_weekly_run("2026-08-17")["status"] == "completed"
    assert lib.get_weekly_run("2026-08-24") is None
    assert sleeps == 2


@pytest.mark.asyncio
async def test_daemon_maps_non_monday_schedule_to_its_monday_run(tmp_path):
    import music_agent

    lib = library.Library(str(tmp_path / "library.db"))
    config = music_agent.AgentConfig(str(tmp_path / "library.db"), "listener", "token", 2, 6, 30)

    async def stop_after_first_iteration(_):
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await music_agent.daemon(
            config, lib, FakeListenBrainz(),
            now_fn=lambda: datetime(2026, 8, 26, 7, tzinfo=timezone.utc), sleep=stop_after_first_iteration,
        )

    assert lib.get_weekly_run("2026-08-24")["status"] == "completed"
    assert lib.get_weekly_run("2026-08-26") is None


@pytest.mark.asyncio
async def test_daemon_recovers_from_get_weekly_run_error_without_logging_token(tmp_path, monkeypatch, caplog):
    import music_agent

    secret = "token=not-for-logs"
    lib = library.Library(str(tmp_path / "library.db"))
    config = music_agent.AgentConfig(str(tmp_path / "library.db"), "listener", secret, 0, 6, 30)
    original_get_weekly_run = lib.get_weekly_run
    attempts = 0
    sleeps = 0

    def flaky_get_weekly_run(week_start):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError(secret)
        return original_get_weekly_run(week_start)

    async def stop_after_second_iteration(_):
        nonlocal sleeps
        sleeps += 1
        if sleeps == 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(lib, "get_weekly_run", flaky_get_weekly_run)
    caplog.set_level(logging.ERROR)
    with pytest.raises(asyncio.CancelledError):
        await music_agent.daemon(
            config, lib, FakeListenBrainz(),
            now_fn=lambda: datetime(2026, 8, 29, 12, tzinfo=timezone.utc), sleep=stop_after_second_iteration,
        )

    assert lib.get_weekly_run("2026-08-24")["status"] == "completed"
    assert sleeps == 2
    assert "RuntimeError" in caplog.text
    assert secret not in caplog.text


def test_agent_config_repr_hides_token():
    import music_agent

    assert "not-for-repr" not in repr(
        music_agent.AgentConfig("/tmp/music.db", "listener", "not-for-repr", 0, 6, 30)
    )


def test_agent_compose_is_opt_in_and_allows_empty_listenbrainz_credentials():
    compose = Path("compose.yaml").read_text()

    assert "profiles: [agent]" in compose
    assert "LISTENBRAINZ_USER: ${LISTENBRAINZ_USER:-}" in compose
    assert "LISTENBRAINZ_TOKEN: ${LISTENBRAINZ_TOKEN:-}" in compose
