"""Run an async coroutine to completion from sync OR already-async code.

No event loop running (sync route, threadpool, CLI, tests) -> asyncio.run().
A loop already running (async FastAPI route) -> asyncio.run() would raise, so
execute the coroutine in a one-shot worker thread that owns its own loop.
Future.result() re-raises any exception, so callers' try/except still works.
"""

import asyncio
import concurrent.futures
from typing import Awaitable, Callable, TypeVar

T = TypeVar("T")


def run_coro_sync(make_coro: Callable[[], Awaitable[T]]) -> T:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(make_coro())

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(lambda: asyncio.run(make_coro())).result()


if __name__ == "__main__":
    async def _double(n):
        await asyncio.sleep(0)
        return n * 2

    assert run_coro_sync(lambda: _double(3)) == 6

    async def _from_loop():
        return await asyncio.get_running_loop().run_in_executor(
            None, run_coro_sync, lambda: _double(4)
        )

    assert asyncio.run(_from_loop()) == 8
    print("async_bridge self-check OK")
