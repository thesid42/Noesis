"""Run six AI roles in one local Flower AgentApp, with Nebius model inference."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import httpx
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from noesis.model_config import launcher_environment, load_model_profile
from noesis.model_gateway import ensure_gateway_token, load_provider_targets, model_catalog_json
from supergrid_agents import port_open, read, write

RUNTIME = ROOT / ".runtime" / "local"
RUN_FILE = RUNTIME / "run.json"
STOP_FILE = RUNTIME / "stop-request"
API_URL = "http://127.0.0.1:8000"


def environments(source: dict[str, str], profile_name: str, inference_transport: str = "gateway") -> tuple[dict, dict, dict]:
    """Only the loopback gateway receives Nebius provider keys."""
    if inference_transport not in {"flower", "gateway"}:
        raise ValueError("Inference transport must be flower or gateway.")
    targets = load_provider_targets(source)
    profile = load_model_profile(profile_name, source)
    if not any(item.profile == profile_name for item in targets.values()):
        raise ValueError(f"Configure the {profile_name} Nebius profile before starting.")
    env = launcher_environment(source, profile)
    for key in tuple(env):
        if key.startswith(("FLWR_RUNTIME_", "NOESIS_GATEWAY_", "NOESIS_AGENT_GATEWAY_")):
            env.pop(key, None)
    env.update(
        NOESIS_MODEL_CATALOG_JSON=model_catalog_json(targets),
        NOESIS_RUNTIME_DEPLOYMENT="local",
        NOESIS_INFERENCE_TRANSPORT=inference_transport,
        NOESIS_GRID_RUN_FILE=str(RUN_FILE),
        NOESIS_HOST="127.0.0.1", NOESIS_PORT="8765", PYTHONUTF8="1",
        FLWR_HOME=str(RUNTIME / "flower-home"),
        UV_CACHE_DIR=str(ROOT / ".runtime" / "uv-cache"),
        FLWR_SUPEREXEC_TASK_POLL_INTERVAL="0.1",
    )
    env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
    token = ensure_gateway_token(RUNTIME / "gateway-token", {})
    gateway = {**env, "NOESIS_GATEWAY_TOKEN": token}
    gateway.update({key: value for key, value in source.items() if key.startswith(("NEBIUS_KIMI_", "NEBIUS_MINIMAX_"))})
    superlink = {**env, "FLWR_MODEL_API_ENDPOINT": "http://127.0.0.1:8770/v1/responses", "FLWR_MODEL_API_KEY": token}
    if inference_transport == "gateway":
        superlink["NOESIS_AGENT_GATEWAY_TOKEN"] = token
    superlink.pop("OBS_PASSWORD", None)
    return env, gateway, superlink


def serve(args) -> None:
    from flwr.cli.chat.chat_local_agent import build_local_agent
    from flwr.proto.control_pb2 import ListRunsRequest, StartRunRequest, StopRunRequest
    from flwr.proto.fab_pb2 import Fab
    from flwr.supercore.control import ControlHttpClient

    if args.agent_budget_s < 10 or args.agent_budget_s > 43200:
        raise ValueError("Runtime duration must be between 10 and 43200 seconds.")
    if any(port_open(port) for port in (8765, 8770, 8000, 9092)):
        raise RuntimeError("A Noesis service port is occupied. Stop the previous launcher before starting local mode.")
    RUNTIME.mkdir(parents=True, exist_ok=True)
    STOP_FILE.unlink(missing_ok=True)
    source = {key: value for key, value in dotenv_values(ROOT / ".env").items() if value is not None}
    source.update(os.environ)
    inference_transport = getattr(args, "inference_transport", None) or "gateway"
    env, gateway_env, superlink_env = environments(source, args.model_profile, inference_transport)
    if getattr(args, "trace_inference", False):
        gateway_env["NOESIS_GATEWAY_TIMING_LOG_PATH"] = str(RUNTIME / "gateway-timings.jsonl")
    processes: list[tuple[str, subprocess.Popen]] = []
    streams = []
    control = ControlHttpClient(API_URL, timeout=5.0)
    run_id = None
    app_started = False
    final_status = "stopped"
    metadata = {"deployment": "local", "inference_transport": inference_transport,
                "status": "starting", "sub_status": "", "checked_at": time.time()}
    write(RUN_FILE, metadata)

    def spawn(command, name, child_env, cwd=ROOT):
        out = (RUNTIME / f"{name}.out.log").open("a", encoding="utf-8")
        err = (RUNTIME / f"{name}.err.log").open("a", encoding="utf-8")
        streams.extend((out, err))
        process = subprocess.Popen(command, cwd=cwd, env=child_env, stdin=subprocess.DEVNULL,
                                   stdout=out, stderr=err, start_new_session=os.name != "nt",
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        processes.append((name, process))
        return process

    def ensure_alive():
        failed = [name for name, process in processes if name != "obs" and process.poll() is not None]
        if failed:
            raise RuntimeError(f"Local services exited: {', '.join(failed)}. Inspect .runtime/local logs.")

    def wait_ready(check, label):
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            ensure_alive()
            try:
                check()
                return
            except Exception:
                time.sleep(0.25)
        raise RuntimeError(f"{label} did not become ready. Inspect .runtime/local logs.")

    def http_ready(url):
        httpx.get(url, timeout=1, trust_env=False).raise_for_status()

    try:
        spawn([sys.executable, "-m", "noesis.model_gateway"], "model-gateway", gateway_env)
        wait_ready(lambda: http_ready("http://127.0.0.1:8770/health"), "Model gateway")
        spawn([sys.executable, "-m", "noesis"], "app", env)
        app_started = True
        wait_ready(lambda: http_ready("http://127.0.0.1:8765/api/state"), "Control room")
        if args.obs:
            if not port_open(4455):
                obs = ROOT / ".runtime" / "obs/bin/64bit/obs64.exe"
                if not obs.exists():
                    raise RuntimeError("Start OBS with its WebSocket server enabled, or install portable OBS first.")
                spawn([str(obs), "--portable", "--disable-updater", "--minimize-to-tray", "--disable-missing-files-check", "--profile", "Noesis", "--collection", "Noesis"], "obs", env, obs.parent)
            for endpoint in ("connect", "setup"):
                wait_ready(lambda endpoint=endpoint: httpx.post(f"http://127.0.0.1:8765/api/obs/{endpoint}", json={}, timeout=5, trust_env=False).raise_for_status(), f"OBS {endpoint}")
        executable = Path(sys.executable).parent / ("flower-superlink.exe" if os.name == "nt" else "flower-superlink")
        spawn([str(executable), "--insecure", "--host", "127.0.0.1", "--port", "8000",
               "--fleet-api-address", "127.0.0.1:9092", "--disable-runtime-dependency-installation"], "superlink", superlink_env)
        wait_ready(lambda: control.ListRuns(ListRunsRequest()), "Local Flower SuperLink")
        bundle = build_local_agent(ROOT / "flower_apps")
        response = control.StartRun(StartRunRequest(
            fab=Fab(hash_str=bundle.fab_hash, content=bundle.fab_content),
            user_prompt=json.dumps({"mode": "local_crew", "duration_s": args.agent_budget_s}),
        ))
        if not response.run_id:
            raise RuntimeError("Local Flower did not return a run ID.")
        run_id = str(response.run_id)
        metadata.update(run_id=run_id, status="pending", checked_at=time.time())
        write(RUN_FILE, metadata)
        print(f"Local Flower run {run_id}: six AI roles, Nebius {args.model_profile} inference.", flush=True)
        print(f"Inference transport: {inference_transport}.", flush=True)
        print("Noesis: http://127.0.0.1:8765 - Ctrl+C stops owned services.", flush=True)
        deadline = time.monotonic() + args.agent_budget_s + 60
        startup_deadline = time.monotonic() + 60
        announced = False
        while time.monotonic() < deadline:
            if STOP_FILE.exists():
                break
            ensure_alive()
            runs = control.ListRuns(ListRunsRequest(run_id=int(run_id))).run_dict
            run = runs.get(int(run_id))
            if run is not None:
                metadata.update(status=run.status.status, sub_status=run.status.sub_status, checked_at=time.time())
                write(RUN_FILE, metadata)
                if run.status.status == "finished":
                    final_status = run.status.sub_status
                    if final_status != "completed":
                        raise RuntimeError(f"Local Flower run ended with {final_status}; inspect .runtime/local logs.")
                    break
                if run.status.status == "running" and not announced:
                    print("Local Flower crew is running; start a replay in the control room.", flush=True)
                    announced = True
            if not announced and time.monotonic() > startup_deadline:
                raise RuntimeError("Local Flower run did not start within 60 seconds.")
            time.sleep(0.5)
    except Exception:
        final_status = "failed"
        raise
    finally:
        if app_started:
            try:
                httpx.post("http://127.0.0.1:8765/api/session/stop", json={}, timeout=10, trust_env=False)
            except httpx.HTTPError:
                pass
        if run_id:
            try:
                control.StopRun(StopRunRequest(run_id=int(run_id)))
            except Exception:
                print("Local Flower stop request failed; shutting down owned runtime processes.", flush=True)
        for name, process in reversed(processes):
            if process.poll() is None and name != "obs":
                if os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True)
                else:
                    import signal
                    try:
                        os.killpg(process.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    if os.name != "nt":
                        os.killpg(process.pid, signal.SIGKILL)
                    process.kill()
                    process.wait(timeout=5)
        metadata.update(status="finished", sub_status=final_status, checked_at=time.time())
        write(RUN_FILE, metadata)
        control.close()
        for stream in streams:
            stream.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("serve", "start", "status", "stop"), nargs="?", default="serve")
    parser.add_argument("--obs", action="store_true")
    parser.add_argument("--model-profile", choices=("kimi", "minimax"), default="kimi")
    parser.add_argument("--duration-s", "--agent-budget-s", dest="agent_budget_s", type=int, default=3600)
    parser.add_argument("--trace-inference", action="store_true", help="Record private, payload-free inference timings.")
    parser.add_argument("--inference-transport", choices=("flower", "gateway"), default="gateway",
                        help="Use persistent gateway connections (default), or opt into Flower model tasks.")
    args = parser.parse_args()
    if args.command == "status":
        print(json.dumps(read(RUN_FILE, {}), indent=2))
    elif args.command == "stop":
        RUNTIME.mkdir(parents=True, exist_ok=True)
        STOP_FILE.write_text("stop", encoding="ascii")
        print("Requested local launcher shutdown.")
    else:
        serve(args)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Noesis local runtime stopped.")
