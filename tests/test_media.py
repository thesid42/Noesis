from __future__ import annotations

import json
import time
from pathlib import Path

import cv2
import numpy as np
import soundfile as sf
from PIL import Image

from noesis.media import CAMERA_IDS, MediaEngine


def test_synthetic_feeds_are_labeled_and_mapping_is_explicit(tmp_path: Path):
    # Keep absence checks independent of AMI data installed on the developer's machine.
    engine = MediaEngine(tmp_path)
    snapshot = engine.start("synthetic", start_s=1.0)

    assert snapshot["input_mode"] == "synthetic"
    assert [camera["id"] for camera in snapshot["cameras"]] == list(CAMERA_IDS)
    mapping = {camera["id"]: camera["participant"] for camera in snapshot["cameras"]}
    assert mapping == {
        "closeup1": "A",
        "closeup2": "C",
        "closeup3": "D",
        "closeup4": "B",
        "corner": None,
    }
    assert snapshot["cameras"][0]["speaking"] is True
    frame = Image.open(__import__("io").BytesIO(engine.frame_jpeg("closeup1")))
    assert frame.size == (640, 360)
    assert engine.availability()["ami_available"] is False


def test_monotonic_clock_pause_resume_and_seek():
    engine = MediaEngine()
    first = engine.start("synthetic", start_s=5.0)
    time.sleep(0.12)
    running = engine.snapshot()
    assert running["time_s"] > first["time_s"]

    paused = engine.pause()
    paused_at = paused["time_s"]
    time.sleep(0.12)
    assert engine.snapshot()["time_s"] == paused_at

    sought = engine.seek(17.25)
    assert sought["time_s"] == 17.25
    assert sought["status"] == "paused"
    time.sleep(0.08)
    assert engine.snapshot()["time_s"] == 17.25

    resumed = engine.resume()
    assert resumed["status"] == "running"
    time.sleep(0.12)
    assert engine.snapshot()["time_s"] > 17.25


def test_faults_change_the_jpeg_and_health_seen_by_consumers():
    engine = MediaEngine()
    engine.start("synthetic", start_s=1.0)
    clean = engine.frame_jpeg("closeup1")
    assert clean[:2] == b"\xff\xd8"

    first_black = engine.inject_fault("closeup1", "black", duration_s=2.0)
    camera = next(item for item in first_black["cameras"] if item["id"] == "closeup1")
    assert camera["healthy"] is True  # black pixels arrive before the health debounce
    black_image = np.asarray(Image.open(__import__("io").BytesIO(engine.frame_jpeg("closeup1"))))
    assert int(black_image.max()) <= 1
    time.sleep(0.9)
    black_health = next(item for item in engine.snapshot()["cameras"] if item["id"] == "closeup1")
    assert black_health["healthy"] is False
    assert black_health["status"] == "black"

    freeze_engine = MediaEngine()
    freeze_engine.start("synthetic", start_s=5.0)
    first_freeze = freeze_engine.inject_fault("closeup1", "freeze", duration_s=2.0)
    camera = next(item for item in first_freeze["cameras"] if item["id"] == "closeup1")
    assert camera["healthy"] is True
    frozen = freeze_engine.frame_jpeg("closeup1")
    assert freeze_engine.frame_jpeg("closeup1") == frozen
    time.sleep(0.9)
    frozen_status = next(item for item in freeze_engine.snapshot()["cameras"] if item["id"] == "closeup1")
    assert frozen_status["healthy"] is False
    assert frozen_status["status"] == "stalled"
    assert frozen_status["age_ms"] >= 800

    offline_engine = MediaEngine()
    offline_engine.start("synthetic", start_s=2.0)
    offline_engine.inject_fault("closeup1", "offline", duration_s=2.0)
    time.sleep(0.9)
    offline = offline_engine.snapshot()
    offline_status = next(item for item in offline["cameras"] if item["id"] == "closeup1")
    assert offline_status["healthy"] is False
    assert offline_status["status"] == "offline"
    assert offline_engine.frame_jpeg("closeup1") != clean


def _write_video(path: Path, color: tuple[int, int, int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), 10.0, (96, 64))
    assert writer.isOpened()
    for index in range(50):
        frame = np.full((64, 96, 3), color, dtype=np.uint8)
        cv2.putText(frame, str(index), (8, 35), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 1)
        writer.write(frame)
    writer.release()


def _write_audio(path: Path, active: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rate = 16000
    times = np.arange(rate * 5, dtype=np.float32) / rate
    samples = np.zeros_like(times)
    if active:
        mask = (times >= 0.5) & (times < 1.7)
        samples[mask] = 0.16 * np.sin(2 * np.pi * 220 * times[mask])
    sf.write(path, samples, rate, subtype="PCM_16")


def test_ami_replay_decodes_generated_fixture_at_the_common_clock(tmp_path: Path):
    video_root = tmp_path / "video"
    audio_root = tmp_path / "audio"
    video_files = [
        "ES2002a.Closeup1.avi",
        "ES2002a.Closeup2.avi",
        "ES2002a.Closeup3.avi",
        "ES2002a.Closeup4.avi",
        "ES2002a.Corner.avi",
    ]
    for index, filename in enumerate(video_files):
        _write_video(video_root / filename, (35 + index * 30, 75, 125))
    for index in range(4):
        _write_audio(audio_root / f"ES2002a.Headset-{index}.wav", active=index == 0)
    _write_audio(audio_root / "ES2002a.Mix-Headset.wav")

    engine = MediaEngine(tmp_path)
    initial = engine.start("ami", start_s=0.3)
    assert initial["input_mode"] == "ami"
    assert initial["duration_s"] == 5.0
    assert all(item["healthy"] for item in initial["cameras"])
    assert engine.audio_path() == audio_root / "ES2002a.Mix-Headset.wav"
    frame = cv2.imdecode(np.frombuffer(engine.frame_jpeg("closeup1"), dtype=np.uint8), cv2.IMREAD_COLOR)
    assert frame is not None and frame.shape[:2] == (64, 96)

    # Sustained headset energy becomes visible after the causal confirmation window.
    engine.seek(0.72)
    time.sleep(0.40)
    observed = engine.snapshot()
    closeup_a = next(item for item in observed["cameras"] if item["id"] == "closeup1")
    assert closeup_a["speaking"] is True
    assert closeup_a["energy"] > 0.0
    assert observed["active_speakers"] == ["A"]

    engine.inject_fault("closeup1", "black", duration_s=2.0)
    assert int(np.asarray(Image.open(__import__("io").BytesIO(engine.frame_jpeg("closeup1")))).max()) <= 1
    time.sleep(0.9)
    fault = engine.snapshot()
    closeup_a = next(item for item in fault["cameras"] if item["id"] == "closeup1")
    assert closeup_a["healthy"] is False
    assert closeup_a["status"] == "black"

