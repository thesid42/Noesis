from __future__ import annotations

import time

from noesis.media import CAMERA_IDS, MediaEngine


def _wait_for(predicate, timeout_s: float = 2.0):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.01)
    raise AssertionError("condition did not become true before timeout")


def test_delayed_broadcast_uses_captured_past_frames_and_freezes_on_pause():
    media = MediaEngine(output_delay_s=0.18)
    try:
        first = media.start("synthetic")
        assert first["broadcast"]["phase"] == "buffering"
        assert media.frame_at("closeup1", 0.0) is None

        def ready_with_frames():
            state = media.broadcast_snapshot()
            if state["ready"] and all(
                media.frame_at(camera_id, state["time_s"]) is not None for camera_id in CAMERA_IDS
            ):
                return state
            return None

        playing = _wait_for(ready_with_frames)
        assert playing["phase"] == "playing"
        assert playing["time_s"] <= playing["capture_head_s"]
        assert all(media.frame_at(camera_id, playing["time_s"]) for camera_id in CAMERA_IDS)

        media.pause()
        paused = media.broadcast_snapshot()
        time.sleep(0.12)
        later = media.broadcast_snapshot()
        assert paused["phase"] == "paused"
        assert later["time_s"] == paused["time_s"]
        assert later["session_time_s"] == paused["session_time_s"]

        media.resume()
        resumed = _wait_for(lambda: (
            state if (state := media.broadcast_snapshot())["session_time_s"] > paused["session_time_s"] + 0.04 else None
        ))
        assert abs((resumed["session_time_s"] - resumed["time_s"]) - 0.18) < 0.06
    finally:
        media.close()


def test_seek_starts_a_new_buffer_epoch_and_stop_clears_it():
    media = MediaEngine(output_delay_s=0.08)
    try:
        media.start("synthetic")
        _wait_for(lambda: media.broadcast_snapshot()["capture_head_s"] is not None)
        epoch = media.broadcast_snapshot()["buffer_epoch"]

        media.seek(10.0)
        sought = media.broadcast_snapshot()
        assert sought["buffer_epoch"] == epoch + 1
        assert media.frame_at("closeup1", 9.99) is None
        _wait_for(lambda: media.frame_at("closeup1", 10.2) is not None)

        media.stop()
        stopped = media.broadcast_snapshot()
        assert stopped["buffer_epoch"] == epoch + 2
        assert stopped["phase"] == "stopped"
        assert stopped["buffer_bytes"] == 0
        assert media.frame_at("closeup1", 20.0) is None
    finally:
        media.close()


def test_end_of_media_drains_the_configured_output_delay():
    media = MediaEngine(output_delay_s=0.1)
    try:
        media.start("synthetic")
        with media._lock:
            media._duration_s = 0.3

        draining = _wait_for(
            lambda: state if (state := media.broadcast_snapshot())["phase"] == "draining" else None,
            timeout_s=1.0,
        )
        assert draining["session_time_s"] == 0.3
        ended = _wait_for(
            lambda: state if (state := media.broadcast_snapshot())["phase"] == "ended" else None,
            timeout_s=1.0,
        )
        assert ended["time_s"] == 0.3
    finally:
        media.close()


def test_short_tail_waits_full_delay_then_drains_for_start_and_near_end_seek():
    delay_s = 0.3
    cases = ((0.0, 0.12), (0.8, 1.0))
    for start_s, duration_s in cases:
        media = MediaEngine(output_delay_s=delay_s)
        try:
            media.start("synthetic", start_s=start_s)
            with media._lock:
                media._duration_s = duration_s
            if start_s:
                _wait_for(lambda: media.broadcast_snapshot()["capture_head_s"] is not None)
                media.seek(start_s)

            started = time.monotonic()
            draining = _wait_for(
                lambda: state if (state := media.broadcast_snapshot())["phase"] == "draining" else None,
                timeout_s=1.0,
            )
            assert not draining["ready"]
            if start_s == 0.0:
                media.pause()
                paused = media.broadcast_snapshot()
                time.sleep(0.06)
                still_paused = media.broadcast_snapshot()
                assert paused["phase"] == "paused"
                assert still_paused["time_s"] == paused["time_s"]
                media.resume()

            ready = _wait_for(
                lambda: state if (state := media.broadcast_snapshot())["ready"] else None,
                timeout_s=1.0,
            )
            assert time.monotonic() - started >= delay_s - 0.04
            assert ready["phase"] == "draining"
            assert all(media.frame_at(camera_id, ready["time_s"]) for camera_id in CAMERA_IDS)

            ended = _wait_for(
                lambda: state if (state := media.broadcast_snapshot())["phase"] == "ended" else None,
                timeout_s=1.0,
            )
            expected_elapsed = delay_s + duration_s - start_s
            assert time.monotonic() - started >= expected_elapsed - 0.06
            assert ended["time_s"] == duration_s
        finally:
            media.close()


def test_missing_ami_cameras_do_not_hold_the_delayed_clock_in_buffering(tmp_path):
    media = MediaEngine(data_dir=tmp_path, output_delay_s=0.08)
    try:
        media.start("ami")
        ready = _wait_for(lambda: (
            state if (state := media.broadcast_snapshot())["ready"] else None
        ))
        assert ready["phase"] == "playing"
        assert not any(ready["camera_ready"].values())
        assert all(media.broadcast_frame(camera_id) for camera_id in CAMERA_IDS)
    finally:
        media.close()


def test_camera_age_tracks_capture_clock_and_stays_frozen_while_paused():
    media = MediaEngine(output_delay_s=1.0)
    try:
        with media._lock:
            media._duration_s = 20.0
            media._base_time = 1.0
            media._anchor_mono = time.monotonic()
            media._status = "running"
            media._last_analysis_mono = time.monotonic()
            media._camera_info["closeup1"]["_last"] = {
                "healthy": True,
                "status": "synthetic",
                "source_time_s": 0.8,
                "age_ms": 200.0,
            }

        first = media.snapshot()
        time.sleep(0.05)
        second = media.snapshot()
        first_age = next(camera["age_ms"] for camera in first["cameras"] if camera["id"] == "closeup1")
        second_age = next(camera["age_ms"] for camera in second["cameras"] if camera["id"] == "closeup1")
        assert second_age >= first_age + 30.0
        assert second["observation_age_ms"] >= first["observation_age_ms"] + 30.0

        paused = media.pause()
        paused_age = next(camera["age_ms"] for camera in paused["cameras"] if camera["id"] == "closeup1")
        time.sleep(0.05)
        still_paused = media.snapshot()
        still_paused_age = next(
            camera["age_ms"] for camera in still_paused["cameras"] if camera["id"] == "closeup1"
        )
        assert abs(still_paused_age - paused_age) < 1.0
        assert still_paused["observation_age_ms"] > paused["observation_age_ms"] + 30.0
    finally:
        media.close()
