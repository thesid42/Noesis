from __future__ import annotations

import asyncio
import threading
from io import BytesIO

import pytest
from PIL import Image

from noesis.controller import CAMERA_IDS, ControllerError, DirectorController


PARTICIPANTS = {"closeup1": "A", "closeup2": "C", "closeup3": "D", "closeup4": "B"}


class FakeMedia:
    def __init__(self) -> None:
        self.time_s = 0.0
        self.duration_s = 60.0
        self.status = "idle"
        self.input_mode = "synthetic"
        self.revision = 0
        self.snapshot_calls = 0
        self.cameras = {
            camera_id: {
                "id": camera_id,
                "name": camera_id,
                "participant": PARTICIPANTS.get(camera_id),
                "healthy": True,
                "status": "healthy",
                "speaking": False,
                "speaker_state": "quiet",
                "energy": 0.0,
                "quality": 0.8,
                "age_ms": 0,
                "source_time_s": 0.0,
                "frame_url": f"/api/frame/{camera_id}.jpg",
            }
            for camera_id in CAMERA_IDS
        }

    def start(self, input_mode="synthetic", start_s=0.0):
        self.input_mode = input_mode
        self.time_s = float(start_s)
        self.status = "stopped" if self.time_s >= self.duration_s else "running"
        self.revision += 1

    def pause(self):
        if self.status == "running":
            self.status = "paused"

    def resume(self):
        if self.time_s < self.duration_s:
            self.status = "running"

    def stop(self):
        self.status = "stopped"

    def seek(self, time_s):
        self.time_s = min(float(time_s), self.duration_s)
        if self.time_s >= self.duration_s:
            self.status = "stopped"
        self.revision += 1

    def inject_fault(self, camera_id, kind, duration_s=10.0):
        camera = self.cameras[camera_id]
        camera["status"] = "healthy" if kind == "none" else kind
        camera["healthy"] = kind == "none"
        self.revision += 1

    def snapshot(self):
        self.snapshot_calls += 1
        self.revision += 1
        return {
            "time_s": self.time_s,
            "duration_s": self.duration_s,
            "input_mode": self.input_mode,
            "status": self.status,
            "observation_revision": self.revision,
            "cameras": [dict(camera) for camera in self.cameras.values()],
        }

    def frame_jpeg(self, camera_id):
        image = Image.new("RGB", (32, 24), (32, 80, 120))
        output = BytesIO()
        image.save(output, format="JPEG")
        return output.getvalue()

    def audio_path(self):
        return None

    def availability(self):
        return {"ami_available": True, "missing_files": [], "credits": "AMI test fixture"}


class FakeOBS:
    def __init__(self, media=None):
        self.media = media
        self.connected = False
        self.recording = False
        self.calls = []
        self.media_status_during_record_start = None

    def snapshot(self):
        return {"connected": self.connected, "status": "recording" if self.recording else "disconnected", "recording": self.recording}

    async def connect(self):
        self.connected = True

    async def setup_program_scene(self):
        return {"scene_name": "NOESIS_Program", "browser_source_confirmed": True}

    async def select_program_scene(self, scene_name):
        self.calls.append(("scene", scene_name))

    async def start_recording(self):
        if self.media is not None:
            self.media_status_during_record_start = self.media.status
        self.recording = True

    async def stop_recording(self):
        self.recording = False

    async def poll_status(self):
        return self.snapshot()


async def make_controller(*, output_mode="preview", **kwargs):
    media = FakeMedia()
    obs = FakeOBS(media)
    controller = DirectorController(media, obs, **kwargs)
    await controller.session_start("synthetic", output_mode)
    return controller, media, obs


@pytest.mark.asyncio
async def test_pause_response_reports_the_actual_frozen_clock():
    controller, media, _ = await make_controller()
    # Media advances between the last controller poll and the pause operation.
    media.time_s = 2.75
    paused = await controller.session_pause()
    assert paused["session"]["time_s"] == 2.75
    await controller.tick()
    assert (await controller.get_state())["session"]["time_s"] == 2.75


@pytest.mark.asyncio
async def test_new_session_metrics_and_trace_exclude_previous_session():
    controller, _, _ = await make_controller()
    first_id = (await controller.get_state())["session"]["id"]
    await controller.manual_override("closeup1")
    assert (await controller.get_state())["metrics"]["cuts"] == 2
    await controller.session_stop()
    restarted = await controller.session_start("synthetic", "preview")
    assert restarted["session"]["id"] != first_id
    assert restarted["metrics"]["cuts"] == 1  # Initial usable camera, only.
    assert all(e["session_id"] == restarted["session"]["id"] for e in restarted["events"])


