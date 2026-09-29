from __future__ import annotations

import io
from pathlib import Path
import threading
import time
from types import SimpleNamespace

import cv2
import numpy as np
import soundfile as sf

from noesis.perception import BackgroundPerception, MAX_TRANSCRIPT_CHARS, MAX_TRANSCRIPT_SEGMENTS, _measure_jpeg


class _Media:
    def __init__(self, audio_path: Path | None = None, *, frame_delay: float = 0.0,
                 future_frame: bool = False, missing_frame: bool = False):
        self.path = audio_path
        self.frame_delay = frame_delay
        self.future_frame = future_frame
        self.missing_frame = missing_frame
        self.frame_calls = 0

    def audio_path(self):
        return self.path

    def frame_at(self, camera_id: str, time_s: float):
        self.frame_calls += 1
        if self.frame_delay:
            time.sleep(self.frame_delay)
        frame = np.full((96, 160, 3), 86, dtype=np.uint8)
        cv2.putText(frame, camera_id, (8, 50), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (220, 220, 220), 1)
        return cv2.imencode(".jpg", frame)[1].tobytes()

    def buffered_camera(self, _camera_id: str, time_s: float):
        if self.missing_frame:
            return None
        source_time = time_s + 0.1 if self.future_frame else max(0.0, time_s - 0.1)
        return {"frame_time_s": max(0.0, time_s - 0.05), "source_time_s": source_time}


def _audio_file(path: Path, seconds: int = 24) -> Path:
    rate = 16_000
    chunk_samples = rate * 4
    samples = np.empty(rate * seconds, dtype=np.float32)
    for index, start in enumerate(range(0, len(samples), chunk_samples)):
        samples[start:start + chunk_samples] = (index + 1) / 10.0
    sf.write(path, samples, rate)
    return path


