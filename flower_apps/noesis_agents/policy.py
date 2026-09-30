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


def parse_camera_result(value: Any, *, has_image: bool = False) -> dict[str, Any]:
    required = {"recommendation", "confidence", "reason"}
    if not isinstance(value, dict) or set(value) not in (required, required | {"visual"}):
        raise ValueError("model response has an invalid JSON schema")
    result = value
    recommendation = result["recommendation"]
    confidence = result["confidence"]
    if recommendation not in {"take", "hold", "avoid"}:
        raise ValueError("invalid camera recommendation")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not math.isfinite(float(confidence)) or not 0 <= confidence <= 1:
        raise ValueError("confidence must be a finite number from 0 through 1")
    visual = result.get("visual")
    if has_image and visual is None:
        raise ValueError("image-backed camera response must include visual assessment")
    if visual is not None:
        visual = _exact_object(visual, {"person_visibility", "board_visibility", "activity", "summary"})
        if visual["person_visibility"] not in {"visible", "absent", "uncertain"}:
            raise ValueError("invalid person visibility")
        if visual["board_visibility"] not in {"visible", "not_visible", "uncertain"}:
            raise ValueError("invalid board visibility")
        if visual["activity"] not in {"seated", "standing", "presenting", "empty", "other", "uncertain"}:
            raise ValueError("invalid activity")
        if not isinstance(visual["summary"], str) or not visual["summary"].strip() or len(visual["summary"]) > 180:
            raise ValueError("visual summary must be 1 to 180 characters")
        visual = {
            "person_visibility": visual["person_visibility"],
            "board_visibility": visual["board_visibility"],
            "activity": visual["activity"],
            "summary": visual["summary"].strip(),
        }
    if not has_image:
        # Old no-image adapters may return the pre-vision three-field result.
        # Their visual claims are never treated as observations.
        visual = {"person_visibility": "uncertain", "board_visibility": "uncertain",
                  "activity": "uncertain", "summary": "unavailable"}
    return {"recommendation": recommendation, "confidence": float(confidence),
            "reason": _reason(result["reason"]), "visual": visual}


def _safe_image_provenance(value: Any, *, expected_camera_id: str | None = None) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    camera_id = value.get("camera_id")
    if camera_id not in CAMERA_CHOICES or (expected_camera_id is not None and camera_id != expected_camera_id):
        return None
    frame_time, source_time = _number(value.get("frame_time_s")), _number(value.get("source_time_s"))
    buffer_epoch = value.get("buffer_epoch")
    width, height = value.get("width"), value.get("height")
    sha256 = value.get("sha256")
    if (frame_time is None or frame_time < 0 or source_time is None or source_time < 0
            or isinstance(buffer_epoch, bool) or not isinstance(buffer_epoch, int) or buffer_epoch < 0
            or isinstance(width, bool) or not isinstance(width, int) or not 1 <= width <= 640
            or isinstance(height, bool) or not isinstance(height, int) or not 1 <= height <= 360
            or not isinstance(sha256, str) or len(sha256) != 64
            or any(char not in "0123456789abcdef" for char in sha256)):
        return None
    return {"camera_id": camera_id, "frame_time_s": float(frame_time),
            "source_time_s": float(source_time), "buffer_epoch": buffer_epoch,
            "sha256": sha256, "width": width, "height": height}


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


def _context_number(value: Any) -> int | float | None:
    number = _number(value)
    return round(number, 3) if isinstance(number, float) else number


