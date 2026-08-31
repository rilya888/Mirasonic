# The Subsonic contract

Reference for the Subsonic API layer. The reference client is **Amperfy**
(iOS, GPL-3.0, `BLeeEZ/amperfy`). Everything below about client behaviour comes
from its source rather than from the Subsonic specification: the files
`AmperfyKit/Api/Subsonic/SubsonicServerApi.swift`, `SubsonicLibrarySyncer.swift`,
`Ss*ParserDelegate.swift` and the XML fixtures in
`AmperfyKitTests/Cases/API/Subsonic/Samples/`.

The Subsonic specification describes about a hundred methods. Amperfy calls 28
of them, and only those 28 matter here. The list below is exhaustive.

## 1. What this layer does

A Subsonic client can do everything a hand-written player would have to learn:
background playback, lock screen, AirPlay, queue, playlists, offline cache. In
exchange it demands a server that speaks Subsonic.

This server already does the two hard things — search YouTube Music and serve
bytes. The Subsonic layer adds no new data sources; it **repackages the
existing ones** into the shape a client understands, and adds exactly one new
entity: a stored playlist library.

```text
Subsonic client
     │  GET {base}/rest/{action}.view?u=…&t=…&s=…&v=1.13.0&c=Amperfy
     ▼
subsonic.py
     ├─ library → SQLite (library.py)
     ├─ search  → the existing InnerTube code in main.py
     ├─ stream  → the existing byte proxy in main.py
     └─ covers  → proxied from lh3.googleusercontent.com
```

The private `/search`, `/prefetch/{id}` and `/stream/{id}` endpoints are
untouched. This layer lives under the `/rest` prefix.

## 2. Transport

None of this is guesswork; it is behaviour baked into the client. Violating any
of it breaks the connection silently.

**Path.** `{base URL}/rest/{action}.view` — the `.view` suffix is mandatory,
the client always appends it (`createBasicApiUrlComponent`). The method is
always GET.

**Response format: XML only.** Amperfy does not send the `f` parameter and
parses with `XMLParser`. Implementing JSON is unnecessary and actively harmful:
it will not be parsed.

**Status code: always `200`.** The client wraps requests in
`AF.request(...).validate()`, which treats anything outside `200..299` as an
error. A protocol error must be delivered in the body with `status="failed"` at
HTTP 200. The one exception is `404`, which Amperfy handles separately and
turns into an internal "data not found".

**Response root:**

```xml
<?xml version="1.0" encoding="UTF-8"?>
<subsonic-response xmlns="http://subsonic.org/restapi" status="ok" version="1.16.1" type="mirasonic" serverVersion="0.1.0">
  …
</subsonic-response>
```

`version` is the protocol version the server declares. Amperfy reads it from
the `ping` response and remembers it. Declare **`1.16.1`**: below `1.13.0` the
client falls back to sending the password in plaintext, and below `1.14.0` it
stops expecting an `id` in the `createPlaylist` response.

**Errors:**

```xml
<subsonic-response xmlns="http://subsonic.org/restapi" status="failed" version="1.16.1">
  <error code="70" message="Song not found"/>
</subsonic-response>
```

Codes the client distinguishes (`SubsonicServerApi.SubsonicError`):

| Code | Meaning | When it is used |
|---|---|---|
| `0` | Generic error | Internal failure that fits nothing else |
| `10` | Required parameter missing | No `id`, `name` or `query` |
| `40` | Wrong username or password | Credential check failed |
| `70` | Requested data not found | No such playlist, song or album |

Code `70` is the only one Amperfy does **not** show the user: it reads it as a
normal "this is gone" and silently removes the object from its own database.
Use it deliberately — see pitfall #8 in [PITFALLS.md](PITFALLS.md).

## 3. Authentication

The protocol requires credentials on every request. There is no way around it:
the client always sends them, and a server that ignores them still has to
accept the parameters.

Amperfy appends to every request:

| Parameter | Value |
|---|---|
| `u` | username |
| `v` | the client's protocol version, `1.13.0` |
| `c` | `Amperfy` |
| `t` + `s` | `t = md5(password + s)`, `s` a random 16-character string |
| `p` | instead of `t`+`s`, if the server declared a version below `1.13.0` |

Both schemes must be implemented: token is the primary one, `p` the fallback.
In the `p` form the password may arrive as `enc:<hex>`, a hex encoding of the
same bytes; decode and compare.

