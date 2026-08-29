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
        # We are on a running loop here; run_coro_sync must offload to a thread.
        return await asyncio.get_running_loop().run_in_executor(
            None, run_coro_sync, lambda: _ok(5)
        )

    assert asyncio.run(driver()) == 10


def test_exception_propagates():
    try:
        run_coro_sync(lambda: _boom())
        raise AssertionError("expected ValueError")
    except ValueError as exc:
        assert str(exc) == "kaboom"
