"""Start, inspect, and stop Noesis's five local Flower AgentApp runs."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from urllib.parse import urlsplit

from dotenv import dotenv_values


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from noesis.model_config import (  # noqa: E402
    ModelProfile,
    ModelProfileError,
    PROFILE_MANIFEST,
    launcher_environment,
    load_model_profile,
    read_profile_manifest,
    require_matching_profile,
    superlink_environment,
    write_profile_manifest,
)

FLOWER_APP_DIR = PROJECT_ROOT / "flower_apps"
RUNTIME_DIR = PROJECT_ROOT / ".runtime"
MANAGED_RUNS_FILE = RUNTIME_DIR / "flower-agents.json"
SUPERLINK_PROFILE_FILE = PROJECT_ROOT / PROFILE_MANIFEST
CAMERA_IDS = ("closeup1", "closeup2", "closeup3", "closeup4")
DEFAULT_API_URL = "http://127.0.0.1:8000"
DEFAULT_CONTROLLER_URL = "http://127.0.0.1:8765"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_state() -> dict[str, Any]:
    try:
        state = json.loads(MANAGED_RUNS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"schema_version": 1, "runs": []}
    return state if isinstance(state, dict) and isinstance(state.get("runs"), list) else {"schema_version": 1, "runs": []}


def _write_state(state: dict[str, Any]) -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    temporary = MANAGED_RUNS_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, indent=2), encoding="utf-8")
    temporary.replace(MANAGED_RUNS_FILE)


def _flower_client(api_url: str):
    try:
        from flwr.supercore.control import ControlHttpClient
    except ImportError as exc:
        raise RuntimeError("Flower is not installed in this Python environment; use .venv/Scripts/python.exe.") from exc
    return ControlHttpClient(api_url.rstrip("/"), timeout=4.0)


def _list_runs(client):
    from flwr.proto.control_pb2 import ListRunsRequest

    response = client.ListRuns(ListRunsRequest(limit=1000))
    return {str(run_id): run for run_id, run in response.run_dict.items()}


def _status_fields(run: Any) -> tuple[str, str, str]:
    status = getattr(run, "status", None)
    return (
        str(getattr(status, "status", "unknown") or "unknown").lower(),
        str(getattr(status, "sub_status", "") or "").lower(),
        str(getattr(status, "details", "") or ""),
    )


def _active(run: Any | None) -> bool:
    if run is None:
        return False
    status, sub_status, _ = _status_fields(run)
    return status not in {"finished", "failed", "stopped", "cancelled", "canceled", "completed"} and sub_status not in {"completed", "failed", "stopped", "cancelled", "canceled"}


def _http_state(controller_url: str) -> dict[str, Any]:
    request = Request(f"{controller_url.rstrip('/')}/api/state", headers={"Accept": "application/json"})
    try:
        with urlopen(request, timeout=2.0) as response:
            payload = json.loads(response.read(256_001).decode("utf-8"))
    except HTTPError as exc:
        raise RuntimeError(f"Noesis controller returned HTTP {exc.code}.") from exc
    except (URLError, OSError, TimeoutError) as exc:
        raise RuntimeError(f"Noesis controller is unavailable at {controller_url}: {type(exc).__name__}.") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Noesis controller returned invalid state JSON.") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("session"), dict):
        raise RuntimeError("Noesis controller did not return a session state object.")
    return payload


def _profile_environment(args: argparse.Namespace) -> tuple[dict[str, str], ModelProfile | None]:
    # Read .env as values rather than injecting provider keys into this process.
    source = {key: value for key, value in dotenv_values(PROJECT_ROOT / ".env").items() if value is not None}
    source.update(os.environ)
    if getattr(args, "model_endpoint", None):
        names = {
            "kimi": "NEBIUS_KIMI_API_ENDPOINT",
            "minimax": "NEBIUS_MINIMAX_API_ENDPOINT",
        }
        source[names[args.model_profile]] = args.model_endpoint
    if getattr(args, "profile_model", None):
        names = {"kimi": "NEBIUS_KIMI_MODEL", "minimax": "NEBIUS_MINIMAX_MODEL"}
        source[names[args.model_profile]] = args.profile_model
    if getattr(args, "profile_key_fingerprint", None):
        source["NOESIS_PROFILE_KEY_FINGERPRINT"] = args.profile_key_fingerprint
    profile = load_model_profile(args.model_profile, source)
    return source, profile


def _environment(source: dict[str, str], profile: ModelProfile | None) -> dict[str, str]:
    env = launcher_environment(source, profile)
    scripts_dir = PROJECT_ROOT / ".venv" / "Scripts"
    env["PATH"] = f"{scripts_dir}{os.pathsep}{env.get('PATH', '')}"
    env["FLWR_HOME"] = str(RUNTIME_DIR / "flower-home")
    env["UV_CACHE_DIR"] = str(RUNTIME_DIR / "uv-cache")
    env["PYTHONUTF8"] = "1"
    return env


def _port_open(api_url: str) -> bool:
    parsed = urlsplit(api_url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        with socket.create_connection((host, port), timeout=0.3):
            return True
    except OSError:
        return False


def _ensure_superlink(
    api_url: str,
    profile: ModelProfile | None,
    source_environment: dict[str, str],
) -> tuple[Any, int | None, int | None]:
    client = _flower_client(api_url)
    try:
        _list_runs(client)
    except Exception:
        client.close()
        if _port_open(api_url):
            # A live but unverifiable service must never silently retain old credentials.
            raise RuntimeError(
                "Flower SuperLink is occupying the configured port but its API/profile cannot be verified. "
                "Stop it and restart using this launcher."
            )
    else:
        try:
            manifest = require_matching_profile(SUPERLINK_PROFILE_FILE, profile)
        except Exception:
            client.close()
            raise
        pid_value = manifest.get("pid")
        service_pid = int(pid_value) if isinstance(pid_value, int) else None
        print(f"Flower SuperLink ready at {api_url} (profile: {profile.name if profile else 'rules'}; reusing matching service).")
        return client, None, service_pid

    scripts_dir = PROJECT_ROOT / ".venv" / "Scripts"
    executable = scripts_dir / "flower-superlink.exe"
    if not executable.is_file():
        executable = scripts_dir / "flower-superlink"
    if not executable.is_file():
        raise RuntimeError(f"Flower SuperLink executable not found under {scripts_dir}.")
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    stdout_path = RUNTIME_DIR / "flower-superlink.out.log"
    stderr_path = RUNTIME_DIR / "flower-superlink.err.log"
    stdout_handle = stdout_path.open("ab")
    stderr_handle = stderr_path.open("ab")
    try:
        process = subprocess.Popen(
            [
                str(executable), "--insecure", "--host", "127.0.0.1", "--port", "8000",
                "--fleet-api-address", "127.0.0.1:9092", "--disable-runtime-dependency-installation",
            ],
            cwd=PROJECT_ROOT,
            env=superlink_environment(_environment(source_environment, profile), profile),
            stdin=subprocess.DEVNULL,
            stdout=stdout_handle,
            stderr=stderr_handle,
            close_fds=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    finally:
        stdout_handle.close()
        stderr_handle.close()

    deadline = time.monotonic() + 15.0
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"Flower SuperLink exited with code {process.returncode}; see {stderr_path}.")
        try:
            client = _flower_client(api_url)
            _list_runs(client)
            write_profile_manifest(SUPERLINK_PROFILE_FILE, profile, process.pid)
            print(f"Started Flower SuperLink (PID {process.pid}) at {api_url}; profile: {profile.name if profile else 'rules'}.")
            return client, process.pid, process.pid
        except Exception as exc:
            last_error = exc
            if client:
                client.close()
            time.sleep(0.4)
    process.terminate()
    raise RuntimeError(f"Flower SuperLink did not become ready at {api_url}: {last_error}")


def _start(args: argparse.Namespace) -> int:
    _http_state(args.controller_url)
    source_environment, profile = _profile_environment(args)
    if profile and args.model and args.model != profile.model:
        raise RuntimeError("The --model value must match the selected Nebius profile.")
    director_mode = args.director_mode or ("llm" if profile else "rules")
    director_model = profile.model if profile else (args.model or source_environment.get("NOESIS_MODEL"))
    if director_mode == "llm" and not director_model:
        raise RuntimeError("Director LLM mode requires --model or a selected model profile.")

    client, started_pid, service_pid = _ensure_superlink(args.api_url, profile, source_environment)
    try:
        existing = _read_state()
        existing_runs = _list_runs(client)
        active = [
            entry for entry in existing["runs"]
            if _active(existing_runs.get(str(entry.get("run_id"))))
        ]
        if active:
            names = ", ".join(f"{entry.get('agent_id')}={entry.get('run_id')}" for entry in active)
            raise RuntimeError(f"Managed Flower AgentApps are already active: {names}. Run `status` or `stop` first.")

        from flwr.cli.chat.chat_local_agent import build_local_agent
        from flwr.proto.control_pb2 import StartRunRequest
        from flwr.proto.fab_pb2 import Fab

        local_agent = build_local_agent(FLOWER_APP_DIR)
        state: dict[str, Any] = {
            "schema_version": 1,
            "started_at": _now(),
            "api_url": args.api_url,
            "controller_url": args.controller_url,
            "model_profile": profile.name if profile else "rules",
            "superlink_pid": service_pid,
            "superlink_started_here": started_pid is not None,
            "runs": [],
        }
        specs = [
            {"role": "camera", "camera_id": camera_id, "agent_id": f"camera-{camera_id}", "decision_mode": "rules"}
            for camera_id in CAMERA_IDS
        ] + [{"role": "director", "agent_id": "director", "decision_mode": director_mode, "model": director_model}]
        for spec in specs:
            config = {
                **spec,
                "controller_url": args.controller_url.rstrip("/"),
                "duration_s": args.duration_s,
                "camera_interval_s": args.camera_interval_s,
            }
            prompt = json.dumps(config, separators=(",", ":"), allow_nan=False)
            request = StartRunRequest(
                fab=Fab(hash_str=local_agent.fab_hash, content=local_agent.fab_content),
                user_prompt=prompt,
            )
            response = client.StartRun(request)
            entry = {
                "run_id": str(response.run_id),
                "agent_id": spec["agent_id"],
                "role": spec["role"],
                "camera_id": spec.get("camera_id"),
                "decision_mode": spec.get("decision_mode", "rules"),
                "started_at": _now(),
            }
            state["runs"].append(entry)
            _write_state(state)
            print(f"Started {entry['agent_id']} Flower run {entry['run_id']}.")
        _write_state(state)
        print(f"Started {len(state['runs'])} separate Flower AgentApp runs. Director mode: {director_mode}; profile: {profile.name if profile else 'rules'}.")
        return 0
    except Exception:
        # If a partial start failed, stop only the run IDs created in this call.
        state = locals().get("state")
        if isinstance(state, dict):
            _stop_entries(client, state.get("runs", []), quiet=True)
            state["failed_at"] = _now()
            _write_state(state)
        raise
    finally:
        client.close()


def _verify_profile(args: argparse.Namespace) -> int:
    source_environment, profile = _profile_environment(args)
    del source_environment
    client = _flower_client(args.api_url)
    try:
        _list_runs(client)
        require_matching_profile(SUPERLINK_PROFILE_FILE, profile)
        print(f"Flower SuperLink profile verified: {profile.name if profile else 'rules'}.")
        return 0
    except Exception as exc:
        print(f"Flower SuperLink profile verification failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    finally:
        client.close()


def _stop_entries(client, entries: list[dict[str, Any]], *, quiet: bool = False) -> list[str]:
    from flwr.proto.control_pb2 import StopRunRequest

    errors = []
    run_map = _list_runs(client)
    for entry in entries:
        run_id_text = str(entry.get("run_id", ""))
        try:
            run_id = int(run_id_text)
        except ValueError:
            errors.append(f"Invalid managed run ID {run_id_text!r}.")
            continue
        run = run_map.get(run_id_text)
        if not _active(run):
            if not quiet:
                print(f"Run {run_id_text} ({entry.get('agent_id')}) is already finished.")
            continue
        try:
            response = client.StopRun(StopRunRequest(run_id=run_id))
            if not response.success:
                errors.append(f"Flower did not confirm stop for run {run_id_text}.")
            elif not quiet:
                print(f"Stopped {entry.get('agent_id')} Flower run {run_id_text}.")
        except Exception as exc:
            errors.append(f"Could not stop run {run_id_text}: {type(exc).__name__}.")
    return errors


def _status(args: argparse.Namespace) -> int:
    state = _read_state()
    if not state["runs"]:
        print("No launcher-managed Flower AgentApp runs are recorded.")
        return 0
    client = _flower_client(args.api_url)
    try:
        runs = _list_runs(client)
    except Exception as exc:
        print(f"Flower SuperLink unavailable at {args.api_url}: {type(exc).__name__}: {exc}")
        return 2
    finally:
        client.close()
    for entry in state["runs"]:
        run = runs.get(str(entry.get("run_id")))
        if run is None:
            print(f"{entry.get('agent_id')} run {entry.get('run_id')}: NOT FOUND")
            continue
        status, sub_status, detail = _status_fields(run)
        suffix = f" · {sub_status}" if sub_status else ""
        print(f"{entry.get('agent_id')} run {entry.get('run_id')}: {status}{suffix}{f' · {detail}' if detail else ''}")
    print(f"SuperLink: {args.api_url}. Controller: {state.get('controller_url', DEFAULT_CONTROLLER_URL)}.")
    return 0


def _stop(args: argparse.Namespace) -> int:
    state = _read_state()
    if not state["runs"] and not args.stop_superlink:
        print("No launcher-managed Flower AgentApp runs are recorded.")
        return 0
    errors = []
    if state["runs"]:
        client = _flower_client(args.api_url)
        try:
            errors = _stop_entries(client, state["runs"])
        except Exception as exc:
            print(f"Could not query Flower runs at {args.api_url}: {type(exc).__name__}: {exc}")
            return 2
        finally:
            client.close()
    state["stopped_at"] = _now()
    _write_state(state)
    for error in errors:
        print(error, file=sys.stderr)
    if args.stop_superlink:
        manifest = read_profile_manifest(SUPERLINK_PROFILE_FILE)
        pid_value = manifest.get("pid") if manifest else None
        expected_pid = state.get("superlink_pid")
        if isinstance(pid_value, int) and pid_value == expected_pid:
            try:
                if os.name == "nt":
                    result = subprocess.run(["taskkill", "/PID", str(pid_value), "/T", "/F"], capture_output=True, text=True)
                    if result.returncode != 0 and "not found" not in (result.stderr + result.stdout).lower():
                        errors.append("Could not stop launcher-owned Flower SuperLink; see its .runtime log.")
                else:
                    os.kill(pid_value, 15)
                SUPERLINK_PROFILE_FILE.unlink(missing_ok=True)
                print("Stopped the Flower SuperLink started by this launcher.")
            except OSError:
                errors.append("Could not stop launcher-owned Flower SuperLink.")
        else:
            errors.append("SuperLink ownership record changed; left the service running.")
    else:
        print("Flower AgentApp stop complete. The shared SuperLink was left running.")
    return 1 if errors else 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    start = subparsers.add_parser("start", help="start four camera runs and one Director run")
    start.add_argument("--api-url", default=DEFAULT_API_URL, help="Flower SuperLink Control API URL")
    start.add_argument("--controller-url", default=DEFAULT_CONTROLLER_URL, help="Noesis HTTP broker URL")
    start.add_argument("--duration-s", type=float, default=3600.0, help="maximum runtime for each AgentApp")
    start.add_argument("--camera-interval-s", type=float, default=0.20, help="camera snapshot sampling interval")
    start.add_argument("--director-mode", choices=("rules", "llm"), default=None, help="rules or optional Flower-runtime model mode (profile defaults to llm)")
    start.add_argument("--model", default=None, help="Flower runtime model name for --director-mode llm")
    start.add_argument("--model-profile", choices=("kimi", "minimax"), default=None, help="use a configured Nebius model profile and LLM directing")
    start.add_argument("--model-endpoint", default=None, help=argparse.SUPPRESS)
    start.add_argument("--profile-model", default=None, help=argparse.SUPPRESS)
    start.add_argument("--profile-key-fingerprint", default=None, help=argparse.SUPPRESS)
    start.set_defaults(handler=_start)
    verify = subparsers.add_parser("verify-profile", help="verify the running SuperLink matches configured model credentials")
    verify.add_argument("--api-url", default=DEFAULT_API_URL)
    verify.add_argument("--model-profile", choices=("kimi", "minimax"), default=None)
    verify.add_argument("--model-endpoint", default=None, help=argparse.SUPPRESS)
    verify.add_argument("--profile-model", default=None, help=argparse.SUPPRESS)
    verify.add_argument("--profile-key-fingerprint", default=None, help=argparse.SUPPRESS)
    verify.set_defaults(handler=_verify_profile)
    status = subparsers.add_parser("status", help="show the status of launcher-managed runs")
    status.add_argument("--api-url", default=DEFAULT_API_URL)
    status.set_defaults(handler=_status)
    stop = subparsers.add_parser("stop", help="stop only the run IDs recorded by this launcher")
    stop.add_argument("--api-url", default=DEFAULT_API_URL)
    stop.add_argument("--stop-superlink", action="store_true", help="also stop this launcher-owned SuperLink")
    stop.set_defaults(handler=_stop)
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        return args.handler(args)
    except Exception as exc:
        print(f"Flower AgentApp launcher failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
