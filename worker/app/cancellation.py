import asyncio
from collections.abc import Coroutine
from contextlib import suppress
from typing import TypeVar

from fastapi import Request

from .errors import WorkerError

T = TypeVar("T")


async def while_connected(request: Request, operation: Coroutine[None, None, T]) -> T:
    """Stop local HTTP/SQL work when C++ closes a timed-out upstream request.

    Database status checks remain necessary: a disconnect can race with commit,
    and closing an Ollama socket does not guarantee immediate model cancellation.
    """
    task = asyncio.create_task(operation)
    try:
        while not task.done():
            await asyncio.wait({task}, timeout=.25)
            if not task.done() and await request.is_disconnected():
                raise WorkerError(499, "client_disconnected", "The backend cancelled this worker request.")
        return await task
    finally:
        if not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        elif not task.cancelled():
            # Drain an exception if a disconnect races with task completion.
            task.exception()
