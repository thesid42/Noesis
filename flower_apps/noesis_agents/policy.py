"""Strict schemas and prompt input filtering for the Noesis SuperGrid roles."""

from __future__ import annotations

import math
from typing import Any


CAMERA_IDS = ("closeup1", "closeup2", "closeup3", "closeup4")
CAMERA_CHOICES = (*CAMERA_IDS, "corner", "slate")


def _reason(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 300:
        raise ValueError("reason must be a non-empty string of at most 300 characters")
    return value.strip()


def _exact_object(value: Any, keys: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError("model response has an invalid JSON schema")
    return value


def parse_camera_result(value: Any) -> dict[str, Any]:
    result = _exact_object(value, {"recommendation", "confidence", "reason"})
    recommendation = result["recommendation"]
    confidence = result["confidence"]
    if recommendation not in {"take", "hold", "avoid"}:
        raise ValueError("invalid camera recommendation")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not math.isfinite(float(confidence)) or not 0 <= confidence <= 1:
        raise ValueError("confidence must be a finite number from 0 through 1")
    return {"recommendation": recommendation, "confidence": float(confidence), "reason": _reason(result["reason"])}


def parse_critic_result(value: Any) -> dict[str, str]:
    result = _exact_object(value, {"assessment", "reason"})
    assessment = result["assessment"]
    if assessment not in {"steady", "change", "wide"}:
        raise ValueError("invalid critic assessment")
    return {"assessment": assessment, "reason": _reason(result["reason"])}


def parse_director_result(value: Any) -> dict[str, str]:
    result = _exact_object(value, {"action", "camera_id", "reason"})
    action, camera_id = result["action"], result["camera_id"]
    if action not in {"hold", "switch"} or camera_id not in CAMERA_CHOICES:
        raise ValueError("invalid director action")
    return {"action": action, "camera_id": camera_id, "reason": _reason(result["reason"])}


def _number(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        return None
    return value


def safe_round_input(round_data: dict[str, Any]) -> dict[str, Any]:
    """Whitelist current measured evidence; never pass arbitrary controller fields."""
    cameras = []
    for camera in round_data.get("cameras", []) if isinstance(round_data.get("cameras"), list) else []:
        if not isinstance(camera, dict) or camera.get("id") not in CAMERA_CHOICES:
            continue
        cameras.append({
            "id": camera.get("id"),
            "participant": camera.get("participant") if isinstance(camera.get("participant"), str) else None,
            "healthy": camera.get("healthy") is True,
            "status": str(camera.get("status", "unknown"))[:60],
            "speaker_state": str(camera.get("speaker_state", "unknown"))[:40],
            "speaking": camera.get("speaking") if isinstance(camera.get("speaking"), bool) else None,
            "energy": _number(camera.get("energy")),
            "quality": _number(camera.get("quality")),
            "age_ms": _number(camera.get("age_ms")),
            "source_time_s": _number(camera.get("source_time_s")),
        })
    program = round_data.get("program")
    if not isinstance(program, dict):
        program = {}
    result: dict[str, Any] = {
        "request_id": round_data.get("request_id"),
        "session_id": round_data.get("session_id"),
        "epoch": round_data.get("epoch"),
        "override_epoch": round_data.get("override_epoch"),
        "model_epoch": round_data.get("model_epoch"),
        "model": round_data.get("model") if isinstance(round_data.get("model"), str) else None,
        "profile": round_data.get("profile") if isinstance(round_data.get("profile"), str) else None,
        "observation_revision": round_data.get("observation_revision"),
        "media_time_s": _number(round_data.get("media_time_s")),
        "cameras": cameras,
        "program": {
            "camera_id": program.get("camera_id") if program.get("camera_id") in CAMERA_CHOICES else None,
            "reason": str(program.get("reason", ""))[:160],
        },
    }
    events = []
    for event in round_data.get("recent_events", []) if isinstance(round_data.get("recent_events"), list) else []:
        if not isinstance(event, dict):
            continue
        events.append({
            key: str(event[key])[:80]
            for key in ("kind", "source", "camera_id")
            if isinstance(event.get(key), str)
        } | ({"time_s": _number(event.get("time_s"))} if _number(event.get("time_s")) is not None else {}))
    result["recent_events"] = events[:12]
    previous = []
    for report in round_data.get("previous_reports", []) if isinstance(round_data.get("previous_reports"), list) else []:
        if not isinstance(report, dict) or report.get("role") not in {"camera", "critic"}:
            continue
        row: dict[str, Any] = {
            "role": report["role"],
            "agent_id": str(report.get("agent_id", ""))[:80],
            "response_id": str(report.get("response_id", ""))[:200],
        }
        if report["role"] == "camera":
            row["camera_id"] = report.get("camera_id") if report.get("camera_id") in CAMERA_IDS else None
            try:
                row["result"] = parse_camera_result(report.get("result"))
            except ValueError:
                continue
        else:
            try:
                row["result"] = parse_critic_result(report.get("result"))
            except ValueError:
                continue
        previous.append(row)
    result["previous_reports"] = previous[:8]
    return result
