"""Record the installed AMI excerpt and verify real video and non-silent audio.

This is an adapter/recording smoke test, not a speaker-accuracy or lip-sync eval.
It replaces the current session and requires OBS plus a complete AMI set.
"""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import time

import cv2
import httpx
import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    client = httpx.Client(base_url="http://127.0.0.1:8765", timeout=20)

    def state():
        response = client.get("/api/state")
        response.raise_for_status()
        return response.json()

    def post(path, body=None):
        response = client.post(path, json=body or {})
        response.raise_for_status()
        return response.json()

    report = {"input": "AMI ES2002a prepared excerpt", "checks": {}, "samples": []}
    try:
        assert state()["data"]["ami_available"], "A complete AMI prepared set is required"
        post("/api/session/stop")
        started = post("/api/session/start", {"input_mode": "ami", "output_mode": "obs"})
        assert started["obs"]["recording"]
        report["session"] = started["session"]
        duration = started["session"]["duration_s"]
        assert 19.9 <= duration <= 180, "Use a bounded 20–180 second AMI excerpt for this rehearsal"
        audio_range = client.get("/api/audio", headers={"Range": "bytes=0-4095"})
        assert audio_range.status_code == 206 and len(audio_range.content) == 4096
        report["checks"]["audio_range_requests"] = True
        deadline = time.monotonic() + duration + 15
        next_sample = 0.0
        while time.monotonic() < deadline:
            current = state()
            if current["session"]["time_s"] >= next_sample:
                report["samples"].append({
                    "time_s": current["session"]["time_s"],
                    "program": current["program"]["camera_id"],
                    "cameras": [{k: c.get(k) for k in ("id", "healthy", "speaking", "energy", "source_time_s")}
                                for c in current["cameras"]],
                })
                next_sample += 2
            if current["session"]["status"] == "stopped" and not current["obs"]["recording"]:
                break
            time.sleep(0.2)
        else:
            raise AssertionError("AMI replay did not finish the recording")
        assert any(len(sample["cameras"]) == 5 and all(c["healthy"] for c in sample["cameras"])
                   for sample in report["samples"]), "No sample had five healthy decoded cameras"
        report["checks"]["five_real_camera_feeds"] = True
        recording = Path(current["obs"]["output_path"])
        assert recording.exists()
        video = cv2.VideoCapture(str(recording))
        frames, fps = video.get(cv2.CAP_PROP_FRAME_COUNT), video.get(cv2.CAP_PROP_FPS)
        assert fps > 0 and frames / fps >= duration - 1
        video.set(cv2.CAP_PROP_POS_FRAMES, int(frames / 2))
        ok, frame = video.read()
        video.release()
        assert ok and frame.std() > 10
        cv2.imwrite(str(ROOT / "recordings/verified-ami-program.jpg"), frame)
        extracted = ROOT / "recordings/verified-ami-audio.wav"
        ffmpeg = shutil.which("ffmpeg")
        assert ffmpeg, "FFmpeg is required"
        subprocess.run([ffmpeg, "-v", "error", "-y", "-i", str(recording), "-vn", "-ac", "1", "-ar", "16000", str(extracted)], check=True)
        audio, rate = sf.read(extracted)
        peak = float(np.max(np.abs(audio)))
        rms = float(np.sqrt(np.mean(audio ** 2)))
        assert peak > 0.01 and rms > 0.0001, "OBS recording audio was silent"
        report.update(recording=str(recording), metrics=current["metrics"], events=current["events"])
        report["checks"].update(recorded_seconds=round(frames / fps, 2),
                                 non_silent_program_audio=True, audio_peak=peak, audio_rms=rms,
                                 audio_seconds=round(len(audio) / rate, 2), replay_end_finalized=True)
        path = ROOT / ".runtime/ami-acceptance.json"
        path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps({"passed": True, "checks": report["checks"], "recording": str(recording), "report": str(path)}, indent=2))
    finally:
        post("/api/session/stop")
        client.close()


if __name__ == "__main__":
    main()
