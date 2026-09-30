# Importing Spotify playlists

Your Spotify playlists end up in the library and appear in a Subsonic client as
ordinary playlists. A monthly refresh needs no manual work and does not
re-search tracks that are already matched.

Not covered: Spotify OAuth, live sync, Apple Music, likes and listening
history, and deleting tracks that vanished from a Spotify playlist.

## Input

A CSV from [Exportify](https://exportify.net), a web page that exports Spotify
playlists to a table. Four columns are needed: `Track URI`, `Track Name`,
`Artist Name(s)`, `Duration (ms)` (`Track Duration (ms)` in 2026 exports).

There is deliberately no Spotify API integration. OAuth means an account,
tokens and somewhere to keep them, for an operation done once a month that
takes a minute.

## Running it

```sh
docker compose cp playlist.csv worker:/data/playlist.csv

# report only, nothing written
docker compose exec -T worker python spotify_import.py --dry-run /data/playlist.csv

# for real
docker compose exec -T worker python spotify_import.py /data/playlist.csv
```

The playlist name comes from the file name (Exportify names files after the
playlist); `--name` overrides it for a single file.

## How tracks are matched

One search request per track. Candidates are scored on three signals —
duration, title, artist — with an acceptance threshold of **8.0**. Below that,
the track goes into the report rather than the playlist.

**Duration is the main signal, not the title.** Titles diverge constantly: the
same song carries a Korean title next to an English one (`꽃(FLOWER)` against
`FLOWER`), `feat.` sits inside the heading instead of a separate field, a
`(7 ver.)` is glued on. The length of a recording matches to the second.

**`yt-dlp` is never called during an import.** The `videoId`, the duration and
the artwork all arrive directly in the search response. A hundred resolves in a
row from one address is pitfall #1 — a captcha for the whole machine.

Fallback queries run only when the first fell short of the threshold: once
without the version tail (`- Stadium Live`), once with artist and title
swapped. The second exists because search results are not deterministic — one
track did not make the top twenty during an import though a minute later it
stood first.

## Manual mapping

```sh
docker compose exec -T worker python spotify_import.py \
    --map spotify:track:XXXX=videoId
```

Artist and title are compared transliterated, so a plain romanisation matches
on its own: `Nautilus Pompilius` against `Наутилус Помпилиус`, `Monetochka`
against `Монеточка` (a live Monetochka Mix went from 12 to 44 of 50 with it).

Needed where the automation cannot be trusted but a human can: a name that is
not a plain romanisation. `Уматурман` against `Uma2rman`, `Splean` against
`Сплин`, `Agatha Christie` against `Агата Кристи` — title and duration match
exactly, and the score is 6.0.

The threshold is not lowered for this, because a *different* song sharing a
title and a length scores exactly the same (`HWASA — Maria` against
`Pianella Piano — Maria`). Only a person can tell those apart, so the
automation stays quiet and the decision is made by hand — once, after which
`spotify_map` remembers it.

Metadata for `--map` comes from `/player` rather than from the CSV: the library
should hold what actually plays.

## Re-importing

The `spotify_map` table (SUBSONIC.md §4) holds `spotify_uri → song_id`. A known
track is not searched again, and a hand-corrected match stays corrected.

An import only **appends**. The playlist is found by name; tracks already in it
are skipped; the order of existing entries does not change; anything added by
hand in the client stays. Tracks removed from the playlist on Spotify are not
removed here — a deliberate choice, since a silent deletion is worse than a
surplus track.

## Results from a live run

173 tracks across three playlists:

| | |
|---|---|
| Matched and added | **171** (98.8%) |
| Not found | 2 — neither song exists in the YouTube Music catalogue |
| Wrong matches | 0 |
| Duration within ±2 s | 172 of 173 |
| Rows in `spotify_map` | 169 (two tracks appear in two playlists) |
| Orphaned rows in `playlist_items` | 0 |

Both misses were caught by the threshold at 6.0 against 8.0 — there was a clean
gap between correct and incorrect matches.

A second run over three more playlists (150 tracks): 146 matched on their own,
3 were artist names in a different alphabet and were set with `--map`, and 1 is
absent from the catalogue — **149 of 150**. The library after both runs: 6
playlists, 320 positions, 303 tracks, 299 rows in `spotify_map`, 0 orphans.

## Spotify mixes: `--replace`

Artist Mix and Daily Mix playlists are rebuilt by Spotify, not grown, so
appending would make them swell forever. `--replace` makes the playlist exactly
the matched tracks of the file, in file order. Known tracks still come from
`spotify_map` without a search. Hand-made additions do not survive a replace.
If no track matched at all, the playlist is left alone: that is an upstream
outage, not an empty mix.

```sh
docker compose exec -T worker python spotify_import.py --replace "/data/Монеточка Mix.csv"
```

## Automatic weekly mirror

`spotify_sync.py` does the Exportify step itself, in headless Chromium, and
imports each listed mix with `--replace`. Spotify's API answers 404 on
Spotify-owned playlists for apps registered after November 2024; Exportify's
app is older and still reads them, which is why the browser goes through it
rather than through an API client of our own.

Once, on a machine with a screen (Spotify may ask for a captcha or a code):

```sh
pip install playwright==1.63.0 && playwright install chromium
python spotify_sync.py login --session ./data/spotify-session.json
```

Copy `spotify-session.json` into the server's `LIBRARY_PATH`. It holds live
Spotify cookies: treat it like a password. Then in `.env`:

```sh
SPOTIFY_SYNC_PLAYLISTS=Монеточка Mix|Daily Mix 1
```

```sh
docker compose --profile spotify-sync up -d --build
docker compose run --rm spotify-sync python spotify_sync.py run   # once, now
```

The daemon checks hourly and runs once per week (Monday 07:00 UTC by default)
and retries until it succeeds. When the session dies, the log says
`spotify session expired`; repeat `login` and copy the file again.

## Tests

`test_spotify_import.py`, 16 tests: candidate scoring against the real
mismatches (a different artist with the same title, a different song by the
same artist, a live take caught by duration), CSV parsing (Spotify local files
skipped, a foreign file rejected, several artists in one column), and the
import itself (order preserved from the file, doubtful matches reported not
added, a second run searching and adding nothing, monthly append, hand-made
additions surviving, fallback queries, `--dry-run`, upstream failure, manual
mapping through `--map`).
