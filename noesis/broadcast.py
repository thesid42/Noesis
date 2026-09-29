"""Delayed program scheduling. AI choices are applied only as their footage airs."""
from __future__ import annotations

import copy
import math
from collections import deque


class BroadcastMixin:
    def _init_broadcast(self, perception=None):
        self.perception = perception
        self._broadcast = {}
        self._scheduled_cuts = []
        self._shot_history = deque(maxlen=32)
        self._shot_started_s = None
        self._frame_output_count = 0
        self._metrics.update(ai_scheduled=0, ai_deadline_misses=0, ai_scheduled_rejected=0)

    def _clear_scheduled_cuts_locked(self):
        if hasattr(self, "_scheduled_cuts"):
            self._scheduled_cuts.clear()

    def _broadcast_time_locked(self):
        return float(self._broadcast.get("time_s", self._session["time_s"]))

    def _broadcast_active_locked(self):
        return float(self._broadcast.get("delay_s", 0)) > 0

    def _program_cameras_locked(self):
        if not self._broadcast_active_locked():
            return self._camera_map(self._latest_media)
        if not self._broadcast.get("ready"):
            return {}
        return self._ai_target_cameras_locked(self._broadcast_time_locked())

    def _ai_target_cameras_locked(self, target_time_s):
        if not self._broadcast_active_locked():
            return self._camera_map(self._latest_media)
        getter = getattr(self.media, "buffered_camera", None)
        if not callable(getter):
            return {}
        cameras = {}
        for camera_id in ("closeup1", "closeup2", "closeup3", "closeup4", "corner"):
            camera = getter(camera_id, target_time_s)
            if isinstance(camera, dict):
                cameras[camera_id] = {**camera, "id": camera_id,
                                      "age_ms": camera.get("buffer_age_ms", 0)}
        return cameras

    def _ai_program_at_locked(self, target_time_s):
        program = copy.deepcopy(self._program)
        for cut in self._scheduled_cuts:
            if cut["target_media_time_s"] <= target_time_s:
                program.update(camera_id=cut["camera_id"], reason=cut["reason"],
                               last_cut_s=cut["target_media_time_s"])
        return program

    def _apply_ai_director_locked(self, body):
        from .controller import ControllerError
        clock = getattr(self.media, "broadcast_snapshot", None)
        if callable(clock):
            self._broadcast = clock()
        result = body["result"]
        target_s = float(body["media_time_s"])
        if not math.isfinite(target_s):
            raise ControllerError(422, "Director target time must be finite.")
        if not self._broadcast_active_locked():
            changed = result["action"] == "switch" and result["camera_id"] != self._program["camera_id"]
            if changed:
                self._set_program_locked(result["camera_id"], result["reason"], source="ai_director", decision_source="model")
                self._program["response_id"] = body["response_id"]
                self._metrics["ai_cuts"] += 1
            return {"ok": True, "executed": True, "scheduled": False, "action": result["action"],
                    "program": copy.deepcopy(self._program), "target_media_time_s": target_s}
        # There is no retroactive cut. The caller can increase delay while stopped.
        if self._broadcast.get("ready") and target_s < self._broadcast_time_locked() - 1 / 30:
            self._metrics["ai_deadline_misses"] += 1
            self._add_event("broadcast_deadline_missed", "validator", "AI decision arrived after its footage aired; holding the current shot.")
            raise ControllerError(409, "Director target already aired; request fresh evidence.")
        projected = self._ai_program_at_locked(target_s)
        if result["action"] == "hold" and result["camera_id"] != projected["camera_id"]:
            raise ControllerError(409, "Hold decision no longer matches the scheduled program.")
        if len(self._scheduled_cuts) >= 64:
            raise ControllerError(409, "Broadcast schedule is full.")
        if result["action"] == "switch":
            self._scheduled_cuts.append({
                "camera_id": result["camera_id"], "reason": result["reason"],
                "response_id": body["response_id"], "target_media_time_s": target_s,
                "session_id": self._session["id"], "epoch": self._session["epoch"],
                "override_epoch": self._override_epoch, "model_epoch": self._model_epoch,
            })
            self._scheduled_cuts.sort(key=lambda cut: cut["target_media_time_s"])
            self._metrics["ai_scheduled"] += 1
        self._add_event("ai_cut_scheduled" if result["action"] == "switch" else "ai_director_hold", "ai_director", result["reason"], result["camera_id"])
        return {"ok": True, "executed": False, "scheduled": result["action"] == "switch", "action": result["action"],
                "program": copy.deepcopy(self._program), "target_media_time_s": target_s}

    def _sync_broadcast_locked(self, broadcast=None):
        if isinstance(broadcast, dict):
            incoming_epoch = broadcast.get("buffer_epoch", 0)
            current_epoch = self._broadcast.get("buffer_epoch", 0)
            if incoming_epoch < current_epoch:
                return
            if incoming_epoch == current_epoch and broadcast.get("time_s", 0) < self._broadcast.get("time_s", 0):
                return
            self._broadcast = dict(broadcast)
        if not self._broadcast_active_locked() or not self._broadcast.get("ready"):
            return
        if self._session["status"] != "running" or self._session_lock.locked():
            return
        cameras = self._program_cameras_locked()
        while self._scheduled_cuts and self._scheduled_cuts[0]["target_media_time_s"] <= self._broadcast_time_locked():
            cut = self._scheduled_cuts.pop(0)
            valid = not self._manual_latched and all(cut[key] == value for key, value in (
                ("session_id", self._session["id"]), ("epoch", self._session["epoch"]),
                ("override_epoch", self._override_epoch), ("model_epoch", self._model_epoch)))
            target = cut["camera_id"]
            healthy = self._is_healthy(cameras.get(target), running=True)
            if target == "slate":
                healthy = not any(self._is_healthy(c, running=True) for c in cameras.values())
            if not valid or not healthy:
                self._metrics["ai_scheduled_rejected"] += 1
                continue
            if target != self._program["camera_id"]:
                self._set_program_locked(target, cut["reason"], source="ai_director", decision_source="model")
                self._program["response_id"] = cut["response_id"]
                self._metrics["ai_cuts"] += 1
        # A capture-head fault must not prematurely switch buffered good footage.
        current = self._program["camera_id"]
        if not self._manual_latched and not self._is_healthy(cameras.get(current), running=True):
            target = self._fallback_target(cameras)
            if current != target:
                self._set_program_locked(target, "Selected a healthy buffered source for broadcast.", source="health", fallback=current != "slate")

    def _record_aired_shot_locked(self, prior, now_s):
        if self._shot_started_s is not None and now_s >= self._shot_started_s:
            self._shot_history.append({"camera_id": prior["camera_id"], "start_s": self._shot_started_s,
                                       "end_s": now_s, "duration_s": round(now_s - self._shot_started_s, 3),
                                       "reason": str(prior.get("reason", ""))[:180],
                                       "source": prior.get("decision_source", "unknown")})
        self._shot_started_s = now_s

    def _editorial_context_locked(self, target_time_s=None):
        target = self._session["time_s"] if target_time_s is None else target_time_s
        perception = self.perception.snapshot(self._session["id"], self._session["epoch"], target) if self.perception else {}
        history = copy.deepcopy(list(self._shot_history)[-8:])
        if self._shot_started_s is not None and self._broadcast.get("ready", True):
            now_s = self._broadcast_time_locked()
            history.append({"camera_id": self._program["camera_id"], "start_s": self._shot_started_s,
                            "end_s": now_s, "duration_s": max(0, now_s - self._shot_started_s),
                            "source": self._program.get("decision_source", "unknown"), "ongoing": True})
        return {"broadcast": copy.deepcopy(self._broadcast), "shot_history": history,
                "pending_cuts": [{"camera_id": c["camera_id"], "target_media_time_s": c["target_media_time_s"]} for c in self._scheduled_cuts[-8:]],
                "perception": perception}
