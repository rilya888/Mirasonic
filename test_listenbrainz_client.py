import json

import httpx
import pytest

from listenbrainz_client import ListenBrainzClient


def make_client(handler):
    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://api.listenbrainz.org",
    )


@pytest.mark.asyncio
async def test_submit_listens_converts_timestamp_and_metadata():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"status": "ok"})

    async with make_client(handler) as http:
        client = ListenBrainzClient("secret", http)
        await client.submit_listens(
            [
                {
                    "played_at_ms": 1_700_000_000_000.0,
                    "title": "One",
                    "artist": "Artist",
                    "album": "Album",
                }
            ]
        )

    body = json.loads(seen[0].content)
    assert seen[0].method == "POST"
    assert seen[0].url == "https://api.listenbrainz.org/1/submit-listens"
    assert seen[0].headers["Authorization"] == "Token secret"
    assert isinstance(body["payload"][0]["listened_at"], int)
    assert body == {
        "listen_type": "import",
        "payload": [
            {
                "listened_at": 1_700_000_000,
                "track_metadata": {
                    "track_name": "One",
                    "artist_name": "Artist",
                    "release_name": "Album",
                },
            }
        ],
    }


@pytest.mark.asyncio
async def test_submit_listens_omits_empty_album_and_empty_input_is_a_noop():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200)

    async with make_client(handler) as http:
        client = ListenBrainzClient("secret", http)
        await client.submit_listens([])
        await client.submit_listens(
            [{"played_at_ms": 2_000, "title": "Two", "artist": "Band", "album": ""}]
        )

    assert len(seen) == 1
    assert json.loads(seen[0].content)["payload"][0]["track_metadata"] == {
        "track_name": "Two",
        "artist_name": "Band",
    }


@pytest.mark.asyncio
async def test_submit_listens_rejects_more_than_100_events():
    async with make_client(lambda request: pytest.fail("request should not be made")) as http:
        client = ListenBrainzClient("secret", http)
        with pytest.raises(ValueError, match="100"):
            await client.submit_listens([{}] * 101)


@pytest.mark.asyncio
async def test_recommendations_returns_mbids_and_handles_no_content():
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.params["count"] == "3":
            return httpx.Response(204)
        return httpx.Response(200, json={"payload": {"mbids": [{"recording_mbid": "one"}]}})

    async with make_client(handler) as http:
        client = ListenBrainzClient("secret", http)
        recommendations = await client.get_recommendation_mbids("some user", 10)
        no_recommendations = await client.get_recommendation_mbids("some user", 3)

    assert recommendations == [{"recording_mbid": "one"}]
    assert no_recommendations == []
    assert requests[0].method == "GET"
    assert str(requests[0].url).startswith(
        "https://api.listenbrainz.org/1/cf/recommendation/user/some%20user/recording?"
    )
    assert dict(requests[0].url.params) == {"count": "10", "offset": "0"}
    assert "Authorization" not in requests[0].headers


@pytest.mark.asyncio
async def test_recording_metadata_normalizes_documented_mapping_and_empty_input_is_a_noop():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "first": {"artist_name": "First Artist"},
                "second": {"artist_name": "Second Artist"},
            },
        )

    async with make_client(handler) as http:
        client = ListenBrainzClient("secret", http)
        empty = await client.get_recording_metadata([])
        metadata = await client.get_recording_metadata(["first", "second"])

    assert empty == []
    assert metadata == [
        {"artist_name": "First Artist", "recording_mbid": "first"},
        {"artist_name": "Second Artist", "recording_mbid": "second"},
    ]
    assert seen[0].method == "POST"
    assert seen[0].url == "https://api.listenbrainz.org/1/metadata/recording/"
    assert json.loads(seen[0].content) == {
        "recording_mbids": ["first", "second"],
        "inc": "artist release",
    }
    assert "Authorization" not in seen[0].headers


@pytest.mark.asyncio
async def test_recording_metadata_preserves_compatible_list_response():
    async with make_client(
        lambda request: httpx.Response(200, json=[{"recording_mbid": "first"}])
    ) as http:
        client = ListenBrainzClient("secret", http)
        metadata = await client.get_recording_metadata(["first"])

    assert metadata == [{"recording_mbid": "first"}]


@pytest.mark.asyncio
async def test_recording_metadata_rejects_more_than_50_mbids():
    async with make_client(lambda request: pytest.fail("request should not be made")) as http:
        client = ListenBrainzClient("secret", http)
        with pytest.raises(ValueError, match="50"):
            await client.get_recording_metadata(["mbid"] * 51)


@pytest.mark.asyncio
async def test_fresh_releases_uses_personalized_endpoint_and_payload_shape():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"payload": {"releases": [{"title": "New release"}]}})

    async with make_client(handler) as http:
        client = ListenBrainzClient("secret", http)
        releases = await client.get_fresh_releases("listener", days=21)

    assert releases == [{"title": "New release"}]
    assert seen[0].method == "GET"
    assert seen[0].url.path == "/1/user/listener/fresh_releases"
    assert dict(seen[0].url.params) == {
        "days": "21",
        "past": "true",
        "future": "false",
        "sort": "release_date",
    }
    assert "Authorization" not in seen[0].headers


@pytest.mark.asyncio
async def test_release_tracks_posts_and_normalizes_jspf_tracks():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "playlist": {
                    "track": [
                        {
                            "title": "A Song",
                            "creator": "An Artist",
                            "album": "An Album",
                            "duration": 243700,
                            "identifier": "https://musicbrainz.org/recording/12345678-1234-1234-1234-123456789abc",
                        }
                    ]
                }
            },
        )

    async with make_client(handler) as http:
        client = ListenBrainzClient("secret", http)
        tracks = await client.get_release_tracks("release-id")

    assert seen[0].method == "POST"
    assert seen[0].url == "https://api.listenbrainz.org/player/release/release-id/"
    assert tracks == [
        {
            "title": "A Song",
            "artist": "An Artist",
            "album": "An Album",
            "duration_seconds": 243.7,
            "recording_mbid": "12345678-1234-1234-1234-123456789abc",
        }
    ]


@pytest.mark.asyncio
async def test_release_tracks_finds_recording_uri_in_identifier_array():
    async with make_client(
        lambda request: httpx.Response(
            200,
            json={
                "playlist": {
                    "track": [
                        {
                            "title": "Array Song",
                            "creator": "An Artist",
                            "album": "An Album",
                            "duration": 1_000,
                            "identifier": [
                                "https://example.invalid/not-a-recording/ignored",
                                "https://musicbrainz.org/recording/87654321-4321-4321-4321-cba987654321",
                            ],
                        }
                    ]
                }
            },
        )
    ) as http:
        client = ListenBrainzClient("secret", http)
        tracks = await client.get_release_tracks("release-id")

    assert tracks[0]["duration_seconds"] == 1
    assert tracks[0]["recording_mbid"] == "87654321-4321-4321-4321-cba987654321"


@pytest.mark.asyncio
async def test_http_429_raises_for_status():
    async with make_client(lambda request: httpx.Response(429)) as http:
        client = ListenBrainzClient("secret", http)
        with pytest.raises(httpx.HTTPStatusError) as error:
            await client.get_fresh_releases("listener")

    assert error.value.response.status_code == 429


@pytest.mark.asyncio
async def test_malformed_json_propagates_from_successful_response():
    async with make_client(lambda request: httpx.Response(200, content=b"not-json")) as http:
        client = ListenBrainzClient("secret", http)
        with pytest.raises(json.JSONDecodeError):
            await client.get_recommendation_mbids("listener", 1)
