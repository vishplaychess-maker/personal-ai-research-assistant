import asyncio

from app.services.async_bridge import run_coro_sync


async def _ok(x):
    await asyncio.sleep(0)
    return x * 2


async def _boom():
    await asyncio.sleep(0)
    raise ValueError("kaboom")


def test_runs_from_sync_context():
    assert run_coro_sync(lambda: _ok(21)) == 42


def test_runs_from_inside_running_loop():
    async def driver():
        # A loop IS running on this thread; run_coro_sync must detect it and
        # offload to a worker thread rather than call asyncio.run() directly.
        return run_coro_sync(lambda: _ok(5))

    assert asyncio.run(driver()) == 10


def test_exception_propagates():
    try:
        run_coro_sync(lambda: _boom())
        raise AssertionError("expected ValueError")
    except ValueError as exc:
        assert str(exc) == "kaboom"


def test_exception_propagates_from_running_loop():
    async def driver():
        return run_coro_sync(lambda: _boom())

    try:
        asyncio.run(driver())
        raise AssertionError("expected ValueError")
    except ValueError as exc:
        assert str(exc) == "kaboom"
