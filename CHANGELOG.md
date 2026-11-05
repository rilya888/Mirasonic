# Changelog

All notable changes to Mirasonic are documented here.

## [Unreleased]

### Added

- `getTopSongs`, `getSimilarSongs`, `getSimilarSongs2`, `getArtistInfo2` and
  `getAlbumInfo2` return real data from YouTube Music instead of empty stubs,
  and an empty success if it fails. Top songs are ranked by actual play count.

### Changed

- Every call to YouTube Music goes through one layer in `main.py`: timeouts,
  retry on 429 and 5xx with a pause, a 5-minute cache, and merging of identical
  concurrent requests. `/search` and other non-Subsonic callers still report an
  outage as an error.

## [0.2.0] - 2026-08-31

### Added

- Optional weekly discovery agent (`--profile agent`): syncs listening
  history to ListenBrainz, requests recommendations and fresh releases, and
  builds a `Discoveries` playlist from tracks it can match on YouTube Music.
  Off by default; the playback server never imports it or needs its
  credentials.
- Local, network-free library ranking (`music_agent.py rankings`) by play
  frequency, recency, and starred status.
- Real `scrobble.view` handling: playing-now pings from clients that never
  send `submission=true` (Amperfy included) now credit a listen.

### Fixed

- Scrobbles synced to ListenBrainz hourly instead of only once a completed
  discovery week rolled over, which had delayed a Monday-afternoon listen by
  up to two weeks before it could affect recommendations.
- The agent now logs at INFO level, so a successful run leaves a trace
  instead of looking identical to a container that never started.

### Known limitations

- Other Subsonic clients have not been verified yet.
- Age-restricted tracks cannot be resolved anonymously.
- There is no multi-user support, transcoding, lyrics, podcasts, or radio.
- YouTube internal interfaces can change without notice and temporarily break playback.

[0.2.0]: https://github.com/rilya888/Mirasonic/releases/tag/v0.2.0

## [0.1.0] - 2026-08-27

### Added

- Anonymous YouTube Music search through the Subsonic API.
- AAC streaming through a self-hosted FastAPI service.
- Persistent playlists, starred tracks, artists, and albums in SQLite.
- Spotify playlist import from Exportify CSV files.
- Docker Compose deployment with a read-only container filesystem.
- Compatibility with Amperfy on iOS.

### Known limitations

- Other Subsonic clients have not been verified yet.
- Age-restricted tracks cannot be resolved anonymously.
- There is no multi-user support, transcoding, lyrics, podcasts, or radio.
- YouTube internal interfaces can change without notice and temporarily break playback.

[0.1.0]: https://github.com/rilya888/Mirasonic/releases/tag/v0.1.0
