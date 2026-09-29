"""Long-lived, HTTP-brokered Flower AgentApps for Noesis."""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as FutureTimeout
from datetime import datetime, timezone
import json
import math
import os
import threading
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from flwr.agentapp import AgentApp, AgentSession
from flwr.app import Context

from noesis_agents.policy import (
    CAMERA_IDS,
    make_camera_observation,
    observation_revision,
    rule_decision,
    usable_camera_reports,
    validate_model_decision,
)


RUNTIME = "Flower 1.39.0 AgentApp / application-managed HTTP broker"
DEFAULT_CONTROLLER_URL = "http://127.0.0.1:8765"
DEFAULT_INTERVAL_S = 0.20
DEFAULT_DIRECTOR_INTERVAL_S = 0.12
DEFAULT_HEARTBEAT_INTERVAL_S = 2.0
DEFAULT_REQUEST_TIMEOUT_S = 0.9
DEFAULT_DURATION_S = 3600.0


class BrokerError(RuntimeError):
    """An HTTP error from the local Noesis broker."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code


def _event(agent: AgentSession, event_type: str, **fields: Any) -> None:
    try:
        agent.events.emit({"type": event_type, **fields})
    except Exception:
        # Flower event streaming is useful for review, but must not stop directing.
        pass


def _log(event_type: str, **fields: Any) -> None:
    print(json.dumps({"type": event_type, **fields}, separators=(",", ":"), allow_nan=False), flush=True)


def _parse_config(agent: AgentSession, context: Context) -> dict[str, Any]:
    raw = agent.prompt
    parsed: Any = None
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = None
    if not isinstance(parsed, dict):
        prompt = context.run_config.get("agent.input")
        if isinstance(prompt, str):
            try:
                parsed = json.loads(prompt)
            except json.JSONDecodeError:
                parsed = None
    if not isinstance(parsed, dict):
        raise ValueError("Flower run prompt must be a JSON object.")
    return parsed


def _as_float(value: Any, default: float, minimum: float, maximum: float) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(result):
        return default
    return max(minimum, min(maximum, result))


def _http_json(
    base_url: str,
    path: str,
    *,
    payload: dict[str, Any] | None = None,
    timeout_s: float = DEFAULT_REQUEST_TIMEOUT_S,
) -> Any:
    data = None if payload is None else json.dumps(payload, separators=(",", ":"), allow_nan=False).encode("utf-8")
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = Request(f"{base_url.rstrip('/')}{path}", data=data, headers=headers, method="POST" if data is not None else "GET")
    try:
        with urlopen(request, timeout=timeout_s) as response:
            raw = response.read(64_001)
            if len(raw) > 64_000:
                raise BrokerError(response.status, "Broker response exceeded 64 KB.")
    except HTTPError as exc:
        raw = exc.read(8_193)
        try:
            error_body = json.loads(raw.decode("utf-8"))
            detail = error_body.get("detail", str(error_body)) if isinstance(error_body, dict) else str(error_body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            detail = raw.decode("utf-8", errors="replace")[:500]
        raise BrokerError(exc.code, str(detail)) from exc
    except (TimeoutError, URLError, OSError) as exc:
        raise BrokerError(0, f"Broker request failed: {type(exc).__name__}.") from exc
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise BrokerError(0, "Broker returned invalid JSON.") from exc


def _heartbeat_payload(config: dict[str, Any], run_id: str, decision_mode: str) -> dict[str, Any]:
    role = config["role"]
    payload: dict[str, Any] = {
        "agent_id": config["agent_id"],
        "role": role,
        "runtime": RUNTIME,
        "run_id": run_id,
        "decision_mode": decision_mode,
    }
    if role == "camera":
        payload["camera_id"] = config["camera_id"]
    return payload


def _heartbeat_loop(
    stop_event: threading.Event,
    agent: AgentSession,
    config: dict[str, Any],
    run_id: str,
    decision_mode: str,
    interval_s: float,
) -> None:
    was_online: bool | None = None
    while not stop_event.is_set():
        try:
            result = _http_json(
                config["controller_url"],
                "/api/agents/heartbeat",
                payload=_heartbeat_payload(config, run_id, decision_mode),
                timeout_s=min(1.2, config["http_timeout_s"]),
            )
            if was_online is not True:
                _event(agent, "noesis.heartbeat", role=config["role"], status="connected", run_id=run_id)
                _log("heartbeat", role=config["role"], agent_id=config["agent_id"], status="connected", run_id=run_id)
            was_online = True
        except BrokerError as exc:
            if was_online is not False:
                _event(agent, "noesis.heartbeat", role=config["role"], status="retrying", detail=str(exc), run_id=run_id)
                _log("heartbeat", role=config["role"], agent_id=config["agent_id"], status="retrying", detail=str(exc), run_id=run_id)
            was_online = False
        stop_event.wait(interval_s)


def _source_snapshot(config: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Pair session identity with the broker's camera vector and exact revision."""
    snapshot = _http_json(config["controller_url"], "/api/agents/snapshot", timeout_s=config["http_timeout_s"])
    if not isinstance(snapshot, dict):
        raise BrokerError(0, "Agent snapshot returned no object.")
    if isinstance(snapshot.get("session"), dict):
        return {"session": snapshot["session"], "observation_revision": snapshot.get("observation_revision")}, snapshot
    # Compatibility with the initial controller schema; the current controller
    # exposes an atomic session+camera snapshot and needs only one HTTP read.
    state = _http_json(config["controller_url"], "/api/state", timeout_s=config["http_timeout_s"])
    if not isinstance(state, dict):
        raise BrokerError(0, "Controller returned no state object.")
    session = state.get("session") or {}
    session_id = session.get("id")
    epoch = session.get("epoch")
    if session_id != snapshot.get("session_id") or epoch != snapshot.get("epoch"):
        raise BrokerError(409, "Session changed while reading the source snapshot.")
    revision = observation_revision(snapshot)
    state_revision = observation_revision(state)
    if revision is None or (state_revision is not None and state_revision != revision):
        raise BrokerError(409, "Source revision changed while reading the camera vector.")
    return state, snapshot


