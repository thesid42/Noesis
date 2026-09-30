"""Leased AI collaboration rounds. This module validates; it never ranks shots."""
from __future__ import annotations

import base64
import copy
import hashlib
import io
import math
import os
import time
import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError


AI_EVIDENCE_TTL_S = 15.0
AI_ROLE_MIN_INTERVAL_S = {"camera": 1.0, "critic": 2.0, "director": 1.0}
AI_DIRECTOR_MIN_INFERENCE_S = 3.0
AI_DIRECTOR_SCHEDULING_MARGIN_S = 0.5
AI_DIRECTOR_MIN_ADMISSION_S = AI_DIRECTOR_MIN_INFERENCE_S + AI_DIRECTOR_SCHEDULING_MARGIN_S


class CameraImageEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    camera_id: Literal["closeup1", "closeup2", "closeup3", "closeup4"]
    frame_time_s: float = Field(ge=0)
    source_time_s: float | None = Field(default=None, ge=0)
    buffer_epoch: int = Field(ge=0, strict=True)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    width: int = Field(gt=0, le=640, strict=True)
    height: int = Field(gt=0, le=360, strict=True)


class CameraVisualAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    person_visibility: Literal["visible", "absent", "uncertain"]
    board_visibility: Literal["visible", "not_visible", "uncertain"]
    activity: Literal["seated", "standing", "presenting", "empty", "other", "uncertain"]
    summary: str = Field(min_length=1, max_length=180)