def _wait_until(predicate, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    assert predicate(), "background work did not complete before timeout"


class _BlockingASR:
    def __init__(self, entered: threading.Event, release: threading.Event, starts: list[float], text: str = " causal words ", segment_count: int = 1):
        self.entered = entered
        self.release = release
        self.starts = starts
        self.text = text
        self.segment_count = segment_count
        self.sample_lengths: list[int] = []

    def transcribe(self, samples, **kwargs):
        self.starts.append(round(float(samples[0]), 1) if len(samples) else -1.0)
        self.sample_lengths.append(len(samples))
        self.entered.set()
        self.release.wait(2.0)
        return iter([
            SimpleNamespace(start=0.1 + index * 0.3, end=0.25 + index * 0.3, text=self.text)
            for index in range(self.segment_count)
        ]), None


def test_latest_only_queue_replaces_pending_and_results_are_causal(tmp_path: Path):
    entered, release = threading.Event(), threading.Event()
    starts: list[float] = []
    model = _BlockingASR(entered, release, starts)
    media = _Media(_audio_file(tmp_path / "mix.wav"))
    worker = BackgroundPerception(media, tmp_path / "model", asr_factory=lambda _path: model,
                                  visual_enabled=False)
    try:
        worker.observe({"time_s": 0.0, "input_mode": "ami"}, media, "s1", 1)
        worker.observe({"time_s": 4.0, "input_mode": "ami"}, media, "s1", 1)
        assert entered.wait(1.0)
        worker.observe({"time_s": 8.0, "input_mode": "ami"}, media, "s1", 1)
        worker.observe({"time_s": 12.0, "input_mode": "ami"}, media, "s1", 1)
        release.set()
        _wait_until(lambda: len(worker.snapshot("s1", 1, 20.0)["transcript"]["segments"]) >= 2)

        assert starts == [0.1, 0.3]  # First active job plus only the latest queued job.
        result = worker.snapshot("s1", 1, 20.0)
        assert result["status"]["speech"]["queue_replaced"] >= 1
        assert [item["source_start_s"] for item in result["transcript"]["segments"]] == [0.1, 8.1]
        assert result["transcript"]["segments"][0]["speaker_id"] is None
        assert result["transcript"]["segments"][0]["untrusted"] is True
        # Even a completed transcript is hidden when the requested source cutoff is earlier.
        early = worker.snapshot("s1", 1, 0.2)
        assert early["transcript"]["segments"] == []
    finally:
        release.set()
        worker.close()


def test_epoch_reset_discards_inflight_asr_and_bounds_context(tmp_path: Path):
    entered, release = threading.Event(), threading.Event()
    starts: list[float] = []
    model = _BlockingASR(entered, release, starts, text="x" * 180, segment_count=10)
    media = _Media(_audio_file(tmp_path / "mix.wav"))
    worker = BackgroundPerception(media, tmp_path / "model", asr_factory=lambda _path: model,
                                  visual_enabled=False)
    try:
        worker.observe({"time_s": 0.0, "input_mode": "ami"}, media, "s1", 1)
        worker.observe({"time_s": 4.0, "input_mode": "ami"}, media, "s1", 1)
        assert entered.wait(1.0)
        worker.reset("s1", 2)
        release.set()
        assert worker.snapshot("s1", 2, 10.0)["transcript"]["segments"] == []

        # New epoch begins at its current source position and cannot inherit old text.
        worker.observe({"time_s": 8.0, "input_mode": "ami"}, media, "s1", 2)
        worker.observe({"time_s": 12.0, "input_mode": "ami"}, media, "s1", 2)
        _wait_until(lambda: worker.snapshot("s1", 2, 20.0)["status"]["speech"]["state"] == "ready")
        result = worker.snapshot("s1", 2, 20.0)
        assert len(result["transcript"]["segments"]) <= MAX_TRANSCRIPT_SEGMENTS
        assert len(result["transcript"]["text"]) <= MAX_TRANSCRIPT_CHARS
        assert all(row["source_end_s"] <= 20.0 for row in result["transcript"]["segments"])
        assert worker.snapshot("s1", 1, 20.0)["transcript"]["segments"] == []
    finally:
        release.set()
        worker.close()


def test_visual_work_is_background_measured_and_rejects_future_frames():
    media = _Media(frame_delay=0.25)
    worker = BackgroundPerception(
        media,
        speech_enabled=False,
        visual_enabled=True,
        visual_processor=lambda _jpeg: {"blur_score": 1.25, "luma_mean": 86.0, "face_count": 0, "face_status": "measured"},
    )
    try:
        start = time.monotonic()
        worker.observe({"time_s": 2.0, "input_mode": "ami"}, media, "s2", 3)
        assert time.monotonic() - start < 0.1
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            state = worker.snapshot("s2", 3, 2.0)["status"]["visual"]["state"]
            if state in {"ready", "error"}:
                break
            time.sleep(0.01)
        result = worker.snapshot("s2", 3, 2.0)
        assert result["status"]["visual"]["state"] == "ready", result["status"]["visual"]
        assert len(result["visual_observations"]) == 5
        assert all(row["source_time_s"] <= 2.0 for row in result["visual_observations"])
        assert all(row["frame_time_s"] <= 2.0 for row in result["visual_observations"])
        assert all(row["luma_mean"] is not None and row["blur_score"] is not None for row in result["visual_observations"])
        pixels = np.full((64, 96, 3), 80, dtype=np.uint8)
        jpeg = cv2.imencode(".jpg", pixels)[1].tobytes()
        measured = _measure_jpeg(jpeg)
        assert measured["luma_mean"] is not None and measured["blur_score"] is not None
    finally:
        worker.close()

    future_media = _Media(future_frame=True)
    future_worker = BackgroundPerception(future_media, speech_enabled=False, visual_enabled=True)
    try:
        future_worker.observe({"time_s": 2.0, "input_mode": "ami"}, future_media, "s3", 1)
        _wait_until(lambda: future_worker.snapshot("s3", 1, 2.0)["status"]["visual"]["state"] == "ready")
        assert future_worker.snapshot("s3", 1, 2.0)["visual_observations"] == []
    finally:
        future_worker.close()

    missing_media = _Media(missing_frame=True)
    missing_worker = BackgroundPerception(missing_media, speech_enabled=False, visual_enabled=True)
    try:
        missing_worker.observe({"time_s": 2.0, "input_mode": "ami"}, missing_media, "s4", 1)
        _wait_until(lambda: missing_worker.snapshot("s4", 1, 2.0)["status"]["visual"]["state"] == "ready")
        assert missing_worker.snapshot("s4", 1, 2.0)["visual_observations"] == []
    finally:
        missing_worker.close()


def test_synthetic_input_skips_real_asr_and_visual_perception():
    media = _Media()
    worker = BackgroundPerception(media)
    try:
        worker.observe({"time_s": 1.0, "input_mode": "synthetic"}, media, "synthetic", 1)
        result = worker.snapshot("synthetic", 1, 1.0)
        assert result["status"]["speech"]["state"] == "skipped_synthetic"
        assert result["status"]["visual"]["state"] == "skipped_synthetic"
        assert result["transcript"]["segments"] == []
        assert result["visual_observations"] == []
        assert media.frame_calls == 0
    finally:
        worker.close()


def test_worker_errors_are_safe_and_close_is_bounded():
    class BrokenMedia(_Media):
        def buffered_camera(self, camera_id: str, time_s: float):
            return super().buffered_camera(camera_id, time_s)

        def frame_at(self, _camera_id: str, _time_s: float):
            raise ValueError("private payload must not be logged")

    media = BrokenMedia()
    worker = BackgroundPerception(media, speech_enabled=False)
    worker.observe({"time_s": 1.0, "input_mode": "ami"}, media, "s4", 1)
    _wait_until(lambda: worker.snapshot("s4", 1, 1.0)["status"]["visual"]["state"] == "error")
    status = worker.snapshot("s4", 1, 1.0)["status"]["visual"]
    assert status["last_error_class"] == "ValueError"
    assert "private payload" not in str(status)
    start = time.monotonic()
    worker.close(timeout_s=0.1)
    assert time.monotonic() - start < 0.2


def test_eof_submits_a_causal_partial_audio_tail(tmp_path: Path):
    entered, release = threading.Event(), threading.Event()
    starts: list[float] = []
    model = _BlockingASR(entered, release, starts)
    media = _Media(_audio_file(tmp_path / "short-mix.wav", seconds=2))
    worker = BackgroundPerception(media, tmp_path / "model", asr_factory=lambda _path: model,
                                  visual_enabled=False)
    try:
        worker.observe({"time_s": 0.0, "duration_s": 2.0, "status": "running", "input_mode": "ami"}, media, "eof", 1)
        # A short chunk is deferred while playback continues.
        assert worker.snapshot("eof", 1, 2.0)["status"]["speech"]["state"] == "idle"
        worker.observe({"time_s": 2.0, "duration_s": 2.0, "status": "stopped", "input_mode": "ami"}, media, "eof", 1)
        assert entered.wait(1.0)
        release.set()
        _wait_until(lambda: worker.snapshot("eof", 1, 2.0)["status"]["speech"]["state"] == "ready")
        result = worker.snapshot("eof", 1, 2.0)
        assert model.sample_lengths == [2 * 16_000]
        assert all(segment["source_end_s"] <= 2.0 for segment in result["transcript"]["segments"])
    finally:
        release.set()
        worker.close()