@pytest.mark.asyncio
async def test_paused_recording_label_is_truthful(monkeypatch):
    controller, _, obs = await make_controller(output_mode="obs")
    await controller.session_pause()
    labels = []
    monkeypatch.setattr(controller, "_label_jpeg", lambda payload, label: labels.append(label) or payload)
    await controller.get_program_frame()
    assert obs.recording
    assert "REPLAY PAUSED" in labels[0]
    assert "NOT RECORDING" not in labels[0]


@pytest.mark.asyncio
async def test_unconfirmed_recording_stop_is_retryable_after_reconnect():
    controller, _, obs = await make_controller(output_mode="obs")

    async def stop_only_when_connected():
        if not obs.connected:
            raise ConnectionError("OBS status is unavailable")
        obs.recording = False
        return {"stopped": True, "recording": False}

    obs.stop_recording = stop_only_when_connected
    obs.connected = False
    failed = await controller.session_stop()
    assert failed["session"]["status"] == "stopped"
    assert failed["obs"]["stop_pending"] is True and obs.recording
    assert "unconfirmed" in failed["events"][0]["message"]
    with pytest.raises(ControllerError, match="unconfirmed"):
        await controller.session_start("synthetic", "preview")
    await controller.connect_obs()
    retried = await controller.session_stop()
    assert retried["obs"]["stop_pending"] is False
    assert obs.recording is False


async def connect_flower(controller: DirectorController):
    await controller.heartbeat({
        "agent_id": "camera-closeup1",
        "role": "camera",
        "runtime": "Flower AgentApp",
        "run_id": "cam-run-1",
        "camera_id": "closeup1",
        "decision_mode": "rules",
    })
    await controller.heartbeat({
        "agent_id": "director-main",
        "role": "director",
        "runtime": "Flower AgentApp",
        "run_id": "director-run-1",
        "decision_mode": "rules",
    })


async def get_pending_for_closeup1(controller: DirectorController):
    media = controller.media
    media.cameras["closeup1"]["speaking"] = True
    media.cameras["closeup1"]["energy"] = 0.9
    await controller.tick()
    await asyncio.sleep(0.27)
    await controller.tick()
    pending = await controller.current_request()
    assert pending is not None
    return pending


def proposal_for(pending, **updates):
    body = {
        "schema_version": 1,
        "agent_id": "director-main",
        "request_id": pending["request_id"],
        "session_id": pending["session_id"],
        "epoch": pending["epoch"],
        "override_epoch": pending["override_epoch"],
        "observation_revision": pending["observation_revision"],
        "decision_id": "decision-1",
        "action": "switch",
        "camera_id": "closeup1",
        "reason": "The current speaker has sustained evidence.",
    }
    body.update(updates)
    return body


@pytest.mark.asyncio
async def test_proposal_checks_epoch_health_and_duplicate_decisions():
    controller, media, _obs = await make_controller(min_shot_s=0.0)
    await connect_flower(controller)
    pending = await get_pending_for_closeup1(controller)

    with pytest.raises(ControllerError, match="old session epoch"):
        await controller.accept_proposal(proposal_for(pending, epoch=pending["epoch"] - 1))
    assert (await controller.get_state())["program"]["camera_id"] == "corner"

    accepted = await controller.accept_proposal(proposal_for(pending))
    assert accepted["executed"] is True
    assert accepted["confirmed_by"] == "controller_program_clock"
    assert (await controller.get_state())["program"]["camera_id"] == "closeup1"
    with pytest.raises(ControllerError, match="no pending"):
        await controller.accept_proposal(proposal_for(pending, decision_id="decision-2"))

    # A new request targeting an unhealthy source is rejected even when the
    # request's original evidence was valid.
    media.cameras["closeup2"]["speaking"] = True
    media.cameras["closeup2"]["energy"] = 1.0
    media.cameras["closeup1"]["speaking"] = False
    await controller.tick()
    await asyncio.sleep(0.27)
    await controller.tick()
    next_pending = await controller.current_request()
    assert next_pending is not None
    media.cameras["closeup2"]["healthy"] = False
    media.cameras["closeup2"]["status"] = "offline"
    await controller.tick()
    with pytest.raises(ControllerError, match="unhealthy"):
        await controller.accept_proposal(proposal_for(next_pending, decision_id="decision-3", camera_id="closeup2"))

    state = await controller.get_state()
    assert state["metrics"]["rejected_proposals"] >= 3


