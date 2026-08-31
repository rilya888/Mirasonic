# Design notes

Decisions with the evidence underneath them, so closed questions stay closed.
Numbered as they were made; a few early ones were superseded and say so.

## D-001 — Anonymous, always

Neither the server nor anything it talks to uses a Google account or a YouTube
Premium subscription. This is the property the whole project is built on, and
everything else bends around it.

## D-002 — No circumvention on the client side

The project does not forge a YouTube client, mint PoTokens, or work around age,
regional or DRM restrictions. Content that an anonymous visitor cannot reach is
simply out of scope, and the server answers 404 for it.

## D-006 — Resolving a stream on the device is impossible

**Decision:** do not attempt media-URL resolution inside a client application.

**Measured live, 2026-08-26:**

- `POST /youtubei/v1/player` from a `WEB_REMIX` client returned
  `playabilityStatus.status = UNPLAYABLE` for three different public tracks;
- the same request with `visitorData`, `Origin`, `Referer` and
  `X-Goog-Visitor-Id` returned `UNPLAYABLE` too;
- meanwhile `POST /youtubei/v1/search` from that same client works fine — so
  the problem is the player specifically, not anonymity as such.

**Why:** a web client needs a PoToken issued by Google's anti-abuse system.
Obtaining one means either running BotGuard JS or calling the internal
`waa.v1.Waa` endpoint with forged headers. Both contradict D-002.

**Outside confirmation:** the mature `EchoMusicApp/Echo-Music` client (a
SimpMusic fork) solves this with NewPipeExtractor plus PoToken minting, and its
iOS implementation `Extractor.ios.kt` is empty stubs — `newPipePlayer` returns
`emptyList()`. Nobody has walked the on-device path there either.

## D-007 — The server proxies bytes; it does not hand out the URL

**Decision:** `GET /stream/{videoId}` returns the audio body, not a redirect to
googlevideo.

**Measured live:** the signed media URL contains an `ip` parameter, and that
parameter is in the signed `sparams` list. The original URL returned `206`; the
same URL with a modified `ip` returned `403`.

**Why:** the link is cryptographically bound to the IP of the machine that
obtained it. Resolution happens on the server, playback on another device — the
IPs differ, so a redirect cannot work.

**Cost:** all audio traffic flows through the server, ~5 MB per track. For one
listener that is fine.

## D-008 — Search runs on the server too

**Decision:** search lives in the server, not in a client.

**Why:** otherwise knowledge of YouTube's internal formats lives in two places
and each breaks independently. With everything in one Python file, an upstream
change is a one-file fix with no client to rebuild and reinstall.

**Why InnerTube rather than yt-dlp for search:** measured 2026-08-26 —
InnerTube `0.8 s`, songs only, with artist and artwork; `yt-dlp ytsearch5`
`1.8 s`, returning clips and lyric videos with no artist field.

## D-009 — Reached over a private network, not published

**Decision:** the server listens on loopback and is exposed to a private
network by Tailscale (`tailscale serve`), a VPN or a reverse proxy.

**Why:** the reference deployment sits behind NAT with no public address and no
domain. `serve` traverses NAT, opens no ports and issues a real certificate on
a MagicDNS name. With valid TLS, a phone client keeps App Transport Security
fully enabled.

**Boundary:** `tailscale funnel` is never used. `serve` is visible only inside
the private network; `funnel` would publish the service to the internet.

## D-010 — An existing client instead of writing one

**Decision:** implement the Subsonic protocol and let an existing client
(Amperfy) play the music, rather than building a full client application.

**Why:** background playback, lock screen, queue, AirPlay 2, offline cache,
playlists and a real interface are already written and debugged in those
clients. Building the same thing is months of work for something one protocol
buys outright.

**Measured live:** the Spotube alternative was rejected. It was signed and
installed on a real device; its home screen returned `401` because its
ListenBrainz plugin requires an account — precisely what this project avoids —
and playback never started.

**Cost:** the server has to speak someone else's protocol rather than a
convenient one of its own.

## D-011 — Local Subsonic credentials

**Decision:** the server takes a username and password from `SUBSONIC_USER` and
`SUBSONIC_PASSWORD` and verifies them on every `/rest/*` request.

**Why:** the protocol leaves no choice. Clients append `u`, `v`, `c` and `t`+`s`
to every request, and a server that ignores them still has to accept them.

**Why this is not a contradiction of D-001:** the anonymity that matters here
is anonymity *toward YouTube*. Outward, the server still sends no cookie of any
kind. A local username and password on your own server is a protocol parameter,
not an account in a service.

**How:** `t` is checked against `md5(SUBSONIC_PASSWORD + s)`; the fallback form
`p` is accepted both as plaintext and as `enc:<hex>`. A mismatch returns
`<error code="40"/>` with HTTP 200.

**Why verify at all** when the server is unreachable from outside the private
network: the check is not against strangers, it is against yourself. On a
machine hosting several services, a request arriving from a misconfigured
neighbour should be visible rather than quietly served.

**Boundary:** there are no defaults. Without the variables the server refuses to
start.

