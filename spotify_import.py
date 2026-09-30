"""Imports Spotify playlists into the server's library.

Input is a CSV from Exportify (`Track URI`, `Track Name`, `Artist Name(s)`,
`Duration (ms)`); output is a playlist in SQLite that a Subsonic client sees
as its own. Run it inside the container, with the files placed in the volume
that holds the database:

    docker compose cp playlist.csv worker:/data/playlist.csv
    docker compose exec -T worker python spotify_import.py /data/playlist.csv

What this deliberately does NOT do:

- it never resolves a stream (`yt-dlp`). Neither the `videoId`, the duration
  nor the artwork needs it — all of that arrives in the search response
  (measured 2026-08-27: 1707 of 1707 candidates carried both duration and
  artwork). Importing a hundred tracks through resolution is exactly pitfall
  #1 in docs/PITFALLS.md: a queue of requests from one address, and a captcha
  for the whole server;
- it never guesses at a doubtful match. A score below the threshold puts the
  track in the report, not in the playlist. Measured over 173 real tracks: 171
  matched confidently, and the two mistakes scored 6.0 against a threshold of
  8.0 — the threshold cuts off exactly those;
- it never deletes anything. A repeat import fills in what is missing and
  touches neither the order nor anything added by hand in the client.
"""
import argparse
import asyncio
import csv
import os
import re
import sys
import unicodedata
from typing import Optional

import library
import main

# Acceptance threshold. Below it a track goes to the report rather than the
# playlist (measured 2026-08-27: correct matches scored >= 8.0, both mistakes
# scored 6.0, and nothing landed in between).
ACCEPT_SCORE = 8.0
# Pause between searches. Import is the only place where requests come in a
# batch; a second per track looks like a person, a hundred in a row like a bot.
PAUSE_SECONDS = 0.3

REQUIRED_COLUMNS = ("Track URI", "Track Name", "Artist Name(s)")
# Exportify renamed the column in 2026; older dumps still carry the first name.
DURATION_COLUMNS = ("Duration (ms)", "Track Duration (ms)")

_FEAT = re.compile(r"\s*[\(\[]?\s*(feat\.?|ft\.?|with)\s+[^\)\]]*[\)\]]?", re.I)
_NOISE = re.compile(
    r"\s*[\(\[](remaster(ed)?[^\)\]]*|\d{4} remaster[^\)\]]*|radio edit|single version|"
    r"album version|bonus track|explicit|clean|deluxe[^\)\]]*)[\)\]]",
    re.I,
)
# A tail like "- Stadium Live" or "- Remastered 2011" is how Spotify separates
# the recording version; YouTube Music has no such title for the same track.
_VERSION_TAIL = re.compile(r"\s+-\s+.*$")


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "").lower()
    text = _FEAT.sub(" ", text)
    text = _NOISE.sub(" ", text)
    text = re.sub(r"[^\w\s]", " ", text)
    return " ".join(text.split())


# Spotify spells Russian artists in Latin (`Monetochka`, `Korol i Shut`) while
# YouTube Music keeps Cyrillic. Same person, different alphabet: compare both
# sides transliterated. `й` and `ё` need no entry — NFKD in _norm already
# folded them into `и` and `е`.
_TRANSLIT = str.maketrans({
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ж": "zh", "з": "z",
    "и": "i", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r",
    "с": "s", "т": "t", "у": "u", "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh",
    "щ": "shch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
})


def _latin(text: str) -> str:
    return _norm(text).translate(_TRANSLIT)


def _overlap(a: str, b: str) -> float:
    ta, tb = set(_latin(a).split()), set(_latin(b).split())
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / min(len(ta), len(tb))


def score(title: str, artists: list[str], seconds: int, candidate: dict) -> float:
    """How closely a candidate resembles the Spotify track.

    Duration is the most reliable signal: titles diverge constantly (the same
    song carries a Korean title alongside an English one, or `feat.` inside the
    heading instead of a separate field), while the length of a recording
    matches to the second. So a gap of more than 12 seconds is penalised: that
    is already a different recording — live, sped up, or someone else's.
    """
    delta = abs((candidate.get("durationSeconds") or 0) - seconds)
    if delta <= 2:
        total = 3.0
    elif delta <= 5:
        total = 2.0
    elif delta <= 12:
        total = 0.5
    else:
        total = -2.0

    if _latin(title) == _latin(candidate["title"]):
        total += 3.0
    else:
        total += 2.0 * _overlap(title, candidate["title"])

    if any(_latin(a) == _latin(candidate["artist"]) for a in artists):
        total += 3.0
    else:
        total += 2.0 * max((_overlap(a, candidate["artist"]) for a in artists), default=0.0)

    return total


