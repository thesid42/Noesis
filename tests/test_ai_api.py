from __future__ import annotations

import math
import time

import httpx
import pytest
from fastapi import FastAPI

from noesis.app import create_app
from noesis.controller import CAMERA_IDS, ControllerError, DirectorController
from test_controller import FakeMedia, FakeOBS


MODEL_CATALOG = [
    {"id": "kimi", "label": "Kimi K2.7", "model": "test-kimi-model", "available": True},
    {"id": "minimax", "label": "MiniMax M3", "model": "test-minimax-model", "available": True},
]
CAMERA_ROLE_IDS = CAMERA_IDS[:4]


async def make_ai_controller(*, model_catalog=None, **controller_options):
    media = FakeMedia()
    obs = FakeOBS(media)
    controller = DirectorController(
        media,
        obs,
        model_catalog=MODEL_CATALOG if model_catalog is None else model_catalog,
        **controller_options,
    )
    await controller.session_start("synthetic", "preview")
    for camera_id in CAMERA_ROLE_IDS:
        await controller.heartbeat({
            "agent_id": f"camera-{camera_id}", "role": "camera", "camera_id": camera_id,
            "runtime": "Flower AgentApp", "run_id": f"run-{camera_id}", "decision_mode": "llm",
        })
    for role in ("director", "critic"):
        await controller.heartbeat({
            "agent_id": role, "role": role, "runtime": "Flower AgentApp",
            "run_id": f"run-{role}", "decision_mode": "llm",
        })
    return controller, media, obs


async def new_round(controller: DirectorController) -> dict:
    round_payload = await controller.ai_round()
    assert round_payload is not None
    return round_payload


def ai_result(
    round_payload: dict,
    *,
    role: str,
    agent_id: str | None = None,
    camera_id: str | None = None,
    result: dict | None = None,
    response_id: str | None = None,
    **overrides,
) -> dict:
    agent_id = agent_id or (f"camera-{camera_id}" if role == "camera" else role)
    body = {
        "request_id": round_payload["request_id"],
        "session_id": round_payload["session_id"],
        "epoch": round_payload["epoch"],
        "override_epoch": round_payload["override_epoch"],
        "model_epoch": round_payload["model_epoch"],
        "agent_id": agent_id,
        "role": role,
        "source_revision": round_payload["observation_revision"],
        "media_time_s": round_payload["media_time_s"],
        "model": round_payload["model"],
        "response_id": response_id or f"resp-{agent_id}-{round_payload['request_id'][:8]}",
        "latency_ms": 250.0,
        "input_tokens": 80,
        "output_tokens": 24,
        "result": result or ({
            "recommendation": "hold", "confidence": 0.6, "reason": f"{camera_id} is usable."
        } if role == "camera" else {"assessment": "steady", "reason": "Current framing remains clear."}
          if role == "critic" else {
            "action": "hold", "camera_id": "corner", "reason": "Hold the current shot."
          }),
    }
    if camera_id is not None:
        body["camera_id"] = camera_id
    body.update(overrides)
    return body


async def submit_five_reports(controller: DirectorController, round_payload: dict) -> list[str]:
    response_ids = []
    for camera_id in CAMERA_ROLE_IDS:
        body = ai_result(round_payload, role="camera", camera_id=camera_id)
        await controller.accept_ai_report(body)
        response_ids.append(body["response_id"])
    critic = ai_result(round_payload, role="critic")
    await controller.accept_ai_report(critic)
    response_ids.append(critic["response_id"])
    return response_ids


def director_choice(round_payload: dict, evidence_response_ids: list[str], camera_id: str = "closeup3") -> dict:
    return ai_result(
        round_payload,
        role="director",
        result={"action": "switch", "camera_id": camera_id, "reason": "The editorial context favors this healthy view."},
        response_id=f"resp-director-{round_payload['request_id'][:8]}",
        evidence_response_ids=evidence_response_ids,
    )


@pytest.mark.asyncio
async def test_camera_vad_alone_never_triggers_a_cut_without_ai_director_decision():
    controller, media, _obs = await make_ai_controller()
    media.cameras["closeup1"].update(speaking=True, energy=1.0, speaker_state="speaking")
    media.cameras["closeup2"].update(speaking=True, energy=0.2, speaker_state="speaking")

    await controller.tick()
    state = await controller.get_state()
    assert state["program"]["camera_id"] == "corner"
    assert state["program"].get("decision_source") != "model"
    assert state["flower"]["inference_results"] == []
    assert state["metrics"]["ai_decisions"] == 0


