"""Run Noesis's AI crew on authenticated SuperGrid with six local SuperNodes."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import time

import httpx
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from noesis.model_config import launcher_environment, load_model_profile

RUNTIME = ROOT / ".runtime" / "supergrid"
MANIFEST = RUNTIME / "nodes.json"
RUN_FILE = RUNTIME / "run.json"
FEDERATION = "@thesid42/noesis"
SPECS = [{"agent_id": f"camera-closeup{i}", "role": "camera", "camera_id": f"closeup{i}"} for i in range(1, 5)] + [{"agent_id": "critic", "role": "critic"}, {"agent_id": "director", "role": "director"}]


def read(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2), encoding="utf-8")
    # Windows readers can briefly hold the destination without delete sharing.
    for delay in (0.02, 0.05, 0.1, 0.25, 0.5):
        try:
            temporary.replace(path)
            return
        except PermissionError:
            time.sleep(delay)
    temporary.replace(path)


def client():
    # Use the normal CLI's authenticated connection; never copy its tokens.
    from flwr.cli.flower_config import read_superlink_connection
    from flwr.cli.utils import init_http_client_from_connection
    return init_http_client_from_connection(read_superlink_connection("supergrid"))


def setup_nodes() -> dict:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from flwr.proto.control_pb2 import RegisterNodeRequest, AddNodeToFederationRequest
    from flwr.supercore.primitives.asymmetric import public_key_to_bytes
    state = read(MANIFEST, {"federation": FEDERATION, "nodes": []})
    if state["federation"] != FEDERATION:
        raise RuntimeError("Existing nodes belong to a different federation.")
    control = client()
    try:
        for spec in SPECS:
            entry = next((n for n in state["nodes"] if n["agent_id"] == spec["agent_id"]), None)
            key_path = RUNTIME / "keys" / spec["agent_id"]
            if entry and not key_path.is_file():
                raise RuntimeError(f"Private node key is missing for {spec['agent_id']}; recover it before re-registering.")
            if not key_path.exists():
                key_path.parent.mkdir(parents=True, exist_ok=True)
                private = ec.generate_private_key(ec.SECP384R1())
                key_path.write_bytes(private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH, serialization.NoEncryption()))
                key_path.with_suffix(".pub").write_bytes(private.public_key().public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH))
            if entry is None:
                private = serialization.load_ssh_private_key(key_path.read_bytes(), password=None)
                response = control.RegisterNode(RegisterNodeRequest(public_key=public_key_to_bytes(private.public_key()), name=f"Noesis {spec['agent_id']}"))
                if not response.node_id:
                    raise RuntimeError("SuperGrid did not return a registered node ID.")
                entry = {**spec, "node_id": str(response.node_id), "member": False}
                state["nodes"].append(entry)
                write(MANIFEST, state)
            if not entry.get("member"):
                control.AddNodeToFederation(AddNodeToFederationRequest(federation_name=FEDERATION, node_id=int(entry["node_id"])))
                entry["member"] = True
                write(MANIFEST, state)
            print(f"Registered {spec['agent_id']}: {entry['node_id']}", flush=True)
    finally:
        control.close()
    return state


def start_run(control, nodes: dict, duration_s: int = 240) -> str:
    from flwr.cli.chat.chat_local_agent import build_local_agent
    from flwr.proto.control_pb2 import StartRunRequest
    from flwr.proto.fab_pb2 import Fab
    bundle = build_local_agent(ROOT / "flower_apps")
    prompt = {"mode": "coordinator", "duration_s": min(240, duration_s), "node_ids": [int(n["node_id"]) for n in nodes["nodes"]]}
    response = control.StartRun(StartRunRequest(fab=Fab(hash_str=bundle.fab_hash, content=bundle.fab_content), federation=FEDERATION, user_prompt=json.dumps(prompt)))
    if not response.run_id:
        raise RuntimeError("SuperGrid did not return a run ID.")
    run_id = str(response.run_id)
    write(RUN_FILE, {"run_id": run_id, "federation": FEDERATION, "node_ids": prompt["node_ids"], "duration_s": prompt["duration_s"]})
    print(f"SuperGrid coordinator run: {run_id}", flush=True)
    return run_id


def port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
            return True
    except OSError:
        return False


def serve(args) -> None:
    from flwr.proto.control_pb2 import ListNodesRequest, ListRunsRequest, StopRunRequest
    nodes = read(MANIFEST, {})
    if len(nodes.get("nodes", [])) != 6:
        raise RuntimeError("Run scripts/supergrid_agents.py setup first to register the six local nodes.")
    config = {k: v for k, v in dotenv_values(ROOT / ".env").items() if v is not None}
    config.update(os.environ)
    profiles = {name: load_model_profile(name, config) for name in ("kimi", "minimax")}
    catalog = [{"id": name, "label": "Kimi K2.7" if name == "kimi" else "MiniMax M3", "model": profile.model, "available": True} for name, profile in profiles.items()]
    selected = profiles[args.model_profile]
    env = launcher_environment(config, selected)
    env["NOESIS_MODEL_CATALOG_JSON"] = json.dumps(catalog)
    env["PYTHONUTF8"] = "1"
    env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
    env["UV_CACHE_DIR"] = str(ROOT / ".runtime" / "uv-cache")
    env["FLWR_SUPEREXEC_TASK_POLL_INTERVAL"] = "0.1"
    env["NOESIS_GRID_RUN_FILE"] = str(RUN_FILE)
    token_path = RUNTIME / "gateway-token"
    if not token_path.exists():
        token_path.write_text(secrets.token_hex(32), encoding="ascii")
    token = token_path.read_text(encoding="ascii").strip()
    if any(port_open(p) for p in (8765, 8770, *range(9100, 9106))):
        raise RuntimeError("A Noesis service port is occupied; stop the previous launcher before starting SuperGrid mode.")
    processes = []
    streams = []
    control = client()
    run_id = None

    def spawn(command, name, child_env, cwd=ROOT):
        out = (RUNTIME / f"{name}.out.log").open("a", encoding="utf-8")
        err = (RUNTIME / f"{name}.err.log").open("a", encoding="utf-8")
        streams.extend((out, err))
        process = subprocess.Popen(command, cwd=cwd, env=child_env, stdout=out, stderr=err,
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        processes.append((name, process))
        return process

    def wait_local(url):
        until = time.monotonic() + 30
        while time.monotonic() < until:
            try:
                httpx.get(url, timeout=1).raise_for_status()
                return
            except httpx.HTTPError:
                time.sleep(0.25)
        raise RuntimeError(f"Local service did not start: {url}")

    try:
        gateway_env = {**env, "NOESIS_GATEWAY_TOKEN": token}
        # Only the loopback gateway receives provider credentials, including env-only configuration.
        gateway_env.update({k: v for k, v in config.items() if k.startswith(("NEBIUS_KIMI_", "NEBIUS_MINIMAX_"))})
        spawn([sys.executable, "-m", "noesis.model_gateway"], "model-gateway", gateway_env)
        wait_local("http://127.0.0.1:8770/health")
        spawn([sys.executable, "-m", "noesis"], "app", env)
        wait_local("http://127.0.0.1:8765/api/state")
        if args.obs:
            if not port_open(4455):
                obs = ROOT / ".runtime" / "obs/bin/64bit/obs64.exe"
                if not obs.exists():
                    raise RuntimeError("Install/configure portable OBS first.")
                spawn([str(obs), "--portable", "--disable-updater", "--minimize-to-tray", "--disable-missing-files-check", "--profile", "Noesis", "--collection", "Noesis"], "obs", env, obs.parent)
            for endpoint in ("connect", "setup"):
                for attempt in range(30):
                    try:
                        httpx.post(f"http://127.0.0.1:8765/api/obs/{endpoint}", json={}, timeout=5).raise_for_status()
                        break
                    except httpx.HTTPError:
                        if attempt == 29:
                            raise
                        time.sleep(0.5)
        for index, entry in enumerate(nodes["nodes"]):
            name = entry["agent_id"]
            node_env = {**env, "FLWR_HOME": str(RUNTIME / name), "FLWR_MODEL_API_ENDPOINT": "http://127.0.0.1:8770/v1/responses", "FLWR_MODEL_API_KEY": token}
            node_env.pop("OBS_PASSWORD", None)
            node_config = {k: entry[k] for k in ("agent_id", "role", "camera_id") if k in entry}
            node_config["controller_url"] = "http://127.0.0.1:8765"
            config_text = " ".join(f"{key}={json.dumps(value)}" for key, value in node_config.items())
            executable = Path(sys.executable).parent / ("flower-supernode.exe" if os.name == "nt" else "flower-supernode")
            spawn([str(executable), "--superlink", "fleet-supergrid.flower.ai:443", "--auth-supernode-private-key", str(RUNTIME / "keys" / name), "--host", "127.0.0.1", "--port", str(9100 + index), "--node-config", config_text], name, node_env)
        expected = {int(n["node_id"]) for n in nodes["nodes"]}
        until = time.monotonic() + 60
        while time.monotonic() < until:
            live = {n.node_id for n in control.ListNodes(ListNodesRequest()).nodes_info if n.status == "online"}
            if expected.issubset(live):
                break
            failed = [name for name, p in processes if p.poll() is not None]
            if failed:
                raise RuntimeError(f"Services failed: {', '.join(failed)}. Inspect local runtime logs.")
            time.sleep(1)
        else:
            raise RuntimeError("The six SuperNodes did not become online within 60 seconds.")
        run_id = start_run(control, nodes)
        print("Noesis ready at http://127.0.0.1:8765 â€” Kimi/MiniMax switch in the UI. Ctrl+C stops owned services.", flush=True)
        budget_end = time.monotonic() + args.agent_budget_s
        while time.monotonic() < budget_end:
            time.sleep(2)
            if any(p.poll() is not None for name, p in processes if name != "obs"):
                raise RuntimeError("A managed service exited. Inspect .runtime/supergrid logs.")
            runs = control.ListRuns(ListRunsRequest(run_id=int(run_id))).run_dict
            run = runs.get(int(run_id))
            if run:
                metadata = read(RUN_FILE, {})
                metadata.update(status=run.status.status, sub_status=run.status.sub_status, checked_at=time.time())
                write(RUN_FILE, metadata)
            if run and run.status.status == "finished":
                if run.status.sub_status != "completed":
                    raise RuntimeError(f"SuperGrid run stopped with {run.status.sub_status}; inspect its logs before retrying.")
                if budget_end - time.monotonic() < 10:
                    break
                run_id = start_run(control, nodes, min(240, int(budget_end - time.monotonic())))
        print("Agent runtime budget reached; stopping services.", flush=True)
    finally:
        try:
            httpx.post("http://127.0.0.1:8765/api/session/stop", json={}, timeout=10)
        except httpx.HTTPError:
            pass
        if run_id:
            try:
                control.StopRun(StopRunRequest(run_id=int(run_id)))
            except Exception:
                print("SuperGrid stop could not be confirmed; inspect the recorded run ID.")
        for name, process in reversed(processes):
            if process.poll() is None and name != "obs":
                if os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True)
                else:
                    process.terminate()
        control.close()
        for stream in streams:
            stream.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("setup", "serve", "status", "run", "stop"))
    parser.add_argument("--model-profile", choices=("kimi", "minimax"), default="kimi")
    parser.add_argument("--obs", action="store_true")
    parser.add_argument("--agent-budget-s", type=int, default=3600)
    args = parser.parse_args()
    os.chdir(ROOT)
    RUNTIME.mkdir(parents=True, exist_ok=True)
    if args.command == "setup":
        setup_nodes()
    elif args.command == "serve":
        serve(args)
    else:
        from flwr.proto.control_pb2 import ListNodesRequest, ListRunsRequest, StopRunRequest
        control = client()
        try:
            if args.command == "run":
                start_run(control, read(MANIFEST, {}))
            elif args.command == "stop":
                run = read(RUN_FILE, {})
                if run.get("run_id"):
                    control.StopRun(StopRunRequest(run_id=int(run["run_id"])))
            else:
                print(json.dumps({"nodes": [{"node_id": str(n.node_id), "status": n.status} for n in control.ListNodes(ListNodesRequest()).nodes_info], "managed_run": read(RUN_FILE, {})}, indent=2))
        finally:
            control.close()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Noesis SuperGrid launcher stopped.")
