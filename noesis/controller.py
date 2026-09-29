"""Session and directing state for Noesis.

The controller owns the replay generation, manual latch, expiring director
request, and the camera selected for the one shared program clock.  OBS only
captures the program Browser Source; per-camera cuts happen here and are not
reported as OBS scene acknowledgements.
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import time
import uuid
from collections import deque
from pathlib import Path
from .ai_control import AIControlMixin
from typing import Any


CAMERA_IDS = ("closeup1", "closeup2", "closeup3", "closeup4", "corner")
TARGET_IDS = frozenset((*CAMERA_IDS, "slate"))
PARTICIPANT_BY_CAMERA = {
    "closeup1": "A",
    "closeup2": "C",
    "closeup3": "D",
    "closeup4": "B",
}
SCENE_BY_CAMERA = {
    "closeup1": "LD_Closeup1",
    "closeup2": "LD_Closeup2",
    "closeup3": "LD_Closeup3",
    "closeup4": "LD_Closeup4",
    "corner": "LD_Corner",
    "slate": "LD_Slate",
}
CAMERA_BY_SCENE = {scene: camera for camera, scene in SCENE_BY_CAMERA.items()}


class ControllerError(Exception):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class DirectorController(AIControlMixin):
    """Concurrency-safe local controller with non-blocking editorial input."""

    def __init__(
        self,
        media: Any,
        obs: Any,
        *,
        request_timeout_s: float = 30.0,
        heartbeat_ttl_s: float = 30.0,
        camera_observation_ttl_s: float = 2.0,
        stale_source_ms: int = 1500,
        event_limit: int = 120,
        model_name: str | None = None,
        model_catalog: list[dict] | None = None,
        model_profile: str = "kimi",
    ) -> None:
        self.media = media
        self.obs_bridge = obs
        self.request_timeout_s = request_timeout_s
        self.heartbeat_ttl_s = heartbeat_ttl_s
        self.camera_observation_ttl_s = camera_observation_ttl_s
        self.stale_source_ms = stale_source_ms
        self.model_name = model_name

        self._lock = asyncio.Lock()
        self._action_lock = asyncio.Lock()
        self._session_lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self._obs_task: asyncio.Task[None] | None = None
        self._closed = False
        self._latest_media: dict[str, Any] = {
            "time_s": 0.0,
            "duration_s": 0.0,
            "input_mode": "synthetic",
            "status": "idle",
            "observation_revision": 0,
            "cameras": [],
        }
        self._session: dict[str, Any] = {
            "id": None,
            "epoch": 0,
            "status": "idle",
            "input_mode": "synthetic",
            "output_mode": "preview",
            "time_s": 0.0,
            "duration_s": 0.0,
        }
        self._mode = "degraded"
        self._manual_latched = False
        self._program: dict[str, Any] = {
            "camera_id": "slate",
            "scene": "NOESIS_Program",
            "reason": "Waiting for a session.",
            "last_cut_s": 0.0,
        }
        self._override_epoch = 0
        self._last_model_result: dict[str, Any] | None = None
        self._seen_decisions: deque[str] = deque(maxlen=512)
        self._seen_decision_set: set[str] = set()
        self._agents: dict[str, dict[str, Any]] = {}
        self._agent_seen_mono: dict[str, float] = {}
        self._camera_observations: dict[str, dict[str, Any]] = {}
        self._camera_observation_mono: dict[str, float] = {}
        self._revision_history: dict[int, tuple[float, dict[str, dict[str, Any]]]] = {}
        self._revision_order: deque[int] = deque(maxlen=64)
        self._unhealthy_since: dict[str, float] = {}
        self._reported_manual_faults: set[str] = set()
        self._event_seq = 0
        self._events: deque[dict[str, Any]] = deque(maxlen=event_limit)
        self._metrics: dict[str, Any] = {
            "cuts": 0,
            "fallbacks": 0,
            "rejected_proposals": 0,
            "model_timeouts": 0,
            "last_recovery_ms": None,
            "ai_rounds": 0, "ai_responses": 0, "ai_decisions": 0, "ai_cuts": 0,
        }
        self._last_error: str | None = None
        self._obs_stop_pending = False
        self._last_obs_status = self._obs_snapshot()
        self._init_ai(model_catalog, model_profile)

    async def start_background(self, interval_s: float = 0.15, obs_interval_s: float = 1.0) -> None:
        async with self._lock:
            if self._task and not self._task.done() and self._obs_task and not self._obs_task.done():
                return
            self._closed = False
            if not self._task or self._task.done():
                self._task = asyncio.create_task(self._poll_loop(interval_s), name="noesis-controller")
            if not self._obs_task or self._obs_task.done():
                self._obs_task = asyncio.create_task(self._obs_poll_loop(obs_interval_s), name="noesis-obs-status")

    async def close(self) -> None:
        async with self._lock:
            self._closed = True
            tasks = (self._task, self._obs_task)
            self._task = None
            self._obs_task = None
        for task in tasks:
            if task:
                task.cancel()
        if tasks:
            await asyncio.gather(*(task for task in tasks if task), return_exceptions=True)

    async def _poll_loop(self, interval_s: float) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                async with self._lock:
                    self._last_error = f"Controller tick failed: {type(exc).__name__}: {exc}"
                    self._add_event("controller_error", "controller", self._last_error)
            async with self._lock:
                if self._closed:
                    return
            await asyncio.sleep(interval_s)

    async def _obs_poll_loop(self, interval_s: float) -> None:
        while True:
            try:
                poll_status = getattr(self.obs_bridge, "poll_status", None)
                if callable(poll_status):
                    observed = await poll_status()
                    if isinstance(observed, dict):
                        async with self._lock:
                            active_obs_session = (
                                self._session["status"] in ("running", "paused")
                                and self._session["output_mode"] == "obs"
                            )
                            was_connected = bool(self._last_obs_status.get("connected"))
                            was_recording = bool(self._last_obs_status.get("recording"))
                            recording_stopped = was_recording and not bool(observed.get("recording"))
                            disconnected = was_connected and not bool(observed.get("connected"))
                            if active_obs_session and recording_stopped and bool(observed.get("connected")):
                                self._add_event("obs_recording_stopped_externally", "obs", "OBS reports that recording stopped outside the session controller.")
                            elif active_obs_session and disconnected:
                                self._add_event("obs_disconnected", "obs", "OBS connection was lost; recording status is unavailable.")
                            self._last_obs_status = dict(observed)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                async with self._lock:
                    self._add_event("obs_poll_error", "obs", f"OBS status poll failed: {type(exc).__name__}.")
            async with self._lock:
                if self._closed:
                    return
            await asyncio.sleep(interval_s)

    async def tick(self) -> None:
        async with self._lock:
            if self._session_lock.locked():
                return
            epoch_before = self._session["epoch"]
            session_id_before = self._session["id"]
        try:
            media_snapshot = await asyncio.to_thread(self.media.snapshot)
            if not isinstance(media_snapshot, dict):
                raise ValueError("MediaEngine.snapshot() did not return an object")
            media_error = None
        except Exception as exc:  # the controller must survive a media worker error
            media_snapshot = None
            media_error = f"Media snapshot failed: {type(exc).__name__}: {exc}"

        now = time.monotonic()
        fallback: tuple[str, str] | None = None
        natural_end = False
        async with self._lock:
            if epoch_before != self._session["epoch"] or self._session_lock.locked():
                return
            if media_error:
                self._last_error = media_error
                self._add_event("media_error", "controller", media_error)
            elif media_snapshot is not None:
                self._last_error = None
                self._latest_media = media_snapshot
                self._remember_revision_locked(now)
                self._session["time_s"] = self._as_float(media_snapshot.get("time_s"), 0.0)
                self._session["duration_s"] = self._as_float(media_snapshot.get("duration_s"), 0.0)
                natural_end = self._session["status"] == "running" and self._is_media_end(media_snapshot)

            flower = self._flower_snapshot(now)
            self._mode = self._mode_for(flower)
            if self._ai_round and now >= self._ai_round["deadline_mono"]:
                self._invalidate_ai()
                self._metrics["model_timeouts"] += 1
                self._add_event("ai_round_expired", "validator", "AI response deadline passed; retaining the current healthy shot.")

            if self._session["status"] == "running" and media_snapshot is not None and not natural_end:
                cameras = self._camera_map(media_snapshot)
                current = self._program["camera_id"]
                current_camera = cameras.get(current)
                if current == "slate" and self._mode != "manual":
                    recovered = self._fallback_target(cameras)
                    if recovered != "slate":
                        fallback = (recovered, "slate")
                elif current != "slate" and not self._is_healthy(current_camera, running=True):
                    self._unhealthy_since.setdefault(current, now)
                    if self._mode == "manual":
                        if current not in self._reported_manual_faults:
                            self._reported_manual_faults.add(current)
                            self._add_event(
                                "manual_source_unavailable",
                                "health",
                                "The manually selected source is unavailable; automatic switching is latched off.",
                                current,
                            )
                    else:
                        target = self._fallback_target(cameras)
                        if target != current:
                            fallback = (target, current)
                elif current != "slate":
                    self._unhealthy_since.pop(current, None)
                    self._reported_manual_faults.discard(current)

        if natural_end and media_snapshot is not None:
            await self._finalize_media_end(session_id_before, epoch_before, media_snapshot)
            return
        if fallback:
            target, failed_camera = fallback
            await self._automatic_cut(
                target,
                source="health",
                reason=("A usable camera is available; leaving the standby slate." if failed_camera == "slate"
                        else f"{failed_camera} became unavailable; selected the best usable source."),
                fallback=failed_camera != "slate",
                expected_unhealthy=failed_camera,
                expected_epoch=epoch_before,
            )

    @staticmethod
    def _is_media_end(snapshot: dict[str, Any]) -> bool:
        status = str(snapshot.get("status", "")).lower()
        if status in {"stopped", "ended", "eof", "finished"}:
            return True
        time_s = DirectorController._as_float(snapshot.get("time_s"), 0.0)
        duration_s = DirectorController._as_float(snapshot.get("duration_s"), 0.0)
        return duration_s > 0.0 and time_s >= duration_s

    async def _finalize_media_end(
        self,
        session_id: str | None,
        epoch: int,
        media_snapshot: dict[str, Any],
        *,
        session_lock_held: bool = False,
    ) -> bool:
        async def finalize() -> bool:
            async with self._lock:
                if (
                    self._session["id"] != session_id
                    or self._session["epoch"] != epoch
                    or self._session["status"] not in ("running", "paused")
                    or not self._is_media_end(media_snapshot)
                ):
                    return False
                self._latest_media = media_snapshot
                self._remember_revision_locked(time.monotonic())
                self._session["time_s"] = self._as_float(media_snapshot.get("time_s"), self._session["time_s"])
                self._session["duration_s"] = self._as_float(media_snapshot.get("duration_s"), self._session["duration_s"])
                self._session["status"] = "stopped"
                self._session["epoch"] += 1
                stop_obs = self._session["output_mode"] == "obs"
                self._cancel_pending("Replay reached its end; pending decisions were invalidated.")
                self._clear_media_evidence_locked()
                self._add_event("session_ended", "media", "Replay reached the end of the selected media.")
            if stop_obs:
                try:
                    await self._stop_obs_recording()
                    async with self._lock:
                        self._last_obs_status = self._obs_snapshot()
                except Exception as exc:
                    async with self._lock:
                        self._last_error = f"OBS recording stop at replay end failed: {type(exc).__name__}: {exc}"
                        self._add_event("session_end_obs_error", "obs", self._last_error)
            return True

        if session_lock_held:
            return await finalize()
        async with self._session_lock:
            return await finalize()


    async def get_state(self) -> dict[str, Any]:
        async with self._lock:
            self._mode = self._mode_for(self._flower_snapshot(time.monotonic()))
            return self._state_locked()

    def _state_locked(self) -> dict[str, Any]:
        media = self._latest_media
        cameras = []
        now = time.monotonic()
        for camera in media.get("cameras", []) or []:
            if not isinstance(camera, dict):
                continue
            camera_id = str(camera.get("id", ""))
            copy_camera = dict(camera)
            copy_camera["healthy"] = self._is_healthy(camera, running=self._session["status"] == "running")
            copy_camera.setdefault("name", camera_id)
            copy_camera.setdefault("participant", PARTICIPANT_BY_CAMERA.get(camera_id))
            copy_camera.setdefault("speaking", False)
            copy_camera.setdefault("energy", 0.0)
            copy_camera.setdefault("quality", 0.0)
            copy_camera.setdefault("age_ms", 0)
            copy_camera.setdefault("frame_url", f"/api/frame/{camera_id}.jpg")
            cameras.append(copy_camera)
        obs_state = self._obs_snapshot()
        flower_state = self._flower_snapshot(now)
        availability = self._media_availability()
        return {
            "session": copy.deepcopy(self._session),
            "mode": self._mode,
            "program": copy.deepcopy(self._program),
            "cameras": cameras,
            "obs": obs_state,
            "flower": flower_state,
            "models": self._models_snapshot(),
            "metrics": copy.deepcopy(self._metrics),
            "events": list(reversed(copy.deepcopy(self._events))),
            "data": availability,
            "observation_revision": self._revision(media),
        }

    def _media_availability(self) -> dict[str, Any]:
        try:
            raw = self.media.availability()
            if isinstance(raw, dict):
                return {
                    "ami_available": bool(raw.get("ami_available", False)),
                    "missing_files": list(raw.get("missing_files", []))[:32],
                    "credits": str(raw.get("credits", "")),
                }
        except Exception:
            pass
        return {"ami_available": False, "missing_files": [], "credits": ""}

    def _obs_snapshot(self) -> dict[str, Any]:
        try:
            raw = self.obs_bridge.snapshot()
            if isinstance(raw, dict):
                return {
                    "connected": bool(raw.get("connected", False)),
                    "status": str(raw.get("status", "disconnected")),
                    **({"error": str(raw["error"])} if raw.get("error") else {}),
                    "recording": bool(raw.get("recording", False)),
                    "stop_pending": self._obs_stop_pending,
                    **({"output_path": str(raw["output_path"])} if raw.get("output_path") else {}),
                }
        except Exception as exc:
            return {"connected": False, "status": "error", "error": f"OBS status failed: {type(exc).__name__}", "recording": False}
        return {"connected": False, "status": "disconnected", "recording": False}

    async def _stop_obs_recording(self) -> None:
        # Keep a retryable obligation until OBS acknowledges that output stopped.
        self._obs_stop_pending = True
        result = await self.obs_bridge.stop_recording()
        if isinstance(result, dict) and result.get("stopped") is False:
            raise RuntimeError("OBS did not confirm recording stopped. Reconnect and retry Stop.")
        self._obs_stop_pending = False

    def _flower_snapshot(self, now: float) -> dict[str, Any]:
        agents = []
        roles: set[str] = set()
        for agent_id, entry in self._agents.items():
            age_s = max(0.0, now - self._agent_seen_mono.get(agent_id, now))
            healthy = age_s <= self.heartbeat_ttl_s
            if healthy:
                roles.add(str(entry.get("role", "")))
            agents.append({
                **entry,
                "age_ms": int(age_s * 1000),
                "healthy": healthy,
            })
        agents.sort(key=lambda item: (item.get("role", ""), item.get("agent_id", "")))
        required_agents = {"director", "critic", *(f"camera-closeup{i}" for i in range(1, 5))}
        live_agents = {entry["agent_id"] for entry in agents if entry["healthy"] and entry.get("decision_mode") == "llm"}
        if required_agents.issubset(live_agents):
            status = "connected"
        elif roles:
            status = "degraded"
        else:
            status = "disconnected"
        decision_modes = {
            str(entry.get("role")): str(entry.get("decision_mode"))
            for entry in agents
            if entry.get("healthy") and entry.get("decision_mode")
        }
        run_file = os.getenv("NOESIS_GRID_RUN_FILE")
        if run_file:
            try:
                managed = json.loads(Path(run_file).read_text(encoding="utf-8"))
                self._grid_run = {key: managed[key] for key in ("run_id", "federation", "status", "sub_status", "checked_at", "deployment") if key in managed}
            except (OSError, ValueError, TypeError):
                self._grid_run = {}
        deployment = os.getenv("NOESIS_RUNTIME_DEPLOYMENT", "local")
        deployment = "supergrid" if deployment == "supergrid" else "local"
        return {
            "status": status,
            "transport": ("Flower local AgentApp; Nebius inference" if deployment == "local"
                          else "Flower SuperGrid native Grid; local media bridge"),
            **({"model": self.model_name} if self.model_name else {}),
            "model_status": "verified" if self._last_model_result else "configured_not_verified" if self.model_name else "not_configured",
            "topology": deployment,
            "deployment": deployment,
            "inference_results": copy.deepcopy(list(self._inference_results.values())),
            "grid_run": copy.deepcopy(self._grid_run),
            "last_model_result": copy.deepcopy(self._last_model_result),
            "decision_modes": decision_modes,
            **({"error": "Flower AgentApp heartbeats are stale or unavailable."} if status != "connected" else {}),
            "agents": agents,
        }

    def _mode_for(self, flower: dict[str, Any]) -> str:
        if self._manual_latched:
            return "manual"
        return "autopilot" if flower.get("status") == "connected" else "degraded"

    async def session_start(self, input_mode: str, output_mode: str, start_s: float = 0.0) -> dict[str, Any]:
        if input_mode not in ("synthetic", "ami"):
            raise ControllerError(422, "input_mode must be 'synthetic' or 'ami'.")
        if output_mode not in ("preview", "obs"):
            raise ControllerError(422, "output_mode must be 'preview' or 'obs'.")
        if not isinstance(start_s, (float, int)) or start_s < 0:
            raise ControllerError(422, "start_s must be a non-negative number.")

        async with self._session_lock:
            async with self._lock:
                if self._session["status"] in ("running", "paused"):
                    raise ControllerError(409, "A session is already active.")
                if self._obs_stop_pending:
                    raise ControllerError(409, "The previous recording stop is unconfirmed. Reconnect OBS and retry Stop first.")
                session_id = str(uuid.uuid4())
                epoch = self._session["epoch"] + 1
                self._session["epoch"] = epoch
                self._cancel_pending("A new session is starting; previous decisions were invalidated.")
                self._clear_media_evidence_locked()
            if input_mode == "ami" and not self._media_availability().get("ami_available"):
                raise ControllerError(409, "AMI media is unavailable; see data.missing_files in /api/state.")
            try:
                await asyncio.to_thread(self.media.start, input_mode, float(start_s))
                # Hold the shared replay clock while OBS prepares and confirms
                # its one output source and recording.
                await asyncio.to_thread(self.media.pause)
                media_snapshot = await asyncio.to_thread(self.media.snapshot)
            except Exception as exc:
                try:
                    await asyncio.to_thread(self.media.stop)
                except Exception:
                    pass
                async with self._lock:
                    self._session.update({"id": session_id, "epoch": epoch, "status": "stopped", "input_mode": input_mode, "output_mode": output_mode})
                    self._last_error = f"Media start failed: {type(exc).__name__}: {exc}"
                    self._add_event("session_start_error", "media", self._last_error)
                raise ControllerError(500, f"Could not start media: {type(exc).__name__}: {exc}") from exc

            if self._is_media_end(media_snapshot):
                async with self._lock:
                    self._session = {
                        "id": session_id,
                        "epoch": epoch,
                        "status": "stopped",
                        "input_mode": input_mode,
                        "output_mode": output_mode,
                        "time_s": self._as_float(media_snapshot.get("time_s"), float(start_s)),
                        "duration_s": self._as_float(media_snapshot.get("duration_s"), 0.0),
                    }
                    self._latest_media = media_snapshot
                    self._remember_revision_locked(time.monotonic())
                    self._add_event("session_start_rejected", "operator", "Start time is at or past the end of the selected media.")
                raise ControllerError(409, "start_s is at or past the end of the selected media.")

            if output_mode == "obs":
                try:
                    await self.obs_bridge.connect()
                    await self.obs_bridge.setup_program_scene()
                    await self.obs_bridge.select_program_scene("NOESIS_Program")
                    await self.obs_bridge.start_recording()
                except Exception as exc:
                    try:
                        await self._stop_obs_recording()
                    except Exception:
                        pass
                    await asyncio.to_thread(self.media.stop)
                    async with self._lock:
                        self._session = {
                            "id": session_id,
                            "epoch": epoch,
                            "status": "stopped",
                            "input_mode": input_mode,
                            "output_mode": output_mode,
                            "time_s": self._as_float(media_snapshot.get("time_s"), float(start_s)),
                            "duration_s": self._as_float(media_snapshot.get("duration_s"), 0.0),
                        }
                        self._latest_media = media_snapshot
                        self._last_error = f"OBS session start failed: {type(exc).__name__}: {exc}"
                        self._add_event("obs_error", "obs", self._last_error)
                    raise ControllerError(502, "OBS setup or recording start failed; media was stopped. Check /api/state.obs.error.") from exc

            try:
                await asyncio.to_thread(self.media.resume)
                media_snapshot = await asyncio.to_thread(self.media.snapshot)
            except Exception as exc:
                if output_mode == "obs":
                    try:
                        await self._stop_obs_recording()
                    except Exception:
                        pass
                try:
                    await asyncio.to_thread(self.media.stop)
                except Exception:
                    pass
                async with self._lock:
                    self._session = {
                        "id": session_id,
                        "epoch": epoch,
                        "status": "stopped",
                        "input_mode": input_mode,
                        "output_mode": output_mode,
                        "time_s": self._as_float(media_snapshot.get("time_s"), float(start_s)),
                        "duration_s": self._as_float(media_snapshot.get("duration_s"), 0.0),
                    }
                    self._last_error = f"Media resume failed: {type(exc).__name__}: {exc}"
                    self._add_event("session_start_error", "media", self._last_error)
                raise ControllerError(500, "Could not resume media after OBS setup.") from exc

            async with self._lock:
                self._session = {
                    "id": session_id,
                    "epoch": epoch,
                    "status": "running",
                    "input_mode": input_mode,
                    "output_mode": output_mode,
                    "time_s": self._as_float(media_snapshot.get("time_s"), float(start_s)),
                    "duration_s": self._as_float(media_snapshot.get("duration_s"), 0.0),
                }
                self._latest_media = media_snapshot
                self._remember_revision_locked(time.monotonic())
                self._last_obs_status = self._obs_snapshot()
                self._override_epoch = 0
                self._manual_latched = False
                self._program = {
                    "camera_id": "slate",
                    "scene": "NOESIS_Program",
                    "reason": "Session started; waiting for a usable camera.",
                    "last_cut_s": float(start_s),
                }
                self._unhealthy_since.clear()
                self._reported_manual_faults.clear()
                self._metrics.update(cuts=0, fallbacks=0,
                                     rejected_proposals=0, model_timeouts=0, last_recovery_ms=None,
                                     ai_rounds=0, ai_responses=0, ai_decisions=0, ai_cuts=0)
                self._last_model_result = None
                self._inference_results.clear()
                self._events.clear()
                self._add_event("session_started", "operator", f"Started {input_mode} session in {output_mode} mode.")
        await self.tick()
        async with self._lock:
            if self._program["camera_id"] == "slate":
                first_view = self._fallback_target(self._camera_map(self._latest_media))
                if first_view != "slate":
                    self._set_program_locked(first_view, "Selected an available source at session start.", source="health")
        return await self.get_state()

    async def session_pause(self) -> dict[str, Any]:
        async with self._session_lock:
            async with self._lock:
                self._require_running()
                session_epoch = self._session["epoch"]
                self._session["epoch"] = session_epoch + 1
                self._cancel_pending("Session pause began; old decisions were invalidated.")
                self._clear_media_evidence_locked()
            try:
                await asyncio.to_thread(self.media.pause)
                media_snapshot = await asyncio.to_thread(self.media.snapshot)
            except Exception as exc:
                raise ControllerError(500, f"Could not pause media: {type(exc).__name__}: {exc}") from exc
            async with self._lock:
                self._latest_media = media_snapshot
                self._session["time_s"] = self._as_float(media_snapshot.get("time_s"), self._session["time_s"])
                self._session["duration_s"] = self._as_float(media_snapshot.get("duration_s"), self._session["duration_s"])
                self._session["status"] = "paused"
                self._session["epoch"] = session_epoch + 1
                self._cancel_pending("Session paused; old decisions were invalidated.")
                self._add_event("session_paused", "operator", "Paused replay.")
        return await self.get_state()

    async def session_resume(self) -> dict[str, Any]:
        async with self._session_lock:
            async with self._lock:
                if self._session["status"] != "paused":
                    raise ControllerError(409, "Only a paused session can be resumed.")
                session_epoch = self._session["epoch"]
                self._session["epoch"] = session_epoch + 1
                self._cancel_pending("Session resume began; fresh evidence is required.")
                self._clear_media_evidence_locked()
            try:
                await asyncio.to_thread(self.media.resume)
            except Exception as exc:
                raise ControllerError(500, f"Could not resume media: {type(exc).__name__}: {exc}") from exc
            async with self._lock:
                self._session["status"] = "running"
                self._session["epoch"] = session_epoch + 1
                self._cancel_pending("Session resumed; fresh evidence is required.")
                self._add_event("session_resumed", "operator", "Resumed replay on a new decision epoch.")
        return await self.get_state()

    async def session_stop(self) -> dict[str, Any]:
        async with self._session_lock:
            async with self._lock:
                retry_obs = self._obs_stop_pending or (self._session["output_mode"] == "obs" and self._obs_snapshot().get("recording"))
                if self._session["status"] in ("idle", "stopped") and not retry_obs:
                    return self._state_locked()
                session_epoch = self._session["epoch"]
                self._session["epoch"] = session_epoch + 1
                self._cancel_pending("Session stop began; pending decisions were invalidated.")
                self._clear_media_evidence_locked()
                stop_obs = self._session["output_mode"] == "obs" or retry_obs
            error: str | None = None
            try:
                await asyncio.to_thread(self.media.stop)
            except Exception as exc:
                error = f"Media stop failed: {type(exc).__name__}: {exc}"
            if stop_obs:
                try:
                    await self._stop_obs_recording()
                except Exception as exc:
                    error = f"OBS recording stop failed: {type(exc).__name__}: {exc}"
            async with self._lock:
                self._session["status"] = "stopped"
                self._session["epoch"] = session_epoch + 1
                self._cancel_pending("Session stopped; pending decisions were invalidated.")
                if error:
                    self._last_error = error
                    self._add_event("session_stop_error", "controller", error)
                self._last_obs_status = self._obs_snapshot()
                self._add_event("session_stopped", "operator", (
                    "Replay stopped; recording stop is unconfirmed. Reconnect OBS and retry Stop."
                    if self._obs_stop_pending else "Stopped replay and confirmed any managed OBS recording stopped."
                ))
        return await self.get_state()

    async def session_seek(self, time_s: float) -> dict[str, Any]:
        if not isinstance(time_s, (float, int)) or time_s < 0:
            raise ControllerError(422, "time_s must be a non-negative number.")
        async with self._session_lock:
            async with self._lock:
                if self._session["status"] not in ("running", "paused"):
                    raise ControllerError(409, "Seek requires a running or paused session.")
                session_epoch = self._session["epoch"]
                session_id = self._session["id"]
                self._session["epoch"] = session_epoch + 1
                self._cancel_pending("Replay seek began; older decisions were invalidated.")
                self._clear_media_evidence_locked()
            try:
                await asyncio.to_thread(self.media.seek, float(time_s))
                media_snapshot = await asyncio.to_thread(self.media.snapshot)
            except Exception as exc:
                raise ControllerError(422, f"Could not seek media: {type(exc).__name__}: {exc}") from exc
            async with self._lock:
                self._session["epoch"] = session_epoch + 1
                self._latest_media = media_snapshot
                self._remember_revision_locked(time.monotonic())
                self._session["time_s"] = self._as_float(media_snapshot.get("time_s"), float(time_s))
                self._session["duration_s"] = self._as_float(media_snapshot.get("duration_s"), 0.0)
                self._cancel_pending("Replay seek invalidated all older decisions.")
                self._program["reason"] = "Replay seek; awaiting fresh source and speaker evidence."
                self._program["last_cut_s"] = self._session["time_s"]
                self._add_event("session_seek", "operator", "Sought replay and advanced the decision epoch.")
            if self._is_media_end(media_snapshot):
                await self._finalize_media_end(
                    session_id,
                    session_epoch + 1,
                    media_snapshot,
                    session_lock_held=True,
                )
                return await self.get_state()
        await self.tick()
        return await self.get_state()

    async def inject_fault(self, camera_id: str, kind: str, duration_s: float = 10.0) -> dict[str, Any]:
        if camera_id not in CAMERA_IDS:
            raise ControllerError(422, "camera_id must name one of the five source cameras.")
        if kind not in ("black", "freeze", "offline", "none"):
            raise ControllerError(422, "kind must be black, freeze, offline, or none.")
        if not isinstance(duration_s, (int, float)) or duration_s <= 0 or duration_s > 300:
            raise ControllerError(422, "duration_s must be greater than 0 and at most 300.")
        async with self._lock:
            if self._session["status"] not in ("running", "paused"):
                raise ControllerError(409, "Fault injection requires an active session.")
        try:
            await asyncio.to_thread(self.media.inject_fault, camera_id, kind, float(duration_s))
        except Exception as exc:
            raise ControllerError(422, f"Could not inject fault: {type(exc).__name__}: {exc}") from exc
        async with self._lock:
            self._add_event("fault_injected", "operator", f"Injected {kind} fault for {duration_s:g}s (test).", camera_id)
        await self.tick()
        return await self.get_state()

    async def manual_override(self, camera_id: str) -> dict[str, Any]:
        if camera_id not in TARGET_IDS:
            raise ControllerError(422, "camera_id is not an allowed program target.")
        async with self._action_lock:
            async with self._lock:
                if self._session_lock.locked():
                    raise ControllerError(409, "Wait for the session transition to finish.")
                if self._session["status"] != "running":
                    raise ControllerError(409, "Manual override requires a running session.")
                target = camera_id
                if target != "slate":
                    camera = self._camera_map(self._latest_media).get(target)
                    if not self._is_healthy(camera, running=True):
                        raise ControllerError(409, "Cannot manually select an unavailable camera.")
                self._manual_latched = True
                self._override_epoch += 1
                self._invalidate_ai()
                self._cancel_pending("Manual override latched; outstanding director work was discarded.")
                self._mode = "manual"
                if target != self._program["camera_id"]:
                    self._set_program_locked(target, "Manual operator override.", source="manual")
                else:
                    self._add_event("manual_override", "operator", "Manual control latched on the current camera.", target)
        return await self.get_state()

    async def resume_autopilot(self) -> dict[str, Any]:
        async with self._action_lock:
            async with self._lock:
                if self._session_lock.locked():
                    raise ControllerError(409, "Wait for the session transition to finish.")
                if self._session["status"] != "running":
                    raise ControllerError(409, "Resume Autopilot requires a running session.")
                self._manual_latched = False
                self._override_epoch += 1
                self._invalidate_ai()
                self._mode = self._mode_for(self._flower_snapshot(time.monotonic()))
                self._cancel_pending("Autopilot resumed; fresh evidence is required.")
                self._add_event("autopilot_resumed", "operator", "Autopilot resumed on a new override epoch.")
        await self.tick()
        return await self.get_state()

    async def heartbeat(self, body: dict[str, Any]) -> dict[str, Any]:
        agent_id = body.get("agent_id")
        role = body.get("role")
        runtime = body.get("runtime")
        decision_mode = body.get("decision_mode")
        if not isinstance(agent_id, str) or not agent_id.strip() or len(agent_id) > 120:
            raise ControllerError(422, "agent_id is required and must be at most 120 characters.")
        if role not in ("camera", "director", "critic"):
            raise ControllerError(422, "role must be camera, director, or critic.")
        if not isinstance(runtime, str) or not runtime.strip() or len(runtime) > 120:
            raise ControllerError(422, "runtime is required and must be at most 120 characters.")
        if decision_mode is not None and decision_mode not in ("rules", "llm"):
            raise ControllerError(422, "decision_mode must be rules or llm when supplied.")
        camera_id = body.get("camera_id")
        if role == "camera" and camera_id not in CAMERA_IDS:
            raise ControllerError(422, "camera-role heartbeats require a known camera_id.")
        safe_entry = {
            "agent_id": agent_id.strip(),
            "role": role,
            "runtime": runtime.strip(),
        }
        if decision_mode:
            safe_entry["decision_mode"] = decision_mode
        for key in ("run_id", "camera_id"):
            value = body.get(key)
            if value is not None:
                if not isinstance(value, str) or len(value) > 200:
                    raise ControllerError(422, f"{key} must be a short string.")
                safe_entry[key] = value
        async with self._lock:
            previous = self._agents.get(agent_id.strip())
            prior_age = time.monotonic() - self._agent_seen_mono.get(agent_id.strip(), 0.0)
            self._agents[agent_id.strip()] = safe_entry
            self._agent_seen_mono[agent_id.strip()] = time.monotonic()
            self._mode = self._mode_for(self._flower_snapshot(time.monotonic()))
            if previous != safe_entry or prior_age > self.heartbeat_ttl_s:
                self._add_event("agent_heartbeat", "flower", f"{role.title()} AgentApp connected; heartbeat is current.", camera_id)
        return {"ok": True, "agent_id": agent_id.strip(), "flower": (await self.get_state())["flower"]}

    async def camera_observation(self, body: dict[str, Any]) -> dict[str, Any]:
        camera_id = body.get("camera_id")
        revision = body.get("source_observation_revision", body.get("observation_revision"))
        if camera_id not in CAMERA_IDS:
            raise ControllerError(422, "camera_id must name one of the five source cameras.")
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
            raise ControllerError(422, "source_observation_revision must be a non-negative integer.")
        observation = body.get("observation", body.get("observations"))
        if not isinstance(observation, dict):
            raise ControllerError(422, "observation must be an object.")
        async with self._lock:
            if self._session_lock.locked():
                raise ControllerError(409, "Session transition in progress; observations must be sampled again.")
            reported_session = observation.get("session_id")
            reported_epoch = observation.get("epoch")
            if reported_session is not None and reported_session != self._session["id"]:
                raise ControllerError(409, "Camera observation belongs to another session.")
            if reported_epoch is not None and reported_epoch != self._session["epoch"]:
                raise ControllerError(409, "Camera observation belongs to an old session epoch.")
            current_revision = self._revision(self._latest_media)
            if revision > current_revision:
                raise ControllerError(409, "Camera observation refers to a future source revision.")
            source_record = self._revision_history.get(revision)
            if source_record is None or time.monotonic() - source_record[0] > self.camera_observation_ttl_s:
                raise ControllerError(409, "Camera observation source revision is unknown or expired.")
            if camera_id not in source_record[1]:
                raise ControllerError(409, "Camera observation source revision does not contain this camera.")
            previous = self._camera_observations.get(camera_id, {}).get("observation", {})
            changed = any(previous.get(key) != observation.get(key) for key in ("session_id", "epoch", "healthy", "speaking", "speaker_state"))
            self._camera_observations[camera_id] = {
                "camera_id": camera_id,
                "source_observation_revision": revision,
                "observation": self._bounded_json(observation),
            }
            self._camera_observation_mono[camera_id] = time.monotonic()
            if changed:
                self._add_event("camera_observation", "flower", "Camera AgentApp reported changed speaker or source health evidence.", camera_id)
        return {"ok": True, "camera_id": camera_id, "source_observation_revision": revision}

    async def agents_snapshot(self) -> dict[str, Any]:
        async with self._lock:
            if self._session_lock.locked():
                raise ControllerError(409, "Session transition in progress; sample again after it completes.")
            now = time.monotonic()
            return {
                "session_id": self._session["id"],
                "epoch": self._session["epoch"],
                "session": copy.deepcopy(self._session),
                "observation_revision": self._revision(self._latest_media),
                "model_epoch": self._model_epoch, "model": self.model_name,
                "override_epoch": self._override_epoch,
                "program": copy.deepcopy(self._program),
                "flower": self._flower_snapshot(now),
                "cameras": [self._compact_camera(camera) for camera in self._camera_map(self._latest_media).values()],
                "observations": self._recent_camera_observations(now),
            }







    async def connect_obs(self) -> dict[str, Any]:
        try:
            await self.obs_bridge.connect()
        except Exception as exc:
            async with self._lock:
                self._add_event("obs_error", "obs", f"OBS connection failed: {type(exc).__name__}: {exc}")
            raise ControllerError(502, "OBS connection failed; see /api/state.obs.error.") from exc
        async with self._lock:
            self._add_event("obs_connected", "obs", "Connected to OBS WebSocket and read actual server status.")
        return {"ok": True, "obs": self._obs_snapshot()}

    async def setup_obs(self) -> dict[str, Any]:
        try:
            result = await self.obs_bridge.setup_program_scene()
        except Exception as exc:
            async with self._lock:
                self._add_event("obs_error", "obs", f"OBS setup failed: {type(exc).__name__}: {exc}")
            raise ControllerError(502, "OBS scene setup failed; see /api/state.obs.error.") from exc
        async with self._lock:
            self._add_event("obs_setup", "obs", "Configured the NOESIS_Program browser source and preserved other scenes.")
        return {"ok": True, "obs": self._obs_snapshot(), "setup": result}

    async def get_frame(self, camera_id: str) -> bytes:
        if camera_id not in TARGET_IDS:
            raise ControllerError(404, "Unknown camera.")
        if camera_id == "slate":
            return self._slate_jpeg()
        try:
            payload = await asyncio.to_thread(self.media.frame_jpeg, camera_id)
        except Exception as exc:
            raise ControllerError(404, f"Frame unavailable: {type(exc).__name__}.") from exc
        if not isinstance(payload, bytes) or not payload:
            raise ControllerError(404, "Frame unavailable.")
        return payload

    async def get_program_frame(self) -> tuple[bytes, str]:
        async with self._lock:
            camera_id = self._program["camera_id"]
            input_mode = self._session["input_mode"]
            output_mode = self._session["output_mode"]
            status = self._session["status"]
            obs_state = self._obs_snapshot()
        payload = await self.get_frame(camera_id)
        labels = []
        if input_mode == "synthetic":
            labels.append("SYNTHETIC FEED")
        elif input_mode == "ami":
            labels.append("AMI REPLAY")
        if output_mode == "preview" and not obs_state.get("recording"):
            labels.append("PREVIEW Ã‚Â· NOT RECORDING")
        elif output_mode == "obs" and not obs_state.get("connected"):
            labels.append("OBS STATUS UNAVAILABLE")
        elif output_mode == "obs" and not obs_state.get("recording"):
            labels.append("OBS NOT RECORDING")
        if status == "paused":
            labels.append("REPLAY PAUSED")
        if not labels:
            return payload, camera_id
        return self._label_jpeg(payload, "  |  ".join(labels)), camera_id

    async def get_audio_path(self) -> Any:
        try:
            return await asyncio.to_thread(self.media.audio_path)
        except Exception as exc:
            raise ControllerError(404, f"Program audio unavailable: {type(exc).__name__}.") from exc

    async def _automatic_cut(
        self,
        target: str,
        *,
        source: str,
        reason: str,
        fallback: bool = False,
        expected_unhealthy: str | None = None,
        expected_epoch: int | None = None,
    ) -> bool:
        async with self._action_lock:
            async with self._lock:
                if self._session_lock.locked() or self._session["status"] != "running" or self._manual_latched:
                    return False
                if expected_epoch is not None and expected_epoch != self._session["epoch"]:
                    return False
                if expected_unhealthy:
                    current = self._program["camera_id"]
                    if current != expected_unhealthy:
                        return False
                if target != "slate" and not self._is_healthy(self._camera_map(self._latest_media).get(target), running=True):
                    return False
                if target == self._program["camera_id"]:
                    return False
                self._set_program_locked(target, reason, source=source, fallback=fallback)
                return True

    def _set_program_locked(self, camera_id: str, reason: str, *, source: str, fallback: bool = False, decision_source: str | None = None) -> None:
        prior = self._program["camera_id"]
        if prior == camera_id:
            return
        now_s = self._session["time_s"]
        self._program = {
            "camera_id": camera_id,
            "scene": "NOESIS_Program",
            "reason": reason,
            "last_cut_s": now_s,
            "decision_source": decision_source or source,
        }
        self._metrics["cuts"] += 1
        if fallback:
            self._metrics["fallbacks"] += 1
            started = self._unhealthy_since.pop(prior, None)
            if started is not None:
                self._metrics["last_recovery_ms"] = int(max(0.0, time.monotonic() - started) * 1000)
        self._add_event(
            "camera_cut" if not fallback else "fallback_cut",
            source,
            reason,
            camera_id,
        )
    def _clear_media_evidence_locked(self) -> None:
        self._invalidate_ai()
        self._revision_history.clear()
        self._revision_order.clear()
        self._camera_observations.clear()
        self._camera_observation_mono.clear()

    def _cancel_pending(self, reason: str) -> None:
        if self._ai_round:
            self._add_event("ai_round_cancelled", "validator", reason)
        self._invalidate_ai()

    def _remember_decision(self, decision_id: str) -> None:
        if len(self._seen_decisions) == self._seen_decisions.maxlen:
            old = self._seen_decisions[0]
            self._seen_decision_set.discard(old)
        self._seen_decisions.append(decision_id)
        self._seen_decision_set.add(decision_id)

    def _add_event(self, kind: str, source: str, message: str, camera_id: str | None = None) -> None:
        self._event_seq += 1
        event = {
            "id": self._event_seq,
            "session_id": self._session["id"],
            "time_s": self._as_float(self._session.get("time_s"), 0.0),
            "kind": kind,
            "source": source,
            "message": message[:500],
        }
        if camera_id:
            event["camera_id"] = camera_id
        self._events.append(event)

    def _camera_map(self, snapshot: dict[str, Any]) -> dict[str, dict[str, Any]]:
        return {
            str(camera.get("id")): camera
            for camera in snapshot.get("cameras", []) or []
            if isinstance(camera, dict) and camera.get("id")
        }

    def _is_healthy(self, camera: dict[str, Any] | None, *, running: bool) -> bool:
        if camera is None:
            return False
        explicit = camera.get("healthy")
        status = str(camera.get("status", "")).lower()
        if explicit is False or status in {"offline", "black", "frozen", "unavailable", "error"}:
            return False
        age_ms = camera.get("age_ms")
        if running and isinstance(age_ms, (int, float)) and age_ms > self.stale_source_ms:
            return False
        if explicit is True:
            return True
        return status in {"healthy", "online", "ok", "ready"}

    def _fallback_target(self, cameras: dict[str, dict[str, Any]]) -> str:
        for camera_id in ("corner", *CAMERA_IDS):
            if self._is_healthy(cameras.get(camera_id), running=True):
                return camera_id
        return "slate"


    @staticmethod
    def _compact_camera(camera: dict[str, Any]) -> dict[str, Any]:
        keys = ("id", "participant", "healthy", "status", "speaking", "speaker_state", "energy", "quality", "age_ms", "source_time_s")
        return {key: camera.get(key) for key in keys if key in camera}

    def _recent_camera_observations(self, now: float, max_revision: int | None = None) -> list[dict[str, Any]]:
        observations = []
        for camera_id, item in self._camera_observations.items():
            age_ms = int(max(0.0, now - self._camera_observation_mono.get(camera_id, now)) * 1000)
            revision = item.get("source_observation_revision", -1)
            if age_ms > self.camera_observation_ttl_s * 1000:
                continue
            if max_revision is not None and revision > max_revision:
                continue
            observations.append({**copy.deepcopy(item), "age_ms": age_ms})
        return observations


    @staticmethod
    def _revision(snapshot: dict[str, Any]) -> int:
        value = snapshot.get("observation_revision", 0)
        return value if isinstance(value, int) and not isinstance(value, bool) else 0

    def _remember_revision_locked(self, now: float) -> None:
        revision = self._revision(self._latest_media)
        current = self._camera_map(self._latest_media)
        if revision not in self._revision_history:
            if len(self._revision_order) == self._revision_order.maxlen:
                self._revision_history.pop(self._revision_order[0], None)
            self._revision_order.append(revision)
        self._revision_history[revision] = (now, copy.deepcopy(current))

    @staticmethod
    def _as_float(value: Any, default: float) -> float:
        try:
            result = float(value)
            return result if result == result and abs(result) != float("inf") else default
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _reason(value: Any, fallback: str) -> str:
        if not isinstance(value, str) or not value.strip():
            return fallback
        return value.strip()[:400]

    @staticmethod
    def _bounded_json(value: dict[str, Any]) -> dict[str, Any]:
        # The HTTP broker transports compact evidence only. This also prevents
        # accidental image/base64 payloads from filling the request queue.
        import json

        try:
            encoded = json.dumps(value, allow_nan=False, separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            raise ControllerError(422, "observation must contain finite JSON values.") from exc
        if len(encoded) > 16_000:
            raise ControllerError(413, "observation payload exceeds 16 KB.")
        return json.loads(encoded)

    def _require_running(self) -> None:
        if self._session["status"] != "running":
            raise ControllerError(409, "This operation requires a running session.")

    @staticmethod
    def _label_jpeg(payload: bytes, label: str) -> bytes:
        try:
            from io import BytesIO
            from PIL import Image, ImageDraw, ImageFont

            with Image.open(BytesIO(payload)) as original:
                image = original.convert("RGB")
            draw = ImageDraw.Draw(image)
            font = ImageFont.load_default()
            height = max(30, image.height // 18)
            draw.rectangle((0, 0, image.width, height), fill=(16, 22, 34))
            draw.text((12, 8), label, fill=(255, 218, 102), font=font)
            output = BytesIO()
            image.save(output, format="JPEG", quality=88)
            return output.getvalue()
        except Exception:
            return payload

    @staticmethod
    def _slate_jpeg() -> bytes:
        from io import BytesIO
        from PIL import Image, ImageDraw, ImageFont

        image = Image.new("RGB", (1280, 720), (16, 22, 34))
        draw = ImageDraw.Draw(image)
        font = ImageFont.load_default()
        draw.text((48, 320), "NOESIS Ã‚Â· NO USABLE CAMERA", fill=(255, 218, 102), font=font)
        output = BytesIO()
        image.save(output, format="JPEG", quality=88)
        return output.getvalue()