@pytest.mark.asyncio
async def test_round_requires_four_camera_reports_and_critic_then_accepts_director_choice():
    controller, media, _obs = await make_ai_controller()
    media.cameras["closeup1"].update(speaking=True, energy=1.0, speaker_state="speaking")
    await controller.tick()
    round_payload = await new_round(controller)
    assert {camera["id"] for camera in round_payload["cameras"]} >= set(CAMERA_ROLE_IDS)

    camera_response_ids = []
    for camera_id in CAMERA_ROLE_IDS:
        body = ai_result(round_payload, role="camera", camera_id=camera_id)
        await controller.accept_ai_report(body)
        camera_response_ids.append(body["response_id"])
    with pytest.raises(ControllerError, match="four AI camera reports and critic"):
        await controller.accept_ai_decision(director_choice(round_payload, camera_response_ids))

    critic = ai_result(round_payload, role="critic")
    await controller.accept_ai_report(critic)
    evidence_ids = [*camera_response_ids, critic["response_id"]]
    decision = director_choice(round_payload, evidence_ids, camera_id="closeup3")
    result = await controller.accept_ai_decision(decision)

    state = await controller.get_state()
    assert result["executed"] is True
    assert state["program"]["camera_id"] == "closeup3"  # AI may choose a healthy, non-speaking camera.
    assert state["program"]["decision_source"] == "model"
    assert state["program"]["reason"] == decision["result"]["reason"]
    assert state["metrics"]["ai_cuts"] == 1
    assert len(state["flower"]["inference_results"]) == 6
    assert state["flower"]["model_status"] == "verified"


@pytest.mark.asyncio
async def test_malformed_duplicate_nonfinite_and_future_source_ai_reports_are_rejected():
    controller, _media, _obs = await make_ai_controller()
    round_payload = await new_round(controller)
    valid = ai_result(round_payload, role="camera", camera_id="closeup1")
    await controller.accept_ai_report(valid)
    with pytest.raises(ControllerError, match="already accepted|already reported"):
        await controller.accept_ai_report(valid)

    malformed = ai_result(
        round_payload,
        role="camera",
        camera_id="closeup2",
        result={"recommendation": "take", "confidence": 0.7, "reason": "Clear.", "camera_id": "closeup2"},
    )
    with pytest.raises(ControllerError, match="Malformed AI camera recommendation"):
        await controller.accept_ai_report(malformed)

    nonfinite = ai_result(round_payload, role="camera", camera_id="closeup2", media_time_s=math.nan)
    with pytest.raises(ControllerError, match="Malformed AI result"):
        await controller.accept_ai_report(nonfinite)

    future_source = ai_result(round_payload, role="camera", camera_id="closeup2", source_revision=10**9)
    with pytest.raises(ControllerError, match="source time or revision"):
        await controller.accept_ai_report(future_source)


@pytest.mark.asyncio
async def test_stale_epoch_model_manual_and_replay_responses_are_rejected():
    controller, _media, _obs = await make_ai_controller()
    round_payload = await new_round(controller)
    stale_epoch = ai_result(round_payload, role="camera", camera_id="closeup1", epoch=round_payload["epoch"] - 1)
    with pytest.raises(ControllerError, match="epoch"):
        await controller.accept_ai_report(stale_epoch)

    old_model_response = ai_result(round_payload, role="camera", camera_id="closeup1")
    await controller.select_model("minimax")
    with pytest.raises(ControllerError, match="inactive|model_epoch|model"):
        await controller.accept_ai_report(old_model_response)

    controller, _media, _obs = await make_ai_controller()
    round_payload = await new_round(controller)
    old_manual_response = ai_result(round_payload, role="camera", camera_id="closeup1")
    await controller.manual_override("closeup2")
    with pytest.raises(ControllerError, match="inactive|manual"):
        await controller.accept_ai_report(old_manual_response)

    controller, _media, _obs = await make_ai_controller()
    round_payload = await new_round(controller)
    old_replay_response = ai_result(round_payload, role="camera", camera_id="closeup1")
    await controller.session_seek(5.0)
    with pytest.raises(ControllerError, match="inactive|epoch"):
        await controller.accept_ai_report(old_replay_response)


@pytest.mark.asyncio
async def test_director_target_is_rechecked_for_health_immediately_before_commit():
    controller, media, _obs = await make_ai_controller()
    round_payload = await new_round(controller)
    evidence_ids = await submit_five_reports(controller, round_payload)
    decision = director_choice(round_payload, evidence_ids, camera_id="closeup3")

    media.cameras["closeup3"].update(healthy=False, status="offline")
    await controller.tick()
    with pytest.raises(ControllerError, match="currently unavailable"):
        await controller.accept_ai_decision(decision)
    assert (await controller.get_state())["program"]["camera_id"] == "corner"


@pytest.mark.asyncio
async def test_hot_model_switch_keeps_playhead_and_invalidates_old_ai_round():
    controller, media, _obs = await make_ai_controller()
    media.time_s = 7.25
    await controller.tick()
    old_round = await new_round(controller)
    stale_report = ai_result(old_round, role="camera", camera_id="closeup1")
    before = (await controller.get_state())["session"]["time_s"]

    switched = await controller.select_model("minimax")
    assert switched["models"]["selected"] == "minimax"
    assert switched["models"]["epoch"] == old_round["model_epoch"] + 1
    assert switched["session"]["status"] == "running"
    assert switched["session"]["time_s"] == before
    assert media.status == "running"
    with pytest.raises(ControllerError, match="inactive|model_epoch|model"):
        await controller.accept_ai_report(stale_report)
    next_round = await new_round(controller)
    assert next_round["request_id"] != old_round["request_id"]
    assert next_round["model"] == "test-minimax-model"


