"""Exercise the running local controller, real Flower runs, and an OBS recording."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import time

import cv2
import httpx

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preview-only", action="store_true")
    parser.add_argument("--without-flower", action="store_true")
    args = parser.parse_args()
    client = httpx.Client(base_url="http://127.0.0.1:8765", timeout=20)
    report: dict = {"input": "synthetic test feeds", "checks": {}}

    def state():
        response = client.get("/api/state"); response.raise_for_status(); return response.json()

    def post(path, body=None):
        response = client.post(path, json=body or {}); response.raise_for_status(); return response.json()

    try:
        post("/api/session/stop")
        if not args.without_flower:
            agents = [a for a in state()["flower"]["agents"] if a["healthy"]]
            assert len(agents) >= 5, f"Five live Flower runs required; found {len(agents)}"
            report["flower_runs"] = [{k: a.get(k) for k in ("agent_id", "run_id", "runtime", "decision_mode")} for a in agents]
        if not args.preview_only:
            post("/api/obs/connect"); post("/api/obs/setup")
        s = post("/api/session/start", {"input_mode": "synthetic", "output_mode": "preview" if args.preview_only else "obs"})
        assert args.preview_only or s["obs"]["recording"]
        print("Session started; observing automatic cuts…", flush=True)
        # Inject within A's stable 0–7s turn, before the scheduled overlap.
        # Otherwise an ordinary editorial cut could mask fault recovery.
        time.sleep(5)
        before = state(); failed = before["program"]["camera_id"]
        assert failed != "slate"
        started = time.monotonic()
        post("/api/fault", {"camera_id": failed, "kind": "black", "duration_s": 10})
        while time.monotonic() - started < 3:
            s = state()
            if s["program"]["camera_id"] != failed and s["metrics"]["fallbacks"] > before["metrics"]["fallbacks"]:
                break
            time.sleep(0.05)
        else:
            raise AssertionError("No health-driven recovery occurred within 3 seconds")
        report["checks"]["camera_failure_recovery_ms"] = round((time.monotonic() - started) * 1000)
        report["checks"]["fallback_camera"] = s["program"]["camera_id"]
        assert s["metrics"]["fallbacks"] >= 1
        post("/api/fault", {"camera_id": failed, "kind": "none", "duration_s": 10})
        post("/api/control/override", {"camera_id": "corner"})
        for _ in range(7):
            time.sleep(1)
            s = state()
            assert s["mode"] == "manual" and s["program"]["camera_id"] == "corner"
        report["checks"]["manual_latch_held_across_speaker_change"] = True
        post("/api/control/autopilot")
        time.sleep(8)
        before_pause = post("/api/session/pause")["session"]["time_s"]
        time.sleep(1)
        assert abs(state()["session"]["time_s"] - before_pause) < 0.15
        report["checks"]["pause_freezes_media_clock"] = True
        post("/api/session/resume")
        time.sleep(3)
        s = post("/api/session/stop")
        report["metrics"] = s["metrics"]
        report["events"] = s["events"]
        report["obs"] = s["obs"]
        assert s["metrics"]["cuts"] >= 3
        if not args.without_flower:
            director_cuts = [e for e in s["events"] if e["source"] == "director" and e["kind"] == "camera_cut"]
            assert director_cuts, "No accepted Flower director camera switch observed"
            report["checks"]["flower_director_camera_cuts"] = len(director_cuts)
        if not args.preview_only:
            assert not s["obs"]["recording"]
            recording = Path(s["obs"]["output_path"])
            assert recording.exists() and recording.stat().st_size > 10000
            video = cv2.VideoCapture(str(recording))
            frames = int(video.get(cv2.CAP_PROP_FRAME_COUNT)); fps = video.get(cv2.CAP_PROP_FPS)
            report["checks"]["recorded_seconds"] = round(frames / fps, 2)
            samples = []
            for fraction in (0.15, 0.5, 0.85):
                video.set(cv2.CAP_PROP_POS_FRAMES, int(frames * fraction))
                ok, frame = video.read()
                assert ok and frame.std() > 15, "Recorded output contained blank or undecodable frames"
                samples.append(frame)
            video.release()
            change = max(float(cv2.absdiff(samples[0], sample).mean()) for sample in samples[1:])
            assert change > 5, "Recorded output did not visibly change between automatic and manual shots"
            frame = samples[-1]
            proof = ROOT / "recordings/verified-program.jpg"
            cv2.imwrite(str(proof), frame)
            report["checks"]["recorded_frame_stddev"] = round(float(frame.std()), 2)
            report["checks"]["recorded_frame"] = str(proof)
            report["checks"]["recorded_shot_difference"] = round(change, 2)
        print("Checking natural replay completion…", flush=True)
        end_session = post("/api/session/start", {"input_mode": "synthetic", "output_mode": "preview" if args.preview_only else "obs", "start_s": 148.5})
        end_deadline = time.monotonic() + 8
        while time.monotonic() < end_deadline:
            ended = state()
            if ended["session"]["status"] == "stopped" and not ended["obs"]["recording"]:
                break
            time.sleep(0.15)
        else:
            raise AssertionError("Replay end did not finalize the session and recording")
        assert ended["session"]["epoch"] > end_session["session"]["epoch"]
        report["checks"]["replay_end_stops_session_and_recording"] = True
        path = ROOT / ".runtime/acceptance.json"
        path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps({"passed": True, "checks": report["checks"], "report": str(path)}, indent=2))
    finally:
        post("/api/session/stop")
        client.close()


if __name__ == "__main__":
    main()
