from __future__ import annotations

import time

import httpx
import pytest
from fastapi import FastAPI

from noesis.app import create_app
from noesis.controller import CAMERA_IDS, ControllerError
from test_ai_api import ai_result, make_ai_controller


def failure_body(lease: dict, *, error_code: str = "model_request_timeout", **overrides) -> dict:
    body = {
        key: lease[key]
        for key in ("agent_id", "request_id", "session_id", "epoch", "override_epoch", "model_epoch")
    }
    body.update(error_code=error_code, elapsed_ms=2500.0)
    body.update(overrides)
    return body


async def populate_current_reports(controller) -> list[str]:
    ids = []
    for camera_id in CAMERA_IDS[:4]:
        lease = await controller.ai_lease(f"camera-{camera_id}")
        assert lease is not None
        report = ai_result(lease, role="camera", camera_id=camera_id,
                           response_id=f"camera-report-{camera_id}-{lease['request_id']}")
        await controller.accept_ai_report(report)
        ids.append(report["response_id"])
    lease = await controller.ai_lease("critic")
    assert lease is not None
    report = ai_result(lease, role="critic", response_id=f"critic-report-{lease['request_id']}")
    await controller.accept_ai_report(report)
    ids.append(report["response_id"])
    return ids


@pytest.mark.asyncio
async def test_matching_failure_releases_lease_records_safe_status_and_is_idempotent():
    controller, _media, _obs = await make_ai_controller()
    lease = await controller.ai_lease("camera-closeup1")
    assert lease is not None

    first = await controller.fail_ai_lease(failure_body(lease))
    duplicate = await controller.fail_ai_lease(failure_body(lease, error_code="different_retry_code"))
    state = await controller.get_state()

    assert first == {"ok": True, "released": True, "duplicate": False}
    assert duplicate == {"ok": True, "released": False, "duplicate": True}
    assert "camera-closeup1" not in controller._ai_role_leases
    assert controller._metrics["ai_role_failures"] == 1
    assert controller._metrics["ai_role_timeouts"] == 1
    assert state["flower"]["inference_failures"] == [{
        "agent_id": "camera-closeup1", "role": "camera", "camera_id": "closeup1",
        "request_id": lease["request_id"], "model": lease["model"],
        "session_id": lease["session_id"], "epoch": lease["epoch"],
        "override_epoch": lease["override_epoch"], "model_epoch": lease["model_epoch"],
        "media_time_s": lease["media_time_s"], "error_code": "model_request_timeout",
        "elapsed_ms": 2500.0,
    }]
    assert all("exception" not in event["message"].lower() for event in state["events"]
               if event["kind"] == "ai_role_lease_failed")


@pytest.mark.asyncio
async def test_stale_failure_is_idempotent_but_cannot_release_a_newer_lease():
    controller, _media, _obs = await make_ai_controller()
    first_lease = await controller.ai_lease("camera-closeup2")
    assert first_lease is not None
    old_failure = failure_body(first_lease)
    await controller.fail_ai_lease(old_failure)

    controller._ai_last_lease_mono["camera-closeup2"] = 0.0
    newer_lease = await controller.ai_lease("camera-closeup2")
    assert newer_lease is not None
    assert newer_lease["request_id"] != first_lease["request_id"]

    assert await controller.fail_ai_lease(old_failure) == {
        "ok": True, "released": False, "duplicate": True,
    }
    assert controller._ai_role_leases["camera-closeup2"]["request_id"] == newer_lease["request_id"]
    stale_unknown = {**old_failure, "request_id": "never-issued-request"}
    with pytest.raises(ControllerError, match="active request"):
        await controller.fail_ai_lease(stale_unknown)
    assert controller._ai_role_leases["camera-closeup2"]["request_id"] == newer_lease["request_id"]


@pytest.mark.asyncio
async def test_failure_cannot_overwrite_success_and_later_success_clears_current_failure():
    controller, _media, _obs = await make_ai_controller()
    lease = await controller.ai_lease("camera-closeup3")
    assert lease is not None
    report = ai_result(lease, role="camera", camera_id="closeup3", response_id="success-before-failure")
    await controller.accept_ai_report(report)
    with pytest.raises(ControllerError, match="completed successfully"):
        await controller.fail_ai_lease(failure_body(lease))
    assert controller._inference_results["camera-closeup3"]["response_id"] == report["response_id"]
    assert controller._ai_lease_failures == {}

    controller._ai_last_lease_mono["camera-closeup3"] = 0.0
    next_lease = await controller.ai_lease("camera-closeup3")
    assert next_lease is not None
    await controller.fail_ai_lease(failure_body(next_lease, error_code="agent_valueerror"))
    controller._ai_last_lease_mono["camera-closeup3"] = 0.0
    final_lease = await controller.ai_lease("camera-closeup3")
    assert final_lease is not None
    await controller.accept_ai_report(ai_result(final_lease, role="camera", camera_id="closeup3",
                                                response_id="success-after-failure"))
    assert controller._ai_lease_failures == {}
    assert controller._inference_results["camera-closeup3"]["status"] == "completed"
    assert controller._metrics["ai_role_failures"] == 1