def _camera_loop(agent: AgentSession, context: Context, config: dict[str, Any]) -> None:
    run_id = str(context.run_id)
    camera_id = config["camera_id"]
    stop_event = threading.Event()
    heartbeat = threading.Thread(
        target=_heartbeat_loop,
        args=(stop_event, agent, config, run_id, "rules", config["heartbeat_interval_s"]),
        name=f"heartbeat-{camera_id}",
        daemon=True,
    )
    heartbeat.start()
    deadline = time.monotonic() + config["duration_s"]
    last_report_key: tuple[Any, Any, int] | None = None
    next_report_at = 0.0
    last_error_at = 0.0
    _log("agent_started", role="camera", agent_id=config["agent_id"], camera_id=camera_id, run_id=run_id)
    _event(agent, "noesis.agent_started", role="camera", camera_id=camera_id, run_id=run_id)

    try:
        while time.monotonic() < deadline and not stop_event.is_set():
            session_status = "unknown"
            try:
                state, snapshot = _source_snapshot(config)
                session = state["session"]
                session_status = session.get("status", "unknown")
                now = time.monotonic()
                if session_status == "running" and now >= next_report_at:
                    cameras = snapshot.get("cameras")
                    if not isinstance(cameras, list):
                        # Compatibility during backend startup; only use a state
                        # vector carrying the same exact revision.
                        cameras = state.get("cameras", []) if observation_revision(state) == observation_revision(snapshot) else []
                    camera = next((item for item in cameras if isinstance(item, dict) and item.get("id") == camera_id), None)
                    revision = observation_revision(snapshot)
                    if camera is not None and revision is not None:
                        key = (session.get("id"), session.get("epoch"), revision)
                        if key != last_report_key:
                            observation = make_camera_observation(
                                camera,
                                session_id=str(session["id"]),
                                epoch=int(session["epoch"]),
                                media_time_s=_as_float(session.get("time_s"), 0.0, 0.0, 1_000_000_000.0),
                                timestamp_utc=datetime.now(timezone.utc).isoformat(),
                            )
                            body = {
                                "camera_id": camera_id,
                                "source_observation_revision": revision,
                                "observation": observation,
                            }
                            _http_json(config["controller_url"], "/api/agents/observations", payload=body, timeout_s=config["http_timeout_s"])
                            last_report_key = key
                            _event(agent, "noesis.camera_observation", camera_id=camera_id, source_observation_revision=revision, healthy=observation["healthy"], speaking=observation["speaking"], media_time_ms=observation["media_time_ms"])
                            _log("camera_observation", agent_id=config["agent_id"], camera_id=camera_id, source_observation_revision=revision, healthy=observation["healthy"], speaking=observation["speaking"], media_time_ms=observation["media_time_ms"], run_id=run_id)
                            next_report_at = now + config["camera_interval_s"]
                if now - last_error_at > 10.0:
                    last_error_at = 0.0
            except BrokerError as exc:
                now = time.monotonic()
                if now - last_error_at >= 5.0:
                    last_error_at = now
                    _event(agent, "noesis.camera_retry", camera_id=camera_id, detail=str(exc), run_id=run_id)
                    _log("camera_retry", agent_id=config["agent_id"], camera_id=camera_id, detail=str(exc), run_id=run_id)
            stop_event.wait(config["camera_interval_s"])
    finally:
        stop_event.set()
        heartbeat.join(timeout=1.5)
        _log("agent_finished", role="camera", camera_id=camera_id, run_id=run_id)


