from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "flower_apps"))

from noesis.ai_control import AIResultBody  # noqa: E402
from noesis_agents import agent_app  # noqa: E402
from noesis_agents.policy import (  # noqa: E402
    CAMERA_IDS,
    parse_camera_result,
    parse_critic_result,
    parse_director_result,
    safe_round_input,
)


MODEL = "dedicated/flowerai/Kimi-K2.7-Code-1OUHWL"


def _round() -> dict:
    return {
        "request_id": "request-1",
        "session_id": "session-1",
        "epoch": 2,
        "override_epoch": 3,
        "model_epoch": 4,
        "model": MODEL,
        "profile": "kimi",
        "observation_revision": 12,
        "media_time_s": 8.0,
        "deadline_remaining_ms": 28000,
        "cameras": [{"id": camera, "healthy": True, "speaking": camera == "closeup1", "age_ms": 20} for camera in CAMERA_IDS],
        "program": {"camera_id": "corner", "reason": "current shot"},
        "recent_events": [],
        "previous_reports": [],
    }


def test_model_result_schemas_reject_extra_or_unbounded_values() -> None:
    assert parse_camera_result({"recommendation": "take", "confidence": 0.7, "reason": "Speaker activity is measured."})["confidence"] == 0.7
    assert parse_critic_result({"assessment": "steady", "reason": "Signals are stable."})["assessment"] == "steady"
    assert parse_director_result({"action": "switch", "camera_id": "closeup1", "reason": "Current evidence supports the cut."})["camera_id"] == "closeup1"
    with pytest.raises(ValueError):
        parse_camera_result({"recommendation": "take", "confidence": True, "reason": "x"})
    with pytest.raises(ValueError):
        parse_critic_result({"assessment": "switch", "reason": "x"})
    with pytest.raises(ValueError):
        parse_director_result({"action": "switch", "camera_id": "unknown", "reason": "x"})


def test_safe_round_input_filters_untrusted_fields_and_nonfinite_metrics() -> None:
    value = _round()
    value["provider_key"] = "never pass this"
    value["cameras"][0]["energy"] = float("nan")
    safe = safe_round_input(value)
    assert "provider_key" not in safe
    assert safe["cameras"][0]["energy"] is None
    json.dumps(safe, allow_nan=False)


