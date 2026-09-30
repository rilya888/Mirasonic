"""Tests for the weekly Spotify mix mirror. The browser part is stubbed."""
import asyncio
from datetime import datetime, timezone

import library
import spotify_import
import spotify_sync

HEADER = "Track URI,Track Name,Artist Name(s),Track Duration (ms)\n"


def config(tmp_path, playlists=("Монеточка Mix",)):
    return spotify_sync.SyncConfig(list(playlists), str(tmp_path / "session.json"),
                                   str(tmp_path / "spotify-sync.week"), 0, 7)


def test_playlist_names_split_on_pipe_not_comma():
    assert spotify_sync.playlist_names(" Монеточка Mix | Rock, Pop Mix ||") == [
        "Монеточка Mix", "Rock, Pop Mix"]


def test_sync_once_mirrors_each_exported_mix(tmp_path, monkeypatch):
    lib = library.Library(str(tmp_path / "test.db"))

    async def search(q="", limit=20, continuation=""):
        return {"tracks": [{"id": "vA", "title": "First", "artist": "Artist",
                            "durationSeconds": 200}], "continuation": None}

    async def export(names, session_path, out_dir):
        path = f"{out_dir}/0.csv"
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(HEADER + 'spotify:track:a,"First","Artist",200000\n')
        return {names[0]: path}

    monkeypatch.setattr(spotify_import.main, "search", search)
    playlist_id = lib.create_playlist("Монеточка Mix")
    lib.update_playlist(playlist_id, "Монеточка Mix", [], [("old", "Old", "X", None, 100, None)])

    reports = asyncio.run(spotify_sync.sync_once(lib, config(tmp_path), export))

    assert reports[0]["removed"] == 1
    assert [s["id"] for s in lib.get_playlist(playlist_id)["songs"]] == ["vA"]


class StopLoop(Exception):
    pass


def run_daemon(cfg, run, now, ticks):
    calls = {"sleeps": 0}

    async def sleep(_):
        calls["sleeps"] += 1
        if calls["sleeps"] >= ticks:
            raise StopLoop

    try:
        asyncio.run(spotify_sync.daemon(cfg, run, now_fn=lambda: now, sleep=sleep))
    except StopLoop:
        pass


def test_daemon_runs_once_per_week_and_retries_after_a_failure(tmp_path):
    cfg = config(tmp_path)
    now = datetime(2026, 9, 28, 8, tzinfo=timezone.utc)  # Monday, after 07:00
    attempts = []

    async def run():
        attempts.append(1)
        if len(attempts) == 1:
            raise spotify_sync.SessionExpired("https://accounts.spotify.com/login")

    run_daemon(cfg, run, now, ticks=4)

    assert len(attempts) == 2  # failed, retried an hour later, then left alone
    assert open(cfg.state_path).read() == "2026-09-28"
