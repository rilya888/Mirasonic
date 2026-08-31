# Mirasonic

**A Subsonic server backed by YouTube Music.**

![Mirasonic demo](docs/assets/mirasonic-demo.gif)

Point Amperfy, Symfonium, substreamer, play:Sub or any other Subsonic client at
this server and the whole YouTube Music catalogue becomes searchable and
playable — with playlists, starred songs, artists and albums that persist on
your own machine. No Google account, no cookies, no browser.

Your client already knows how to do background playback, lock-screen controls,
a queue, seeking, AirPlay and offline caching. This server exists so you do not
have to rebuild any of that: it speaks Subsonic on one side and talks to
YouTube Music on the other.

```
Subsonic client (phone, desktop)
     │  GET /rest/{action}.view?u=…&t=…&s=…
     ▼
Mirasonic  (FastAPI, your machine)
     ├─ library  → SQLite: playlists, stars, track metadata
     ├─ search   → InnerTube (music.youtube.com), Songs filter
     ├─ browse   → artists and albums derived from the library
     └─ stream   → yt-dlp resolve, bytes proxied and repacked as ADTS
```

- **Small Python services.** One playback worker by default, plus an opt-in
  weekly recommendation agent. About 3,100 lines of code, 249 tests, one SQLite
  file.
- **Nothing is written to disk except the library.** The container runs
  `read_only: true`. Audio is never stored anywhere.
- **Anonymous upstream.** Not a single cookie leaves this server toward
  YouTube.
- **Import your Spotify playlists** from an Exportify CSV, matched by duration,
  with a mapping table so a monthly re-import is cheap and stable.

## Quick start

Requires Docker and a machine that can reach YouTube.

```sh
git clone https://github.com/rilya888/Mirasonic.git
cd Mirasonic
cp .env.example .env
$EDITOR .env          # set SUBSONIC_USER, SUBSONIC_PASSWORD and REGION
docker compose up -d --build
```

The server now listens on `127.0.0.1:8094`. In your Subsonic client, add a
server with that address and the credentials you just set.

The library database lands in `./data/mirasonic.db` — a plain directory, so a
backup is one `cp`. Point `LIBRARY_PATH` elsewhere if you prefer.

`REGION` matters more than it looks. It must be the two-letter country code of
the country **this machine** reaches YouTube from: stream resolution happens
from the server's real IP, so a mismatch makes search return tracks that then
fail to play.

To run it without Docker:

```sh
pip install -r requirements.txt         # needs ffmpeg on PATH
SUBSONIC_USER=you SUBSONIC_PASSWORD=secret REGION=US \
  uvicorn main:app --host 127.0.0.1 --port 8094
```

## Exposing it

The server binds to loopback by default, and that default is deliberate: it has
no TLS of its own, and its credentials travel in every request as the Subsonic
protocol demands. Do not publish the port straight to the internet.

