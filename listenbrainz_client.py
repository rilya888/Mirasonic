"""Small injected-transport wrapper for documented ListenBrainz API endpoints.

Sources:
https://listenbrainz.readthedocs.io/en/latest/users/api/core.html
https://listenbrainz.readthedocs.io/en/latest/users/api/recommendation.html
https://listenbrainz.readthedocs.io/en/latest/users/api/metadata.html
https://listenbrainz.readthedocs.io/en/latest/users/api/misc.html
https://listenbrainz.readthedocs.io/en/latest/users/api/player.html
"""

from urllib.parse import urlparse

import httpx


class ListenBrainzClient:
    """Async ListenBrainz API client that uses the supplied HTTP client."""

    def __init__(self, token: str, client: httpx.AsyncClient):
        self.token = token
        self.client = client

    @property
    def headers(self) -> dict[str, str]:
        """Return the authentication header required for listen submission."""
        return {"Authorization": f"Token {self.token}"}

    async def submit_listens(self, events: list[dict]) -> None:
        """Import up to 100 listening events, converting milliseconds to seconds."""
        if len(events) > 100:
            raise ValueError("ListenBrainz accepts at most 100 listens per request")
        if not events:
            return

        payload = []
        for event in events:
            metadata = {
                "track_name": event["title"],
                "artist_name": event["artist"],
            }
            if event.get("album"):
                metadata["release_name"] = event["album"]
            payload.append(
                {
                    "listened_at": int(event["played_at_ms"] // 1000),
                    "track_metadata": metadata,
                }
            )

        response = await self.client.post(
            "/1/submit-listens",
            headers=self.headers,
            json={"listen_type": "import", "payload": payload},
        )
        response.raise_for_status()

    async def get_recommendation_mbids(self, user: str, count: int) -> list[dict]:
        """Return collaborative-filtering recording recommendations for a user."""
        response = await self.client.get(
            f"/1/cf/recommendation/user/{user}/recording",
            params={"count": count, "offset": 0},
        )
        if response.status_code == 204:
            return []
        response.raise_for_status()
        return response.json().get("payload", {}).get("mbids", [])

    async def get_recording_metadata(self, mbids: list[str]) -> list[dict]:
        """Look up up to 50 recordings and their artists/releases."""
        if len(mbids) > 50:
            raise ValueError("ListenBrainz accepts at most 50 recording MBIDs per request")
        if not mbids:
            return []

        response = await self.client.post(
            "/1/metadata/recording/",
            json={"recording_mbids": mbids, "inc": "artist release"},
        )
        response.raise_for_status()
        body = response.json()
        if isinstance(body, list):
            return body
        return [
            {**recording, "recording_mbid": recording_mbid}
            for recording_mbid, recording in body.items()
        ]

    async def get_fresh_releases(self, user: str, days: int = 14) -> list[dict]:
        """Return personalized recently released music for a user."""
        response = await self.client.get(
            f"/1/user/{user}/fresh_releases",
            params={
                "days": days,
                "past": "true",
                "future": "false",
                "sort": "release_date",
            },
        )
        response.raise_for_status()
        body = response.json()
        return body.get("payload", {}).get("releases", body.get("releases", []))

    async def get_release_tracks(self, release_mbid: str) -> list[dict]:
        """Fetch and normalize JSPF tracks for a MusicBrainz release."""
        response = await self.client.post(f"/player/release/{release_mbid}/")
        response.raise_for_status()
        tracks = response.json().get("playlist", {}).get("track", [])
        return [
            {
                "title": track.get("title"),
                "artist": track.get("creator"),
                "album": track.get("album"),
                "duration_seconds": self._duration_seconds(track.get("duration")),
                "recording_mbid": self._recording_mbid(track),
            }
            for track in tracks
        ]

    @staticmethod
    def _recording_mbid(track: dict) -> str | None:
        """Extract a recording MBID from a JSPF MusicBrainz identifier."""
        if track.get("recording_mbid"):
            return track["recording_mbid"]
        identifiers = track.get("identifier", [])
        if isinstance(identifiers, str):
            identifiers = [identifiers]
        if not isinstance(identifiers, list):
            return None
        for identifier in identifiers:
            if not isinstance(identifier, str):
                continue
            parsed = urlparse(identifier)
            path_parts = parsed.path.strip("/").split("/")
            if parsed.netloc in {"musicbrainz.org", "www.musicbrainz.org"} and path_parts[:1] == [
                "recording"
            ]:
                return path_parts[1] if len(path_parts) > 1 else None
        return None

    @staticmethod
    def _duration_seconds(duration: int | float | None) -> int | float | None:
        """Convert JSPF millisecond durations into seconds."""
        if duration is None:
            return None
        return duration / 1000