def _model_decision(
    *,
    request: dict[str, Any],
    baseline: dict[str, str],
    model: str,
    timeout_s: float,
) -> dict[str, str] | None:
    """Ask Flower's injected OpenAI-compatible endpoint for constrained JSON."""
    from openai import OpenAI

    reports = usable_camera_reports(request)
    healthy_reports = {
        camera_id: {
            "healthy": report["observation"].get("healthy"),
            "speaking": report["observation"].get("speaking"),
            "speaker_state": report["observation"].get("speaker_state"),
            "participant": report["observation"].get("participant"),
        }
        for camera_id, report in reports.items()
    }
    current_cameras = {
        item.get("id"): item
        for item in request.get("cameras", []) or []
        if isinstance(item, dict) and item.get("healthy") is True and item.get("id") in CAMERA_IDS
    }
    allowed = sorted(camera_id for camera_id, data in healthy_reports.items()
                     if data.get("healthy") is True and data.get("speaking") is True and camera_id in current_cameras)
    prompt = {
        "request_id": request.get("request_id"),
        "candidate_camera_id": request.get("candidate_camera_id"),
        "current_program_camera_id": request.get("program_camera_id"),
        "allowed_camera_ids": allowed,
        "camera_agent_reports": healthy_reports,
        "rules_baseline": baseline,
    }
    client = OpenAI(
        base_url=os.environ["FLWR_RUNTIME_BASE_URL"],
        api_key=os.environ["FLWR_RUNTIME_API_KEY"],
        max_retries=0,
        timeout=max(0.15, timeout_s),
    )
    response = client.responses.create(
        model=model,
        instructions=(
            "You are the Noesis shot selector. Use only the supplied current camera-agent reports. "
            "Choose hold for overlap, ambiguity, or no fresh evidence. Switch only to one of allowed_camera_ids. "
            "Do not invent participants, camera state, or events. Return strict JSON with action (hold or switch), "
            "camera_id only for switch, and a short reason."
        ),
        input=json.dumps(prompt, separators=(",", ":")),
        text={"format": {"type": "json_object"}},
        max_output_tokens=120,
    )
    output_text = getattr(response, "output_text", "")
    try:
        decision = json.loads(output_text)
    except (TypeError, json.JSONDecodeError):
        return None
    return validate_model_decision(decision, request=request, allowed_camera_ids=set(allowed))