def _safe_editorial_context(raw: Any, *, assigned_camera_id: str | None = None) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    result: dict[str, Any] = {}
    broadcast = raw.get("broadcast")
    if isinstance(broadcast, dict):
        result["broadcast"] = {
            key: _context_number(broadcast.get(key))
            for key in ("time_s", "delay_s") if _number(broadcast.get(key)) is not None
        } | ({"phase": str(broadcast["phase"])[:32]} if isinstance(broadcast.get("phase"), str) else {})
    history = []
    for item in raw.get("shot_history", []) if assigned_camera_id is None and isinstance(raw.get("shot_history"), list) else []:
        if not isinstance(item, dict) or item.get("camera_id") not in CAMERA_CHOICES:
            continue
        history.append({
            "camera_id": item["camera_id"],
            **{key: _context_number(item.get(key)) for key in ("start_s", "end_s", "duration_s") if _number(item.get(key)) is not None},
            **({"source": str(item["source"])[:40]} if isinstance(item.get("source"), str) else {}),
        })
    if history:
        result["shot_history"] = history[-8:]
    pending = []
    for item in raw.get("pending_cuts", []) if assigned_camera_id is None and isinstance(raw.get("pending_cuts"), list) else []:
        if not isinstance(item, dict) or item.get("camera_id") not in CAMERA_CHOICES:
            continue
        pending.append({"camera_id": item["camera_id"], **({"target_media_time_s": _context_number(item.get("target_media_time_s"))} if _number(item.get("target_media_time_s")) is not None else {})})
    if pending:
        # Historical scheduled cuts are context only, never current evidence.
        result["pending_cuts_history"] = pending[-4:]

    perception = raw.get("perception")
    if not isinstance(perception, dict):
        return result
    safe_perception: dict[str, Any] = {}
    status = perception.get("status")
    if isinstance(status, dict):
        safe_perception["status"] = {
            str(name)[:24]: {
                **({"state": str(item.get("state"))[:32]} if isinstance(item.get("state"), str) else {}),
                **({"latency_ms": _context_number(item.get("latency_ms"))} if _number(item.get("latency_ms")) is not None else {}),
            }
            for name, item in status.items() if name in {"speech", "visual"} and isinstance(item, dict)
        }
    transcript = perception.get("transcript") if assigned_camera_id is None else None
    if isinstance(transcript, dict):
        segments = []
        for item in transcript.get("segments", []) if isinstance(transcript.get("segments"), list) else []:
            if not isinstance(item, dict) or not isinstance(item.get("text"), str):
                continue
            segments.append({
                "text": item["text"][:280],
                **{key: _context_number(item.get(source)) for key, source in (("source_start_s", "source_start_s"), ("source_end_s", "source_end_s"), ("age_ms", "age_ms")) if _number(item.get(source)) is not None},
                "speaker_id": item.get("speaker_id")[:80] if isinstance(item.get("speaker_id"), str) else None,
            })
        if segments:
            safe_perception["transcript"] = {"segments": segments[-5:]}
    observations_by_camera: dict[str, tuple[float, int, dict[str, Any]]] = {}
    cutoff = _number(perception.get("source_end_s"))
    for index, item in enumerate(perception.get("visual_observations", []) if isinstance(perception.get("visual_observations"), list) else []):
        if not isinstance(item, dict) or item.get("camera_id") not in CAMERA_CHOICES:
            continue
        if assigned_camera_id is not None and item["camera_id"] != assigned_camera_id:
            continue
        source_time = _number(item.get("source_time_s"))
        if source_time is None or (cutoff is not None and source_time > cutoff + 1e-6):
            continue
        safe_item = {
            "camera_id": item["camera_id"],
            **{key: _context_number(item.get(key)) for key in ("source_time_s", "age_ms", "blur_score", "luma_mean") if _number(item.get(key)) is not None},
            **({"face_count": int(item["face_count"])} if isinstance(item.get("face_count"), int) and not isinstance(item.get("face_count"), bool) and item["face_count"] >= 0 else {}),
            **({"face_status": str(item["face_status"])[:48]} if isinstance(item.get("face_status"), str) else {}),
        }
        prior = observations_by_camera.get(item["camera_id"])
        if prior is None or (source_time, index) > (prior[0], prior[1]):
            observations_by_camera[item["camera_id"]] = (source_time, index, safe_item)
    observations = [item for _source_time, _index, item in observations_by_camera.values()]
    observations.sort(key=lambda item: (-item["source_time_s"], CAMERA_CHOICES.index(item["camera_id"])))
    if observations:
        safe_perception["visual_observations"] = observations[:1] if assigned_camera_id is not None else observations[:5]
    if cutoff is not None:
        safe_perception["source_end_s"] = _context_number(cutoff)
    if safe_perception:
        result["perception"] = safe_perception
    return result


def safe_round_input(
    round_data: dict[str, Any],
    *,
    assigned_camera_id: str | None = None,
    include_cameras: bool = True,
    include_program: bool = True,
    include_previous_reports: bool = True,
    previous_report_roles: set[str] | None = None,
) -> dict[str, Any]:
    """Whitelist current measured evidence; never pass arbitrary controller fields."""
    cameras = []
    for camera in round_data.get("cameras", []) if isinstance(round_data.get("cameras"), list) else []:
        if not isinstance(camera, dict) or camera.get("id") not in CAMERA_CHOICES:
            continue
        if assigned_camera_id is not None and camera.get("id") != assigned_camera_id:
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
    }
    if include_cameras:
        result["cameras"] = cameras
    if assigned_camera_id is None and include_program:
        # Current shot identity is useful continuity. The controller's reason
        # is historical narrative and must never steer a fresh model decision.
        result["program"] = {"camera_id": program.get("camera_id") if program.get("camera_id") in CAMERA_CHOICES else None}
    events = []
    for event in round_data.get("recent_events", []) if isinstance(round_data.get("recent_events"), list) else []:
        if not isinstance(event, dict):
            continue
        events.append({
            key: str(event[key])[:80]
            for key in ("kind", "source", "camera_id")
            if isinstance(event.get(key), str)
        } | ({"time_s": _number(event.get("time_s"))} if _number(event.get("time_s")) is not None else {}))
    if assigned_camera_id is None:
        result["recent_events"] = events[:12]
    previous = []
    allowed_roles = previous_report_roles or {"camera", "critic"}
    for report in round_data.get("previous_reports", []) if include_previous_reports and isinstance(round_data.get("previous_reports"), list) else []:
        if not isinstance(report, dict) or report.get("role") not in allowed_roles:
            continue
        row: dict[str, Any] = {
            "role": report["role"],
            "agent_id": str(report.get("agent_id", ""))[:80],
            "response_id": str(report.get("response_id", ""))[:200],
        }
        if _number(report.get("media_time_s")) is not None:
            row["media_time_s"] = _context_number(report["media_time_s"])
        if isinstance(report.get("source_revision"), int) and not isinstance(report.get("source_revision"), bool):
            row["source_revision"] = report["source_revision"]
        if report["role"] == "camera":
            row["camera_id"] = report.get("camera_id") if report.get("camera_id") in CAMERA_IDS else None
            image = _safe_image_provenance(report.get("image"), expected_camera_id=row["camera_id"])
            if image:
                row["image"] = image
            try:
                row["result"] = parse_camera_result(report.get("result"), has_image=image is not None)
            except ValueError:
                continue
        else:
            try:
                row["result"] = parse_critic_result(report.get("result"))
            except ValueError:
                continue
        previous.append(row)
    if assigned_camera_id is None and include_previous_reports:
        result["previous_reports"] = previous[:8]
    context = _safe_editorial_context(round_data.get("editorial_context"), assigned_camera_id=assigned_camera_id)
    if context:
        result["editorial_context"] = context
    return result