Credentials come from `SUBSONIC_USER` and `SUBSONIC_PASSWORD`. There must be no
defaults: with the variables unset, the server refuses to start rather than
letting anyone in.

They must be verified for real rather than accepted blindly. The server is
unreachable from outside its private network anyway, but a machine usually
hosts more than one service, and a request from a neighbour's misconfiguration
should be visible rather than quietly served.

> This does not conflict with the project's anonymity rule, which is about
> Google accounts and anonymity toward YouTube. A local username and password
> on your own server is a protocol parameter. Outward, the server still sends
> no cookie. See DESIGN-NOTES D-011.

## 4. The data model

This is the one place where the protocol and the source disagree
fundamentally, and how that is resolved determines everything else.

**Subsonic describes a library.** A finite set of files arranged by artist and
album, with permanent identifiers. The client downloads it whole on first
connection and works from its own copy afterwards.

**YouTube Music is a search catalogue.** It is unbounded, it has no "all
albums", and the question "show me my music collection" has no meaning in it.

The resolution:

- **The library** is what you put in it: playlists and starred tracks. It lives
  in SQLite on the server and fits entirely in memory.
- **Search** is a live query that stores nothing.
- **A track enters the library** when it is added to a playlist or starred. Its
  metadata is fixed at that moment.

Synthesising a full browsable collection out of YouTube Music is not attempted:
it is far more work and the result would be useless.

### Identifiers

Subsonic requires identifiers that persist across requests.

| Entity | ID | Example |
|---|---|---|
| Song | the `videoId` as-is | `wU26xVT_vBU` |
| Playlist | decimal SQLite rowid | `4` |
| Song cover | the same `videoId` | `wU26xVT_vBU` |
| Playlist cover | `pl-{id}` | `pl-4` |
| Artist | `ar-{first 16 hex of sha1(name)}` | `ar-4f2c1b9d0e7a3f55` |
| Album | `al-{first 16 hex of sha1(artist + "\0" + album)}` | `al-91ab77c4e0d21f38` |

A `videoId` is already permanent, opaque and unique; numbering it again would
add nothing.

`ar-`/`al-` are computed on the fly in `_add_song_element`, without storage —
they are a deterministic function of a name or a pair of names, not a stored
ID. They are needed from the very first phase, not just for browsing: without
them a `<song>` has no album relationship, and without that the client does not
display the track at all (§6). They also resolve back: `getArtist` and
`getAlbum` find library tracks by them.

**The grouping key must be computed the same way as a song's `albumId`** (for a
single with no album, from the track title), or the album on the tab and the
album reached from the song become two different entities.

### SQLite schema

```sql
CREATE TABLE songs (
  id             TEXT PRIMARY KEY,   -- videoId
  title          TEXT NOT NULL,
  artist         TEXT NOT NULL,
  album          TEXT,               -- NULL when InnerTube gave none
  duration       INTEGER,            -- seconds; NULL until resolved
  artwork_url    TEXT,
  added_at       TEXT NOT NULL       -- ISO 8601 with milliseconds, UTC
);

CREATE TABLE playlists (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  name       TEXT NOT NULL,
  created_at TEXT NOT NULL,
  changed_at TEXT NOT NULL
);

CREATE TABLE playlist_items (
  playlist_id INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
  position    INTEGER NOT NULL,      -- 0-based, no gaps
  song_id     TEXT    NOT NULL REFERENCES songs(id),
  PRIMARY KEY (playlist_id, position)
);

CREATE TABLE starred (
  song_id    TEXT PRIMARY KEY REFERENCES songs(id),
  starred_at TEXT NOT NULL
);

-- What has already been matched to YouTube during a Spotify import.
CREATE TABLE spotify_map (
  spotify_uri TEXT PRIMARY KEY,
  song_id     TEXT NOT NULL REFERENCES songs(id),
  mapped_at   TEXT NOT NULL
);
```

Three more tables belong to the optional weekly discovery agent. They are
created whether or not the agent runs — `scrobble` fills the first one from
ordinary playback — but nothing reads them until it is enabled.