def test_ai_report_body_matches_controller_schema_without_observation_revision(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[dict] = []

    def fake_post(_url: str, _path: str, *, payload: dict | None = None, **_kwargs):
        assert payload is not None
        sent.append(payload)
        return {"ok": True, "response_id": "resp-camera"}

    monkeypatch.setattr(agent_app, "_http_json", fake_post)
    inference = {
        "result": {"recommendation": "take", "confidence": 0.8, "reason": "Measured activity."},
        "response_id": "resp-camera", "latency_ms": 321, "input_tokens": 100, "output_tokens": 20,
    }
    report = agent_app._report(
        {"agent_id": "camera-closeup1", "camera_id": "closeup1", "controller_url": agent_app.DEFAULT_CONTROLLER_URL},
        _round(), "camera", inference,
    )
    assert report == sent[0]
    assert "observation_revision" not in report
    parsed = AIResultBody.model_validate(report)
    assert parsed.source_revision == 12
    assert parsed.media_time_s == 8.0


def test_director_uses_fresh_program_and_posts_strict_provenance(monkeypatch: pytest.MonkeyPatch) -> None:
    round_data = _round()
    reports = []
    for camera_id in CAMERA_IDS:
        reports.append({
            "request_id": round_data["request_id"], "session_id": round_data["session_id"],
            "epoch": round_data["epoch"], "override_epoch": round_data["override_epoch"],
            "model_epoch": round_data["model_epoch"], "source_revision": round_data["observation_revision"],
            "model": MODEL, "role": "camera", "camera_id": camera_id,
            "agent_id": f"camera-{camera_id}", "response_id": f"response-{camera_id}",
            "result": {"recommendation": "hold", "confidence": 0.6, "reason": "Measured signals are stable."},
        })
    critic = {
        "request_id": round_data["request_id"], "session_id": round_data["session_id"],
        "epoch": round_data["epoch"], "override_epoch": round_data["override_epoch"],
        "model_epoch": round_data["model_epoch"], "source_revision": round_data["observation_revision"],
        "model": MODEL, "role": "critic", "agent_id": "critic", "response_id": "response-critic",
        "result": {"assessment": "steady", "reason": "Measured signals are stable."},
    }
    snapshot = {
        "session": {"id": "session-1", "epoch": 2, "status": "running", "time_s": 9.0},
        "model_epoch": 4, "override_epoch": 3, "model": MODEL, "observation_revision": 15,
        "program": {"camera_id": "closeup2", "reason": "Fresh shot"},
        "cameras": [{"id": camera_id, "healthy": True, "age_ms": 30} for camera_id in CAMERA_IDS] + [{"id": "corner", "healthy": True}],
    }
    posts: list[dict] = []

    def fake_http(_url: str, path: str, *, payload: dict | None = None, **_kwargs):
        if path == "/api/agents/snapshot":
            return snapshot
        posts.append(payload or {})
        return {"ok": True}

    inference_input: dict = {}

    def fake_infer(_model: str, _instructions: str, data: dict, _schema_name: str, _schema: dict, *, timeout_s: float):
        inference_input.update(data)
        return {"result": {"action": "switch", "camera_id": "closeup1", "reason": "Fresh evidence supports this camera."},
                "response_id": "response-director", "latency_ms": 300, "input_tokens": 120, "output_tokens": 24}

    monkeypatch.setattr(agent_app, "_http_json", fake_http)
    monkeypatch.setattr(agent_app, "_infer", fake_infer)
    decision = agent_app._director_job(
        {"agent_id": "director", "role": "director", "controller_url": agent_app.DEFAULT_CONTROLLER_URL},
        {"round": round_data, "camera_reports": reports, "critic_report": critic, "model_timeout_s": 8},
    )
    body = posts[0]
    parsed = AIResultBody.model_validate(body)
    assert parsed.source_revision == 15
    assert parsed.media_time_s == 9.0
    assert "observation_revision" not in body
    assert body["evidence_response_ids"] == [*(f"response-{camera_id}" for camera_id in CAMERA_IDS), "response-critic"]
    assert inference_input["fresh_snapshot"]["program"]["camera_id"] == "closeup2"
    assert decision["accepted"] == {"ok": True}


class _Grid:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict]] = []
        self.message_id = "grid-request-1"

    def tools(self):
        return [{"name": "push_messages"}, {"name": "pull_messages"}]

    def call(self, request: dict) -> dict:
        name = request["name"]
        args = json.loads(request["arguments"])
        self.sent.append((name, args))
        if name == "push_messages":
            return {"type": "function_call_output", "output": json.dumps({"results": [{"message_id": self.message_id}]})}
        if name == "pull_messages":
            reply = {"src_node_id": "42", "reply_to_message_id": self.message_id,
                     "payload": json.dumps({"ok": True, "role": "director", "kind": "round_poll", "round": _round()})}
            return {"type": "function_call_output", "output": json.dumps({"messages": [reply], "pending_message_ids": []})}
        raise AssertionError(name)


def test_cloud_round_poll_uses_director_grid_bridge() -> None:
    grid = _Grid()

    class FakeAgent:
        def __init__(self):
            self.grid = grid

    current = agent_app._poll_round_via_director(FakeAgent(), "42", 4)
    assert current["request_id"] == "request-1"
    assert grid.sent[0][1]["messages"][0]["dst_node_id"] == "42"
    assert grid.sent[0][1]["messages"][0]["payload"] == '{"kind":"round_poll"}'


def test_worker_handshake_emits_exactly_one_reply() -> None:
    class FakeGrid:
        def __init__(self):
            self.reply_calls = []

        def tools(self):
            return [{"name": "push_reply_message"}]

        def call(self, request: dict):
            self.reply_calls.append(request)
            return {"type": "function_call_output", "output": json.dumps({"message_id": "reply-id"})}

    grid = FakeGrid()

    class FakeAgent:
        def __init__(self):
            self.grid = grid
            self.events = type("Events", (), {"emit": lambda *_args, **_kwargs: None})()

    context = type("Context", (), {"node_config": {
        "role": "camera", "camera_id": "closeup1", "controller_url": agent_app.DEFAULT_CONTROLLER_URL,
        "agent_id": "camera-closeup1",
    }, "run_id": "test-run"})()
    agent_app._worker_main(FakeAgent(), context, {
        "message_id": "incoming-id", "payload": json.dumps({"kind": "handshake", "handshake_id": "h1"}),
    })
    assert len(grid.reply_calls) == 1
    args = json.loads(grid.reply_calls[0]["arguments"])
    reply = json.loads(args["payload"])
    assert reply["message_id"] == "incoming-id"
    assert reply["kind"] == "handshake"
    assert reply["role"] == "camera"
