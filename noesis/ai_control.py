"""Leased AI collaboration rounds. This module validates; it never ranks shots."""
from __future__ import annotations

import copy
import math
import time
import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError


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
    evidence_response_ids: list[str] = Field(default_factory=list, max_length=5)


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
        self._model_epoch = 0
        self._ai_round: dict[str, Any] | None = None
        self._ai_reports: dict[str, dict] = {}
        self._inference_results: dict[str, dict] = {}
        self._ai_next_mono = 0.0
        self._grid_run: dict[str, Any] = {}

    def _models_snapshot(self) -> dict:
        return {"selected": self.model_profile, "epoch": self._model_epoch, "options": copy.deepcopy(self._model_catalog)}

    def _invalidate_ai(self) -> None:
        self._ai_round = None
        self._ai_reports.clear()
        self._ai_next_mono = 0.0

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
        pending = self._ai_round
        now = time.monotonic()
        if (self._session_lock.locked() or self._session["status"] != "running" or self._manual_latched
                or pending is None or now >= pending["deadline_mono"]):
            raise error(409, "AI round is inactive, expired, or blocked by manual control.")
        for key, value in (("request_id", pending["request_id"]), ("session_id", self._session["id"]),
                           ("epoch", self._session["epoch"]), ("override_epoch", self._override_epoch),
                           ("model_epoch", self._model_epoch), ("model", self.model_name)):
            if body[key] != value or body[key] != pending[key]:
                raise error(409, f"AI result has stale or mismatched {key}.")
        if (body["source_revision"] < pending["observation_revision"]
                or body["source_revision"] > self._revision(self._latest_media)
                or body["media_time_s"] < pending["media_time_s"] - 0.1
                or body["media_time_s"] > self._session["time_s"] + 0.25):
            raise error(409, "AI evidence source time or revision is invalid.")
        role = body["role"]
        if director != (role == "director"):
            raise error(422, "AI result uses the wrong role endpoint.")
        expected_id = f"camera-{body.get('camera_id')}" if role == "camera" else role
        if body["agent_id"] != expected_id or (role == "camera" and body.get("camera_id") not in {"closeup1", "closeup2", "closeup3", "closeup4"}):
            raise error(422, "Unknown AI role or camera identity.")
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
            if (set(result) != {"recommendation", "confidence", "reason"}
                    or result["recommendation"] not in {"take", "hold", "avoid"}
                    or isinstance(confidence, bool) or not isinstance(confidence, (int, float))
                    or not math.isfinite(confidence) or not 0 <= confidence <= 1):
                raise error(422, "Malformed AI camera recommendation.")
        elif role == "critic":
            if set(result) != {"assessment", "reason"} or result["assessment"] not in {"steady", "change", "wide"}:
                raise error(422, "Malformed AI critic assessment.")
        else:
            if (set(result) != {"action", "camera_id", "reason"} or result["action"] not in {"hold", "switch"}
                    or result["camera_id"] not in {"closeup1", "closeup2", "closeup3", "closeup4", "corner", "slate"}):
                raise error(422, "Malformed AI director choice.")
        return body

    def _record_inference(self, body: dict) -> None:
        result = {k: copy.deepcopy(v) for k, v in body.items() if k not in {"evidence_response_ids"}}
        result["status"] = "completed"
        self._inference_results[body["agent_id"]] = result
        self._remember_decision(body["response_id"])
        self._last_model_result = {k: body[k] for k in ("model", "response_id", "latency_ms", "input_tokens", "output_tokens")}
        self._metrics["ai_responses"] += 1

    async def accept_ai_report(self, raw: dict) -> dict:
        async with self._lock:
            body = self._validate_ai_body(raw, director=False)
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
                expected_roles = {"camera-closeup1", "camera-closeup2", "camera-closeup3", "camera-closeup4", "critic"}
                expected_responses = {report["response_id"] for report in self._ai_reports.values()}
                if (set(self._ai_reports) != expected_roles or len(body["evidence_response_ids"]) != 5
                        or set(body["evidence_response_ids"]) != expected_responses):
                    raise error(409, "Director must reference this round's four AI camera reports and critic assessment.")
                result = body["result"]
                target = result["camera_id"]
                if result["action"] == "hold" and target != self._program["camera_id"]:
                    raise error(409, "Hold decision no longer matches the current camera.")
                if target != "slate" and not self._is_healthy(self._camera_map(self._latest_media).get(target), running=True):
                    raise error(409, "The AI-selected source is currently unavailable.")
                if target == "slate" and any(self._is_healthy(camera, running=True) for camera in self._camera_map(self._latest_media).values()):
                    raise error(409, "Slate is reserved for complete source failure; a healthy camera is available.")
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