class AIResultBody(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    request_id: str = Field(min_length=1, max_length=160)
    session_id: str = Field(min_length=1, max_length=160)
    epoch: int = Field(ge=0, strict=True)
    override_epoch: int = Field(ge=0, strict=True)
    model_epoch: int = Field(ge=0, strict=True)
    agent_id: str = Field(min_length=1, max_length=120)
    role: Literal["camera", "critic", "director"]
    camera_id: str | None = Field(default=None, max_length=64)
    source_revision: int = Field(ge=0, strict=True)
    media_time_s: float = Field(ge=0)
    model: str = Field(min_length=1, max_length=160)
    response_id: str = Field(min_length=1, max_length=200)
    latency_ms: float = Field(ge=0, le=30000)
    input_tokens: int = Field(ge=0, strict=True)
    output_tokens: int = Field(ge=0, strict=True)
    result: dict[str, Any]
    image: CameraImageEvidence | None = None
    evidence_response_ids: list[str] = Field(default_factory=list, max_length=5)


class AILeaseFailureBody(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    agent_id: str = Field(pattern=r"^(camera-closeup[1-4]|critic|director)$", max_length=120)
    request_id: str = Field(min_length=1, max_length=160)
    session_id: str = Field(min_length=1, max_length=160)
    epoch: int = Field(ge=0, strict=True)
    override_epoch: int = Field(ge=0, strict=True)
    model_epoch: int = Field(ge=0, strict=True)
    error_code: str = Field(pattern=r"^[a-z][a-z0-9_]{0,79}$", max_length=80)
    elapsed_ms: float | None = Field(default=None, ge=0, le=30000, strict=True)


def error(status: int, message: str):
    from .controller import ControllerError
    return ControllerError(status, message)


class AIControlMixin:
    def _init_ai(self, catalog: list[dict] | None, profile: str) -> None:
        if catalog is None:
            catalog = [{"id": "kimi", "label": "Kimi K2.7", "model": self.model_name or "dedicated/flowerai/Kimi-K2.7-Code-1OUHWL", "available": bool(self.model_name)},
                       {"id": "minimax", "label": "MiniMax M3", "model": "dedicated/flowerai/MiniMax-M3-OOLI9o", "available": False}]
        self._model_catalog = [{k: item[k] for k in ("id", "label", "model", "available")} for item in catalog if item.get("id") in {"kimi", "minimax"}]
        self.model_profile = profile if profile in {"kimi", "minimax"} else "kimi"
        selected = next((m for m in self._model_catalog if m["id"] == self.model_profile), None)
        self.model_name = selected["model"] if selected and selected["available"] else None
        # Camera agents may use a separately configured Flower model/key. Keep
        # this override scoped to the four camera roles; the director and critic
        # remain on the selected model profile.
        camera_model = os.environ.get("NOESIS_CAMERA_MODEL", "").strip()
        if (not camera_model or len(camera_model) > 160
                or any(not (char.isalnum() or char in "._:/-") for char in camera_model)):
            camera_model = None
        camera_effort = os.environ.get("NOESIS_CAMERA_REASONING_EFFORT", "none").strip().lower()
        self.camera_model = camera_model
        self.camera_reasoning_effort = camera_effort if camera_model and camera_effort in {"low", "none"} else None
        if not camera_model:
            self.camera_reasoning_effort = None
        self._model_epoch = 0
        self._ai_round: dict[str, Any] | None = None
        self._ai_reports: dict[str, dict] = {}
        self._ai_role_leases: dict[str, dict] = {}
        self._ai_lease_failures: dict[str, dict] = {}
        self._ai_director_wait_reason: str | None = None
        self._ai_latest_reports: dict[str, dict] = {}
        self._ai_last_lease_mono: dict[str, float] = {}
        self._ai_last_director_evidence_ids: set[str] = set()
        self._inference_results: dict[str, dict] = {}
        self._ai_next_mono = 0.0
        self._grid_run: dict[str, Any] = {}
        self._metrics.setdefault("ai_role_failures", 0)
        self._metrics.setdefault("ai_role_timeouts", 0)

    def _models_snapshot(self) -> dict:
        camera_model = self.camera_model or self.model_name
        camera_profile = "flower_camera" if self.camera_model else self.model_profile
        camera_provider = "flower" if self.camera_model else "nebius"
        def verification_status(agent_ids: tuple[str, ...], expected_model: str | None) -> str:
            if not expected_model:
                return "not_configured"
            for agent_id in agent_ids:
                report = self._inference_results.get(agent_id)
                if (not isinstance(report, dict)
                        or report.get("status") != "completed"
                        or report.get("model") != expected_model
                        or report.get("model_epoch") != self._model_epoch
                        or report.get("session_id") != self._session.get("id")
                        or report.get("epoch") != self._session.get("epoch")
                        or report.get("override_epoch") != self._override_epoch
                        or not isinstance(report.get("response_id"), str)
                        or not report.get("response_id")):
                    return "configured_not_verified"
            return "verified"

        camera_ids = tuple(f"camera-{camera_id}" for camera_id in ("closeup1", "closeup2", "closeup3", "closeup4"))
        return {
            "selected": self.model_profile,
            "epoch": self._model_epoch,
            "options": copy.deepcopy(self._model_catalog),
            "roles": {
                "camera": {"model": camera_model, "profile": camera_profile,
                           "provider": camera_provider, "reasoning_effort": self.camera_reasoning_effort,
                           "verification_status": verification_status(camera_ids, camera_model)},
                "critic": {"model": self.model_name, "profile": self.model_profile, "provider": "nebius",
                           "verification_status": verification_status(("critic",), self.model_name)},
                "director": {"model": self.model_name, "profile": self.model_profile, "provider": "nebius",
                             "verification_status": verification_status(("director",), self.model_name)},
            },
        }

    def _invalidate_ai(self) -> None:
        self._ai_round = None
        self._ai_reports.clear()
        self._ai_role_leases.clear()
        self._ai_lease_failures.clear()
        self._ai_director_wait_reason = None
        self._ai_latest_reports.clear()
        self._ai_last_lease_mono.clear()
        self._ai_last_director_evidence_ids.clear()
        self._ai_next_mono = 0.0
        clear_scheduled = getattr(self, "_clear_scheduled_cuts_locked", None)
        if callable(clear_scheduled):
            clear_scheduled()

    def _inference_failures_snapshot(self) -> list[dict]:
        """Return only safe, current per-agent failure summaries."""
        failures = []
        for failure in self._ai_lease_failures.values():
            failures.append({key: copy.deepcopy(value) for key, value in failure.items()
                             if key != "_occurred_mono"})
        failures.sort(key=lambda item: (item.get("role", ""), item.get("agent_id", "")))
        return failures

    @staticmethod
    def _failure_identity_matches(left: dict, right: dict) -> bool:
        return all(left.get(key) == right.get(key) for key in (
            "agent_id", "request_id", "session_id", "epoch", "override_epoch", "model_epoch",
        ))

    def _record_lease_failure_locked(self, lease: dict, error_code: str, elapsed_ms: float | None = None) -> bool:
        """Record a terminal failure only while this exact request owns the role slot."""
        agent_id = lease.get("agent_id")
        current = self._ai_role_leases.get(agent_id)
        if (not isinstance(agent_id, str) or not isinstance(current, dict)
                or not self._failure_identity_matches(current, lease)):
            return False
        completed = self._inference_results.get(agent_id)
        if completed and completed.get("request_id") == lease.get("request_id"):
            return False
        self._ai_role_leases.pop(agent_id, None)
        role = str(lease.get("role", ""))
        summary = {
            "agent_id": agent_id,
            "role": role,
            "request_id": lease["request_id"],
            "model": lease.get("model"),
            "session_id": lease["session_id"],
            "epoch": lease["epoch"],
            "override_epoch": lease["override_epoch"],
            "model_epoch": lease["model_epoch"],
            "media_time_s": lease.get("target_media_time_s", lease.get("media_time_s")),
            "error_code": error_code,
            "elapsed_ms": elapsed_ms,
            "_occurred_mono": time.monotonic(),
        }
        if lease.get("camera_id"):
            summary["camera_id"] = lease["camera_id"]
        self._ai_lease_failures[agent_id] = summary
        self._metrics["ai_role_failures"] = self._metrics.get("ai_role_failures", 0) + 1
        if "timeout" in error_code or "deadline" in error_code or "expired" in error_code:
            self._metrics["ai_role_timeouts"] = self._metrics.get("ai_role_timeouts", 0) + 1
        self._add_event(
            "ai_role_lease_failed", "flower",
            f"{role} AI task failed ({error_code}); the last accepted inference is retained.",
            lease.get("camera_id"),
        )
        return True

    async def fail_ai_lease(self, raw: dict) -> dict:
        """Release a failed active lease without replacing accepted inference."""
        try:
            body = AILeaseFailureBody.model_validate(raw).model_dump(exclude_none=True)
        except ValidationError:
            raise error(422, "Malformed AI lease failure metadata.") from None

        async with self._lock:
            agent_id = body["agent_id"]
            lease = self._ai_role_leases.get(agent_id)
            previous_failure = self._ai_lease_failures.get(agent_id)
            if previous_failure and self._failure_identity_matches(previous_failure, body):
                return {"ok": True, "released": False, "duplicate": True}

            completed = self._inference_results.get(agent_id)
            if completed and completed.get("request_id") == body["request_id"]:
                raise error(409, "This AI lease already completed successfully.")
            if lease is None or not self._failure_identity_matches(lease, body):
                raise error(409, "AI lease failure does not match the active request.")
            if (self._session_lock.locked() or self._session.get("status") != "running"
                    or self._manual_latched
                    or lease.get("session_id") != self._session.get("id")
                    or lease.get("epoch") != self._session.get("epoch")
                    or lease.get("override_epoch") != self._override_epoch
                    or lease.get("model_epoch") != self._model_epoch):
                raise error(409, "AI lease is no longer active.")
            self._record_lease_failure_locked(lease, body["error_code"], body.get("elapsed_ms"))
            return {"ok": True, "released": True, "duplicate": False}

    def _director_output_remaining_s_locked(self, target_time_s: float) -> float | None:
        active = getattr(self, "_broadcast_active_locked", None)
        if not callable(active) or not active():
            return None
        ready = bool(self._broadcast.get("ready"))
        if ready:
            current_output_time = self._broadcast_time_locked()
            return max(0.0, float(target_time_s) - current_output_time)
        delay = max(0.0, float(self._broadcast.get("delay_s", 0.0)))
        source_time = float(self._session.get("time_s", target_time_s))
        return max(0.0, delay + float(target_time_s) - source_time)

    @staticmethod
    def _lease_public(lease: dict) -> dict:
        return copy.deepcopy({key: value for key, value in lease.items() if not key.startswith("_") and key != "deadline_mono"})

    def _camera_image_locked(self, camera_id: str, target_time_s: float) -> dict | None:
        """Pin one already-captured image, never a later live frame or file seek."""
        getter = getattr(self.media, "agent_image_at", None)
        if not callable(getter):
            return None
        broadcast = self._latest_media.get("broadcast", {})
        expected_epoch = broadcast.get("buffer_epoch") if isinstance(broadcast, dict) else None
        try:
            captured = getter(camera_id, target_time_s, expected_buffer_epoch=expected_epoch)
            if not isinstance(captured, dict) or captured.get("camera_id") != camera_id:
                return None
            if captured.get("source_time_s") is None:
                return None
            jpeg = captured.get("jpeg")
            if not isinstance(jpeg, bytes) or not 1 <= len(jpeg) <= 128 * 1024:
                return None
            from PIL import Image
            with Image.open(io.BytesIO(jpeg)) as picture:
                if picture.format != "JPEG":
                    return None
                width, height = picture.size
            evidence = CameraImageEvidence(
                camera_id=camera_id, frame_time_s=captured["frame_time_s"],
                source_time_s=captured.get("source_time_s"), buffer_epoch=captured["buffer_epoch"],
                sha256=hashlib.sha256(jpeg).hexdigest(), width=width, height=height,
            ).model_dump(exclude_none=True)
            if (not -1e-6 <= target_time_s - evidence["frame_time_s"] <= 2.0
                    or evidence.get("source_time_s", 0) > target_time_s + 1e-6
                    or (expected_epoch is not None and evidence["buffer_epoch"] != expected_epoch)):
                return None
            return {**evidence, "image_url": "data:image/jpeg;base64," + base64.b64encode(jpeg).decode("ascii")}
        except (KeyError, ValueError, OSError, TypeError):
            return None

    def _lease_snapshot_locked(self, role: str, agent_id: str, camera_id: str | None, now: float) -> dict | None:
        target_time = float(self._session["time_s"])
        cameras = [self._compact_camera(c) for c in self._camera_map(self._latest_media).values()]
        if role == "camera" and not any(item.get("id") == camera_id for item in cameras):
            return None
        # Lease TTL is based on when this observation revision was first acquired,
        # not the camera-frame PTS age. A fresh frozen frame must still reach the
        # model so it can report `avoid`; an unchanged stale source must expire.
        revision = self._revision(self._latest_media)
        revision_record = self._revision_history.get(revision)
        revision_age_ms = max(0.0, (now - revision_record[0]) * 1000.0) if revision_record else 0.0
        media_age = self._latest_media.get("observation_age_ms") if isinstance(self._latest_media, dict) else None
        media_age_ms = (float(media_age) if isinstance(media_age, (int, float))
                        and not isinstance(media_age, bool) and math.isfinite(float(media_age)) else 0.0)
        source_age_ms = max(revision_age_ms, media_age_ms)
        if source_age_ms < 0 or source_age_ms >= AI_EVIDENCE_TTL_S * 1000:
            return None
        context_builder = getattr(self, "_editorial_context_locked", None)
        editorial = context_builder(target_time_s=target_time) if callable(context_builder) else {}
        if not isinstance(editorial, dict):
            editorial = {}
        broadcast = editorial.get("broadcast")
        if isinstance(broadcast, dict) and broadcast.get("phase") in {"draining", "ended"}:
            return None
        projected_program = self._program
        if role == "director":
            project_program = getattr(self, "_ai_program_at_locked", None)
            if callable(project_program):
                projected_program = project_program(target_time)
        if not isinstance(projected_program, dict):
            projected_program = self._program
        previous_reports = []
        if role == "critic":
            for report in self._ai_latest_reports.values():
                if report.get("_captured_mono", 0) + AI_EVIDENCE_TTL_S <= now:
                    continue
                previous_reports.append({key: copy.deepcopy(value) for key, value in report.items() if not key.startswith("_")})
        model = self.camera_model if role == "camera" and self.camera_model else self.model_name
        profile = "flower_camera" if role == "camera" and self.camera_model else self.model_profile
        lease = {
            "request_id": uuid.uuid4().hex,
            "session_id": self._session["id"],
            "epoch": self._session["epoch"],
            "override_epoch": self._override_epoch,
            "model_epoch": self._model_epoch,
            "model": model,
            "profile": profile,
            "role": role,
            "agent_id": agent_id,
            **({"camera_id": camera_id} if camera_id else {}),
            "observation_revision": revision,
            "media_time_s": target_time,
            "target_media_time_s": target_time,
            "deadline_remaining_ms": max(0, int((AI_EVIDENCE_TTL_S - source_age_ms / 1000.0) * 1000)),
            "cameras": cameras,
            "program": copy.deepcopy(projected_program),
            "recent_events": list(reversed(copy.deepcopy(list(self._events)[-8:]))),
            "previous_reports": previous_reports[-8:],
            "editorial_context": copy.deepcopy(editorial),
            "_issued_mono": now,
            "_captured_mono": now - source_age_ms / 1000.0,
            "_expires_mono": now + AI_EVIDENCE_TTL_S - source_age_ms / 1000.0,
        }
        if role == "camera" and self.camera_model and self.camera_reasoning_effort:
            lease["reasoning_effort"] = self.camera_reasoning_effort
        if role == "camera":
            lease["camera_image"] = self._camera_image_locked(camera_id, target_time)
        return lease

    def _ai_cameras_at_target_locked(self, target_time_s: float) -> dict[str, dict]:
        getter = getattr(self, "_ai_target_cameras_locked", None)
        cameras = getter(target_time_s) if callable(getter) else None
        if isinstance(cameras, dict):
            return cameras
        if isinstance(cameras, list):
            return {item["id"]: item for item in cameras if isinstance(item, dict) and isinstance(item.get("id"), str)}
        return self._camera_map(self._latest_media)

    async def ai_lease(self, agent_id: str) -> dict | None:
        """Issue/re-serve one immutable model task for a continuously running role."""
        async with self._lock:
            if (self._session_lock.locked() or self._session["status"] != "running"
                    or self._manual_latched or not self.model_name):
                return None
            agent = self._agents.get(agent_id)
            if not agent or agent.get("decision_mode") != "llm" or time.monotonic() - self._agent_seen_mono.get(agent_id, 0) > self.heartbeat_ttl_s:
                return None
            role = agent.get("role")
            camera_id = agent.get("camera_id") if role == "camera" else None
            if role not in {"camera", "critic", "director"}:
                return None
            expected_id = f"camera-{camera_id}" if role == "camera" else role
            if agent_id != expected_id or (role == "camera" and camera_id not in {"closeup1", "closeup2", "closeup3", "closeup4"}):
                return None
            now = time.monotonic()
            current_lease = self._ai_role_leases.get(agent_id)
            if current_lease is not None:
                if current_lease["_expires_mono"] > now:
                    public = self._lease_public(current_lease)
                    evidence_remaining_ms = max(0, int((current_lease["_expires_mono"] - now) * 1000))
                    public["deadline_remaining_ms"] = evidence_remaining_ms
                    if role == "director" and current_lease.get("_output_deadline_mono") is not None:
                        output_remaining_ms = max(0, int((current_lease["_output_deadline_mono"] - now) * 1000))
                        public["output_deadline_remaining_ms"] = output_remaining_ms
                        public["deadline_remaining_ms"] = min(evidence_remaining_ms, output_remaining_ms)
                    return public
                self._record_lease_failure_locked(current_lease, "lease_expired")
                self._ai_role_leases.pop(agent_id, None)
            if now - self._ai_last_lease_mono.get(agent_id, 0.0) < AI_ROLE_MIN_INTERVAL_S[role]:
                return None

            if role == "director":
                expected = {f"camera-{camera}" for camera in ("closeup1", "closeup2", "closeup3", "closeup4")} | {"critic"}
                reports = {key: self._ai_latest_reports[key] for key in expected if key in self._ai_latest_reports}
                if set(reports) != expected:
                    self._ai_director_wait_reason = "evidence_budget"
                    return None
                if any(report.get("_captured_mono", 0) + AI_EVIDENCE_TTL_S <= now for report in reports.values()):
                    self._ai_director_wait_reason = "evidence_budget"
                    return None
                evidence_ids = [reports[f"camera-{camera}"]["response_id"] for camera in ("closeup1", "closeup2", "closeup3", "closeup4")]
                evidence_ids.append(reports["critic"]["response_id"])
                if len(set(evidence_ids)) != 5 or set(evidence_ids) == self._ai_last_director_evidence_ids:
                    return None
                lease = self._lease_snapshot_locked(role, agent_id, None, now)
                if lease is None:
                    return None
                pinned = [copy.deepcopy(reports[f"camera-{camera}"]) for camera in ("closeup1", "closeup2", "closeup3", "closeup4")] + [copy.deepcopy(reports["critic"])]
                lease["camera_reports"] = [{key: value for key, value in report.items() if not key.startswith("_")} for report in pinned[:4]]
                lease["critic_report"] = {key: value for key, value in pinned[4].items() if not key.startswith("_")}
                lease["evidence_response_ids"] = evidence_ids
                lease["_pinned_reports"] = pinned
                lease["_expires_mono"] = min(
                    lease["_expires_mono"],
                    *(report["_captured_mono"] + AI_EVIDENCE_TTL_S for report in pinned),
                )
                evidence_remaining_ms = max(0, int((lease["_expires_mono"] - now) * 1000))
                output_remaining_s = self._director_output_remaining_s_locked(lease["target_media_time_s"])
                if output_remaining_s is not None:
                    output_remaining_ms = max(0, int(output_remaining_s * 1000))
                    lease["output_deadline_remaining_ms"] = output_remaining_ms
                    lease["_output_deadline_mono"] = now + output_remaining_s
                    lease["deadline_remaining_ms"] = min(evidence_remaining_ms, output_remaining_ms)
                    if output_remaining_ms < int(AI_DIRECTOR_MIN_ADMISSION_S * 1000):
                        self._ai_director_wait_reason = "output_budget"
                        return None
                else:
                    lease["deadline_remaining_ms"] = evidence_remaining_ms
                if evidence_remaining_ms < int(AI_DIRECTOR_MIN_ADMISSION_S * 1000):
                    self._ai_director_wait_reason = "evidence_budget"
                    return None
                # A failed/expired director call cannot spin on identical evidence;
                # a changed accepted report set is required for another lease.
                self._ai_last_director_evidence_ids = set(evidence_ids)
                self._ai_director_wait_reason = None
            else:
                lease = self._lease_snapshot_locked(role, agent_id, camera_id, now)
                if lease is None:
                    return None
                if role == "camera":
                    lease["camera"] = next(item for item in lease["cameras"] if item.get("id") == camera_id)
            self._ai_last_lease_mono[agent_id] = now
            self._ai_role_leases[agent_id] = lease
            return self._lease_public(lease)

    async def select_model(self, profile: str) -> dict:
        async with self._lock:
            selected = next((m for m in self._model_catalog if m["id"] == profile and m["available"]), None)
            if selected is None:
                raise error(409, "This model profile is not configured on the local inference gateway.")
            if profile != self.model_profile:
                self.model_profile = profile
                self.model_name = selected["model"]
                self._model_epoch += 1
                self._invalidate_ai()
                self._inference_results.clear()
                self._last_model_result = None
                self._add_event("model_changed", "operator", f"Selected {selected['label']}; outstanding AI work invalidated, playback continues.")
            return self._state_locked()

    async def ai_round(self) -> dict | None:
        async with self._lock:
            if (self._session_lock.locked() or self._session["status"] != "running"
                    or self._manual_latched or not self.model_name):
                return None
            now = time.monotonic()
            if self._ai_round and now >= self._ai_round["deadline_mono"]:
                self._metrics["model_timeouts"] += 1
                self._add_event("ai_round_expired", "validator", "AI round expired; retaining the current healthy shot.")
                self._invalidate_ai()
            if self._ai_round is None:
                if now < self._ai_next_mono:
                    return None
                self._ai_reports.clear()
                self._ai_round = {
                    "request_id": uuid.uuid4().hex, "session_id": self._session["id"],
                    "epoch": self._session["epoch"], "override_epoch": self._override_epoch,
                    "model_epoch": self._model_epoch, "model": self.model_name, "profile": self.model_profile,
                    "observation_revision": self._revision(self._latest_media),
                    "media_time_s": self._session["time_s"], "deadline_mono": now + min(30.0, self.request_timeout_s),
                    "cameras": [self._compact_camera(c) for c in self._camera_map(self._latest_media).values()],
                    "program": copy.deepcopy(self._program),
                    "recent_events": list(reversed(copy.deepcopy(list(self._events)[-8:]))),
                    "previous_reports": copy.deepcopy([
                        report for report in self._inference_results.values()
                        if all(report.get(key) == value for key, value in (
                            ("session_id", self._session["id"]), ("epoch", self._session["epoch"]),
                            ("override_epoch", self._override_epoch), ("model_epoch", self._model_epoch)))
                        and report.get("media_time_s", math.inf) <= self._session["time_s"]
                    ]),
                }
                if self.camera_model:
                    self._ai_round["role_models"] = {
                        "camera": {"model": self.camera_model,
                                   **({"reasoning_effort": self.camera_reasoning_effort} if self.camera_reasoning_effort else {})}
                    }
                self._ai_round["camera_images"] = {
                    camera: self._camera_image_locked(camera, float(self._session["time_s"]))
                    for camera in ("closeup1", "closeup2", "closeup3", "closeup4")
                }
                self._metrics["ai_rounds"] += 1
                self._add_event("ai_round_started", "flower", "Requested four AI camera recommendations, a critic assessment, and an AI director decision.")
            result = {k: copy.deepcopy(v) for k, v in self._ai_round.items() if k != "deadline_mono"}
            result["deadline_remaining_ms"] = max(0, int((self._ai_round["deadline_mono"] - now) * 1000))
            return result

    def _validate_ai_body(self, raw: dict, *, director: bool) -> dict:
        try:
            body = AIResultBody.model_validate(raw).model_dump(exclude_none=True)
        except ValidationError:
            raise error(422, "Malformed AI result or provenance metadata.") from None
        now = time.monotonic()
        role = body["role"]
        if director != (role == "director"):
            raise error(422, "AI result uses the wrong role endpoint.")
        expected_id = f"camera-{body.get('camera_id')}" if role == "camera" else role
        if body["agent_id"] != expected_id or (role == "camera" and body.get("camera_id") not in {"closeup1", "closeup2", "closeup3", "closeup4"}):
            raise error(422, "Unknown AI role or camera identity.")
        lease = self._ai_role_leases.get(body["agent_id"])
        if (self._session_lock.locked() or self._session["status"] != "running" or self._manual_latched):
            raise error(409, "AI work is inactive or blocked by manual control.")
        if lease is not None:
            expected_model = lease["model"]
            for key, value in (("request_id", lease["request_id"]), ("session_id", self._session["id"]),
                               ("epoch", self._session["epoch"]), ("override_epoch", self._override_epoch),
                               ("model_epoch", self._model_epoch), ("model", expected_model)):
                if body[key] != value or body[key] != lease[key]:
                    raise error(409, f"AI result has stale or mismatched {key}.")
            if now >= lease["_expires_mono"] or now >= lease["_captured_mono"] + AI_EVIDENCE_TTL_S:
                self._record_lease_failure_locked(lease, "lease_expired", body["latency_ms"])
                raise error(409, "AI role lease expired before its result arrived.")
            if (body["source_revision"] != lease["observation_revision"]
                    or body["source_revision"] > self._revision(self._latest_media)
                    or not math.isclose(body["media_time_s"], lease["media_time_s"], abs_tol=0.05)):
                raise error(409, "AI result does not match the leased source snapshot.")
        else:
            pending = self._ai_round
            if pending is None or now >= pending["deadline_mono"]:
                raise error(409, "AI round is inactive or expired.")
            role_model = pending.get("role_models", {}).get(role, {}) if isinstance(pending.get("role_models"), dict) else {}
            expected_model = role_model.get("model", self.model_name) if isinstance(role_model, dict) else self.model_name
            for key, value in (("request_id", pending["request_id"]), ("session_id", self._session["id"]),
                               ("epoch", self._session["epoch"]), ("override_epoch", self._override_epoch),
                               ("model_epoch", self._model_epoch), ("model", expected_model)):
                if body[key] != value or (key != "model" and body[key] != pending[key]):
                    raise error(409, f"AI result has stale or mismatched {key}.")
            if (body["source_revision"] < pending["observation_revision"]
                    or body["source_revision"] > self._revision(self._latest_media)
                    or body["media_time_s"] < pending["media_time_s"] - 0.1
                    or body["media_time_s"] > self._session["time_s"] + 0.25):
                raise error(409, "AI evidence source time or revision is invalid.")
        agent = self._agents.get(expected_id)
        if (not agent or agent.get("decision_mode") != "llm" or agent.get("role") != role
                or now - self._agent_seen_mono.get(expected_id, 0) > self.heartbeat_ttl_s):
            raise error(409, "AI sender does not have a current Flower AgentApp heartbeat.")
        if body["response_id"] in self._seen_decision_set:
            raise error(409, "This model response was already accepted.")
        result = body["result"]
        reason = result.get("reason")
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 400:
            raise error(422, "AI result requires a bounded reason.")
        if role == "camera":
            confidence = result.get("confidence")
            if (set(result) not in ({"recommendation", "confidence", "reason"}, {"recommendation", "confidence", "reason", "visual"})
                    or result["recommendation"] not in {"take", "hold", "avoid"}
                    or isinstance(confidence, bool) or not isinstance(confidence, (int, float))
                    or not math.isfinite(confidence) or not 0 <= confidence <= 1):
                raise error(422, "Malformed AI camera recommendation.")
            expected_image = (lease.get("camera_image") if lease is not None else
                              (pending.get("camera_images") or {}).get(body["camera_id"]))
            expected_image = {k: v for k, v in expected_image.items() if k != "image_url" and v is not None} if isinstance(expected_image, dict) else None
            if body.get("image") != expected_image:
                raise error(409, "Camera image provenance does not match the leased frame.")
            visual = result.get("visual")
            if visual is not None:
                try:
                    CameraVisualAssessment.model_validate(visual)
                except ValidationError:
                    raise error(422, "Malformed AI visual assessment.") from None
                if expected_image is None and any(visual[k] != "uncertain" for k in ("person_visibility", "board_visibility", "activity")):
                    raise error(422, "Visual claims require a leased camera image.")
            elif expected_image is not None:
                raise error(422, "Camera image requires a visual assessment.")
        elif role == "critic":
            if body.get("image") is not None:
                raise error(422, "Only camera reports may contain image provenance.")
            if set(result) != {"assessment", "reason"} or result["assessment"] not in {"steady", "change", "wide"}:
                raise error(422, "Malformed AI critic assessment.")
        else:
            if body.get("image") is not None:
                raise error(422, "Only camera reports may contain image provenance.")
            if (set(result) != {"action", "camera_id", "reason"} or result["action"] not in {"hold", "switch"}
                    or result["camera_id"] not in {"closeup1", "closeup2", "closeup3", "closeup4", "corner", "slate"}):
                raise error(422, "Malformed AI director choice.")
        return body

    def _record_inference(self, body: dict) -> None:
        result = {k: copy.deepcopy(v) for k, v in body.items() if k not in {"evidence_response_ids"}}
        result["status"] = "completed"
        self._inference_results[body["agent_id"]] = result
        self._ai_lease_failures.pop(body["agent_id"], None)
        self._remember_decision(body["response_id"])
        self._last_model_result = {k: body[k] for k in ("model", "response_id", "latency_ms", "input_tokens", "output_tokens")}
        self._metrics["ai_responses"] += 1

    async def accept_ai_report(self, raw: dict) -> dict:
        async with self._lock:
            body = self._validate_ai_body(raw, director=False)
            lease = self._ai_role_leases.get(body["agent_id"])
            continuous = lease is not None and lease.get("request_id") == body["request_id"]
            if continuous:
                if body["agent_id"] == "director":
                    raise error(422, "Director results must use the decision endpoint.")
                accepted = copy.deepcopy(body)
                accepted["_captured_mono"] = lease["_captured_mono"]
                accepted["_capture_revision"] = lease["observation_revision"]
                self._ai_latest_reports[body["agent_id"]] = accepted
                self._ai_role_leases.pop(body["agent_id"], None)
            else:
                if body["agent_id"] in self._ai_reports:
                    raise error(409, "This AI role already reported for the round.")
                self._ai_reports[body["agent_id"]] = body
            self._record_inference(body)
            self._add_event("ai_camera_report" if body["role"] == "camera" else "ai_critic_report", "model", body["result"]["reason"], body.get("camera_id"))
            return {"ok": True, "response_id": body["response_id"]}

    async def accept_ai_decision(self, raw: dict) -> dict:
        async with self._action_lock:
            async with self._lock:
                body = self._validate_ai_body(raw, director=True)
                continuous_lease = self._ai_role_leases.get("director")
                if continuous_lease is not None and continuous_lease.get("request_id") == body["request_id"]:
                    expected_responses = continuous_lease.get("evidence_response_ids", [])
                    expired = (
                        time.monotonic() >= continuous_lease["_captured_mono"] + AI_EVIDENCE_TTL_S
                        or any(report.get("_captured_mono", 0) + AI_EVIDENCE_TTL_S <= time.monotonic()
                               for report in continuous_lease.get("_pinned_reports", []))
                    )
                    if expired:
                        self._record_lease_failure_locked(continuous_lease, "lease_expired", body["latency_ms"])
                        raise error(409, "Director evidence lease expired or does not match its pinned reports.")
                    if len(body["evidence_response_ids"]) != 5 or body["evidence_response_ids"] != expected_responses:
                        # The sender may have made a correctable envelope mistake.
                        # Keep the exact lease until a corrected response or an
                        # explicit failure receipt arrives.
                        raise error(409, "Director evidence does not match its pinned reports.")
                    result = body["result"]
                    target = result["camera_id"]
                    projected = continuous_lease.get("program")
                    projected_camera_id = projected.get("camera_id") if isinstance(projected, dict) else None
                    if result["action"] == "hold" and target != projected_camera_id:
                        self._record_lease_failure_locked(continuous_lease, "director_hold_target_changed", body["latency_ms"])
                        raise error(409, "Hold decision does not match the projected shot at its target time.")
                    target_cameras = self._ai_cameras_at_target_locked(body["media_time_s"])
                    if target != "slate" and not self._is_healthy(target_cameras.get(target), running=True):
                        self._record_lease_failure_locked(continuous_lease, "director_target_unavailable", body["latency_ms"])
                        raise error(409, "The AI-selected source is currently unavailable.")
                    if target == "slate" and any(self._is_healthy(camera, running=True) for camera in target_cameras.values()):
                        self._record_lease_failure_locked(continuous_lease, "director_slate_rejected", body["latency_ms"])
                        raise error(409, "Slate is reserved for complete source failure; a healthy camera is available.")
                    apply_decision = getattr(self, "_apply_ai_director_locked", None)
                    if callable(apply_decision):
                        # Let the timeline hook reject an expired target before the
                        # response is recorded as verified or counted as a decision.
                        try:
                            outcome = apply_decision(body)
                        except Exception as exc:
                            if isinstance(getattr(exc, "status_code", None), int) and 400 <= exc.status_code < 500:
                                detail = str(getattr(exc, "detail", ""))
                                failure_code = "director_target_expired" if "already aired" in detail.lower() else "director_decision_rejected"
                                self._record_lease_failure_locked(continuous_lease, failure_code, body["latency_ms"])
                            raise
                    else:
                        if result["action"] == "switch" and target != self._program["camera_id"]:
                            self._set_program_locked(target, result["reason"], source="ai_director", decision_source="model")
                            self._program["response_id"] = body["response_id"]
                        outcome = {"ok": True, "executed": True, "scheduled": False,
                                   "action": result["action"], "program": copy.deepcopy(self._program),
                                   "target_media_time_s": body["media_time_s"]}
                    self._record_inference(body)
                    self._metrics["ai_decisions"] += 1
                    self._ai_last_director_evidence_ids = set(expected_responses)
                    self._ai_role_leases.pop("director", None)
                    return outcome

                expected_roles = {"camera-closeup1", "camera-closeup2", "camera-closeup3", "camera-closeup4", "critic"}
                expected_responses = {report["response_id"] for report in self._ai_reports.values()}
                if (set(self._ai_reports) != expected_roles or len(body["evidence_response_ids"]) != 5
                        or set(body["evidence_response_ids"]) != expected_responses):
                    raise error(409, "Director must reference this round's four AI camera reports and critic assessment.")
                result = body["result"]
                target = result["camera_id"]
                if result["action"] == "hold" and target != self._program["camera_id"]:
                    raise error(409, "Hold decision no longer matches the current camera.")
                target_cameras = self._ai_cameras_at_target_locked(body["media_time_s"])
                if target != "slate" and not self._is_healthy(target_cameras.get(target), running=True):
                    raise error(409, "The AI-selected source is currently unavailable.")
                if target == "slate" and any(self._is_healthy(camera, running=True) for camera in target_cameras.values()):
                    raise error(409, "Slate is reserved for complete source failure; a healthy camera is available.")
                apply_decision = getattr(self, "_apply_ai_director_locked", None)
                if callable(apply_decision):
                    try:
                        outcome = apply_decision(body)
                    except Exception as exc:
                        if isinstance(getattr(exc, "status_code", None), int) and 400 <= exc.status_code < 500:
                            self._ai_round = None
                            self._ai_reports.clear()
                        raise
                    self._record_inference(body)
                    self._metrics["ai_decisions"] += 1
                    self._ai_round = None
                    self._ai_reports.clear()
                    self._ai_next_mono = time.monotonic() + 0.25
                    return outcome
                self._record_inference(body)
                self._metrics["ai_decisions"] += 1
                if result["action"] == "switch" and target != self._program["camera_id"]:
                    self._set_program_locked(target, result["reason"], source="ai_director", decision_source="model")
                    self._program["response_id"] = body["response_id"]
                    self._metrics["ai_cuts"] += 1
                else:
                    self._add_event("ai_director_hold", "ai_director", result["reason"], target)
                self._ai_round = None
                self._ai_reports.clear()
                self._ai_next_mono = time.monotonic() + 0.25
                return {"ok": True, "executed": True, "action": result["action"], "program": copy.deepcopy(self._program)}
