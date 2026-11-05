"""The shared connection pool lives at module level, but each test runs in its
own event loop.

An `httpx.AsyncClient` binds to the loop it opened its connections on. The
first live test creates the pool and the next one receives it already dead:
`is_closed` reads `False`, because what closed is not the client but the loop
underneath it — and the failure surfaces as `SSLWantReadError` /
`Event loop is closed` instead of anything legible. In production there is one
loop for the whole process, so this is purely test isolation, not a bug in the
server.
"""

import pytest

import main


def _reset():
    main._upstream_client = None
    main._adts_cache.clear()
    main._response_cache.clear()
    main._inflight.clear()


@pytest.fixture(autouse=True)
def _fresh_upstream_client():
    _reset()
    yield
    _reset()
