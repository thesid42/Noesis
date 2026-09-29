from __future__ import annotations

import asyncio
import threading

import pytest

from noesis.controller import ControllerError, DirectorController
from test_controller import FakeMedia, FakeOBS


class DelayedMedia(FakeMedia):
    def __init__(self):
        super().__init__()
        self.output_time = 0.0
        self.ready = False
        self.finished = False
        self.buffered_faults = set()

    def broadcast_snapshot(self):
        return {"time_s": self.output_time, "delay_s": 5.0, "ready": self.ready,
                "phase": "ended" if self.finished else "playing" if self.ready else "buffering"}

    def snapshot(self):
        return {**super().snapshot(), "broadcast": self.broadcast_snapshot()}

    def buffered_camera(self, camera_id, _time_s):
        return {"healthy": camera_id not in self.buffered_faults, "age_ms": 0,
                "buffer_age_ms": 0, "status": "healthy" if camera_id not in self.buffered_faults else "offline"}

    def frame_at(self, camera_id, _time_s):
        return self.frame_jpeg(camera_id)


async def setup(output="preview"):
    media = DelayedMedia()
    obs = FakeOBS(media)
    controller = DirectorController(media, obs)
    await controller.session_start("synthetic", output)
    media.time_s, media.output_time, media.ready = 6.0, 1.0, True
    await controller.tick()
    return controller, media, obs


def decision(at=8.0, camera="closeup1"):
    return {"media_time_s": at, "response_id": "future-choice",
            "result": {"action": "switch", "camera_id": camera, "reason": "AI choice for the pinned source time."}}


@pytest.mark.asyncio
async def test_future_cut_applies_at_program_time_not_model_arrival():
    controller, media, _ = await setup()
    before = controller._metrics["cuts"]
    outcome = controller._apply_ai_director_locked(decision())
    assert outcome["scheduled"] and not outcome["executed"]
    assert controller._program["camera_id"] == "corner"
    assert controller._metrics["cuts"] == before
    assert controller._ai_program_at_locked(8.0)["camera_id"] == "closeup1"
    media.time_s, media.output_time = 12.9, 7.9
    await controller.tick()
    assert controller._program["camera_id"] == "corner"
    media.time_s, media.output_time = 13.0, 8.0
    await controller.tick()
    assert controller._program["camera_id"] == "closeup1"
    assert controller._program["last_cut_s"] == 8.0
    assert controller._metrics["ai_cuts"] == 1
    assert controller._shot_history[-1]["end_s"] == 8.0


@pytest.mark.asyncio
async def test_late_choice_cannot_rewrite_aired_footage_and_manual_clears_queue():
    controller, media, _ = await setup()
    with pytest.raises(ControllerError, match="already aired"):
        controller._apply_ai_director_locked(decision(at=0.1))
    assert controller._metrics["ai_deadline_misses"] == 1
    assert not controller._scheduled_cuts
    controller._apply_ai_director_locked(decision())
    await controller.manual_override("closeup2")
    assert not controller._scheduled_cuts
    media.time_s, media.output_time = 14.0, 9.0
    await controller.tick()
    assert controller._program["camera_id"] == "closeup2"


@pytest.mark.asyncio
async def test_capture_head_fault_does_not_cut_healthy_buffered_frames():
    controller, media, _ = await setup()
    media.cameras["corner"].update(healthy=False, status="offline")
    await controller.tick()
    assert controller._program["camera_id"] == "corner"
    media.buffered_faults.add("corner")
    await controller.tick()
    assert controller._program["camera_id"] != "corner"


@pytest.mark.asyncio
async def test_obs_recording_continues_until_delayed_eof():
    controller, media, obs = await setup("obs")
    media.time_s, media.output_time = 60.0, 55.0
    await controller.tick()
    assert controller._session["status"] == "running"
    assert obs.recording
    media.output_time, media.finished = 60.0, True
    await controller.tick()
    assert controller._session["status"] == "stopped"
    assert not obs.recording


@pytest.mark.asyncio
async def test_frame_in_flight_cannot_restore_a_previous_seek_epoch():
    controller, media, _ = await setup()
    entered, release = threading.Event(), threading.Event()
    original = media.broadcast_snapshot

    def slow_snapshot():
        captured = {**original(), "buffer_epoch": 1}
        entered.set()
        release.wait(2)
        return captured

    media.broadcast_snapshot = slow_snapshot
    frame = asyncio.create_task(controller.get_program_frame())
    assert await asyncio.to_thread(entered.wait, 2)
    async with controller._lock:
        controller._session["epoch"] += 1
        controller._broadcast = {**original(), "buffer_epoch": 2, "time_s": 20}
    release.set()
    _, camera = await frame
    assert camera == "slate"
    assert controller._broadcast["buffer_epoch"] == 2
    assert controller._broadcast["time_s"] == 20


@pytest.mark.asyncio
async def test_repeated_revision_does_not_refresh_its_acquisition_time():
    controller, _, _ = await setup()
    revision = controller._revision(controller._latest_media)
    original_time = controller._revision_history[revision][0]
    controller._remember_revision_locked(original_time + 60)
    assert controller._revision_history[revision][0] == original_time
