# Pitfalls

Mistakes that were actually made here, with the measurements that exposed them.
Read this before changing anything in the stream or Subsonic paths — most of
these look like reasonable code right up until they break something subtle.

## 1. Never warm more than one track ahead

A queue of yt-dlp requests from one address reads as a bot to YouTube and earns
the whole machine a captcha. Prefetch depth is one track, and that is a
requirement, not a tuning knob. The same rule governs the Spotify import and
any future bulk operation.

Client-side offline caching is the other way to trip this: a client that
downloads your library in the background produces exactly that queue. The
server's semaphore stretches it out but does not make it smaller, and the
captcha triggers on volume.

## 2. `HOST=0.0.0.0` inside the container is mandatory

Isolation comes from how the port is published on the host
(`127.0.0.1:8094:8080`), not from the bind address inside the container. Bind
to loopback in there and the published port reaches nothing.

## 3. Test what the live response actually contains, not your fixture

A bug involving `durationSeconds` survived for a while because a hand-written
fixture contained a field the live response does not have. Fixtures drift from
reality silently.

## 4. A live test must never skip silently

Use an unconditional unwrap rather than a skip. This project has already been
burned once by green tests running over unreachable code.

## 5. `gl` in search must match the server's country

Stream resolution happens from the server's real IP. If the search region
disagrees, search returns tracks that then fail to resolve, and the failure
looks like a bug in resolution rather than a misconfiguration.

## 6. googlevideo throttles a request without a `Range` header

Measured 2026-08-27 on one track (5.2 MB):

| Request | Result |
|---|---|
| No `Range` | 638 KB in 20 seconds (~32 KB/s) |
| `Range: bytes=0-` | whole file in under 0.15 s |

Four orders of magnitude, resting entirely on one header. On a 320-second track
the slow branch only keeps about twice ahead of playback, and any hiccup is
audible as a dropout.

This matters because AudioStreaming (inside Amperfy) sends its *main* request
without a `Range` — it only adds one when `seekOffset > 0`. So the server always
sends `Range` upstream and rewrites the resulting 206 back to 200 for a client
that did not ask for a range.

## 7. googlevideo drops the connection after about 59 seconds

Byte proxying has to resume with `Range` rather than letting the exception
escape: inside a `StreamingResponse` it truncates the body mid-answer, and the
listener hears a failure instead of a resumption.

## 8. Do not answer a Subsonic client with an error it will simply ask again about

`getAlbum` returning error code 70 produced **684 calls in half an hour**,
peaking at 292 a minute, interleaved with audio dropouts and a frozen app.

The reason: clients call `getAlbum` for every song whose album is not synced
yet, and an error never makes the album synced. An empty but valid response the
client remembers; an error it does not. Error 70 is the one code a client does
*not* show the user — it reads it as "this is gone" and silently drops the
object from its own database.

## 9. uvicorn logs around your logging discipline

Subsonic sends the username and password in every request as `u`/`p`/`t`/`s`.
Our own logger prints parameter names only — but uvicorn's access log prints
the whole request line, and on the very first deployment the credentials landed
in `docker logs` in plain text. Cut by the `_RedactRestQuery` filter in
`main.py`, which redacts `/rest` query strings and leaves everything else alone.

## 10. A song without an album, or with `size="0"`, is silently hidden

Amperfy only displays a song that passes
`excludeServerDeleteUncachedSongsFetchPredicate`:

```text
(size > 0 AND album.remoteStatus == available) OR relFilePath != nil
```

Offline caching is off here, so `relFilePath` is always nil, which leaves the
first half: a song needs both `size > 0` **and** a relationship to an album.
A song element without `album`/`albumId` never creates that relationship.