def _proposal_body(
    config: dict[str, Any],
    request: dict[str, Any],
    decision: dict[str, str],
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "schema_version": 1,
        "agent_id": config["agent_id"],
        "request_id": request["request_id"],
        "session_id": request["session_id"],
        "epoch": request["epoch"],
        "override_epoch": request["override_epoch"],
        "observation_revision": request["observation_revision"],
        "decision_id": f"{config['agent_id']}:{request['request_id']}",
        "action": decision["action"],
        "reason": decision["reason"],
    }
    if decision["action"] == "switch":
        body["camera_id"] = decision["camera_id"]
    return body


def _director_loop(agent: AgentSession, context: Context, config: dict[str, Any], decision_mode: str) -> None:
    run_id = str(context.run_id)
    stop_event = threading.Event()
    heartbeat = threading.Thread(
        target=_heartbeat_loop,
        args=(stop_event, agent, config, run_id, decision_mode, config["heartbeat_interval_s"]),
        name="heartbeat-director",
        daemon=True,
    )
    heartbeat.start()
    model_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="flower-runtime-model") if decision_mode == "llm" else None
    active_model: Future[dict[str, str] | None] | None = None
    deadline = time.monotonic() + config["duration_s"]
    seen_request_ids: set[str] = set()
    last_error_at = 0.0
    _log("agent_started", role="director", agent_id=config["agent_id"], decision_mode=decision_mode, model=config.get("model"), run_id=run_id)
    _event(agent, "noesis.agent_started", role="director", decision_mode=decision_mode, model=config.get("model"), run_id=run_id)

    try:
        while time.monotonic() < deadline and not stop_event.is_set():
            try:
                request = _http_json(config["controller_url"], "/api/agents/request", timeout_s=config["http_timeout_s"])
                if isinstance(request, dict) and request.get("state") == "pending":
                    request_id = request.get("request_id")
                    if isinstance(request_id, str) and request_id not in seen_request_ids:
                        seen_request_ids.add(request_id)
                        if len(seen_request_ids) > 512:
                            seen_request_ids = set(sorted(seen_request_ids)[-256:])
                        baseline = rule_decision(request)
                        decision = baseline
                        model_time_s = min(1.15, max(0.0, request.get("deadline_remaining_ms", 0) / 1000.0 - 0.30))
                        if decision_mode == "llm" and model_pool is not None and model_time_s >= 0.2:
                            if active_model is not None and active_model.done():
                                # An earlier timed-out response belongs to an expired
                                # request and is discarded without execution.
                                try:
                                    active_model.result()
                                except Exception:
                                    pass
                                active_model = None
                            if active_model is None:
                                active_model = model_pool.submit(
                                    _model_decision,
                                    request=request,
                                    baseline=baseline,
                                    model=config["model"],
                                    timeout_s=model_time_s,
                                )
                                try:
                                    result = active_model.result(timeout=model_time_s)
                                    active_model = None
                                    if result is not None:
                                        decision = result
                                        _event(agent, "noesis.model_decision", request_id=request_id, status="accepted", action=decision["action"], reason=decision["reason"])
                                    else:
                                        _event(agent, "noesis.model_decision", request_id=request_id, status="invalid_json_or_target", fallback="rules")
                                except FutureTimeout:
                                    _event(agent, "noesis.model_decision", request_id=request_id, status="timeout", fallback="rules")
                                    # Keep the single future tracked until it ends;
                                    # later requests use the rule baseline meanwhile.
                                except Exception as exc:
                                    active_model = None
                                    _event(agent, "noesis.model_decision", request_id=request_id, status="error", error=type(exc).__name__, fallback="rules")
                        body = _proposal_body(config, request, decision)
                        try:
                            result = _http_json(config["controller_url"], "/api/agents/proposal", payload=body, timeout_s=min(1.2, config["http_timeout_s"]))
                            _event(agent, "noesis.proposal", request_id=request_id, decision_id=body["decision_id"], action=decision["action"], camera_id=decision.get("camera_id"), status="accepted", confirmed_by=(result or {}).get("confirmed_by", "controller"))
                            _log("proposal", agent_id=config["agent_id"], request_id=request_id, decision_id=body["decision_id"], action=decision["action"], camera_id=decision.get("camera_id"), status="accepted", result=result, run_id=run_id)
                        except BrokerError as exc:
                            _event(agent, "noesis.proposal", request_id=request_id, decision_id=body["decision_id"], action=decision["action"], status="rejected", detail=str(exc))
                            _log("proposal", agent_id=config["agent_id"], request_id=request_id, decision_id=body["decision_id"], action=decision["action"], status="rejected", detail=str(exc), run_id=run_id)
            except BrokerError as exc:
                now = time.monotonic()
                if now - last_error_at >= 5.0:
                    last_error_at = now
                    _event(agent, "noesis.director_retry", detail=str(exc), run_id=run_id)
                    _log("director_retry", agent_id=config["agent_id"], detail=str(exc), run_id=run_id)
            stop_event.wait(config["director_interval_s"])
    finally:
        stop_event.set()
        heartbeat.join(timeout=1.5)
        if model_pool is not None:
            model_pool.shutdown(wait=False, cancel_futures=True)
        _log("agent_finished", role="director", decision_mode=decision_mode, run_id=run_id)


