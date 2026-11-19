import asyncio

import pytest

from app.cancellation import while_connected
from app.errors import WorkerError


class RequestStub:
    def __init__(self, disconnected):
        self.disconnected = disconnected

    async def is_disconnected(self):
        return self.disconnected


@pytest.mark.asyncio
async def test_disconnect_cancels_background_operation():
    cancelled = asyncio.Event()
    async def blocking_work():
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
    with pytest.raises(WorkerError) as caught:
        await while_connected(RequestStub(True), blocking_work())
    assert caught.value.code == "client_disconnected"
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_completed_work_returns_and_errors_propagate():
    async def success():
        return 42
    assert await while_connected(RequestStub(False), success()) == 42
    async def fail():
        raise WorkerError(502, "model_unavailable", "Failed")
    with pytest.raises(WorkerError) as caught:
        await while_connected(RequestStub(False), fail())
    assert caught.value.code == "model_unavailable"
