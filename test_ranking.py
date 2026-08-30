import ranking


NOW = 1_800_000_000_000
DAY = 86_400_000


def row(song_id, count, age_days, starred=0):
    return {
        "song_id": song_id,
        "listen_count": count,
        "last_played_ms": NOW - age_days * DAY,
        "starred": starred,
    }


def test_more_recent_repeat_listens_rank_higher():
    ranked = ranking.rank_tracks([row("old", 5, 80), row("recent", 5, 2)], NOW)
    assert [item["song_id"] for item in ranked] == ["recent", "old"]


def test_star_is_a_bonus_not_an_override():
    assert ranking.track_score(row("star", 1, 10, 1), NOW) < ranking.track_score(
        row("habit", 20, 10, 0), NOW
    )


def test_large_playlist_does_not_win_only_because_it_is_large():
    ranked = ranking.rank_playlists(
        [
            {"id": 1, "name": "Focused", "song_ids": ["a", "b"]},
            {"id": 2, "name": "Huge", "song_ids": ["a", "x", "y", "z"]},
        ],
        {"a": 10.0, "b": 10.0, "x": 0.0, "y": 0.0, "z": 0.0},
    )
    assert ranked[0]["name"] == "Focused"


def test_empty_playlist_has_zero_score_and_coverage():
    ranked = ranking.rank_playlists([{"id": 1, "name": "Empty", "song_ids": []}], {})
    assert ranked == [
        {"id": 1, "name": "Empty", "song_ids": [], "score": 0.0, "listened_coverage": 0.0}
    ]


def test_future_timestamp_is_treated_as_current():
    future = row("future", 0, 0)
    future["last_played_ms"] = NOW + 10 * DAY
    assert ranking.track_score(future, NOW) == ranking.track_score(row("now", 0, 0), NOW)


def test_zero_listens_still_uses_recency():
    assert ranking.track_score(row("zero", 0, 0), NOW) == 0.25


def test_equal_scores_are_ordered_by_song_id():
    ranked = ranking.rank_tracks([row("z", 4, 10), row("a", 4, 10)], NOW)
    assert [item["song_id"] for item in ranked] == ["a", "z"]


def test_thirty_day_decay_is_exactly_one_half_of_recency_component():
    assert ranking.track_score(row("month-old", 0, 30), NOW) == 0.125