def read_csv(path: str) -> list[dict]:
    """Exportify rows in playlist order. Spotify local files are skipped."""
    with open(path, encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        fields = reader.fieldnames or []
        missing = [c for c in REQUIRED_COLUMNS if c not in fields]
        duration = next((c for c in DURATION_COLUMNS if c in fields), None)
        if duration is None:
            missing.append(DURATION_COLUMNS[0])
        if missing:
            raise ValueError(f"{path}: missing columns {missing} — not an Exportify export")
        rows = []
        for row in reader:
            uri = (row.get("Track URI") or "").strip()
            if not uri.startswith("spotify:track:"):
                continue  # spotify:local:… — a file from someone's disk, nothing to search for
            rows.append({
                "uri": uri,
                "title": (row.get("Track Name") or "").strip(),
                # exportify.net joins artists with `;`, older dumps with `,`.
                "artists": [a.strip() for a in re.split(r"[,;]", row.get("Artist Name(s)") or "")
                            if a.strip()],
                "album": (row.get("Album Name") or "").strip() or None,
                "seconds": round(int(row[duration]) / 1000),
            })
    return rows


async def _best_candidate(query: str, track: dict) -> tuple[Optional[dict], float]:
    page = await main.search(q=query, limit=main.SEARCH_PAGE_SIZE)
    if not isinstance(page, dict):  # JSONResponse — the search failed
        return None, 0.0
    best, best_score = None, 0.0
    for candidate in page.get("tracks") or []:
        value = score(track["title"], track["artists"], track["seconds"], candidate)
        if best is None or value > best_score:
            best, best_score = candidate, value
    return best, best_score


async def find_match(track: dict) -> tuple[Optional[dict], float]:
    """A candidate and its score. Each further query runs only if the previous
    one fell short of the threshold.

    Search results are not deterministic: on 2026-08-27 one track did not make
    the top twenty during an import, though the same query a minute later put
    it in first place. So the fallback query is not only for titles with a
    version tail — a different phrasing of the same question collects a
    different page.
    """
    artist = track["artists"][0] if track["artists"] else ""
    queries = [f"{artist} {track['title']}".strip()]
    bare = _VERSION_TAIL.sub("", track["title"])
    if bare != track["title"]:
        queries.append(f"{artist} {bare}".strip())
    queries.append(f"{bare} {artist}".strip())

    best, best_score = None, 0.0
    for query in queries:
        candidate, value = await _best_candidate(query, track)
        if value > best_score:
            best, best_score = candidate, value
        if best_score >= ACCEPT_SCORE:
            break
    return best, best_score


async def add_mapping(lib: library.Library, pairs: list[tuple[str, str]]) -> None:
    """A mapping set by hand.

    Needed where the automation cannot be trusted: the artist name is written
    in a different alphabet (`Уматурман` vs `Uma2rman`, `Nautilus Pompilius`
    vs `Наутилус Помпилиус`). Title and duration match exactly in those cases
    — but so do they for a different song that merely shares a title
    (`HWASA — Maria` vs `Pianella Piano — Maria`), which is why the threshold
    stays where it is and a human makes the call.

    Track metadata comes from `/player`: in the CSV it is Spotify's, while the
    library should hold what actually plays. Artwork comes from there too —
    the first four hand-mapped tracks ended up without any precisely because
    it was not requested here.
    """
    for uri, video_id in pairs:
        details = await main.get_song_details(video_id)
        lib.upsert_song(video_id, details["title"] or video_id, details["artist"] or "",
                        None, details["duration"] or None, details["artwork"])
        lib.put_spotify_map([(uri, video_id)])
        print(f"  {uri} → {video_id}  {details['artist']} — {details['title']}")


def _playlist_id_by_name(lib: library.Library, name: str) -> Optional[int]:
    return next((p["id"] for p in lib.get_playlists() if p["name"] == name), None)


async def import_file(lib: library.Library, path: str, name: Optional[str] = None,
                      dry_run: bool = False, replace: bool = False) -> dict:
    """`replace` mirrors the file instead of appending: for Spotify's own mixes
    (Artist Mix, Daily Mix), whose contents are swapped rather than grown. The
    playlist becomes exactly the matched tracks in file order; hand-made
    additions do not survive it."""
    playlist_name = name or os.path.splitext(os.path.basename(path))[0]
    tracks = read_csv(path)
    mapping = lib.get_spotify_map()

    playlist_id = _playlist_id_by_name(lib, playlist_name)
    current = []
    if playlist_id is not None:
        current = [s["id"] for s in (lib.get_playlist(playlist_id) or {}).get("songs", [])]
    existing = set() if replace else set(current)

    to_add: list[tuple] = []
    new_pairs: list[tuple[str, str]] = []
    seen: set[str] = set()
    report = {"playlist": playlist_name, "total": len(tracks), "from_map": 0,
              "matched": 0, "already_in_playlist": 0, "unmatched": []}

    for track in tracks:
        song_id = mapping.get(track["uri"])
        meta = None
        if song_id is not None:
            report["from_map"] += 1
        else:
            candidate, value = await find_match(track)
            await asyncio.sleep(PAUSE_SECONDS)
            if candidate is None or value < ACCEPT_SCORE:
                report["unmatched"].append({
                    "track": f"{', '.join(track['artists'])} — {track['title']}",
                    "score": round(value, 2),
                    "closest": None if candidate is None
                               else f"{candidate['artist']} — {candidate['title']}",
                })
                continue
            song_id, meta = candidate["id"], candidate
            report["matched"] += 1
            new_pairs.append((track["uri"], song_id))

        if song_id in existing or song_id in seen:
            report["already_in_playlist"] += 1
            continue
        seen.add(song_id)
        to_add.append((
            song_id,
            meta["title"] if meta else track["title"],
            meta["artist"] if meta else ", ".join(track["artists"]),
            (meta.get("album") if meta else None) or track["album"],
            (meta.get("durationSeconds") if meta else None) or track["seconds"],
            meta.get("artworkURL") if meta else None,
        ))

    report["added"] = len(to_add)
    report["removed"] = len(current) if replace else 0
    if replace and not to_add and tracks:
        # Nothing matched at all — an upstream outage, not an empty mix.
        # Wiping the playlist over it would be a silent loss.
        report["added"] = report["removed"] = 0
        return report
    if dry_run:
        return report

    if playlist_id is None:
        playlist_id = lib.create_playlist(playlist_name)
    if to_add or report["removed"]:
        lib.update_playlist(playlist_id, playlist_name,
                            list(range(report["removed"])), to_add)
    if new_pairs:
        # Only after update_playlist: spotify_map references songs.
        lib.put_spotify_map([(uri, sid) for uri, sid in new_pairs
                             if lib.get_song(sid) is not None])
    report["playlist_id"] = playlist_id
    return report


def print_report(report: dict) -> None:
    print(f"\n{report['playlist']}: {report['total']} tracks in the file")
    print(f"  matched now:        {report['matched']}")
    print(f"  taken from mappings: {report['from_map']}")
    print(f"  already in playlist: {report['already_in_playlist']}")
    print(f"  added:               {report['added']}")
    if report["removed"]:
        print(f"  replaced (old):      {report['removed']}")
    if report["unmatched"]:
        print(f"  not found ({len(report['unmatched'])}):")
        for item in report["unmatched"]:
            closest = f" (closest: {item['closest']}, score {item['score']})" if item["closest"] else ""
            print(f"    - {item['track']}{closest}")


async def _main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Import Spotify playlists (Exportify CSV)")
    parser.add_argument("files", nargs="*", help="exported CSV files")
    parser.add_argument("--name", help="playlist name (defaults to the file name)")
    parser.add_argument("--dry-run", action="store_true", help="report only, write nothing")
    parser.add_argument("--replace", action="store_true",
                        help="make the playlist exactly the file (for Spotify mixes)")
    parser.add_argument("--map", action="append", metavar="URI=VIDEOID", default=[],
                        help="map a track by hand; later imports pick it up on their own")
    args = parser.parse_args(argv)

    if args.name and len(args.files) > 1:
        parser.error("--name only makes sense for a single file")

    lib = library.Library()

    pairs = []
    for item in args.map:
        uri, _, video_id = item.partition("=")
        if not uri.startswith("spotify:track:") or not video_id:
            parser.error(f"--map expects spotify:track:…=videoId, got {item!r}")
        pairs.append((uri, video_id))
    if pairs:
        print("Mapped by hand:")
        await add_mapping(lib, pairs)
    for path in args.files:
        print_report(await import_file(lib, path, args.name, args.dry_run, args.replace))
    if args.dry_run:
        print("\n--dry-run: nothing was written to the database")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main(sys.argv[1:])))
