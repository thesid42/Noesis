"""Start Noesis's local services and clean up only processes we own."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / ".runtime"
CONTROLLER = "http://127.0.0.1:8765"


def port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3):
            return True
    except OSError:
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--obs", action="store_true", help="start the workspace's portable OBS and set up its program source")
    parser.add_argument("--without-flower", action="store_true", help="use the deterministic controller alone")
    parser.add_argument("--duration-s", type=int, default=3600, help="bounded Flower agent lifetime")
    args = parser.parse_args()
    os.chdir(ROOT)
    load_dotenv(ROOT / ".env")
    RUNTIME.mkdir(exist_ok=True)
    env = os.environ.copy()
    env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
    env["FLWR_HOME"] = str(RUNTIME / "flower-home")
    env["UV_CACHE_DIR"] = str(RUNTIME / "uv-cache")
    env["PYTHONUTF8"] = "1"
    processes: list[subprocess.Popen] = []
    streams = []
    agents_started = False
    ready = False

    def spawn(command: list[str], name: str, cwd: Path = ROOT) -> None:
        out = (RUNTIME / f"{name}.out.log").open("a", encoding="utf-8")
        err = (RUNTIME / f"{name}.err.log").open("a", encoding="utf-8")
        streams.extend([out, err])
        process = subprocess.Popen(command, cwd=cwd, env=env, stdout=out, stderr=err,
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        processes.append(process)

    def wait_port(port: int, name: str) -> None:
        for _ in range(100):
            if port_open(port):
                return
            time.sleep(0.2)
        raise RuntimeError(f"{name} did not become ready; inspect .runtime/{name}.err.log")

    try:
        if not port_open(8765):
            spawn([sys.executable, "-m", "noesis"], "app")
        wait_port(8765, "app")
        for _ in range(50):
            try:
                httpx.get(f"{CONTROLLER}/api/state", timeout=2).raise_for_status()
                break
            except httpx.HTTPError:
                time.sleep(0.2)
        else:
            raise RuntimeError("The controller port is occupied but Noesis is not responding.")

        if args.obs:
            if not port_open(4455):
                obs = RUNTIME / "obs/bin/64bit/obs64.exe"
                if not obs.exists():
                    raise RuntimeError("Install portable OBS first: python scripts/setup_obs.py, then python scripts/configure_obs.py")
                spawn([str(obs), "--portable", "--disable-updater", "--minimize-to-tray",
                       "--disable-missing-files-check", "--profile", "Noesis", "--collection", "Noesis"],
                      "obs", obs.parent)
            wait_port(4455, "obs")
            for endpoint in ("connect", "setup"):
                # obs-websocket accepts TCP before OBS finishes loading scenes.
                # A cold start can return 207 "not ready" for a short interval.
                for attempt in range(20):
                    response = httpx.post(f"{CONTROLLER}/api/obs/{endpoint}", json={}, timeout=20)
                    if response.is_success:
                        break
                    if attempt == 19:
                        response.raise_for_status()
                    time.sleep(0.5)

        if not args.without_flower:
            if not port_open(8000):
                executable = Path(sys.executable).parent / ("flower-superlink.exe" if os.name == "nt" else "flower-superlink")
                if not executable.exists():
                    raise RuntimeError("Flower extra is missing. Run uv sync --extra flower --extra dev.")
                spawn([str(executable), "--insecure", "--host", "127.0.0.1", "--port", "8000",
                       "--fleet-api-address", "127.0.0.1:9092", "--disable-runtime-dependency-installation"], "flower-superlink")
            wait_port(8000, "flower-superlink")
            current_state = httpx.get(f"{CONTROLLER}/api/state", timeout=2).json()
            healthy = {a.get("agent_id") for a in current_state.get("flower", {}).get("agents", []) if a.get("healthy")}
            expected = {"director", "camera-closeup1", "camera-closeup2", "camera-closeup3", "camera-closeup4"}
            if expected.issubset(healthy):
                print("Reusing five healthy Flower AgentApps.", flush=True)
            else:
                subprocess.run([sys.executable, "scripts/flower_agents.py", "start", "--duration-s", str(args.duration_s)],
                               cwd=ROOT, env=env, check=True)
                agents_started = True

        ready = True
        print(f"\nNoesis ready: {CONTROLLER}", flush=True)
        print("Choose your input/output in the dashboard, then Start session. Ctrl+C stops this launcher.", flush=True)
        print("Flower uses rules by default; no model credentials are needed. Logs: .runtime/", flush=True)
        while True:
            time.sleep(1)
            failed = [p.returncode for p in processes if p.poll() is not None]
            if failed:
                raise RuntimeError(f"A managed service exited ({failed}); inspect .runtime logs.")
    except KeyboardInterrupt:
        print("\nStopping this session…", flush=True)
        return 0
    except Exception as exc:
        print(f"Startup failed: {exc}", file=sys.stderr)
        return 1
    finally:
        if ready:
            try:
                httpx.post(f"{CONTROLLER}/api/session/stop", json={}, timeout=10)
            except httpx.HTTPError:
                pass
        if agents_started:
            try:
                subprocess.run([sys.executable, "scripts/flower_agents.py", "stop"], cwd=ROOT, env=env, timeout=20)
            except subprocess.TimeoutExpired:
                print("Agent stop timed out; inspect scripts/flower_agents.py status.", file=sys.stderr)
        # OBS stays open in the tray so it can flush and preserve its scene/profile.
        # Only launcher-owned Python/Flower processes are terminated.
        for process in reversed(processes):
            if process.poll() is None and "obs64.exe" not in str(process.args):
                if os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True)
                else:
                    process.terminate()
        for stream in streams:
            stream.close()


if __name__ == "__main__":
    raise SystemExit(main())