@pytest.mark.asyncio
async def test_backward_seek_does_not_feed_future_reports_to_next_round():
    controller, media, _obs = await make_ai_controller()
    media.time_s = 15.0
    await controller.tick()
    old_round = await new_round(controller)
    await controller.accept_ai_report(ai_result(old_round, role="camera", camera_id="closeup1"))
    await controller.session_seek(0.0)
    replay = await new_round(controller)
    assert replay["media_time_s"] == 0.0
    assert replay["previous_reports"] == []


@pytest.mark.asyncio
async def test_ai_cannot_select_failure_slate_while_healthy_sources_exist():
    controller, _media, _obs = await make_ai_controller()
    round_payload = await new_round(controller)
    evidence_ids = await submit_five_reports(controller, round_payload)
    with pytest.raises(ControllerError, match="Slate"):
        await controller.accept_ai_decision(director_choice(round_payload, evidence_ids, camera_id="slate"))
    assert (await controller.get_state())["program"]["camera_id"] == "corner"


@pytest.mark.asyncio
async def test_flower_outage_holds_the_current_healthy_camera():
    controller, media, _obs = await make_ai_controller(heartbeat_ttl_s=1.0)
    media.cameras["closeup1"].update(speaking=True, energy=1.0)
    for agent_id in list(controller._agent_seen_mono):
        controller._agent_seen_mono[agent_id] = time.monotonic() - 2.0
    await controller.tick()
    state = await controller.get_state()
    assert state["flower"]["status"] == "disconnected"
    assert state["mode"] == "degraded"
    assert state["program"]["camera_id"] == "corner"
    assert state["flower"]["inference_results"] == []


@pytest.mark.asyncio
async def test_model_select_http_schema_and_unavailable_configuration():
    controller, media, obs = await make_ai_controller()
    app: FastAPI = create_app(media=media, obs=obs, controller=controller, auto_connect_obs=False)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://noesis.test") as client:
        initial = await client.get("/api/state")
        assert initial.status_code == 200
        assert initial.json()["models"]["selected"] == "kimi"

        switched = await client.post("/api/models/select", json={"profile": "minimax"})
        assert switched.status_code == 200
        assert switched.json()["models"]["selected"] == "minimax"

        invalid = await client.post("/api/models/select", json={"profile": "unknown"})
        assert invalid.status_code == 422
        extra = await client.post("/api/models/select", json={"profile": "kimi", "model": "secret"})
        assert extra.status_code == 422

    unavailable_catalog = [
        {"id": "kimi", "label": "Kimi", "model": "test-kimi-model", "available": True},
        {"id": "minimax", "label": "MiniMax", "model": "test-minimax-model", "available": False},
    ]
    controller, media, obs = await make_ai_controller(model_catalog=unavailable_catalog)
    app = create_app(media=media, obs=obs, controller=controller, auto_connect_obs=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://noesis.test") as client:
        unavailable = await client.post("/api/models/select", json={"profile": "minimax"})
        assert unavailable.status_code == 409
        assert "not configured" in unavailable.json()["detail"]


@pytest.mark.asyncio
async def test_ai_http_round_and_report_reject_malformed_body_without_network_calls():
    controller, media, obs = await make_ai_controller()
    app = create_app(media=media, obs=obs, controller=controller, auto_connect_obs=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://noesis.test") as client:
        round_response = await client.get("/api/ai/round")
        assert round_response.status_code == 200
        round_payload = round_response.json()
        assert round_payload["model"] == "test-kimi-model"
        assert round_payload["model_epoch"] == 0

        malformed = await client.post("/api/ai/report", json={"role": "camera", "result": {"confidence": 0.5}})
        assert malformed.status_code == 422

        valid = ai_result(round_payload, role="camera", camera_id="closeup1")
        accepted = await client.post("/api/ai/report", json=valid)
        assert accepted.status_code == 200
        assert accepted.json()["response_id"] == valid["response_id"]


@pytest.mark.asyncio
async def test_model_selection_while_manual_control_is_latched_keeps_camera_and_clock():
    controller, media, _obs = await make_ai_controller()
    await controller.manual_override("closeup3")
    media.time_s = 3.75
    await controller.tick()
    before = (await controller.get_state())["session"]["time_s"]

    state = await controller.select_model("minimax")
    assert state["mode"] == "manual"
    assert state["program"]["camera_id"] == "closeup3"
    assert state["session"]["time_s"] == before
    assert media.status == "running"
