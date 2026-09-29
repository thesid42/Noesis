"""Start Noesis's local services and clean up only processes we own."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

import httpx
from dotenv import dotenv_values
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from noesis.model_config import (  # noqa: E402
    ModelProfileError,
    PROFILE_MANIFEST,
    launcher_environment,
    load_model_profile,
    superlink_environment,
    write_profile_manifest,
)

RUNTIME = ROOT / ".runtime"
CONTROLLER = "http://127.0.0.1:8765"
SUPERLINK_PROFILE_FILE = ROOT / PROFILE_MANIFEST


def port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3):
            return True
    except OSError:
        return False


def wait_flower_control_api() -> None:
    from flwr.proto.control_pb2 import ListRunsRequest
    from flwr.supercore.control import ControlHttpClient

    last_error: Exception | None = None
    for _ in range(75):
        client = None
        try:
            client = ControlHttpClient("http://127.0.0.1:8000", timeout=2.0)
            client.ListRuns(ListRunsRequest(limit=1))
            return
        except Exception as exc:
            last_error = exc
            time.sleep(0.2)
        finally:
            if client:
                client.close()
    error_type = type(last_error).__name__ if last_error else "timeout"
    raise RuntimeError(f"Flower SuperLink control API did not become ready ({error_type}).")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--obs", action="store_true", help="start the workspace's portable OBS and set up its program source")
    parser.add_argument("--without-flower", action="store_true", help="use the deterministic controller alone")
    parser.add_argument("--model-profile", choices=("kimi", "minimax"), default=None, help="use the configured Nebius profile and enable Director LLM mode")
    parser.add_argument("--duration-s", type=int, default=3600, help="bounded Flower agent lifetime")
    args = parser.parse_args()
    os.chdir(ROOT)
    config = {key: value for key, value in dotenv_values(ROOT / ".env").items() if value is not None}
    config.update(os.environ)
    try:
        profile = load_model_profile(args.model_profile, config)
    except ModelProfileError as exc:
        print(f"Startup configuration error: {exc}", file=sys.stderr)
        return 2
    RUNTIME.mkdir(exist_ok=True)
    env = launcher_environment(config, profile)
    env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
    env["FLWR_HOME"] = str(RUNTIME / "flower-home")
    env["UV_CACHE_DIR"] = str(RUNTIME / "uv-cache")
    env["PYTHONUTF8"] = "1"
    processes: list[subprocess.Popen] = []
    streams = []
    agents_started = False
    flower_superlink_started = False
    flower_superlink_pid: int | None = None
    ready = False

    def spawn(command: list[str], name: str, cwd: Path = ROOT, child_env: dict[str, str] | None = None) -> subprocess.Popen:
        out = (RUNTIME / f"{name}.out.log").open("a", encoding="utf-8")
        err = (RUNTIME / f"{name}.err.log").open("a", encoding="utf-8")
        streams.extend([out, err])
        process = subprocess.Popen(command, cwd=cwd, env=child_env or env, stdout=out, stderr=err,
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        processes.append(process)
        return process

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
            agents_command = [sys.executable, "scripts/flower_agents.py"]
            profile_args = []
            if profile:
                profile_args = [
                    "--model-profile", profile.name,
                    "--model-endpoint", profile.endpoint,
                    "--profile-model", profile.model,
                    "--profile-key-fingerprint", profile.identity["api_key_fingerprint"],
                ]
            if port_open(8000):
                subprocess.run(
                    [*agents_command, "verify-profile", *profile_args],
                    cwd=ROOT,
                    env=env,
                    check=True,
                )
            else:
                executable = Path(sys.executable).parent / ("flower-superlink.exe" if os.name == "nt" else "flower-superlink")
                if not executable.exists():
                    raise RuntimeError("Flower extra is missing. Run uv sync --extra flower --extra dev.")
                superlink_env = superlink_environment(env, profile)
                superlink = spawn(
                    [str(executable), "--insecure", "--host", "127.0.0.1", "--port", "8000",
                     "--fleet-api-address", "127.0.0.1:9092", "--disable-runtime-dependency-installation"],
                    "flower-superlink",
                    child_env=superlink_env,
                )
                flower_superlink_started = True
                flower_superlink_pid = superlink.pid
                wait_port(8000, "flower-superlink")
                wait_flower_control_api()
                write_profile_manifest(SUPERLINK_PROFILE_FILE, profile, superlink.pid)
            current_state = httpx.get(f"{CONTROLLER}/api/state", timeout=2).json()
            expected_model = profile.model if profile else None
            running_model = current_state.get("flower", {}).get("model")
            if running_model != expected_model:
                raise RuntimeError(
                    "The running Noesis controller has a different NOESIS_MODEL. "
                    "Restart the controller through start.ps1 with the selected profile."
                )
            healthy = {a.get("agent_id") for a in current_state.get("flower", {}).get("agents", []) if a.get("healthy")}
            expected = {"director", "camera-closeup1", "camera-closeup2", "camera-closeup3", "camera-closeup4"}
            expected_director_mode = "llm" if profile else "rules"
            current_director_mode = current_state.get("flower", {}).get("decision_modes", {}).get("director")
            if expected.issubset(healthy):
                if current_director_mode != expected_director_mode:
                    raise RuntimeError(
                        f"Healthy Flower AgentApps use Director mode {current_director_mode!r}; "
                        f"stop them before selecting {expected_director_mode!r}."
                    )
                print("Reusing five healthy Flower AgentApps.", flush=True)
            else:
                director_args = (["--director-mode", "llm", "--model", profile.model] if profile else [])
                subprocess.run(
                    [*agents_command, "start", "--duration-s", str(args.duration_s), *profile_args, *director_args],
                    cwd=ROOT,
                    env=env,
                    check=True,
                )
                agents_started = True

        ready = True
        print(f"\nNoesis ready: {CONTROLLER}", flush=True)
        print("Choose your input/output in the dashboard, then Start session. Ctrl+C stops this launcher.", flush=True)
        print(f"Director profile: {profile.name if profile else 'rules'}; logs: .runtime/", flush=True)
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
                try:
                    run_state = json.loads((RUNTIME / "flower-agents.json").read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    run_state = {}
                stop_args = [sys.executable, "scripts/flower_agents.py", "stop"]
                if flower_superlink_started or run_state.get("superlink_started_here"):
                    stop_args.append("--stop-superlink")
                subprocess.run(stop_args, cwd=ROOT, env=env, timeout=20)
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
        if flower_superlink_started:
            try:
                manifest = json.loads(SUPERLINK_PROFILE_FILE.read_text(encoding="utf-8"))
                if manifest.get("pid") == flower_superlink_pid:
                    SUPERLINK_PROFILE_FILE.unlink(missing_ok=True)
            except (OSError, ValueError):
                pass
        for stream in streams:
            stream.close()


if __name__ == "__main__":
    raise SystemExit(main())
