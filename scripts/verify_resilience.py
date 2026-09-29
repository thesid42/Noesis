"""Actively test agent-outage fallback and an external OBS recording stop.

Requires the local demo stack. Replaces the current session and temporarily
stops/restarts only the launcher-managed Flower runs.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import subprocess
import sys
import time

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from noesis.obs_bridge import SimpleOBSBridge


def main() -> None:
    load_dotenv(ROOT / ".env")
    client = httpx.Client(base_url="http://127.0.0.1:8765", timeout=20)
    checks = {}

    def post(path, body=None):
        response = client.post(path, json=body or {})
        response.raise_for_status()
        return response.json()

    def state():
        response = client.get("/api/state")
        response.raise_for_status()
        return response.json()

    def flower(command):
        subprocess.run([sys.executable, str(ROOT / "scripts/flower_agents.py"), command], cwd=ROOT, check=True)

    agents_stopped = False
    try:
        post("/api/session/stop")
        assert len([a for a in state()["flower"]["agents"] if a["healthy"]]) == 5
        post("/api/session/start", {"input_mode": "synthetic", "output_mode": "preview"})
        flower("stop")
        agents_stopped = True
        time.sleep(10)
        degraded = state()
        assert not any(a["healthy"] for a in degraded["flower"]["agents"])
        assert degraded["mode"] == "degraded"
        assert degraded["session"]["status"] == "running"
        assert any(e["source"] == "baseline" and e["kind"] == "camera_cut" and e["time_s"] > 7
                   for e in degraded["events"])
        checks["baseline_cuts_after_all_flower_agents_stop"] = True
        post("/api/session/stop")
        flower("start")
        agents_stopped = False
        post("/api/session/start", {"input_mode": "synthetic", "output_mode": "obs"})
        time.sleep(1.5)

        async def operator_stops_recording():
            bridge = SimpleOBSBridge()
            try:
                await bridge.connect()
                await bridge.stop_recording()
            finally:
                await bridge.disconnect()

        asyncio.run(operator_stops_recording())
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            observed = state()
            if not observed["obs"]["recording"]:
                break
            time.sleep(0.1)
        assert not observed["obs"]["recording"]
        assert any(e["kind"] == "obs_recording_stopped_externally" for e in observed["events"])
        checks["external_obs_stop_reflected_in_controller"] = True
        path = ROOT / ".runtime/resilience.json"
        path.write_text(json.dumps({"passed": True, "checks": checks}, indent=2), encoding="utf-8")
        print(json.dumps({"passed": True, "checks": checks, "report": str(path)}, indent=2))
    finally:
        post("/api/session/stop")
        if agents_stopped:
            flower("start")
        client.close()


if __name__ == "__main__":
    main()