@pytest.mark.asyncio
async def test_expired_result_is_recorded_and_worker_failure_retry_is_idempotent():
    controller, _media, _obs = await make_ai_controller()
    lease = await controller.ai_lease("camera-closeup4")
    assert lease is not None
    controller._ai_role_leases["camera-closeup4"]["_captured_mono"] = time.monotonic() - 16.0
    controller._ai_role_leases["camera-closeup4"]["_expires_mono"] = time.monotonic() - 1.0
    body = ai_result(lease, role="camera", camera_id="closeup4", response_id="expired-camera-result")

    with pytest.raises(ControllerError, match="lease expired"):
        await controller.accept_ai_report(body)
    state = await controller.get_state()
    assert state["flower"]["inference_failures"][0]["error_code"] == "lease_expired"
    assert state["flower"]["inference_failures"][0]["request_id"] == lease["request_id"]
    assert controller._inference_results == {}
    assert await controller.fail_ai_lease(failure_body(lease)) == {
        "ok": True, "released": False, "duplicate": True,
    }


@pytest.mark.asyncio
async def test_late_director_rejection_is_visible_and_worker_failure_cannot_release_new_work():
    controller, _media, _obs = await make_ai_controller()
    await populate_current_reports(controller)
    director_lease = await controller.ai_lease("director")
    assert director_lease is not None
    decision = ai_result(
        director_lease, role="director", response_id="late-director-decision",
        result={"action": "hold", "camera_id": director_lease["program"]["camera_id"], "reason": "Hold at target."},
        evidence_response_ids=director_lease["evidence_response_ids"],
    )

    def reject_late(_body):
        raise ControllerError(409, "Director target already aired; request fresh evidence.")

    controller._apply_ai_director_locked = reject_late
    with pytest.raises(ControllerError, match="already aired"):
        await controller.accept_ai_decision(decision)
    assert "director" not in controller._ai_role_leases
    assert controller._ai_lease_failures["director"]["error_code"] == "director_target_expired"
    assert controller._metrics["ai_decisions"] == 0
    assert decision["response_id"] not in controller._seen_decision_set
    assert await controller.fail_ai_lease(failure_body(director_lease, error_code="job_failed")) == {
        "ok": True, "released": False, "duplicate": True,
    }

    controller._ai_last_lease_mono["critic"] = 0.0
    newer_critic = await controller.ai_lease("critic")
    assert newer_critic is not None
    stale_retry = failure_body(director_lease, error_code="job_failed")
    assert await controller.fail_ai_lease(stale_retry) == {
        "ok": True, "released": False, "duplicate": True,
    }
    assert controller._ai_role_leases["critic"]["request_id"] == newer_critic["request_id"]


@pytest.mark.asyncio
async def test_failure_endpoint_validates_codes_and_never_returns_exception_text():
    controller, media, obs = await make_ai_controller()
    app: FastAPI = create_app(media=media, obs=obs, controller=controller, auto_connect_obs=False)
    lease = await controller.ai_lease("critic")
    assert lease is not None
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://noesis.test") as client:
        invalid = await client.post("/api/ai/lease/failure", json=failure_body(lease, error_code="TimeoutError: secret"))
        assert invalid.status_code == 422
        accepted = await client.post("/api/ai/lease/failure", json=failure_body(lease, error_code="job_failed"))
        assert accepted.status_code == 200
        assert accepted.json() == {"ok": True, "released": True, "duplicate": False}
        stale = await client.post("/api/ai/lease/failure", json={**failure_body(lease), "request_id": "other"})
        assert stale.status_code == 409
        assert "secret" not in stale.text


@pytest.mark.asyncio
async def test_director_waits_for_minimum_evidence_budget_without_consuming_ids():
    controller, _media, _obs = await make_ai_controller()
    evidence_ids = await populate_current_reports(controller)
    for report in controller._ai_latest_reports.values():
        report["_captured_mono"] = time.monotonic() - 12.0

    assert await controller.ai_lease("director") is None
    assert controller._ai_last_director_evidence_ids == set()
    state = await controller.get_state()
    assert state["flower"]["director_wait_reason"] == "evidence_budget"
    assert state["flower"]["inference_failures"] == []
    assert len(evidence_ids) == 5


@pytest.mark.asyncio
async def test_director_waits_for_output_deadline_then_issues_when_delay_is_sufficient():
    controller, _media, _obs = await make_ai_controller()
    evidence_ids = await populate_current_reports(controller)
    controller._broadcast = {"ready": False, "delay_s": 3.0, "time_s": controller._session["time_s"]}

    assert await controller.ai_lease("director") is None
    assert controller._ai_last_director_evidence_ids == set()
    assert (await controller.get_state())["flower"]["director_wait_reason"] == "output_budget"

    controller._broadcast["delay_s"] = 5.0
    lease = await controller.ai_lease("director")
    assert lease is not None
    assert lease["output_deadline_remaining_ms"] >= 4900
    assert lease["deadline_remaining_ms"] >= 4900
    assert set(controller._ai_last_director_evidence_ids) == set(evidence_ids)
    assert (await controller.get_state())["flower"]["director_wait_reason"] is None
