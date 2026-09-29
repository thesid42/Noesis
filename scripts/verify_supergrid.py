"""Verify real six-role AI collaboration and a model-directed cut on Flower.

Requires a running Noesis launcher. Pass --runtime local for local Flower.
Replaces the current replay, uses live model credits, and stores non-secret
provenance in .runtime/<runtime>.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import time

import httpx

ROOT = Path(__file__).resolve().parents[1]
ROLES = {"director", "critic", *(f"camera-closeup{i}" for i in range(1, 5))}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=("kimi", "minimax"), default="kimi")
    parser.add_argument("--runtime", choices=("local", "supergrid"), default="supergrid")
    parser.add_argument("--preview-only", action="store_true")
    parser.add_argument("--timeout-s", type=int, default=110)
    args = parser.parse_args()
    report = {"profile": args.profile, "runtime": args.runtime, "checks": {}, "model_responses": []}
    seen = set()
    with httpx.Client(base_url="http://127.0.0.1:8765", timeout=20, trust_env=False) as client:
        def post(path, body=None):
            response = client.post(path, json=body or {})
            response.raise_for_status()
            return response.json()

        def state():
            response = client.get("/api/state")
            response.raise_for_status()
            return response.json()

        try:
            post("/api/session/stop")
            post("/api/models/select", {"profile": args.profile})
            current = post("/api/session/start", {"input_mode": "synthetic", "output_mode": "preview" if args.preview_only else "obs"})
            report["session"] = current["session"]
            report["model"] = current["flower"]["model"]
            started_at = time.monotonic()
            deadline = time.monotonic() + args.timeout_s
            while time.monotonic() < deadline:
                current = state()
                for row in current["flower"]["inference_results"]:
                    if row["response_id"] not in seen and row["session_id"] == report["session"]["id"]:
                        seen.add(row["response_id"])
                        report["model_responses"].append({**row, "observed_after_s": round(time.monotonic() - started_at, 3)})
                        print(json.dumps({"role": row["agent_id"], "latency_ms": row["latency_ms"], "result": row["result"]}), flush=True)
                proven = {row["agent_id"] for row in report["model_responses"]}
                if ROLES <= proven and current["metrics"]["ai_cuts"] >= 1:
                    break
                time.sleep(0.4)
            else:
                raise AssertionError("Six verified AI roles and an actual model-directed cut were not observed within the budget")
            assert current["flower"].get("deployment") == args.runtime, "Unexpected Flower deployment"
            assert current["flower"]["grid_run"].get("status") == "running", "No running Flower AgentApp"
            assert all(row["model"] == report["model"] for row in report["model_responses"])
            report["grid_run"] = current["flower"]["grid_run"]
            report["checks"].update(six_ai_roles=True, actual_model_cut=True, runtime_running=True)
            failed = current["program"]["camera_id"]
            fallback_count = current["metrics"]["fallbacks"]
            started = time.monotonic()
            post("/api/fault", {"camera_id": failed, "kind": "offline", "duration_s": 5})
            while time.monotonic() - started < 3:
                current = state()
                if current["metrics"]["fallbacks"] > fallback_count and current["program"]["camera_id"] != failed:
                    break
                time.sleep(0.1)
            else:
                raise AssertionError("Health recovery did not select a usable source")
            report["checks"]["health_recovery_ms"] = round((time.monotonic() - started) * 1000)
            post("/api/fault", {"camera_id": failed, "kind": "none", "duration_s": 5})
            post("/api/control/override", {"camera_id": "corner"})
            manual = state()
            assert manual["mode"] == "manual" and manual["program"]["camera_id"] == "corner"
            report["checks"]["manual_override"] = True
            report["metrics"] = manual["metrics"]
            stopped = post("/api/session/stop")
            report["recording"] = stopped["obs"].get("output_path")
            report["passed"] = True
        finally:
            post("/api/session/stop")
            destination = ROOT / ".runtime" / args.runtime / f"{args.profile}-acceptance.json"
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(json.dumps(report, indent=2), encoding="utf-8")
            print(json.dumps({"passed": report.get("passed", False), "checks": report["checks"], "report": str(destination)}), flush=True)


if __name__ == "__main__":
    main()