## D-012 — The library lives on the server

**Decision:** playlists, stars and the metadata of tracks that entered the
library live in SQLite on the server, on a mounted `/data` volume, with the
container still `read_only: true`.

**Why:** clients create and edit playlists through Subsonic and expect the
server to store them. Beyond that, YouTube Music has no anonymous user
playlists at all — there is nowhere else for them to come from.

**Why this does not violate "nothing on disk":** that rule is about **audio**.
The database holds titles, artists and identifiers, measured in kilobytes.
Audio is still never stored.

**Cost:** the server gains state, and therefore a backup question. The file is
small and copies with one `scp`.

## D-013 — Metadata from InnerTube; no MusicBrainz

**Decision:** artwork, titles and artists come from the same InnerTube parsing
already in place. No external metadata catalogue is wired in.

**Why:** the server already extracts artwork — the largest thumbnail from
`musicThumbnailRenderer`. It is always present, always matches the track, and
costs neither a second request nor a fuzzy match. Cover Art Archive has no
artwork for recent releases at all. MusicBrainz holds no user playlists, and
ListenBrainz requires an account.

**Cost:** no canonical album grouping and no release dates. Acceptable — the
price would be fuzzy title/artist matching and a one-request-per-second limit.

**Narrowed by D-016.** This decision still holds for the playback path: nothing
there consults an external catalogue, and metadata still comes from InnerTube
alone. The clause "ListenBrainz requires an account" was a reason to keep it
out of playback, not a permanent ban — the optional discovery agent does use
it, off by default and never in the path that serves audio.

## D-014 — Duration comes from cheap sources, never from a resolve

**Superseded in part.** This decision originally required resolving every track
through yt-dlp to learn its duration, which was the most captcha-prone part of
the plan. Two measurements retired it.

Duration now comes from two places, neither touching yt-dlp:

1. **From search results.** Measured 2026-08-27 on a live server across 171
   requests and 1707 candidates: `durationSeconds` was present in **1707 of
   1707**, and so was `artworkURL`. Not a single null. The old claim that search
   results always carry `null` was a misreading of a doc that said only "may be
   null".
2. **From `/youtubei/v1/player`.** An anonymous request returns `videoDetails`
   even when `playabilityStatus` is `UNPLAYABLE`, and `title`, `author` and
   artwork come along with `lengthSeconds` for free. This is the fallback path
   for a track added to a playlist when the search cache is already empty — for
   instance after a restart. Without it the database would receive a `videoId`
   where the title belongs, and an INSERT would leave it there.

Stream resolution stays what it always was: a way to get bytes.

## D-015 — Client-side offline caching stays off

**Decision:** automatic caching in the client is turned off before the first
sync. Server-side protection is applied independently — stream resolution is
always serialised behind one semaphore.

**Why:** a client can download the entire library in the background, and every
track is a separate yt-dlp resolve. The captcha triggers on the *volume* of
resolves from one address, not on their concurrency, so the semaphore alone
does not solve it: it stretches the download into a queue without making it
smaller.

**Cost:** no music where the private network is unreachable.

**Not permanent:** worth revisiting once there are real numbers on resolve
volume. Decide by measurement, not by worry.

## D-016 — ListenBrainz, and why it is opt-in

**Decision:** the weekly discovery agent uses ListenBrainz for recommendations
and fresh releases. It ships as a separate container behind the compose profile
`agent`, off unless explicitly started, and the playback server neither imports
it nor needs its credentials.

**Why this decision needed making at all:** until now the project talked to
exactly one outside party, YouTube, and did so with no account and no cookie.
The agent adds a second one and sends it your listening history under an
account you create there. That is a real change in the project's shape, not an
implementation detail, and it should be a choice rather than a default.

Hence the split. `docker compose up -d` starts playback only and reaches
nothing but YouTube. `docker compose --profile agent up -d` is a separate,
deliberate act.

**Why ListenBrainz and not something else:** it is the only recommendation
source that fits the project's constraints. It is open, its API is documented
and stable, and an account there is not a Google account — the anonymity that
matters here is anonymity toward YouTube, which is untouched: the agent's
YouTube requests are the same anonymous searches everything else makes.

MusicBrainz alone has no recommendations and no user data, and Cover Art
Archive was already rejected for missing recent releases (D-013). Spotify and
Last.fm would each mean OAuth and a commercial account.

**Why ranking is local:** `ranking.py` never touches the network and does not
require a ListenBrainz account. Score a library by play counts, recency and
stars, and you get something useful with no third party at all —
`music_agent.py rankings` works on a machine that has never sent a listen
anywhere. Only *discovery*, which by definition needs to know about music you
do not have, requires the outside service.

**Why the token never reaches disk:** it is read from the environment on each
run, kept out of `repr` by a dataclass field, and a failed run persists only an
exception class name. A database that leaks is embarrassing; one that leaks a
credential is worse.

**Cost:** listening history leaves the machine when the agent is on. Anyone who
does not want that gets a strictly smaller product — local rankings and no
discovery — which is the correct trade to offer rather than to make silently.