The symptom is confusing: `search3` answers 200 with tracks, the app shows the
*artist* but no songs. The artist appears because the predicate does not apply
to artists — the parser creates a local artist from the `artist` attribute as a
side effect. Hence: `album` is never empty (for a single with no album, the
track title is substituted) and `size` is never 0 (estimated from duration —
resolving every search result to measure it would run straight into #1).

## 11. `created` needs milliseconds or it parses as nil

Amperfy parses `created` with `ISO8601DateFormatter` and
`.withFractionalSeconds`. `2026-08-26T09:12:44Z` silently becomes nil;
`2026-08-26T09:12:44.000Z` works. The fractional part is not optional.

## 12. One track arrives several times in search results

YouTube puts the same track in the "top result" card and in the song list, and
repeats also occur across pages. Measured 2026-08-27 on one query: 78 positions
across 5 pages against 54 unique ones — a quarter of the window the client
asked for was duplicates.

De-duplication lives in both `parse_search_page` and `_search_songs`, and
neither can be removed: pages are parsed separately.

## 13. 416 must not become 502

AudioStreaming calls `errorOccurred` on any status ≥ 300; Amperfy catches that
in `handleError`, which calls `restartPlayer()` and
`triggerReinsertPlayableCB()` — the track is requeued and plays from the
beginning. On an honest 416 the same client just records end-of-file.

This was the answer to "when I seek, the song starts over".

## 14. googlevideo serves `itag 140` as fragmented mp4, and you cannot seek in it

The most expensive one in this list.

File structure: `[ftyp][moov 735 bytes][sidx][moof][mdat][moof][mdat]…` — there
are no sample tables in `moov`; they are spread across the `moof` boxes along
the whole file.

Measured with a real `AudioFileStream`: when only the beginning of the track has
arrived, `AudioFileStreamSeek` to second 150 returns
`kAudioFileStreamError_DataUnavailable` — the covering `moof` has not been seen,
so there is no way to know the offset. AudioStreaming silently falls back to
the linear estimate `dataOffset + time/duration*length`, lands in the middle of
an `mdat`, and feeds the parser garbage: out of 400 KB after such a seek it
recovers **129 packets instead of a thousand**. That is the crash.

The same track as ADTS: `AudioFileStreamSeek` returns `noErr`, and the same
400 KB yields **1055 packets**. ADTS is a self-synchronising stream of frames,
like mp3 — any offset is a valid entry point.

Hence the server downloads the track whole (0.15 s) and repacks it with
`ffmpeg -c:a copy -f adts`. Bitstream copy, not a re-encode: the same AAC frames
plus 7 bytes per frame, +0.7% in size. Ranges are cut in memory so
`Content-Length` and `Content-Range` are exact. Cost: ~0.4 s on the first
request for a track, then 5 ms from memory.

**The sign that was missed twice:** the first bytes of `/stream` began with
`ftypdash`. The brand `dash` said outright that the file was fragmented.

## 15. "This band has almost no songs" — measure before concluding

This looks like an age restriction and tempts you toward creating a Google
account, which would break the one property this project is built on. Check
`/prefetch/{id}` instead: a `204` means the track resolves anonymously and age
has nothing to do with it.

`UNPLAYABLE` from `/youtubei/v1/player` is **not** evidence — every track
answers that (see DESIGN-NOTES D-006).

The real ceiling is set by the client: Amperfy sends one `search3` with no
`songOffset` and never pages. The Artists tab does not help either — it shows
your library, not YouTube's catalogue. A full discography would require walking
the artist page in InnerTube (`browse` by `browseId`), which is work that does
not exist yet.

## 16. An album's `created` must not be "now"

It is the `added_at` of the album's most recent track. With the current time,
sorting by newest does not work at all: every album has the same timestamp and
it changes on every request.

## 17. `getAlbumList2` must slice honestly

Clients page through it until a response comes back empty. Lying about `offset`
means either losing the tail of the library or looping requests forever.

## 18. A weekly job needs a lock in the database, not in the process

The discovery agent ticks hourly and does one thing per ISO week. "Have I run
this week?" answered by reading a row and then writing it is a race: two ticks,
or a tick overlapping a manual `music_agent.py weekly`, both read "no" and both
build a playlist.

The fix is a claim written under `BEGIN IMMEDIATE`: the winner stamps a random
`claim_token` and a `lease_until_ms` 30 minutes out, and the loser sees a live
lease and reports `running`. Because the lock lives in the same SQLite file as
the data, it also works across processes and containers, which a `asyncio.Lock`
would not.

## 19. A lock with no expiry turns one crash into a permanent outage

The first version of the claim above had no lease. A container killed mid-run
left `status = 'running'` forever, and every later tick politely backed off —
the week was wedged with no error anywhere.

An expired lease is reclaimable, which turns a crash into a delay of at most
one tick. The commit that writes results also re-checks the claim token, so a
run whose lease expired mid-flight cannot commit on top of the newer one that
replaced it.

## 20. Building a playlist is one transaction or it is a mess

Creating the playlist, appending its tracks and recording what went into it are
three writes. Done separately, a failure in the middle leaves a half-filled
`Discoveries` playlist that the next run considers already done.

`finalize_weekly_playlist` does all of it inside one `BEGIN`/`commit`, with an
explicit `rollback` on failure. Either the week has a complete playlist or it
has nothing and will be retried.

## 21. Scrobbles arrive more than once

Clients re-send them: on retry, on reconnect, on their own schedule. Counting
each arrival inflates exactly the number that ranking depends on, and the
inflation is invisible because the playlist still looks plausible.

`UNIQUE(song_id, played_at_ms)` on `listening_events` collapses them. The
timestamp comes from the client's `time` parameter, so a genuine second play of
the same track is a different row.

## 22. Async tests that never run are worse than no tests

`pytest-asyncio` was missing from the dev requirements. Every `async def` test
was collected, skipped with a warning, and reported as passing — the suite was
green while a hundred assertions never executed. It surfaced only when CI ran
on a clean machine.

If a test is async, check that removing its assertions actually turns the suite
red.