@pytest.mark.asyncio
async def test_manual_latch_wins_against_late_reply_and_blocks_fallback():
    controller, media, _obs = await make_controller(min_shot_s=0.0)
    await connect_flower(controller)
    pending = await get_pending_for_closeup1(controller)
    await controller.manual_override("closeup4")

    late = proposal_for(pending)
    with pytest.raises(ControllerError, match="pending director request"):
        await controller.accept_proposal(late)
    media.cameras["closeup4"]["healthy"] = False
    media.cameras["closeup4"]["status"] = "offline"
    media.cameras["closeup1"]["speaking"] = True
    await controller.tick()

    state = await controller.get_state()
    assert state["mode"] == "manual"
    assert state["program"]["camera_id"] == "closeup4"
    assert not state["events"][0]["kind"] == "fallback_cut"


@pytest.mark.asyncio
async def test_health_failure_recovers_immediately_in_degraded_mode():
    controller, media, _obs = await make_controller()
    assert (await controller.get_state())["mode"] == "degraded"
    await controller.inject_fault("corner", "offline", 5.0)

    state = await controller.get_state()
    assert state["program"]["camera_id"] == "closeup1"
    assert state["metrics"]["fallbacks"] == 1
    assert state["events"][0]["kind"] == "fallback_cut"


@pytest.mark.asyncio
async def test_seek_invalidates_pending_request_and_preserves_one_clock():
    controller, media, _obs = await make_controller(min_shot_s=0.0)
    await connect_flower(controller)
    pending = await get_pending_for_closeup1(controller)
    old_epoch = pending["epoch"]

    state = await controller.session_seek(21.5)
    assert state["session"]["time_s"] == 21.5
    assert state["session"]["epoch"] == old_epoch + 1
    assert await controller.current_request() is None
    with pytest.raises(ControllerError, match="pending director request"):
        await controller.accept_proposal(proposal_for(pending))


@pytest.mark.asyncio
async def test_rules_only_flower_heartbeats_are_reported_without_llm_claim():
    controller, _media, _obs = await make_controller()
    await connect_flower(controller)
    flower = (await controller.get_state())["flower"]
    assert flower["status"] == "connected"
    assert flower["decision_modes"]["director"] == "rules"
    assert flower["model_status"] == "not_configured"
    assert (await controller.get_state())["mode"] == "autopilot"


@pytest.mark.asyncio
async def test_local_baseline_does_not_wait_for_flower_or_model():
    controller, media, _obs = await make_controller(min_shot_s=0.0, speaker_confirm_s=0.05)
    media.cameras["closeup2"]["speaking"] = True
    media.cameras["closeup2"]["energy"] = 1.0
    await controller.tick()
    await asyncio.sleep(0.08)
    await controller.tick()

    state = await controller.get_state()
    assert state["mode"] == "degraded"
    assert state["program"]["camera_id"] == "closeup2"
    assert state["program"]["reason"].endswith("local directing baseline.")


@pytest.mark.asyncio
async def test_camera_observation_accepts_recent_source_revision_and_rejects_future():
    controller, _media, _obs = await make_controller()
    snapshot = await controller.agents_snapshot()
    state = await controller.get_state()
    assert snapshot["session"] == state["session"]
    assert snapshot["session_id"] == snapshot["session"]["id"]
    assert snapshot["epoch"] == snapshot["session"]["epoch"]
    assert snapshot["cameras"][0]["speaker_state"] == "quiet"
    assert snapshot["cameras"][0]["source_time_s"] == 0.0
    revision = snapshot["observation_revision"]
    # The shared clock advances between the camera read and its HTTP POST.
    await controller.tick()
    accepted = await controller.camera_observation({
        "camera_id": "closeup1",
        "source_observation_revision": revision,
        "observation": {"speaking": False, "decode_ok": True},
    })
    assert accepted["source_observation_revision"] == revision
    with pytest.raises(ControllerError, match="future source revision"):
        await controller.camera_observation({
            "camera_id": "closeup1",
            "source_observation_revision": 10**9,
            "observation": {"speaking": False},
        })


@pytest.mark.asyncio
async def test_stop_invalidates_decisions_and_stops_obs_recording():
    controller, _media, obs = await make_controller(output_mode="obs")
    assert obs.recording
    assert obs.media_status_during_record_start == "paused"
    await controller.session_stop()
    assert not obs.recording
    assert (await controller.get_state())["session"]["status"] == "stopped"


