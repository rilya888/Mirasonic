# Contributing to Mirasonic

Thanks for helping improve Mirasonic. The project deliberately stays small: one
listener, one SQLite library, one playback service, and one optional persistent
Python service for weekly recommendations.

## Before opening an issue

- Search existing issues first.
- Confirm the problem still occurs after rebuilding with the latest `yt-dlp`.
- Never include Subsonic passwords, tokens, signed media URLs, or public IP addresses.
- For security problems, follow [SECURITY.md](SECURITY.md) instead of opening an issue.

## Development

```sh
pip install -r requirements.txt
python -m pytest -q
```

The default test suite must not access the network. Tests that intentionally use
YouTube belong under the existing `live` marker and run only with
`python -m pytest -m live`.

Keep pull requests focused. New features should fit the documented single-user,
self-hosted scope and include tests for their observable behavior.