```sql
-- Every play a client reported through scrobble.
CREATE TABLE listening_events (
  id                  INTEGER PRIMARY KEY AUTOINCREMENT,
  song_id             TEXT NOT NULL REFERENCES songs(id),
  played_at_ms        INTEGER NOT NULL,
  external_sent_at_ms INTEGER,          -- NULL until sent to ListenBrainz
  created_at          TEXT NOT NULL,
  UNIQUE(song_id, played_at_ms)         -- a replayed scrobble is not a second listen
);

-- One row per ISO week. Doubles as the lock that keeps two runs apart.
CREATE TABLE weekly_runs (
  week_start     TEXT PRIMARY KEY,      -- ISO Monday
  status         TEXT NOT NULL CHECK(status IN ('running', 'completed', 'failed')),
  playlist_id    INTEGER REFERENCES playlists(id) ON DELETE SET NULL,
  started_at     TEXT NOT NULL,
  finished_at    TEXT,
  error_message  TEXT,                  -- exception class only, never a token
  claim_token    TEXT,                  -- random, proves who owns the run
  lease_until_ms INTEGER                -- 30 min; an expired lease is reclaimable
);

-- What went into a week's playlist, and why.
CREATE TABLE recommendation_items (
  week_start     TEXT NOT NULL REFERENCES weekly_runs(week_start) ON DELETE CASCADE,
  position       INTEGER NOT NULL,
  song_id        TEXT NOT NULL REFERENCES songs(id),
  source         TEXT NOT NULL,         -- 'cf' or 'fresh'
  recording_mbid TEXT,
  score          REAL NOT NULL,
  PRIMARY KEY (week_start, position)
);
```

`UNIQUE(song_id, played_at_ms)` matters: clients re-send scrobbles, and without
it a retried submission would inflate the listen count that ranking depends on.

Databases created before these tables existed are upgraded in place on open —
missing tables and columns are added, nothing is rewritten.

`spotify_map` is what makes a monthly re-import cheap and predictable: a known
`spotify:track:…` is not searched again, and a hand-corrected match stays
corrected. Without it, every run would re-query YouTube for the whole playlist.

The journal is WAL. There are two writers — the server and the separate import
process — and in the default mode the second would lock the whole database and
a client would hit `database is locked` mid-song.

The file is `/data/mirasonic.db` inside the container. The container stays
`read_only: true`; only the mounted volume is writable.

`position` is always a dense range `0..n-1`. The order is recomputed in full on
every playlist change — the list is short and there is nothing to save.

### Track duration — the hole that turned out not to exist

This section used to require resolving every library track through `yt-dlp`,
which was the most captcha-prone part of the plan. Two measurements retired it.

Duration comes from two places, neither touching `yt-dlp`:

1. **From search results.** `main._parse_item` reads it from the InnerTube
   response columns. Measured 2026-08-27 on a live server, 171 requests and
   1707 candidates: `durationSeconds` arrived in **1707 of 1707**, and so did
   `artworkURL`. Not a single null.
2. **From `/youtubei/v1/player`** — `main.get_song_details`. An anonymous
   request returns `videoDetails` even when `playabilityStatus` is `UNPLAYABLE`,
   and `title`, `author` and artwork come along with `lengthSeconds` for free.
   This is the fallback path, needed when a track is added to a playlist or
   starred after the search metadata cache (§7) has already been emptied — for
   instance after a restart. Without it the database would receive a `videoId`
   in place of a title, and INSERT would leave it there.

While a duration is unknown, serve `duration="0"`. Clients survive it, showing
`0:00`.

## 5. Required endpoints

The call order on first connection is fixed in `syncInitial`: `getGenres` →
`getArtists` → `getAlbumList2` (paged, **in parallel**) → `getPlaylists` →
`getPodcasts`. All five must answer correctly or the sync aborts and no library
appears.

### `ping.view`

Connection check and the way a client learns the server version. No parameters
beyond credentials.

```xml
<subsonic-response xmlns="http://subsonic.org/restapi" status="ok" version="1.16.1" type="mirasonic" serverVersion="0.1.0"/>
```

With wrong credentials: `status="failed"` and `<error code="40" .../>`, still at
HTTP 200.

### `getPlaylists.view`

Every playlist, without contents.

```xml
<playlists>
  <playlist id="4" name="Morning" comment="" owner="you" public="false"
            songCount="12" duration="2841"
            created="2026-08-26T09:12:44.000Z" changed="2026-08-26T10:03:01.000Z"
            coverArt="pl-4"/>
</playlists>
```

`duration` is the sum of track durations; unknown ones count as zero.

