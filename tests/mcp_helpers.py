"""Run an MCP endpoint's session manager inside one dedicated task (anyio scopes must not cross tasks)."""

import asyncio
from contextlib import asynccontextmanager


@asynccontextmanager
async def running(*endpoints):
    started = asyncio.Event()
    stop = asyncio.Event()

    async def keeper():
        from contextlib import AsyncExitStack
        async with AsyncExitStack() as stack:
            for ep in endpoints:
                await stack.enter_async_context(ep.run())
            started.set()
            await stop.wait()

    task = asyncio.create_task(keeper())
    await started.wait()
    try:
        yield
    finally:
        stop.set()
        await task
