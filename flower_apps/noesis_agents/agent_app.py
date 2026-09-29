"""Native SuperGrid coordinator and model-calling SuperNode workers for Noesis."""

from __future__ import annotations

import json
import math
import os
import threading
import time
from typing import Any
from urllib.parse import urlsplit
import uuid

import httpx
from flwr.agentapp import AgentApp, AgentSession
from flwr.app import Context

from noesis_agents.policy import (
    CAMERA_IDS,
    parse_camera_result,
    parse_critic_result,
    parse_director_result,
    safe_round_input,
)


RUNTIME = "Flower SuperGrid AgentApp/1.39"
DEFAULT_CONTROLLER_URL = "http://127.0.0.1:8765"
HEARTBEAT_INTERVAL_S = 2.0
MODEL_TIMEOUT_S = 25.0
ROUND_TIMEOUT_S = 28.0
POLL_INTERVAL_S = 0.4
REQUIRED_ROLES = {"critic", "director", *(f"camera:{camera_id}" for camera_id in CAMERA_IDS)}


class AgentTaskError(RuntimeError):
    def __init__(self, code: str, status_code: int | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code


def _emit(agent: AgentSession, event_type: str, **fields: Any) -> None:
    try:
        agent.events.emit({"type": event_type, **fields})
    except Exception:
        pass


def _log(event_type: str, **fields: Any) -> None:
    # Never log prompts, model output, provider bodies, or credentials.
    print(json.dumps({"event": event_type, **fields}, separators=(",", ":"), allow_nan=False), flush=True)


def _json_dict(raw: Any) -> dict[str, Any] | None:
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip() or len(raw) > 1_000_000:
        return None
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _node_config(context: Context) -> dict[str, Any]:
    raw = getattr(context, "node_config", {})
    return dict(raw) if isinstance(raw, dict) else {}


def _controller_url(config: dict[str, Any]) -> str:
    candidate = config.get("controller_url", DEFAULT_CONTROLLER_URL)
    if not isinstance(candidate, str):
        raise AgentTaskError("invalid_controller_url")
    parsed = urlsplit(candidate)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"} or parsed.port != 8765 or parsed.path not in {"", "/"}:
        raise AgentTaskError("invalid_controller_url")
    return f"http://{parsed.hostname}:8765"


def _http_json(
    controller_url: str,
    path: str,
    *,
    payload: dict[str, Any] | None = None,
    timeout_s: float = 2.0,
) -> Any:
    try:
        with httpx.Client(timeout=timeout_s, trust_env=False) as client:
            if payload is None:
                response = client.get(f"{controller_url}{path}")
            else:
                response = client.post(f"{controller_url}{path}", json=payload)
    except httpx.TimeoutException as exc:
        raise AgentTaskError("controller_timeout") from exc
    except httpx.HTTPError as exc:
        raise AgentTaskError("controller_unavailable") from exc
    if response.status_code < 200 or response.status_code >= 300:
        raise AgentTaskError("controller_rejected", response.status_code)
    try:
        value = response.json()
    except ValueError as exc:
        raise AgentTaskError("invalid_controller_response", response.status_code) from exc
    return value


def _heartbeat_body(config: dict[str, Any], run_id: str) -> dict[str, Any]:
    body: dict[str, Any] = {
        "agent_id": config["agent_id"],
        "role": config["role"],
        "runtime": RUNTIME,
        "run_id": run_id,
        "decision_mode": "llm",
    }
    if config["role"] == "camera":
        body["camera_id"] = config["camera_id"]
    return body


def _heartbeat_loop(
    stop: threading.Event,
    config: dict[str, Any],
    run_id: str,
    agent: AgentSession,
) -> None:
    last_online: bool | None = None
    while not stop.is_set():
        try:
            _http_json(config["controller_url"], "/api/agents/heartbeat", payload=_heartbeat_body(config, run_id), timeout_s=1.5)
            if last_online is not True:
                _emit(agent, "noesis.heartbeat", role=config["role"], status="connected")
                _log("heartbeat", role=config["role"], agent_id=config["agent_id"], status="connected")
            last_online = True
        except AgentTaskError as exc:
            if last_online is not False:
                _emit(agent, "noesis.heartbeat", role=config["role"], status="retrying", error_code=exc.code)
                _log("heartbeat", role=config["role"], agent_id=config["agent_id"], status="retrying", error_code=exc.code)
            last_online = False
        stop.wait(HEARTBEAT_INTERVAL_S)


def _grid_call(agent: AgentSession, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    returned = agent.grid.call({
        "name": name,
        "call_id": uuid.uuid4().hex,
        "arguments": json.dumps(arguments, separators=(",", ":"), allow_nan=False),
    })
    if not isinstance(returned, dict) or returned.get("type") != "function_call_output":
        raise AgentTaskError("invalid_grid_tool_response")
    output = _json_dict(returned.get("output"))
    if output is None:
        raise AgentTaskError("invalid_grid_tool_output")
    return output


def _grid_nodes(agent: AgentSession) -> list[dict[str, Any]]:
    tools = agent.grid.tools()
    available = {str(tool.get("name")) for tool in tools if isinstance(tool, dict)}
    if "get_nodes" not in available:
        raise AgentTaskError("grid_node_discovery_unavailable")
    result = _grid_call(agent, "get_nodes", {"sample_size": None})
    nodes = result.get("nodes")
    if not isinstance(nodes, list):
        raise AgentTaskError("invalid_grid_node_list")
    normalized = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        node_id = node.get("id")
        if isinstance(node_id, int) and not isinstance(node_id, bool):
            node_id = str(node_id)
        if isinstance(node_id, str) and node_id.isdecimal():
            normalized.append({"id": node_id, "name": node.get("name")})
    return normalized


def _push_grid_messages(agent: AgentSession, messages: list[dict[str, Any]]) -> dict[str, str]:
    if not messages:
        return {}
    result = _grid_call(agent, "push_messages", {"messages": messages})
    rows = result.get("results")
    if not isinstance(rows, list) or len(rows) != len(messages):
        raise AgentTaskError("grid_message_send_failed")
    sent: dict[str, str] = {}
    for request, row in zip(messages, rows):
        if not isinstance(row, dict) or row.get("error") is not None:
            continue
        message_id = row.get("message_id")
        if isinstance(message_id, str) and message_id:
            sent[message_id] = request["dst_node_id"]
    return sent


def _pull_grid_messages(agent: AgentSession, message_ids: list[str], timeout_s: float) -> list[dict[str, Any]]:
    if not message_ids:
        return []
    result = _grid_call(agent, "pull_messages", {"message_ids": message_ids, "timeout": max(0.0, min(25.0, timeout_s))})
    rows = result.get("messages")
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def _discover_roles(agent: AgentSession, allowed_node_ids: set[str] | None, timeout_s: float = 30.0) -> dict[str, str]:
    nodes = _grid_nodes(agent)
    if allowed_node_ids is not None:
        nodes = [node for node in nodes if node["id"] in allowed_node_ids]
    if not nodes:
        raise AgentTaskError("no_supernodes_available")

    payloads = []
    for node in nodes:
        payloads.append({
            "dst_node_id": node["id"],
            "payload": json.dumps({"kind": "handshake", "handshake_id": uuid.uuid4().hex}, separators=(",", ":")),
            "reply_to_message_id": None,
        })
    sent = _push_grid_messages(agent, payloads)
    # First contact may cold-start several SuperNode AgentApps. Keep this bounded,
    # but long enough for the six configured runtimes to load concurrently.
    replies = _pull_grid_messages(agent, list(sent), timeout_s=min(30.0, max(0.1, timeout_s), max(12.0, len(nodes) * 1.5)))
    role_nodes: dict[str, str] = {}
    for reply in replies:
        node_id = reply.get("src_node_id")
        reply_to = reply.get("reply_to_message_id")
        if not isinstance(node_id, str) or sent.get(reply_to) != node_id:
            continue
        message = _json_dict(reply.get("payload"))
        if message is None or message.get("ok") is not True or message.get("kind") != "handshake":
            continue
        role = message.get("role")
        camera_id = message.get("camera_id")
        key = f"camera:{camera_id}" if role == "camera" else role
        if key in REQUIRED_ROLES and key not in role_nodes:
            role_nodes[key] = node_id
    if set(role_nodes) != REQUIRED_ROLES:
        raise AgentTaskError("supernode_roles_incomplete")
    return role_nodes


def _response_text(response: Any) -> tuple[str, str, int, int]:
    if getattr(response, "status", None) != "completed":
        raise AgentTaskError("model_incomplete")
    response_id = getattr(response, "id", None)
    usage = getattr(response, "usage", None)
    input_tokens = getattr(usage, "input_tokens", None)
    output_tokens = getattr(usage, "output_tokens", None)
    if not isinstance(response_id, str) or not response_id or len(response_id) > 200:
        raise AgentTaskError("model_response_id_missing")
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in (input_tokens, output_tokens)):
        raise AgentTaskError("model_usage_missing")
    output = getattr(response, "output_text", None)
    if not isinstance(output, str) or not output:
        raise AgentTaskError("model_output_missing")
    return output, response_id, input_tokens, output_tokens


def _infer(model: str, instructions: str, data: dict[str, Any], schema_name: str, schema: dict[str, Any], *, timeout_s: float = MODEL_TIMEOUT_S) -> dict[str, Any]:
    base_url = os.environ.get("FLWR_RUNTIME_BASE_URL")
    api_key = os.environ.get("FLWR_RUNTIME_API_KEY")
    if not isinstance(base_url, str) or not base_url or not isinstance(api_key, str) or not api_key:
        raise AgentTaskError("flower_model_runtime_unavailable")
    from openai import OpenAI

    started = time.monotonic()
    try:
        with OpenAI(base_url=base_url, api_key=api_key, timeout=max(1.0, min(MODEL_TIMEOUT_S, timeout_s)), max_retries=0) as client:
            response = client.responses.create(
                model=model,
                instructions=instructions,
                input=json.dumps(data, separators=(",", ":"), allow_nan=False),
                text={"format": {"type": "json_schema", "name": schema_name, "schema": schema, "strict": True}},
                max_output_tokens=320,
            )
    except Exception as exc:
        # Model SDK exceptions can embed request/response bodies; report only type.
        raise AgentTaskError(f"model_request_{type(exc).__name__}") from None
    latency_ms = max(0, round((time.monotonic() - started) * 1000))
    text, response_id, input_tokens, output_tokens = _response_text(response)
    result = _json_dict(text)
    if result is None:
        raise AgentTaskError("model_output_invalid_json")
    return {
        "result": result,
        "response_id": response_id,
        "latency_ms": latency_ms,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
    }


def _model_from_round(round_data: dict[str, Any]) -> str:
    model = round_data.get("model")
    if not isinstance(model, str) or not model.strip() or len(model) > 160:
        raise AgentTaskError("round_model_invalid")
    return model


def _identity(round_data: dict[str, Any]) -> dict[str, Any]:
    required = ("request_id", "session_id", "epoch", "override_epoch", "model_epoch", "observation_revision")
    result = {key: round_data.get(key) for key in required}
    if not isinstance(result["request_id"], str) or not isinstance(result["session_id"], str):
        raise AgentTaskError("round_identity_invalid")
    for key in required[2:]:
        if isinstance(result[key], bool) or not isinstance(result[key], int) or result[key] < 0:
            raise AgentTaskError("round_identity_invalid")
    return result


def _wire_identity(round_data: dict[str, Any], *, source_revision: int | None = None) -> dict[str, Any]:
    """Map a leased round to the strict controller POST schema."""
    identity = _identity(round_data)
    return {
        key: identity[key]
        for key in ("request_id", "session_id", "epoch", "override_epoch", "model_epoch")
    } | {"source_revision": identity["observation_revision"] if source_revision is None else source_revision}


def _report(
    config: dict[str, Any],
    round_data: dict[str, Any],
    role: str,
    inference: dict[str, Any],
) -> dict[str, Any]:
    identity = _identity(round_data)
    report: dict[str, Any] = {
        **_wire_identity(round_data),
        "agent_id": config["agent_id"],
        "role": role,
        "media_time_s": round_data.get("media_time_s", 0.0),
        "model": _model_from_round(round_data),
        "response_id": inference["response_id"],
        "latency_ms": inference["latency_ms"],
        "input_tokens": inference["input_tokens"],
        "output_tokens": inference["output_tokens"],
        "result": inference["result"],
    }
    if role == "camera":
        report["camera_id"] = config["camera_id"]
    try:
        accepted = _http_json(config["controller_url"], "/api/ai/report", payload=report, timeout_s=3.0)
    except AgentTaskError as exc:
        raise AgentTaskError(f"ai_report_{exc.code}", exc.status_code) from None
    if not isinstance(accepted, dict) or accepted.get("ok") is not True:
        raise AgentTaskError("ai_report_not_accepted")
    return report


def _task_model_timeout(task: dict[str, Any]) -> float:
    timeout = task.get("model_timeout_s", MODEL_TIMEOUT_S)
    if not _finite(timeout):
        return MODEL_TIMEOUT_S
    return max(1.0, min(MODEL_TIMEOUT_S, float(timeout)))


def _camera_job(config: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
    round_data = task.get("round")
    if not isinstance(round_data, dict):
        raise AgentTaskError("camera_round_missing")
    cameras = round_data.get("cameras")
    if not isinstance(cameras, list):
        raise AgentTaskError("camera_vector_missing")
    camera = next((item for item in cameras if isinstance(item, dict) and item.get("id") == config["camera_id"]), None)
    if camera is None:
        raise AgentTaskError("camera_not_in_round")
    data = {
        "round": safe_round_input(round_data),
        "assigned_camera_id": config["camera_id"],
        "camera_signal": {
            "id": camera.get("id"),
            "participant": camera.get("participant"),
            "healthy": camera.get("healthy") is True,
            "status": str(camera.get("status", "unknown"))[:60],
            "speaker_state": str(camera.get("speaker_state", "unknown"))[:40],
            "speaking": camera.get("speaking") if isinstance(camera.get("speaking"), bool) else None,
            "energy": camera.get("energy") if _finite(camera.get("energy")) else None,
            "quality": camera.get("quality") if _finite(camera.get("quality")) else None,
            "age_ms": camera.get("age_ms") if _finite(camera.get("age_ms")) else None,
            "source_time_s": camera.get("source_time_s") if _finite(camera.get("source_time_s")) else None,
        },
    }
    inference = _infer(
        _model_from_round(round_data),
        "Assess only this camera's measured health, speaker activity, energy, quality, and age. "
        "You have no raw video, audio, transcript, or semantic scene access. Never claim what a person said, "
        "looks like, or feels. Return exactly recommendation take|hold|avoid, confidence 0..1, and a short reason.",
        data,
        "camera_recommendation",
        {
            "type": "object", "additionalProperties": False,
            "properties": {
                "recommendation": {"type": "string", "enum": ["take", "hold", "avoid"]},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "reason": {"type": "string", "minLength": 1, "maxLength": 300},
            },
            "required": ["recommendation", "confidence", "reason"],
        },
        timeout_s=_task_model_timeout(task),
    )
    inference["result"] = parse_camera_result(inference["result"])
    report = _report(config, round_data, "camera", inference)
    return {"ok": True, "role": "camera", "agent_id": config["agent_id"], "report": report}


def _critic_job(config: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
    round_data = task.get("round")
    if not isinstance(round_data, dict):
        raise AgentTaskError("critic_round_missing")
    data = {
        "round": safe_round_input(round_data),
        "prior_ai_reports": safe_round_input({**round_data, "previous_reports": round_data.get("previous_reports", [])})["previous_reports"],
    }
    inference = _infer(
        _model_from_round(round_data),
        "Act as a skeptical editorial critic. Assess whether the measured camera and speaker signals support "
        "staying steady, changing the current shot, or using the wide view. No raw audio/video is provided; "
        "do not invent scene semantics or a camera choice. Return exactly assessment steady|change|wide and a short reason.",
        data,
        "critic_assessment",
        {
            "type": "object", "additionalProperties": False,
            "properties": {
                "assessment": {"type": "string", "enum": ["steady", "change", "wide"]},
                "reason": {"type": "string", "minLength": 1, "maxLength": 300},
            },
            "required": ["assessment", "reason"],
        },
        timeout_s=_task_model_timeout(task),
    )
    inference["result"] = parse_critic_result(inference["result"])
    report = _report(config, round_data, "critic", inference)
    return {"ok": True, "role": "critic", "agent_id": config["agent_id"], "report": report}


def _director_job(config: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
    round_data = task.get("round")
    camera_reports = task.get("camera_reports")
    critic_report = task.get("critic_report")
    if not isinstance(round_data, dict) or not isinstance(camera_reports, list) or len(camera_reports) != len(CAMERA_IDS):
        raise AgentTaskError("director_evidence_incomplete")
    if not isinstance(critic_report, dict):
        raise AgentTaskError("director_critic_missing")

    # This fresh read is immediately before inference; stale sessions/model epochs abort.
    snapshot = _http_json(config["controller_url"], "/api/agents/snapshot", timeout_s=2.0)
    session = snapshot.get("session") if isinstance(snapshot, dict) else None
    identity = _identity(round_data)
    if (
        not isinstance(session, dict)
        or session.get("status") != "running"
        or session.get("id") != identity["session_id"]
        or session.get("epoch") != identity["epoch"]
        or snapshot.get("model_epoch") != identity["model_epoch"]
        or snapshot.get("override_epoch") != identity["override_epoch"]
        or snapshot.get("model") != _model_from_round(round_data)
        or isinstance(snapshot.get("observation_revision"), bool)
        or not isinstance(snapshot.get("observation_revision"), int)
        or snapshot.get("observation_revision") < identity["observation_revision"]
    ):
        raise AgentTaskError("director_snapshot_stale")
    current_cameras = snapshot.get("cameras")
    if not isinstance(current_cameras, list):
        raise AgentTaskError("director_camera_snapshot_missing")
    current_program = snapshot.get("program")
    current_session_time = session.get("time_s")
    if not _finite(current_session_time):
        raise AgentTaskError("director_session_time_missing")

    response_ids = [item.get("response_id") for item in camera_reports if isinstance(item, dict)] + [critic_report.get("response_id")]
    if len(response_ids) != len(CAMERA_IDS) + 1 or any(not isinstance(value, str) or not value for value in response_ids):
        raise AgentTaskError("director_response_evidence_invalid")
    data = {
        "request": safe_round_input(round_data),
        "fresh_snapshot": {
            "observation_revision": snapshot.get("observation_revision"),
            "session_time_s": current_session_time,
            "program": {
                "camera_id": current_program.get("camera_id") if isinstance(current_program, dict) else None,
                "reason": str(current_program.get("reason", ""))[:160] if isinstance(current_program, dict) else "",
            },
            "cameras": [
                {"id": item.get("id"), "participant": item.get("participant"), "healthy": item.get("healthy") is True,
                 "status": item.get("status"), "speaker_state": item.get("speaker_state"), "speaking": item.get("speaking"),
                 "energy": item.get("energy"), "quality": item.get("quality"), "age_ms": item.get("age_ms")}
                for item in current_cameras if isinstance(item, dict)
            ],
        },
        "camera_reports": [{"camera_id": item.get("camera_id"), "result": item.get("result")} for item in camera_reports],
        "critic_report": critic_report.get("result"),
    }
    inference = _infer(
        _model_from_round(round_data),
        "You are the final Noesis Director. Use the current round, fresh source snapshot, four camera AI "
        "recommendations, and critic assessment to choose hold or switch. Do not apply a fixed rule or infer "
        "unobserved audio/video semantics. For hold, camera_id must exactly match fresh_snapshot.program.camera_id. "
        "For switch, name a healthy current camera; use slate only if no camera is healthy. Return exactly "
        "action, camera_id, and a concise reason.",
        data,
        "director_decision",
        {
            "type": "object", "additionalProperties": False,
            "properties": {
                "action": {"type": "string", "enum": ["hold", "switch"]},
                "camera_id": {"type": "string", "enum": [*CAMERA_IDS, "corner", "slate"]},
                "reason": {"type": "string", "minLength": 1, "maxLength": 300},
            },
            "required": ["action", "camera_id", "reason"],
        },
        timeout_s=_task_model_timeout(task),
    )
    result = parse_director_result(inference["result"])
    healthy = {item.get("id") for item in current_cameras if isinstance(item, dict) and item.get("healthy") is True}
    current_camera_id = current_program.get("camera_id") if isinstance(current_program, dict) else None
    if result["action"] == "hold" and result["camera_id"] != current_camera_id:
        raise AgentTaskError("director_hold_target_changed")
    if result["action"] == "switch" and result["camera_id"] != "slate" and result["camera_id"] not in healthy:
        raise AgentTaskError("director_target_not_healthy")
    if result["action"] == "switch" and result["camera_id"] == "slate" and healthy:
        raise AgentTaskError("director_slate_not_required")

    body = {
        **_wire_identity(round_data, source_revision=snapshot.get("observation_revision")),
        "agent_id": config["agent_id"],
        "role": "director",
        "media_time_s": current_session_time,
        "model": _model_from_round(round_data),
        **{key: inference[key] for key in ("response_id", "latency_ms", "input_tokens", "output_tokens")},
        "result": result,
        "evidence_response_ids": response_ids,
    }
    try:
        accepted = _http_json(config["controller_url"], "/api/ai/decision", payload=body, timeout_s=3.0)
    except AgentTaskError as exc:
        raise AgentTaskError(f"ai_decision_{exc.code}", exc.status_code) from None
    if not isinstance(accepted, dict) or accepted.get("ok") is not True:
        raise AgentTaskError("ai_decision_not_accepted")
    return {"ok": True, "role": "director", "agent_id": config["agent_id"], "decision": body, "accepted": accepted}


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def _reply_once(agent: AgentSession, payload: dict[str, Any]) -> None:
    tools = agent.grid.tools()
    if not any(isinstance(tool, dict) and tool.get("name") == "push_reply_message" for tool in tools):
        raise AgentTaskError("grid_reply_unavailable")
    result = _grid_call(agent, "push_reply_message", {"payload": json.dumps(payload, separators=(",", ":"), allow_nan=False)})
    if result.get("error") is not None or result.get("message_id") is None:
        raise AgentTaskError("grid_reply_rejected")


def _worker_config(context: Context) -> dict[str, Any]:
    trusted = _node_config(context)
    role = trusted.get("role")
    if role not in {"camera", "critic", "director"}:
        raise AgentTaskError("trusted_node_role_missing")
    config = {"role": role, "controller_url": _controller_url(trusted)}
    if role == "camera":
        camera_id = trusted.get("camera_id")
        if camera_id not in CAMERA_IDS:
            raise AgentTaskError("trusted_camera_id_invalid")
        config["camera_id"] = camera_id
        expected_id = f"camera-{camera_id}"
    else:
        expected_id = role
    agent_id = trusted.get("agent_id", expected_id)
    if agent_id != expected_id:
        raise AgentTaskError("trusted_agent_id_invalid")
    config["agent_id"] = expected_id
    return config


def _worker_main(agent: AgentSession, context: Context, envelope: dict[str, Any]) -> None:
    envelope_message_id = envelope.get("message_id")
    payload = _json_dict(envelope.get("payload"))
    try:
        if not isinstance(envelope_message_id, str) or not envelope_message_id:
            raise AgentTaskError("grid_message_id_missing")
        config = _worker_config(context)
        if payload is None:
            raise AgentTaskError("grid_payload_invalid")
        if payload.get("kind") == "handshake":
            result = {
                "ok": True,
                "kind": "handshake",
                "role": config["role"],
                "camera_id": config.get("camera_id"),
                "agent_id": config["agent_id"],
            }
        else:
            expected_kind = {"camera": "camera_task", "critic": "critic_task", "director": "director_task"}[config["role"]]
            kind = payload.get("kind")
            if kind == "round_poll" and config["role"] != "director":
                raise AgentTaskError("task_role_mismatch")
            if kind not in {expected_kind, "round_poll"}:
                raise AgentTaskError("task_role_mismatch")
            run_id = str(getattr(context, "run_id", ""))
            stop = threading.Event()
            heartbeat = threading.Thread(target=_heartbeat_loop, args=(stop, config, run_id, agent), name=f"heartbeat-{config['role']}", daemon=True)
            heartbeat.start()
            try:
                if kind == "round_poll":
                    current_round = _http_json(config["controller_url"], "/api/ai/round", timeout_s=2.0)
                    result = {"ok": True, "role": "director", "kind": "round_poll", "round": current_round}
                elif config["role"] == "camera":
                    result = _camera_job(config, payload)
                elif config["role"] == "critic":
                    result = _critic_job(config, payload)
                else:
                    result = _director_job(config, payload)
            finally:
                stop.set()
                heartbeat.join(timeout=2.5)
        result["message_id"] = envelope_message_id
    except AgentTaskError as exc:
        result = {"ok": False, "error_code": exc.code, "status_code": exc.status_code, "message_id": envelope_message_id}
    except Exception as exc:
        result = {"ok": False, "error_code": f"agent_{type(exc).__name__}", "message_id": envelope_message_id}

    _reply_once(agent, result)
    _emit(agent, "noesis.grid_reply", ok=result.get("ok") is True, role=result.get("role"))
    _log("supernode_reply", ok=result.get("ok") is True, role=result.get("role"), agent_id=result.get("agent_id"))


def _prompt_data(agent: AgentSession) -> dict[str, Any]:
    return _json_dict(agent.prompt) or {}


def _reply_payload(reply: dict[str, Any]) -> dict[str, Any] | None:
    return _json_dict(reply.get("payload"))


def _poll_round_via_director(agent: AgentSession, director_node_id: str, timeout_s: float) -> dict[str, Any] | None:
    payload = {"kind": "round_poll"}
    sent = _push_grid_messages(agent, [{
        "dst_node_id": director_node_id,
        "payload": json.dumps(payload, separators=(",", ":")),
        "reply_to_message_id": None,
    }])
    if len(sent) != 1:
        raise AgentTaskError("round_poll_send_failed")
    replies = _pull_grid_messages(agent, list(sent), timeout_s=min(5.0, max(0.1, timeout_s)))
    for reply in replies:
        if reply.get("src_node_id") != director_node_id or sent.get(reply.get("reply_to_message_id")) != director_node_id:
            continue
        result = _reply_payload(reply)
        if result is None or result.get("ok") is not True or result.get("kind") != "round_poll":
            continue
        current = result.get("round")
        return current if isinstance(current, dict) else None
    return None


def _relayed_report_matches(report: Any, round_data: dict[str, Any], *, role: str, camera_id: str | None = None) -> bool:
    if not isinstance(report, dict):
        return False
    if role == "camera" and camera_id not in CAMERA_IDS:
        return False
    expected_agent = f"camera-{camera_id}" if role == "camera" else role
    identity = _identity(round_data)
    revision = report.get("source_revision")
    return (
        report.get("agent_id") == expected_agent
        and report.get("role") == role
        and (role != "camera" or report.get("camera_id") == camera_id)
        and report.get("request_id") == identity["request_id"]
        and report.get("session_id") == identity["session_id"]
        and report.get("epoch") == identity["epoch"]
        and report.get("override_epoch") == identity["override_epoch"]
        and report.get("model_epoch") == identity["model_epoch"]
        and isinstance(revision, int) and not isinstance(revision, bool)
        and revision >= identity["observation_revision"]
        and report.get("model") == _model_from_round(round_data)
        and isinstance(report.get("response_id"), str)
        and bool(report.get("response_id"))
    )


def _coordinator_task(
    agent: AgentSession,
    context: Context,
    prompt: dict[str, Any],
) -> None:
    trusted = _node_config(context)
    duration = prompt.get("duration_s", 240)
    if not isinstance(duration, (int, float)) or isinstance(duration, bool) or not math.isfinite(float(duration)):
        duration = 240
    duration = min(240.0, max(5.0, float(duration)))
    raw_node_ids = prompt.get("node_ids")
    allowed_nodes: set[str] | None = None
    if isinstance(raw_node_ids, list):
        allowed_nodes = {str(item) for item in raw_node_ids if (isinstance(item, (int, str)) and not isinstance(item, bool) and str(item).isdecimal())}
    deadline = time.monotonic() + duration
    role_nodes = _discover_roles(agent, allowed_nodes, timeout_s=max(0.1, deadline - time.monotonic()))
    _emit(agent, "noesis.supergrid_roles", roles=len(role_nodes))
    _log("supergrid_roles_discovered", roles=len(role_nodes))
    processed: set[str] = set()

    while time.monotonic() < deadline:
        remaining_task_s = deadline - time.monotonic()
        if remaining_task_s <= 0:
            break
        round_poll_started = time.monotonic()
        try:
            # The Hub coordinator may be remote from the studio. The trusted local
            # director node brokers all controller HTTP through Grid messages.
            current = _poll_round_via_director(agent, role_nodes["director"], min(5.0, remaining_task_s))
        except AgentTaskError as exc:
            _emit(agent, "noesis.round_poll_error", error_code=exc.code)
            time.sleep(min(POLL_INTERVAL_S, max(0.0, deadline - time.monotonic())))
            continue
        if not isinstance(current, dict):
            time.sleep(min(POLL_INTERVAL_S, max(0.0, deadline - time.monotonic())))
            continue
        poll_elapsed_ms = max(0, round((time.monotonic() - round_poll_started) * 1000))
        if isinstance(current.get("deadline_remaining_ms"), (int, float)) and not isinstance(current.get("deadline_remaining_ms"), bool):
            current = {**current, "deadline_remaining_ms": max(0, current["deadline_remaining_ms"] - poll_elapsed_ms)}
        request_id = current.get("request_id")
        if not isinstance(request_id, str) or not request_id or request_id in processed:
            time.sleep(min(POLL_INTERVAL_S, max(0.0, deadline - time.monotonic())))
            continue
        remaining = current.get("deadline_remaining_ms")
        if not isinstance(remaining, (int, float)) or isinstance(remaining, bool) or remaining < 5000:
            processed.add(request_id)
            time.sleep(min(POLL_INTERVAL_S, max(0.0, deadline - time.monotonic())))
            continue
        try:
            identity = _identity(current)
            model = _model_from_round(current)
            del identity, model
            round_deadline = min(deadline, time.monotonic() + min(ROUND_TIMEOUT_S, float(remaining) / 1000.0))
            initial_budget = round_deadline - time.monotonic()
            camera_phase_s = min(19.0, initial_budget - 6.0)
            if camera_phase_s < 2.0:
                processed.add(request_id)
                continue
            reports = _run_round(agent, role_nodes, current, camera_phase_s, model_timeout_s=max(1.0, camera_phase_s - 3.5))
            if reports:
                director_budget = round_deadline - time.monotonic()
                if director_budget >= 5.0:
                    _run_director(agent, role_nodes["director"], current, reports, director_budget,
                                  model_timeout_s=max(1.0, director_budget - 4.0))
        except AgentTaskError as exc:
            _emit(agent, "noesis.round_skipped", request_id=request_id, error_code=exc.code)
            _log("round_skipped", request_id=request_id, error_code=exc.code)
        processed.add(request_id)


def _run_round(
    agent: AgentSession,
    role_nodes: dict[str, str],
    round_data: dict[str, Any],
    timeout_s: float,
    *,
    model_timeout_s: float,
) -> list[dict[str, Any]] | None:
    messages = []
    for camera_id in CAMERA_IDS:
        key = f"camera:{camera_id}"
        request_payload = {"kind": "camera_task", "round": round_data, "model_timeout_s": model_timeout_s}
        encoded = json.dumps(request_payload, separators=(",", ":"), allow_nan=False)
        messages.append({"dst_node_id": role_nodes[key], "payload": encoded, "reply_to_message_id": None})
    critic_payload = {"kind": "critic_task", "round": round_data, "model_timeout_s": model_timeout_s}
    messages.append({"dst_node_id": role_nodes["critic"], "payload": json.dumps(critic_payload, separators=(",", ":"), allow_nan=False), "reply_to_message_id": None})

    # Camera and critic tasks fan out together; no second round starts while this waits.
    sent = _push_grid_messages(agent, messages)
    if len(sent) != len(messages):
        raise AgentTaskError("supernode_task_send_incomplete")
    replies = _pull_grid_messages(agent, list(sent), timeout_s)
    reports_by_role: dict[str, dict[str, Any]] = {}
    for reply in replies:
        node_id = reply.get("src_node_id")
        reply_to = reply.get("reply_to_message_id")
        if not isinstance(node_id, str) or sent.get(reply_to) != node_id:
            continue
        result = _reply_payload(reply)
        if result is None or result.get("ok") is not True:
            continue
        role = result.get("role")
        if role == "camera":
            report = result.get("report")
            if _relayed_report_matches(report, round_data, role="camera", camera_id=report.get("camera_id") if isinstance(report, dict) else None):
                reports_by_role[f"camera:{report['camera_id']}"] = report
        elif role == "critic" and _relayed_report_matches(result.get("report"), round_data, role="critic"):
            reports_by_role["critic"] = result["report"]
    if any(f"camera:{camera_id}" not in reports_by_role for camera_id in CAMERA_IDS) or "critic" not in reports_by_role:
        return None
    return [reports_by_role[f"camera:{camera_id}"] for camera_id in CAMERA_IDS] + [reports_by_role["critic"]]


def _run_director(
    agent: AgentSession,
    node_id: str,
    round_data: dict[str, Any],
    reports: list[dict[str, Any]],
    timeout_s: float,
    *,
    model_timeout_s: float,
) -> None:
    camera_reports = [item for item in reports if item.get("role") == "camera"]
    critic_report = next((item for item in reports if item.get("role") == "critic"), None)
    if len(camera_reports) != len(CAMERA_IDS) or critic_report is None:
        return
    if any(not _relayed_report_matches(item, round_data, role="camera", camera_id=item.get("camera_id")) for item in camera_reports):
        return
    if not _relayed_report_matches(critic_report, round_data, role="critic"):
        return
    response_ids = [item.get("response_id") for item in camera_reports] + [critic_report.get("response_id")]
    if len(set(response_ids)) != len(response_ids):
        return
    payload = {"kind": "director_task", "round": round_data, "camera_reports": camera_reports,
               "critic_report": critic_report, "model_timeout_s": model_timeout_s}
    sent = _push_grid_messages(agent, [{"dst_node_id": node_id, "payload": json.dumps(payload, separators=(",", ":"), allow_nan=False), "reply_to_message_id": None}])
    if len(sent) != 1:
        return
    replies = _pull_grid_messages(agent, list(sent), timeout_s)
    for reply in replies:
        if reply.get("src_node_id") != node_id or sent.get(reply.get("reply_to_message_id")) != node_id:
            continue
        result = _reply_payload(reply)
        if result and result.get("ok") is True and result.get("role") == "director":
            decision = result.get("decision")
            _emit(agent, "noesis.director_decision", request_id=round_data.get("request_id"), response_id=decision.get("response_id") if isinstance(decision, dict) else None)
            _log("director_decision_accepted", request_id=round_data.get("request_id"))
            return
    _log("director_decision_missing", request_id=round_data.get("request_id"))


def _coordinator(agent: AgentSession, context: Context, prompt: dict[str, Any]) -> None:
    try:
        _coordinator_task(agent, context, prompt)
    except AgentTaskError as exc:
        _emit(agent, "noesis.coordinator_error", error_code=exc.code)
        _log("coordinator_error", error_code=exc.code)


app = AgentApp()


@app.main()
def main(agent: AgentSession, context: Context) -> None:
    """Run one SuperGrid coordinator task or reply to one trusted SuperNode task."""
    prompt = _prompt_data(agent)
    envelope = prompt if {"message_id", "src_node_id", "payload"}.issubset(prompt) else None
    if envelope is not None:
        _worker_main(agent, context, envelope)
        return
    # Hub runs use mode=coordinator; tolerate an empty/plain prompt for the default coordinator.
    _coordinator(agent, context, prompt)
