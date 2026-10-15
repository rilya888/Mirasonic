"""Daily mirror of Spotify's own mixes (Artist Mix, Daily Mix) into the library.

Spotify's Web API answers 404 on playlists Spotify owns for any app registered
after November 2024. Exportify's app predates that and still reads them, so a
headless browser drives exportify.net exactly as a person would: log in, find
the playlist, press Export. The CSV then goes through `spotify_import` with
`replace=True` — a mix is swapped by Spotify, not grown.

The browser rides on a saved Spotify session. It is created once, by hand, on
a machine with a screen (Spotify may ask for a captcha or an email code):

    pip install playwright==1.63.0 && playwright install chromium
    python spotify_sync.py login --session ./data/spotify-session.json

The file holds live Spotify cookies — treat it as a password.
"""
import argparse
import asyncio
import logging
import os
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable

logger = logging.getLogger("spotify_sync")

EXPORTIFY_URL = "https://exportify.net/"
# exportify.net serves pavelkomarov's build (plain exportify.js), not the React
# app on watsonbox's GitHub: one table with every playlist, no search box.
# The table appears once a token is in hand, so it doubles as "logged in".
TABLE_READY = "#exportAll"


class SessionExpired(Exception):
    """Exportify did not get a token without a human: log in again."""


@dataclass(frozen=True)
class SyncConfig:
    playlists: list[str]
    session_path: str
    state_path: str
    hour_utc: int


def playlist_names(value: str) -> list[str]:
    # `|`, not a comma: mix names are free text and commas do occur in them.
    return [name.strip() for name in value.split("|") if name.strip()]


def sync_config(environ=None) -> SyncConfig:
    env = os.environ if environ is None else environ
    session = env.get("SPOTIFY_SESSION", "/data/spotify-session.json")
    return SyncConfig(
        playlist_names(env.get("SPOTIFY_SYNC_PLAYLISTS", "")),
        session,
        os.path.join(os.path.dirname(session), "spotify-sync.last"),
        int(env.get("SPOTIFY_SYNC_HOUR_UTC", "7")),
    )


async def login(session_path: str) -> None:
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context()
        page = await context.new_page()
        await page.goto(EXPORTIFY_URL)
        await page.click("#loginButton")
        print("Log in to Spotify in the opened window and press Agree.", flush=True)
        await page.locator(TABLE_READY).wait_for(timeout=600_000)
        await context.storage_state(path=session_path)
        os.chmod(session_path, 0o600)
        await browser.close()
    print(f"Session saved to {session_path}")


# Exportify lists these too, but they are not playlists: likes are `starred` on
# the server, and saved albums are albums.
NOT_PLAYLISTS = {"Liked Songs", "Saved Albums"}


async def export_playlists(names: list[str], session_path: str, out_dir: str) -> dict[str, str]:
    """Download an Exportify CSV per playlist name; a missing name is skipped.

    No names means every playlist in the Spotify library, so one added there
    turns up on the server with the next run.
    """
    from playwright.async_api import TimeoutError as PlaywrightTimeout, async_playwright

    files: dict[str, str] = {}
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        try:
            context = await browser.new_context(storage_state=session_path, accept_downloads=True)
            page = await context.new_page()
            await page.goto(EXPORTIFY_URL)
            # The saved token lives an hour; every run starts from a fresh one.
            await page.evaluate("localStorage.removeItem('access_token')")
            await page.goto(EXPORTIFY_URL)
            await page.click("#loginButton")
            # With a live Spotify session and consent already given, authorize
            # redirects straight back. A login or consent page means a human is needed.
            try:
                await page.locator(TABLE_READY).wait_for(timeout=60_000)
            except PlaywrightTimeout:
                raise SessionExpired(page.url.split("?")[0]) from None

            if not names:
                rows = await page.locator("tbody tr").evaluate_all(
                    "rs => rs.map(r => [r.cells[1].innerText, r.cells[3].innerText])")
                names = [name for name, total in rows
                         if name not in NOT_PLAYLISTS and total.strip() != "0"]
                logger.info("library playlists=%d", len(names))

            for index, name in enumerate(names):
                # `has` resolves inside each row, so the link locator must not be rooted anywhere.
                row = page.locator("tbody tr").filter(
                    has=page.get_by_role("link", name=name, exact=True)).first
                if not await row.count():
                    logger.error("playlist not found in Exportify name=%r", name)
                    continue
                async with page.expect_download(timeout=180_000) as info:
                    await row.locator("button[id^='export']").click()
                path = os.path.join(out_dir, f"{index}.csv")
                await (await info.value).save_as(path)
                files[name] = path

            # Spotify rotates its cookies; keeping the fresh ones keeps the session alive.
            await context.storage_state(path=session_path)
            os.chmod(session_path, 0o600)
        finally:
            await browser.close()
    return files


async def sync_once(lib, config: SyncConfig,
                    export: Callable[..., Awaitable[dict[str, str]]] = export_playlists) -> list[dict]:
    import spotify_import

    reports = []
    with tempfile.TemporaryDirectory() as out_dir:
        files = await export(config.playlists, config.session_path, out_dir)
        for name, path in files.items():
            report = await spotify_import.import_file(lib, path, name=name, replace=True)
            logger.info("mirrored playlist=%r added=%d removed=%d unmatched=%d",
                        name, report["added"], report["removed"], len(report["unmatched"]))
            reports.append(report)
    return reports


def _last_run(state_path: str) -> str:
    try:
        with open(state_path) as handle:
            return handle.read().strip()
    except FileNotFoundError:
        return ""


async def daemon(config: SyncConfig, run: Callable[[], Awaitable[object]], *,
                 now_fn: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                 sleep: Callable[[float], Awaitable[object]] = asyncio.sleep) -> None:
    """Check hourly; retry today's run until one succeeds."""
    while True:
        # A "day" starts at hour_utc, so before that hour yesterday's run still counts.
        day = (now_fn() - timedelta(hours=config.hour_utc)).date().isoformat()
        if _last_run(config.state_path) != day:
            try:
                await run()
                with open(config.state_path, "w") as handle:
                    handle.write(day)
                logger.info("spotify sync done day=%s", day)
            except SessionExpired as exc:
                logger.error("spotify session expired at %s — run `spotify_sync.py login` again", exc)
            except Exception as exc:
                logger.error("spotify sync failed day=%s error_type=%s",
                             day, type(exc).__name__)
        await sleep(3600)


def cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Mirror Spotify mixes through Exportify")
    parser.add_argument("command", choices=("login", "run", "daemon"))
    parser.add_argument("--session", help="session file (defaults to $SPOTIFY_SESSION)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

    config = sync_config()
    if args.session:
        config = SyncConfig(config.playlists, args.session, config.state_path,
                            config.hour_utc)
    if args.command == "login":
        asyncio.run(login(config.session_path))
        return 0

    if not os.path.exists(config.session_path):
        parser.error(f"{config.session_path} not found — run `spotify_sync.py login` first")

    import library
    lib = library.Library()
    if args.command == "run":
        asyncio.run(sync_once(lib, config))
    else:
        asyncio.run(daemon(config, lambda: sync_once(lib, config)))
    return 0


if __name__ == "__main__":
    sys.exit(cli())
