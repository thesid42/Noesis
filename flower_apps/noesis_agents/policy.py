"""Small, testable payload and shot-selection rules for Noesis agents."""

from __future__ import annotations

from datetime import datetime, timezone
import math
from typing import Any
from urllib.parse import parse_qs, urlparse


CAMERA_IDS = ("closeup1", "closeup2", "closeup3", "closeup4")
MAX_OBSERVATION_AGE_MS = 1_800


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def observation_revision(snapshot: dict[str, Any]) -> int | None:
    """Read the revision attached to a causal controller camera snapshot."""
    value = snapshot.get("observation_revision")
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    for camera in snapshot.get("cameras", []) or []:
        if not isinstance(camera, dict):
            continue
        frame_url = camera.get("frame_url")
        if not isinstance(frame_url, str):
            continue
        raw = parse_qs(urlparse(frame_url).query).get("rev", [None])[0]
        try:
            parsed = int(raw)
        except (TypeError, ValueError):
            continue
        if parsed >= 0:
            return parsed
    return None


def make_camera_observation(
    camera: dict[str, Any],
    *,
    session_id: str,
    epoch: int,
    media_time_s: float,
    timestamp_utc: str | None = None,
) -> dict[str, Any]:
    """Create compact, timestamped evidence copied from one source snapshot."""
    observed_at = timestamp_utc or datetime.now(timezone.utc).isoformat()
    result: dict[str, Any] = {
        "session_id": session_id,
        "epoch": epoch,
        "observed_at_utc": observed_at,
        "media_time_ms": round(media_time_s * 1000),
        "healthy": camera.get("healthy") is True,
        "status": str(camera.get("status", "unknown"))[:80],
        "speaking": camera.get("speaking") if isinstance(camera.get("speaking"), bool) else None,
        "speaker_state": str(camera.get("speaker_state", "unknown"))[:40],
    }
    participant = camera.get("participant")
    if isinstance(participant, str) and participant:
        result["participant"] = participant[:80]
    source_time = _finite_number(camera.get("source_time_s"))
    energy = _finite_number(camera.get("energy"))
    quality = _finite_number(camera.get("quality"))
    age_ms = _finite_number(camera.get("age_ms"))
    if source_time is not None:
        result["source_time_s"] = source_time
    if energy is not None:
        result["energy"] = energy
    if quality is not None:
        result["quality"] = quality
    if age_ms is not None:
        result["source_age_ms"] = max(0, round(age_ms))
    return result


def usable_camera_reports(request: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Return only recent camera-agent reports causal to this director request."""
    request_revision = request.get("observation_revision")
    session_id = request.get("session_id")
    epoch = request.get("epoch")
    if not isinstance(request_revision, int) or isinstance(request_revision, bool):
        return {}
    reports: dict[str, dict[str, Any]] = {}
    raw_reports = request.get("camera_observations", [])
    for item in raw_reports if isinstance(raw_reports, list) else []:
        if not isinstance(item, dict):
            continue
        camera_id = item.get("camera_id")
        revision = item.get("source_observation_revision")
        age_ms = _finite_number(item.get("age_ms"))
        observation = item.get("observation")
        if camera_id not in CAMERA_IDS or not isinstance(observation, dict):
            continue
        if not isinstance(revision, int) or isinstance(revision, bool) or revision > request_revision:
            continue
        if age_ms is not None and age_ms > MAX_OBSERVATION_AGE_MS:
            continue
        if observation.get("session_id") != session_id or observation.get("epoch") != epoch:
            continue
        prior = reports.get(camera_id)
        if prior is None or revision > prior["source_observation_revision"]:
            reports[camera_id] = {**item, "observation": observation}
    return reports


def rule_decision(request: dict[str, Any]) -> dict[str, str]:
    """Choose a shot only when a fresh camera AgentApp confirms one speaker."""
    current = request.get("program_camera_id")
    candidate = request.get("candidate_camera_id")
    reports = usable_camera_reports(request)
    cameras = {
        item.get("id"): item
        for item in request.get("cameras", []) or []
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    active = []
    for camera_id, report in reports.items():
        observation = report["observation"]
        source = cameras.get(camera_id, {})
        if observation.get("healthy") is True and source.get("healthy") is True and observation.get("speaking") is True:
            active.append(camera_id)

    if len(active) == 1:
        target = active[0]
        if target == candidate and target != current:
            return {"action": "switch", "camera_id": target,
                    "reason": f"Camera AgentApp {target} reports the sole sustained active speaker."}
        if target == current:
            return {"action": "hold", "reason": "The current program camera still shows the sole active speaker."}
    if len(active) > 1:
        return {"action": "hold", "reason": "Camera agents report overlapping speakers; holding the current shot."}
    if candidate and candidate not in reports:
        return {"action": "hold", "reason": "No fresh camera-agent report confirms the proposed speaker; holding."}
    if candidate and candidate in reports:
        observation = reports[candidate]["observation"]
        if observation.get("speaker_state") == "overlap":
            return {"action": "hold", "reason": "Camera-agent evidence indicates overlap; holding the current shot."}
    return {"action": "hold", "reason": "Camera-agent evidence is ambiguous or quiet; holding the current shot."}


def validate_model_decision(
    decision: Any,
    *,
    request: dict[str, Any],
    allowed_camera_ids: set[str],
) -> dict[str, str] | None:
    """Accept only a compact decision whose target is independently allowed."""
    if not isinstance(decision, dict):
        return None
    action = decision.get("action")
    if action == "hold":
        reason = decision.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            return None
        return {"action": "hold", "reason": reason.strip()[:300]}
    camera_id = decision.get("camera_id")
    if action != "switch" or camera_id not in allowed_camera_ids:
        return None
    reports = usable_camera_reports(request)
    observation = reports.get(camera_id, {}).get("observation", {})
    if observation.get("healthy") is not True or observation.get("speaking") is not True:
        return None
    reason = decision.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        return None
    return {"action": "switch", "camera_id": camera_id, "reason": reason.strip()[:300]}