### `getPlaylist.view?id=`

One playlist with contents. Tracks are `<entry>` elements in `position` order.
The `<entry>` format is in §6.

Unknown `id` → `<error code="70"/>`.

### `createPlaylist.view?name=`

Creates an empty playlist. The response **must contain the created playlist
with its `id`** — the client reads it from there (`createPlaylistRemote`).
Without it the client falls back to calling `getPlaylists` and matching by
name, and duplicate names then bind to the wrong playlist.

The response is the same `<playlist>` as `getPlaylist`, with empty contents.

Amperfy can also pass `playlistId` instead of `name` (overwriting an existing
playlist), but that path is unused in its code and need not be implemented.

### `updatePlaylist.view`

The only method that changes contents.

| Parameter | Meaning |
|---|---|
| `playlistId` | which playlist |
| `name` | new name (Amperfy always sends it, even when changing only tracks) |
| `songIndexToRemove` | repeated: indices of tracks to remove |
| `songIdToAdd` | repeated: `videoId`s of tracks to add |

**The order of operations is mandatory and not arbitrary:** first remove every
listed index, counting them **against the state of the list before the
operation**, then append the additions at the end.

Reordering depends on exactly this: Amperfy shuffles a playlist by removing all
indices `0..n-1` and re-adding the whole list in the desired order
(`syncUpload(playlistToUpdateOrder:)`). Applying removals sequentially and
recomputing indices destroys the playlist.

A track added for the first time must appear in the `songs` table. Its metadata
comes from the search cache (§7), or from `/youtubei/v1/player` on a miss.

Response: an empty `<subsonic-response status="ok" …/>`.

### `deletePlaylist.view?id=`

Deletes a playlist and its items. Empty success. Unknown `id` →
`<error code="70"/>`.

### `search3.view`

Live search. Parameters from Amperfy: `query`, `artistCount`, `artistOffset`,
`albumCount`, `albumOffset`, `songCount`, `songOffset`. The client calls this
three times, zeroing two of the three counters each time.

- `songCount > 0` — return songs from YouTube Music. The existing search code
  returns 20 per page; to fill a larger request, follow the continuation token,
  but **no more than three upstream calls per request**.
- `artistCount > 0` or `albumCount > 0` — return an empty result. Searching for
  artists and albums needs different InnerTube filters and is not implemented.

Every returned track is put into the in-memory metadata cache (§7) — otherwise
a later `updatePlaylist` would know neither title nor artist.

### `stream.view?id=` and `download.view?id=`

Audio bytes, served by the same code as the private `/stream/{videoId}`. Both
actions lead to one handler — clients use `download` when caching in the
original format and `stream` everywhere else.

