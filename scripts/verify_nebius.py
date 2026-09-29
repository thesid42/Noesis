"""Rehearse one configured Nebius model through five live Flower AgentApps.

Start run_demo.py --obs --model-profile kimi (then minimax) first. This replaces
the current replay and saves only non-secret result metadata under .runtime.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import time

import cv2
import httpx
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=("kimi", "minimax"), required=True)
    parser.add_argument("--preview-only", action="store_true")
    args = parser.parse_args()
    expected_model = dotenv_values(ROOT / ".env").get(f"NEBIUS_{args.profile.upper()}_MODEL")
    report: dict = {"profile": args.profile, "model": expected_model, "checks": {}, "model_responses": []}
    report_path = ROOT / ".runtime" / f"nebius-{args.profile}-acceptance.json"
    client = httpx.Client(base_url="http://127.0.0.1:8765", timeout=20)

    def state():
        response = client.get("/api/state")
        response.raise_for_status()
        return response.json()

    def post(path, body=None):
        response = client.post(path, json=body or {})
        response.raise_for_status()
        return response.json()

    def capture(current):
        last = current["flower"].get("last_model_result")
        if last and last["response_id"] not in {r["response_id"] for r in report["model_responses"]}:
            report["model_responses"].append(last)
            print(json.dumps({"model_response": last}), flush=True)

    try:
        post("/api/session/stop")
        deadline = time.monotonic() + 30
        while True:
            current = state()
            agents = [a for a in current["flower"]["agents"] if a["healthy"]]
            if len(agents) == 5 and current["flower"]["decision_modes"].get("director") == "llm":
                break
            assert time.monotonic() < deadline, "Five live Flower agents with an LLM director are required"
            time.sleep(0.3)
        assert current["flower"].get("model") == expected_model, "Controller model profile does not match the requested test"
        report["flower_runs"] = [{k: a.get(k) for k in ("agent_id", "run_id", "runtime", "decision_mode")} for a in agents]
        started = post("/api/session/start", {"input_mode": "synthetic", "output_mode": "preview" if args.preview_only else "obs"})
        assert args.preview_only or started["obs"]["recording"]
        print(f"Testing {args.profile} through Flower; observing editorial policies and actual cuts.", flush=True)
        until = time.monotonic() + 32
        while time.monotonic() < until:
            current = state()
            capture(current)
            time.sleep(0.2)
        assert report["model_responses"], "No verified Nebius response was accepted through the Flower director"
        assert current["metrics"]["editorial_policies"] >= 1
        assert current["metrics"]["policy_guided_cuts"] >= 1, "No camera cut applied the model's editorial policy"
        report["checks"]["flower_model_policy_applied"] = True
        # Inject immediately after a fresh shot so the normal speaker transition
        # cannot race the 0.8-second black-frame debounce and mask recovery.
        deadline = time.monotonic() + 20
        while True:
            current = state()
            capture(current)
            shot_age = current["session"]["time_s"] - current["program"]["last_cut_s"]
            if (current["program"]["camera_id"] != "slate"
                    and 0 <= shot_age < 1
                    and current["flower"].get("editorial_policy")):
                break
            assert time.monotonic() < deadline, "No fresh policy-controlled shot for the recovery check"
            time.sleep(0.05)
        report["active_policy"] = current["flower"].get("editorial_policy")
        failed_camera = current["program"]["camera_id"]
        assert failed_camera != "slate"
        prior_fallbacks = current["metrics"]["fallbacks"]
        recovery_started = time.monotonic()
        post("/api/fault", {"camera_id": failed_camera, "kind": "black", "duration_s": 10})
        while time.monotonic() - recovery_started < 3:
            current = state()
            capture(current)
            if current["metrics"]["fallbacks"] > prior_fallbacks and current["program"]["camera_id"] != failed_camera:
                break
            time.sleep(0.05)
        else:
            raise AssertionError("Camera health recovery failed within 3 seconds")
        report["checks"]["fault_request_to_controller_recovery_ms"] = round((time.monotonic() - recovery_started) * 1000)
        post("/api/fault", {"camera_id": failed_camera, "kind": "none", "duration_s": 10})
        post("/api/control/override", {"camera_id": "corner"})
        until = time.monotonic() + 5
        while time.monotonic() < until:
            current = state()
            capture(current)
            assert current["mode"] == "manual" and current["program"]["camera_id"] == "corner"
            assert current["flower"]["editorial_policy"] is None
            time.sleep(0.2)
        report["checks"]["manual_override_invalidates_model_policy"] = True
        current = post("/api/session/stop")
        report.update(metrics=current["metrics"], events=current["events"], obs=current["obs"])
        if not args.preview_only:
            assert not current["obs"]["recording"]
            recording = Path(current["obs"]["output_path"])
            video = cv2.VideoCapture(str(recording))
            fps = video.get(cv2.CAP_PROP_FPS)
            frames = video.get(cv2.CAP_PROP_FRAME_COUNT)
            assert fps > 0 and frames / fps >= 18
            video.set(cv2.CAP_PROP_POS_FRAMES, int(frames * 0.5))
            ok, frame = video.read()
            video.release()
            assert ok and frame.std() > 10
            cv2.imwrite(str(ROOT / "recordings" / f"nebius-{args.profile}-program.jpg"), frame)
            report["checks"]["recorded_seconds"] = round(frames / fps, 2)
        report["passed"] = True
    except Exception as exc:
        # Our assertion messages and exception class are safe; provider bodies
        # and request headers must never be copied into this public-style report.
        report["passed"] = False
        report["error"] = str(exc) if isinstance(exc, AssertionError) else type(exc).__name__
        try:
            current = state()
            report.update(metrics=current["metrics"], events=current["events"], obs=current["obs"])
        except Exception:
            pass
        raise
    finally:
        try:
            post("/api/session/stop")
        finally:
            client.close()
            report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
            print(json.dumps({"passed": report.get("passed"), "checks": report["checks"], "report": str(report_path)}, indent=2))


if __name__ == "__main__":
    main()
