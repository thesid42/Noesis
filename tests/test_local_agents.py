from __future__ import annotations

import asyncio
from pathlib import Path
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "flower_apps"))

from noesis_agents import agent_app  # noqa: E402
from noesis_agents.policy import CAMERA_IDS  # noqa: E402


MODEL = "test-model"


def _round() -> dict:
    return {
        "request_id": "local-request-1", "session_id": "session-local", "epoch": 2,
        "override_epoch": 1, "model_epoch": 4, "model": MODEL, "profile": "kimi",
        "observation_revision": 20, "media_time_s": 10.0, "deadline_remaining_ms": 29000,
        "cameras": [{"id": camera_id, "healthy": True} for camera_id in CAMERA_IDS] + [{"id": "corner", "healthy": True}],
        "program": {"camera_id": "corner", "reason": "initial shot"},
        "recent_events": [], "previous_reports": [],
    }


def _report(round_data: dict, role: str, camera_id: str | None = None) -> dict:
    return {
        "request_id": round_data["request_id"], "session_id": round_data["session_id"],
        "epoch": round_data["epoch"], "override_epoch": round_data["override_epoch"],
        "model_epoch": round_data["model_epoch"], "source_revision": round_data["observation_revision"],
        "model": round_data["model"], "role": role,
        "agent_id": f"camera-{camera_id}" if role == "camera" else role,
        "response_id": f"{role}-{camera_id or 'all'}",
        **({"camera_id": camera_id} if role == "camera" else {}),
        "result": {"recommendation": "hold", "confidence": 0.8, "reason": "Measured source is stable."} if role == "camera" else {"assessment": "steady", "reason": "Measured signals are steady."},
    }


class _Events:
    def emit(self, _event) -> None:
        pass


class _Agent:
    events = _Events()


def test_local_crew_fans_five_inferences_in_parallel_then_runs_director(monkeypatch) -> None:
    round_data = _round()
    configs = agent_app._local_crew_configs(agent_app.DEFAULT_CONTROLLER_URL)
    barrier = threading.Barrier(5)
    director_calls = []

    def camera_job(config, task):
        barrier.wait(timeout=1)
        return {"ok": True, "report": _report(task["round"], "camera", config["camera_id"])}

    def critic_job(config, task):
        barrier.wait(timeout=1)
        return {"ok": True, "report": _report(task["round"], "critic")}

    def director_job(config, task):
        director_calls.append(task)
        return {"ok": True, "decision": {"response_id": "director-response"}}

    monkeypatch.setattr(agent_app, "_camera_job", camera_job)
    monkeypatch.setattr(agent_app, "_critic_job", critic_job)
    monkeypatch.setattr(agent_app, "_director_job", director_job)

    started = time.monotonic()
    ok = agent_app._run_local_crew_round(_Agent(), configs, round_data, started + 12.0)
    elapsed = time.monotonic() - started
    assert ok is True
    assert elapsed < 1.0
    assert len(director_calls) == 1
    assert len(director_calls[0]["camera_reports"]) == 4
    assert director_calls[0]["critic_report"]["role"] == "critic"


def test_local_crew_skips_director_after_missing_or_expired_evidence(monkeypatch) -> None:
    round_data = _round()
    configs = agent_app._local_crew_configs(agent_app.DEFAULT_CONTROLLER_URL)
    director_calls = []

    def camera_job(config, task):
        if config["camera_id"] == "closeup4":
            raise agent_app.AgentTaskError("model_request_failed")
        return {"ok": True, "report": _report(task["round"], "camera", config["camera_id"])}

    monkeypatch.setattr(agent_app, "_camera_job", camera_job)
    monkeypatch.setattr(agent_app, "_critic_job", lambda _config, task: {"ok": True, "report": _report(task["round"], "critic")})
    monkeypatch.setattr(agent_app, "_director_job", lambda *_args, **_kwargs: director_calls.append(True))
    assert agent_app._run_local_crew_round(_Agent(), configs, round_data, time.monotonic() + 12.0) is False
    assert director_calls == []

    def unexpected(*_args, **_kwargs):
        raise AssertionError("expired round must not start model calls")

    monkeypatch.setattr(agent_app, "_camera_job", unexpected)
    monkeypatch.setattr(agent_app, "_critic_job", unexpected)
    assert agent_app._run_local_crew_round(_Agent(), configs, round_data, time.monotonic() - 1) is False
    assert director_calls == []


def test_local_heartbeats_cover_all_six_roles_with_truthful_runtime_and_stop(monkeypatch) -> None:
    seen: list[dict] = []
    lock = threading.Lock()
    all_seen = threading.Event()

    def fake_http(_url: str, _path: str, *, payload: dict | None = None, **_kwargs):
        assert payload is not None
        with lock:
            seen.append(payload)
            if {item["agent_id"] for item in seen} >= {config["agent_id"] for config in agent_app._local_crew_configs(agent_app.DEFAULT_CONTROLLER_URL)}:
                all_seen.set()
        return {"ok": True}

    monkeypatch.setattr(agent_app, "_http_json", fake_http)
    configs = agent_app._local_crew_configs(agent_app.DEFAULT_CONTROLLER_URL)
    stop, threads = agent_app._start_local_heartbeats(_Agent(), "local-run", configs)
    assert all_seen.wait(timeout=2)
    agent_app._stop_heartbeats(stop, threads)
    assert len({item["agent_id"] for item in seen}) == 6
    assert all(item["runtime"] == "Flower local AgentApp/1.39" for item in seen)
    assert all(item["run_id"] == "local-run" for item in seen)
    assert all(not thread.is_alive() for thread in threads)


def test_controller_http_helper_cancels_a_hung_response_at_wall_deadline(monkeypatch) -> None:
    cancelled = threading.Event()

    async def stalled_request(*_args, **_kwargs):
        try:
            await asyncio.sleep(30)
        finally:
            cancelled.set()

    monkeypatch.setattr(agent_app, "_async_controller_request", stalled_request)
    started = time.monotonic()
    try:
        agent_app._http_json(agent_app.DEFAULT_CONTROLLER_URL, "/api/ai/round", timeout_s=0.05)
    except agent_app.AgentTaskError as exc:
        assert exc.code == "controller_timeout"
    else:
        raise AssertionError("A hung controller request must time out.")
    assert time.monotonic() - started < 1.0
    assert cancelled.is_set()


def test_model_helper_cancels_a_hung_flower_request_at_wall_deadline(monkeypatch) -> None:
    cancelled = threading.Event()
    monkeypatch.setenv("FLWR_RUNTIME_BASE_URL", "http://127.0.0.1:8000/v1")
    monkeypatch.setenv("FLWR_RUNTIME_API_KEY", "runtime-test-key")

    async def stalled_model(*_args, **_kwargs):
        try:
            await asyncio.sleep(30)
        finally:
            cancelled.set()

    monkeypatch.setattr(agent_app, "_async_model_response", stalled_model)
    started = time.monotonic()
    try:
        agent_app._infer("test-model", "instructions", {}, "schema", {"type": "object"}, timeout_s=0.05)
    except agent_app.AgentTaskError as exc:
        assert exc.code == "model_request_TimeoutError"
    else:
        raise AssertionError("A hung Flower model request must time out.")
    assert time.monotonic() - started < 1.0
    assert cancelled.is_set()