A client may add `format` (`mp3`/`raw`) and `maxBitRate`. There is no
transcoding here: the same AAC bitstream that came from googlevideo is always
served, moved from fragmented mp4 into ADTS (`audio/aac`) — otherwise clients
crash on seek (see [PITFALLS.md](PITFALLS.md) #14). Both parameters are
ignored, so set the client's format preference to "as on the server"; choosing
mp3 gets you AAC labelled as mp3.

Here and only here the response may carry `206` and `416`.

### `getCoverArt.view?id=`

Artwork. `id` is either a `videoId` or `pl-{id}`.

- `videoId`: take `artwork_url` from `songs` or the search cache and **proxy the
  bytes**. A redirect is not acceptable: it would send the client from its
  private network out to `lh3.googleusercontent.com`.
- `pl-{id}`: the cover of the playlist's first track. Empty playlist →
  `<error code="70"/>`.

The `size` parameter is ignored.

### `getStarred2.view`

Starred tracks.

```xml
<starred2>
  <song id="wU26xVT_vBU" … starred="2026-08-26T10:03:01.000Z"/>
</starred2>
```

`<artist>` and `<album>` are not returned inside it.

### `star.view` / `unstar.view`

`id` is a song; `albumId` and `artistId` are accepted and ignored. Songs go into
the `starred` table. Empty success.

### `scrobble.view`

Records a play in `listening_events`. This is the only stub that grew into a
real endpoint, because the weekly discovery agent needs listening history and
the client already reports it.

| Parameter | Handling |
|---|---|
| `id` | repeated; missing entirely → `<error code="10"/>` |
| `time` | repeated, positionally paired with `id`; ms since epoch. Absent, unparseable or negative → recorded as now |
| `submission` | `false`/`0` means "now playing", not a play — accepted and ignored |

The song's metadata is resolved before the row is written, so a track played
straight from search results enters `songs` rather than leaving a dangling
reference.

Recording is unconditional: listens accumulate whether or not the agent is
enabled, and simply stay unsynced. Duplicate submissions of the same
(song, timestamp) collapse — see §4.

### `getSong.view?id=`

One track. The library is the first source, the search cache the second — a
track found by search and not yet added to the library is a normal state and
must not vanish. Never answers 70.

### `getArtists.view`, `getAlbumList2.view`, `getArtist.view`, `getAlbum.view`

Artists and albums are not stored: they are derived from the `songs` table by
grouping on `artist` and on (`artist`, `album`). Measured against a live
database 2026-08-27: 303 tracks, 302 of them with a real album name, 111
artists, 240 albums — on bare singles these tabs would degrade into a list of
three hundred one-song "albums".

**Lists read only the library; point lookups also read the search cache.** This
split is not cosmetic. A list that swells with recent queries and shrinks after
a restart reads to the client as objects being deleted, and it purges them
(§2). But `getAlbum` about a track just found by search must answer
substantively, or the track disappears from the results.

`getArtists` returns `<index>` groups by first letter; digits and symbols go
under `#`. The protocol has no flat list.

```xml
<artists ignoredArticles="">
  <index name="D">
    <artist id="ar-4f2c1b9d0e7a3f55" name="Daft Punk" albumCount="2" coverArt="wU26xVT_vBU"/>
  </index>
</artists>
```

`getAlbumList2` takes `type`, `size` and `offset` and **must slice honestly**:
clients page until a response comes back empty. Lying about `offset` means
either losing the tail of the library or looping forever.

| `type` | Behaviour |
|---|---|
| `alphabeticalByName` (and anything unknown) | by album name |
| `alphabeticalByArtist` | by artist, then by name |
| `newest`, `recent`, `frequent` | by `created`, descending |
| `random` | shuffled |
| `starred` | albums containing a starred song |

The server keeps no play counts, so `frequent` and `recent` answer the same as
`newest`. Adding counters for two tabs is not worth it.

**Single-track releases named after the track itself are removed from the
list.** Measured 2026-08-27: 73 such tiles out of 240, because a library
assembled from Spotify playlists holds exactly one track from most releases —
on the tab that is a duplicate of the track, not an album. Only browsing is
hidden: a song's `albumId` is unchanged and `getAlbum` still answers for it, so
navigating from track to album works. That resolution must not be broken —
without an album relationship the client stops showing the track itself (§6).

Being a single track is not by itself enough to hide a release: 302 of 303
tracks carry a real release name from YouTube.

An album's `created` is the `added_at` of its most recent track, not the time of
the response. Otherwise sorting by newest does not work at all.

`getArtist` returns `<artist>` with nested `<album>` elements and no songs;
`getAlbum` returns `<album>` with songs. **Both answer an unknown `id` with
success and empty contents**, not error 70 — see §2 and pitfall #8.

## 6. The song element format

The same attribute set is used in `<song>`, `<entry>` and `<child>` — Amperfy
parses all three names with one code path (`SsPlayableParserDelegate`).

```xml
<song id="wU26xVT_vBU"
      title="One More Time"
      artist="Daft Punk"
      artistId="ar-4f2c1b9d0e7a3f55"
      album="Discovery"
      albumId="al-91ab77c4e0d21f38"
      duration="320"
      coverArt="wU26xVT_vBU"
      contentType="audio/aac"
      suffix="aac"
      bitRate="129"
      isDir="false"
      type="music"
      created="2026-08-26T09:12:44.000Z"
      starred="2026-08-26T10:03:01.000Z"/>
```

| Attribute | Required | Note |
|---|---|---|
| `id` | yes | Without it the element is silently dropped |
| `isDir` | yes | Must be `"false"`. With `"true"` the element is dropped |
| `title`, `artist` | yes | InnerTube guarantees both |
| `duration` | yes | Seconds. Unknown → `0` |
| `coverArt` | yes | Otherwise the client never requests artwork |
| `contentType`, `suffix` | yes | `audio/aac` and `aac` |
| `album`, `albumId`, `size` | **effectively yes** | Formally optional in Subsonic, but without them the client will not display the track — see below |
| `artistId` | no | Without it the artist is created locally, by name |
| `created` | no | **Only** the millisecond format: `2026-08-26T09:12:44.000Z`. Without them the date parses as nil |
| `bitRate`, `year`, `genre`, `track` | no | Send if known |
| `starred` | no | Presence of the attribute means starred |

> **Trap: without `album`/`albumId` and without `size > 0`, Amperfy silently
> hides the track.** Found 2026-08-26 during a live check: `search3` answered
> 200 with tracks, and the list in the app was empty — only the artist was
> visible. The source is `SongMO+CoreDataClass.swift`: a song is displayed only
> if it passes `excludeServerDeleteUncachedSongsFetchPredicate`:
>
> ```text
> (size > 0 AND album.remoteStatus == available) OR relFilePath != nil
> ```
>
> Offline caching is deliberately off (DESIGN-NOTES D-015), so `relFilePath` is
> always nil and the first half remains: both `size > 0` **and** an album
> relationship are required. Without `album`/`albumId` in `<song>` no
> relationship is created at all — `available` need not be set explicitly,
> since Core Data gives `RemoteStatus.available == 0` to any album the parser
> creates; only the existence of the relationship matters. The artist stays
> visible because the predicate does not apply to artists:
> `SsSongParserDelegate` creates a local artist from the `artist` attribute as
> a side effect, which produces the deceptive "artist present, no tracks".
>
> Practical consequence: `album` can never be empty (for a single with no album
> in the InnerTube results, substitute the track title), and `size` can never
> be `0`. See `subsonic.py::_add_song_element`, where the size is estimated
> from duration rather than measured through `yt-dlp` — resolving every search
> result would run straight into pitfall #1.

## 7. The search metadata cache

Time passes between `search3` and `updatePlaylist`, and the client sends only a
`videoId` when adding. Without intermediate storage the title and artist are
lost and the track lands in the database nameless.

The solution is a `videoId → metadata` dictionary in process memory, filled on
every parse of search results. Bounding it is mandatory: unlike the other
caches here, this one grows with every search. An `OrderedDict` of 2000 entries
with oldest-first eviction is enough.

A miss must not be fatal: metadata then comes from `/youtubei/v1/player`.

## 8. Captcha protection

Pitfall #1: a queue of `yt-dlp` requests from one address reads as a bot and
earns the whole machine a captcha. The Subsonic layer brings that danger much
closer, because a real client can do things a minimal one could not.

- **Stream resolution is serialised.** One `asyncio.Semaphore(1)` for the whole
  process around the `yt-dlp` call. Already-cached URLs bypass it.
- **Client offline caching must be off** (DESIGN-NOTES D-015). A client can
  download the whole library in the background; the semaphore stretches that
  into a queue but does not shrink it, and the captcha triggers on volume.
- **Bulk operations use the same queue.** The Spotify import obeys this rather
  than working around it.

The parallel `getAlbumList2` calls on first connection are harmless: they read
SQLite and never go upstream.

## 9. Stub endpoints

Clients call these, and silence reads as a failure. All answer success with
empty contents, which is enough.

| Action | Response |
|---|---|
| `getGenres` | `<genres/>` |
| `getIndexes` | `<indexes lastModified="0" ignoredArticles=""/>` |
| `getMusicFolders` | `<musicFolders/>` |
| `getPodcasts`, `getNewestPodcasts` | `<podcasts/>`, `<newestPodcasts/>` |
| `getInternetRadioStations` | `<internetRadioStations/>` |
| `getSimilarSongs2` | `<similarSongs2/>` |
| `getOpenSubsonicExtensions` | empty success — the client concludes there are none |
| `setRating`, `deletePodcastEpisode` | empty success, action ignored |
| `getRandomSongs` | a random selection of `size` rows from `songs` |
| `getMusicDirectory` | `<error code="70"/>` — folder browsing, which Amperfy never calls |

`getGenres`, `getArtists`, `getAlbumList2` and `getPlaylists` are the four
without which the first sync will not complete. An empty response passes it; an
error does not. Of these only `getGenres` is still a stub: a track has no genre
in the InnerTube results or in the database, so there is nowhere to get one.

## 10. Not implemented

Podcasts, radio, video, jukebox, chat, sharing, bookmarks, transcoding, users
and permissions, `getLyricsBySongId`, library scanning. Each either has no
source or falls outside the project's scope.
