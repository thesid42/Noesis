from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "flower_apps"))

from noesis_agents import agent_app  # noqa: E402


class _FakeResponses:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.loop_ids: set[int] = set()
        self.thread_ids: set[int] = set()
        self.cancelled = threading.Event()
        self.stall = False

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        self.loop_ids.add(id(asyncio.get_running_loop()))
        self.thread_ids.add(threading.get_ident())
        if self.stall:
            try:
                await asyncio.sleep(30)
            finally:
                self.cancelled.set()
        await asyncio.sleep(0.01)
        return SimpleNamespace(id=f"response-{len(self.calls)}")


class _FakeClient:
    def __init__(self) -> None:
        self.responses = _FakeResponses()
        self.closed = threading.Event()
        self.close_thread_ids: list[int] = []

    async def close(self) -> None:
        self.close_thread_ids.append(threading.get_ident())
        self.closed.set()


def _request(client: agent_app._PersistentInferenceClient, timeout_s: float = 1.0):
    return client.create_response(
        model="test-model", instructions="measured signals only", data={"value": 1},
        schema_name="test_result", schema={"type": "object"}, timeout_s=timeout_s,
    )


def test_persistent_client_reuses_one_loop_client_for_concurrent_requests() -> None:
    clients: list[_FakeClient] = []
    factory_threads: list[int] = []

    async def factory(_token: str) -> _FakeClient:
        factory_threads.append(threading.get_ident())
        client = _FakeClient()
        clients.append(client)
        return client

    client = agent_app._PersistentInferenceClient("private-token", client_factory=factory)
    with ThreadPoolExecutor(max_workers=5) as pool:
        replies = list(pool.map(lambda _index: _request(client), range(5)))

    assert len(clients) == 1
    assert len(replies) == 5
    assert len(clients[0].responses.calls) == 5
    assert len(clients[0].responses.loop_ids) == 1
    assert clients[0].responses.thread_ids == set(factory_threads)
    assert all(call["model"] == "test-model" for call in clients[0].responses.calls)
    client.close()
    client.close()
    assert clients[0].closed.is_set()
    assert clients[0].close_thread_ids == factory_threads
    assert not client._thread.is_alive()
    with pytest.raises(RuntimeError, match="closed"):
        _request(client)


def test_persistent_client_cancels_a_stalled_request_at_the_wall_deadline() -> None:
    fake = _FakeClient()
    fake.responses.stall = True

    async def factory(_token: str) -> _FakeClient:
        return fake

    client = agent_app._PersistentInferenceClient("private-token", client_factory=factory)
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        _request(client, timeout_s=0.05)
    assert time.monotonic() - started < 1.0
    assert fake.responses.cancelled.wait(timeout=1.0)
    fake.responses.stall = False
    assert _request(client).id
    assert not fake.closed.is_set()
    client.close()
    assert fake.closed.is_set()
    assert not client._thread.is_alive()


def test_shutdown_cancels_pending_work_before_closing_client() -> None:
    fake = _FakeClient()
    fake.responses.stall = True
    entered = threading.Event()
    original_create = fake.responses.create

    async def create(**kwargs):
        entered.set()
        return await original_create(**kwargs)

    async def close():
        assert fake.responses.cancelled.is_set()
        fake.closed.set()

    async def factory(_token):
        return fake

    fake.responses.create = create
    fake.close = close
    client = agent_app._PersistentInferenceClient("private-token", client_factory=factory)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(_request, client, 10.0)
        assert entered.wait(timeout=1.0)
        client.close()
        from concurrent.futures import CancelledError
        with pytest.raises(CancelledError):
            pending.result(timeout=1.0)
    assert fake.closed.is_set()
    assert not client._thread.is_alive()


def test_local_gateway_transport_requires_local_runtime_and_token(monkeypatch) -> None:
    monkeypatch.setenv(agent_app.INFERENCE_TRANSPORT_ENV, "unknown")
    with pytest.raises(agent_app.AgentTaskError) as error:
        agent_app._local_inference_client_from_env()
    assert error.value.code == "inference_transport_invalid"

    monkeypatch.setenv(agent_app.INFERENCE_TRANSPORT_ENV, "gateway")
    monkeypatch.delenv(agent_app.RUNTIME_DEPLOYMENT_ENV, raising=False)
    monkeypatch.setenv(agent_app.AGENT_GATEWAY_TOKEN_ENV, "test-token")
    with pytest.raises(agent_app.AgentTaskError) as error:
        agent_app._local_inference_client_from_env()
    assert error.value.code == "gateway_transport_requires_local_deployment"

    monkeypatch.setenv(agent_app.RUNTIME_DEPLOYMENT_ENV, "local")
    monkeypatch.delenv(agent_app.AGENT_GATEWAY_TOKEN_ENV, raising=False)
    with pytest.raises(agent_app.AgentTaskError) as error:
        agent_app._local_inference_client_from_env()
    assert error.value.code == "gateway_transport_token_unavailable"


def test_gateway_mode_never_falls_back_when_persistent_client_is_missing(monkeypatch) -> None:
    monkeypatch.setenv(agent_app.INFERENCE_TRANSPORT_ENV, "gateway")
    monkeypatch.setenv(agent_app.RUNTIME_DEPLOYMENT_ENV, "local")
    monkeypatch.setenv(agent_app.AGENT_GATEWAY_TOKEN_ENV, "test-token")
    with pytest.raises(agent_app.AgentTaskError) as error:
        agent_app._infer("test-model", "instructions", {}, "result", {"type": "object"})
    assert error.value.code == "gateway_persistent_client_unavailable"
