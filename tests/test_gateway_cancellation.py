import asyncio

import httpx
import pytest
from starlette.requests import Request

from noesis.model_gateway import _CallerDisconnected, _send_until_disconnect


@pytest.mark.asyncio
async def test_disconnected_worker_cancels_its_pending_provider_request():
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def provider(_request):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    async def receive():
        await started.wait()
        return {"type": "http.disconnect"}

    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
        request = client.build_request("POST", "https://provider.example/responses")
        with pytest.raises(_CallerDisconnected):
            await asyncio.wait_for(_send_until_disconnect(client, request, Request({"type": "http"}, receive)), 1)
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_completed_response_cleans_up_disconnect_listener():
    listening = asyncio.Event()
    stopped = asyncio.Event()

    async def receive():
        listening.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    async def provider(_request):
        await listening.wait()
        return httpx.Response(200, json={"id": "response"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
        request = client.build_request("POST", "https://provider.example/responses")
        response = await asyncio.wait_for(_send_until_disconnect(client, request, Request({"type": "http"}, receive)), 1)
        assert response.json() == {"id": "response"}
        await response.aclose()
    assert stopped.is_set()


@pytest.mark.asyncio
async def test_cancelled_gateway_handler_leaves_no_provider_task_running():
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def provider(_request):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    async def receive():
        await asyncio.Event().wait()

    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
        request = client.build_request("POST", "https://provider.example/responses")
        task = asyncio.create_task(_send_until_disconnect(client, request, Request({"type": "http"}, receive)))
        await asyncio.wait_for(started.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
    assert cancelled.is_set()