@pytest.mark.asyncio
async def test_start_at_or_past_duration_is_rejected_before_obs_recording():
    media = FakeMedia()
    media.duration_s = 10.0
    obs = FakeOBS(media)
    controller = DirectorController(media, obs)

    with pytest.raises(ControllerError, match="at or past the end"):
        await controller.session_start("synthetic", "obs", start_s=10.0)

    state = await controller.get_state()
    assert state["session"]["status"] == "stopped"
    assert state["session"]["time_s"] == 10.0
    assert not obs.recording
    assert obs.media_status_during_record_start is None


@pytest.mark.asyncio
async def test_natural_media_eof_stops_session_recording_and_pending_work():
    controller, media, obs = await make_controller(output_mode="obs", min_shot_s=0.0)
    await connect_flower(controller)
    pending = await get_pending_for_closeup1(controller)
    assert pending is not None and obs.recording

    media.time_s = media.duration_s
    media.status = "stopped"
    await controller.tick()

    state = await controller.get_state()
    assert state["session"]["status"] == "stopped"
    assert state["session"]["time_s"] == state["session"]["duration_s"]
    assert not obs.recording
    assert await controller.current_request() is None
    assert any(event["kind"] == "session_ended" for event in state["events"])


@pytest.mark.asyncio
async def test_seek_to_or_past_duration_finalizes_even_when_paused():
    controller, media, obs = await make_controller(output_mode="obs")
    await controller.session_pause()

    state = await controller.session_seek(media.duration_s + 20)
    assert state["session"]["time_s"] == media.duration_s
    assert state["session"]["status"] == "stopped"
    assert not obs.recording
    assert any(event["kind"] == "session_ended" for event in state["events"])


@pytest.mark.asyncio
async def test_obs_poll_is_separate_and_tracks_manual_stop_and_closes_cleanly():
    controller, media, obs = await make_controller(output_mode="obs")
    before = media.snapshot_calls
    await controller.start_background(interval_s=0.005, obs_interval_s=0.01)
    controller_task, obs_task = controller._task, controller._obs_task
    await asyncio.sleep(0.03)
    assert media.snapshot_calls > before
    obs.recording = False  # An operator stopped it directly in OBS.
    await asyncio.sleep(0.025)
    state = await controller.get_state()
    assert state["obs"]["recording"] is False
    assert any(event["kind"] == "obs_recording_stopped_externally" for event in state["events"])
    await controller.close()
    assert controller._task is None and controller._obs_task is None
    assert controller_task.done() and obs_task.done()


@pytest.mark.asyncio
async def test_inflight_seek_rejects_old_decisions_and_cross_epoch_observations():
    controller, media, _obs = await make_controller(min_shot_s=0.0)
    await connect_flower(controller)
    pending = await get_pending_for_closeup1(controller)
    snapshot = await controller.agents_snapshot()
    began, release = threading.Event(), threading.Event()
    seek = media.seek

    def slow_seek(value):
        began.set()
        assert release.wait(3)
        seek(value)

    media.seek = slow_seek
    transition = asyncio.create_task(controller.session_seek(20.0))
    try:
        assert await asyncio.to_thread(began.wait, 2)
        with pytest.raises(ControllerError, match="transition"):
            await controller.accept_proposal(proposal_for(pending))
        with pytest.raises(ControllerError, match="transition"):
            await controller.agents_snapshot()
        stale = {"camera_id": "closeup1", "source_observation_revision": snapshot["observation_revision"],
                 "observation": {"session_id": pending["session_id"], "epoch": pending["epoch"] + 1, "speaking": True}}
        with pytest.raises(ControllerError, match="transition"):
            await controller.camera_observation(stale)
    finally:
        release.set()
        await transition
    with pytest.raises(ControllerError, match="unknown or expired"):
        await controller.camera_observation(stale)
    assert (await controller.get_state())["program"]["camera_id"] == "corner"


@pytest.mark.asyncio
async def test_quiet_camera_recovery_leaves_slate_without_speaker_evidence():
    controller, media, _obs = await make_controller()
    for camera in media.cameras.values():
        camera.update(healthy=False, status="offline", speaking=False)
    await controller.tick()
    assert (await controller.get_state())["program"]["camera_id"] == "slate"
    media.cameras["corner"].update(healthy=True, status="healthy")
    await controller.tick()
    assert (await controller.get_state())["program"]["camera_id"] == "corner"
