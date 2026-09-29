"""Causality and action checks for the Flower agent helper policy."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "flower_apps"))

from noesis_agents.policy import (  # noqa: E402
    make_camera_observation,
    observation_revision,
    parse_editorial_policy,
    rule_decision,
    usable_camera_reports,
)
from noesis_agents.agent_app import _editorial_model_input, _proposal_body  # noqa: E402


def _request(*reports: dict, candidate: str | None = "closeup1") -> dict:
    return {
        "request_id": "ld-12-test",
        "session_id": "session-a",
        "epoch": 3,
        "override_epoch": 2,
        "observation_revision": 125,
        "candidate_camera_id": candidate,
        "program_camera_id": "corner",
        "cameras": [
            {"id": "closeup1", "healthy": True},
            {"id": "closeup2", "healthy": True},
            {"id": "corner", "healthy": True},
        ],
        "camera_observations": list(reports),
    }


def _report(camera_id: str, *, revision: int = 125, speaking: bool | None = True,
            healthy: bool = True, age_ms: int = 150) -> dict:
    return {
        "camera_id": camera_id,
        "source_observation_revision": revision,
        "age_ms": age_ms,
        "observation": {
            "session_id": "session-a",
            "epoch": 3,
            "healthy": healthy,
            "speaking": speaking,
            "speaker_state": "speaking" if speaking else "silence",
        },
    }


def test_revision_uses_exact_snapshot_or_frame_revision() -> None:
    assert observation_revision({"observation_revision": 12}) == 12
    assert observation_revision({"cameras": [{"frame_url": "/api/frame/closeup1.jpg?rev=38"}]}) == 38
    assert observation_revision({"observation_revision": True, "cameras": []}) is None


def test_camera_observation_keeps_mapped_causal_evidence_compact() -> None:
    result = make_camera_observation(
        {
            "participant": "C",
            "healthy": True,
            "status": "synthetic",
            "speaking": False,
            "speaker_state": "silence",
            "energy": 0.02,
            "quality": 0.96,
            "age_ms": 4,
            "source_time_s": 12.35,
        },
        session_id="session-a",
        epoch=4,
        media_time_s=12.4,
        timestamp_utc="2026-09-28T20:00:00+00:00",
    )
    assert result["participant"] == "C"
    assert result["session_id"] == "session-a"
    assert result["epoch"] == 4
    assert result["media_time_ms"] == 12400
    assert result["source_time_s"] == 12.35
    assert result["speaking"] is False
    assert "image" not in result and "transcript" not in result


def test_director_uses_only_fresh_same_session_reports_at_or_before_request() -> None:
    request = _request(
        _report("closeup1", revision=123),
        _report("closeup2", revision=126),
        _report("closeup3", revision=125, age_ms=1900),
        {
            **_report("closeup4", revision=125),
            "observation": {**_report("closeup4")["observation"], "epoch": 2},
        },
    )
    assert list(usable_camera_reports(request)) == ["closeup1"]


def test_rules_switch_only_when_candidate_is_the_sole_agent_reported_speaker() -> None:
    request = _request(_report("closeup1"))
    assert rule_decision(request) == {
        "action": "switch",
        "camera_id": "closeup1",
        "reason": "Camera AgentApp closeup1 reports the sole sustained active speaker.",
    }
    overlap = _request(_report("closeup1"), _report("closeup2"), candidate="closeup1")
    assert rule_decision(overlap)["action"] == "hold"
    missing = _request(candidate="closeup1")
    assert rule_decision(missing)["action"] == "hold"


def test_editorial_policy_parser_accepts_only_bounded_policy_json() -> None:
    policy = parse_editorial_policy(
        '{"min_shot_s":4,"overlap_mode":"hold","reason":"Keep the current shot."}'
    )
    assert policy == {
        "min_shot_s": 4.0,
        "overlap_mode": "hold",
        "reason": "Keep the current shot.",
    }
    assert parse_editorial_policy(
        '{"min_shot_s":8,"overlap_mode":"wide","reason":"A wide view is clearer."}'
    )["min_shot_s"] == 8.0


def test_editorial_policy_parser_rejects_malformed_or_out_of_range_results() -> None:
    invalid = (
        "",
        '{"min_shot_s":5,"overlap_mode":"hold",',  # truncated JSON
        '{"min_shot_s":5,"overlap_mode":"hold","reason":"x","camera_id":"closeup1"}',
        '{"min_shot_s":true,"overlap_mode":"hold","reason":"x"}',
        '{"min_shot_s":NaN,"overlap_mode":"hold","reason":"x"}',
        '{"min_shot_s":3.99,"overlap_mode":"hold","reason":"x"}',
        '{"min_shot_s":8.01,"overlap_mode":"hold","reason":"x"}',
        '{"min_shot_s":5,"overlap_mode":"switch","reason":"x"}',
        '{"min_shot_s":5,"overlap_mode":"wide","reason":"  "}',
        {"min_shot_s": 5, "overlap_mode": "hold", "reason": "x"},
    )
    assert all(parse_editorial_policy(value) is None for value in invalid)


def test_rules_proposal_payload_marks_fast_decision_source() -> None:
    request = _request(_report("closeup1"))
    body = _proposal_body(
        {"agent_id": "director"},
        request,
        rule_decision(request),
    )
    assert body["action"] == "switch"
    assert body["camera_id"] == "closeup1"
    assert body["decision_source"] == "rules"


def test_editorial_input_sanitizes_nonfinite_source_metrics() -> None:
    request = {
        "request_id": "req-1",
        "session_id": "session-a",
        "epoch": 3,
        "observation_revision": 125,
        "program_camera_id": "corner",
        "cameras": [{"id": "closeup1", "healthy": True, "quality": float("nan"), "age_ms": 5}],
        "camera_observations": [],
        "recent_events": [{"kind": "speech", "time_s": float("inf")}],
    }
    import json

    payload = _editorial_model_input(request)
    assert payload["cameras"][0]["quality"] is None
    assert payload["recent_events"][0] == {"kind": "speech"}
    json.dumps(payload, allow_nan=False)