The setup this was built against is [Tailscale](https://tailscale.com):

```sh
tailscale serve --bg 8094      # https://<machine>.<tailnet>.ts.net
tailscale serve status         # must say "(tailnet only)"
```

That gives a real certificate on a MagicDNS name without opening a single port,
which is what lets a phone client keep full App Transport Security on. Any
WireGuard VPN or a reverse proxy terminating TLS works just as well.

`tailscale funnel` would publish the service to the open internet. Do not use it.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `SUBSONIC_USER` | — | Username your client logs in with. Required. |
| `SUBSONIC_PASSWORD` | — | Password your client logs in with. Required. |
| `REGION` | `US` | Two-letter country code for search. Must match where this machine reaches YouTube from. |
| `HOST` / `PORT` | `127.0.0.1` / `8080` | Bind address inside the process. |
| `BIND_ADDRESS` / `HOST_PORT` | `127.0.0.1` / `8094` | Where compose publishes the port on the host. |
| `LIBRARY_PATH` | `./data` | Host directory holding the library database. |
| `MIRASONIC_DB` | `/data/mirasonic.db` | Database path inside the container. |
| `LISTENBRAINZ_USER` | — | ListenBrainz account used by the optional discovery agent. |
| `LISTENBRAINZ_TOKEN` | — | ListenBrainz token used only by the optional discovery agent. |
| `AGENT_WEEKDAY` | `0` | Weekly discovery day: Monday is `0`, Sunday is `6`. |
| `AGENT_HOUR_UTC` | `6` | UTC hour at which the weekly discovery run is due. |
| `AGENT_PLAYLIST_SIZE` | `30` | Number of discoveries to add, from `1` through `50`. |

There are no defaults for the required Subsonic credentials. Without
`SUBSONIC_USER` and `SUBSONIC_PASSWORD` the playback server refuses to start
rather than letting anything through. ListenBrainz credentials are optional
and are required only when the `agent` profile or its discovery commands run.

## Weekly music discovery

The optional `agent` service turns your listening history into a weekly
`Discoveries` playlist. It sends unsynced local listens to ListenBrainz, asks
ListenBrainz for recommendations and recent releases, then only adds tracks it
can match on YouTube Music. Rankings remain completely local: they use the
same library database and do not require a ListenBrainz account or network
access.

Set `LISTENBRAINZ_USER` and `LISTENBRAINZ_TOKEN` in `.env`, then start the
agent explicitly:

```sh
docker compose --profile agent up -d
```

The agent checks once per hour and catches up the most recent scheduled week
after a restart or temporary failure. `AGENT_WEEKDAY=0` means Monday, and
`AGENT_HOUR_UTC` is always UTC. Without this profile, ordinary `docker compose
up -d` starts only the playback worker and never needs ListenBrainz credentials.

For one-off operations:

```sh
docker compose run --rm agent python music_agent.py rankings
docker compose run --rm agent python music_agent.py weekly
docker compose logs -f agent
```

If a weekly run fails, inspect the agent logs and rerun the `weekly` command;
incomplete runs are safely retried. To rotate the ListenBrainz token, update
`.env`, then recreate only the agent:

```sh
docker compose up -d --force-recreate agent
```

The agent exposes no port. It shares the library database with `worker`, but
removing the `agent` service leaves normal Subsonic playback unaffected.

## Client setup

Tested against **Amperfy** (iOS, GPL-3.0). Other Subsonic clients are not yet
verified. Two settings matter:

- **Audio format: as on the server.** There is no transcoding here — the same
  AAC bitstream that came from YouTube is served, repacked into ADTS. Asking
  for mp3 gets you AAC labelled as mp3.
- **Turn automatic offline caching off before the first sync.** A client that
  downloads your whole library in the background turns into a queue of yt-dlp
  resolves from one address, which is how you earn a captcha for the entire
  machine.

Other Subsonic clients may work — the endpoint list was derived from Amperfy's
source, so a client using methods outside those 28 may find gaps.
See [docs/SUBSONIC.md](docs/SUBSONIC.md).

## Importing Spotify playlists

Export a playlist with [Exportify](https://exportify.net), drop the CSV into
the library volume, then:

```sh
docker compose cp playlist.csv worker:/data/playlist.csv
docker compose exec -T worker python spotify_import.py --dry-run /data/playlist.csv
docker compose exec -T worker python spotify_import.py /data/playlist.csv
```

Tracks are matched primarily by **duration**, because titles diverge constantly
and lengths do not. Anything below the confidence threshold lands in the report
instead of the playlist, and can be mapped by hand:

```sh
docker compose exec -T worker python spotify_import.py \
    --map spotify:track:xxxx=videoId
```

Details, scoring and the re-import model: [docs/SPOTIFY-IMPORT.md](docs/SPOTIFY-IMPORT.md).

## What it does not do

- **Age-restricted tracks do not play.** They never resolve anonymously; the
  server returns 404. The only workaround would be account cookies, which this
  project does not do.
- **Artists and albums are yours, not YouTube's.** Those tabs are built from
  the tracks in your library, so you will not find a band's full discography
  there until its tracks are in a playlist or starred.
- **Browsing the catalogue.** Search is live; there is no "browse all albums"
  because YouTube Music has no such thing anonymously.
- **No transcoding, no lyrics, no podcasts, no radio, no multi-user.** One
  listener, one library.
- **All audio flows through this machine**, roughly 5 MB per track.

## Development

```sh
pip install -r requirements.txt
python -m pytest -q            # 249 tests, no network
python -m pytest -m live       # hits YouTube for real
```

Tests marked `live` are deselected by default; they are the only ones that
touch the network. `ffmpeg` is required for the live stream test.

When YouTube breaks extraction — which it does regularly, and which is a normal
event rather than an emergency — the fix is rebuilding against a fresh yt-dlp:

```sh
docker compose build --no-cache && docker compose up -d
```

## Documentation

| Question | File |
|---|---|
| How it is put together, HTTP contract | [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) |
| The Subsonic contract and what clients actually send | [docs/SUBSONIC.md](docs/SUBSONIC.md) |
| Why it is built this way | [docs/DESIGN-NOTES.md](docs/DESIGN-NOTES.md) |
| Traps that cost real debugging time | [docs/PITFALLS.md](docs/PITFALLS.md) |
| Spotify import | [docs/SPOTIFY-IMPORT.md](docs/SPOTIFY-IMPORT.md) |

`docs/PITFALLS.md` is the one worth reading before changing anything. Most of
it is measurements from things that broke.

## Scope

This is a personal self-hosted service: one listener, one library, on hardware
you control. It plays what is publicly available to an anonymous visitor and
circumvents no DRM or access control. It is not a public service, it has no
multi-user support, and running it as one is outside what this code is for.

YouTube's internal interfaces are not a stable public contract. They change,
and when they do, this breaks. That is the deal.

## License

GPL-3.0. See [LICENSE](LICENSE).

Mirasonic is an independent project and is not affiliated with, endorsed by,
or sponsored by YouTube, Google, Subsonic, or the developers of Amperfy.