app = AgentApp()


@app.main()
def main(agent: AgentSession, context: Context) -> None:
    """Run one bounded camera or Director agent process."""
    config = _parse_config(agent, context)
    role = config.get("role")
    if role not in {"camera", "director"}:
        raise ValueError("Flower run prompt role must be camera or director.")
    config["controller_url"] = str(config.get("controller_url", DEFAULT_CONTROLLER_URL)).rstrip("/")
    config["http_timeout_s"] = _as_float(config.get("http_timeout_s"), 1.0, 0.15, 4.0)
    config["duration_s"] = _as_float(config.get("duration_s"), DEFAULT_DURATION_S, 1.0, 24 * 3600.0)
    config["camera_interval_s"] = _as_float(config.get("camera_interval_s"), DEFAULT_INTERVAL_S, 0.15, 3.0)
    config["director_interval_s"] = _as_float(config.get("director_interval_s"), DEFAULT_DIRECTOR_INTERVAL_S, 0.05, 1.0)
    config["heartbeat_interval_s"] = _as_float(config.get("heartbeat_interval_s"), DEFAULT_HEARTBEAT_INTERVAL_S, 0.5, 4.0)
    if role == "camera":
        camera_id = config.get("camera_id")
        if camera_id not in CAMERA_IDS:
            raise ValueError(f"camera_id must be one of {', '.join(CAMERA_IDS)}")
        config["camera_id"] = camera_id
        config["agent_id"] = str(config.get("agent_id") or f"camera-{camera_id}")
        _camera_loop(agent, context, config)
        return

    config["agent_id"] = str(config.get("agent_id") or "director")
    requested_mode = config.get("decision_mode", "rules")
    if requested_mode not in {"rules", "llm"}:
        requested_mode = "rules"
    model = config.get("model") or os.environ.get("NOESIS_MODEL")
    runtime_available = bool(os.environ.get("FLWR_RUNTIME_BASE_URL") and os.environ.get("FLWR_RUNTIME_API_KEY"))
    decision_mode = "llm" if requested_mode == "llm" and runtime_available and isinstance(model, str) and model.strip() else "rules"
    if requested_mode == "llm" and decision_mode != "llm":
        _log("model_unavailable", reason="LLM mode requested, but Flower runtime credentials or model name were not injected; using rules.", run_id=str(context.run_id))
    if decision_mode == "llm":
        config["model"] = str(model).strip()[:160]
    _director_loop(agent, context, config, decision_mode)
