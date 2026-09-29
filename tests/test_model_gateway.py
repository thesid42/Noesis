from __future__ import annotations

import json
import hashlib
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

import noesis.model_gateway as gateway
from noesis.model_gateway import (
    ProviderTarget,
    create_app,
    ensure_gateway_token,
    load_provider_targets,
    model_catalog_json,
    _timing_sink_from_env,
)


ENDPOINT = "https://api.tokenfactory.tf-ca1.nebius.com/v1/responses"


def _env() -> dict[str, str]:
    return {
        "NEBIUS_KIMI_API_ENDPOINT": ENDPOINT,
        "NEBIUS_KIMI_MODEL": "kimi-model",
        "NEBIUS_KIMI_API_KEY": "kimi-private-key",
        "NEBIUS_MINIMAX_API_ENDPOINT": ENDPOINT,
        "NEBIUS_MINIMAX_MODEL": "minimax-model",
        "NEBIUS_MINIMAX_API_KEY": "minimax-private-key",
    }


def test_profiles_select_independent_keys_and_catalog_is_safe() -> None:
    targets = load_provider_targets(_env())
    assert targets["kimi-model"].api_key == "kimi-private-key"
    assert targets["minimax-model"].api_key == "minimax-private-key"
    catalog = model_catalog_json(targets)
    assert json.loads(catalog) == [
        {"id": "kimi", "profile": "kimi", "model": "kimi-model", "label": "Kimi", "available": True},
        {"id": "minimax", "profile": "minimax", "model": "minimax-model", "label": "MiniMax", "available": True},
    ]
    assert "private-key" not in catalog and ENDPOINT not in catalog
    assert "kimi-private-key" not in repr(targets["kimi-model"])


def test_profile_endpoint_and_allowlist_fail_closed() -> None:
    invalid = _env()
    invalid["NEBIUS_MINIMAX_API_ENDPOINT"] = "https://attacker.invalid/v1/responses"
    with pytest.raises(ValueError, match="Nebius"):
        load_provider_targets(invalid)

    targets = load_provider_targets(_env())
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"id": "resp-test", "status": "completed"})

    app = create_app(targets, "internal-token", transport=httpx.MockTransport(handler))
    with TestClient(app) as client:
        rejected = client.post("/v1/responses", headers={"Authorization": "Bearer internal-token"}, json={"model": "unlisted"})
        assert rejected.status_code == 400
        streaming = client.post("/v1/responses", headers={"Authorization": "Bearer internal-token"}, json={"model": "kimi-model", "stream": True})
        assert streaming.status_code == 400
        assert calls == []

        accepted = client.post("/v1/responses", headers={"Authorization": "Bearer internal-token"}, json={"model": "minimax-model", "input": "hello"})
        assert accepted.status_code == 200
        assert calls[-1].headers["authorization"] == "Bearer minimax-private-key"
        assert calls[-1].url == ENDPOINT


def test_flower_camera_routing_keeps_provider_keys_and_payloads_separate() -> None:
    env = {**_env(), "NOESIS_CAMERA_MODEL": "qwen/qwen3.5-9b",
           "FLWR_MODEL_API_KEY": "flower-private-key"}
    targets = load_provider_targets(env)
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"id": "response-camera", "status": "completed"})
    with TestClient(create_app(targets, "internal", transport=httpx.MockTransport(handler))) as client:
        payload = {"model": "qwen/qwen3.5-9b", "input": "Measured camera evidence",
                   "reasoning": {"effort": "none"}}
        assert client.post("/v1/responses", headers={"Authorization": "Bearer internal"}, json=payload).status_code == 200
        assert str(calls[-1].url) == gateway.FLOWER_MODEL_ENDPOINT
        assert calls[-1].headers["authorization"] == "Bearer flower-private-key"
        assert json.loads(calls[-1].content) == payload
        client.post("/v1/responses", headers={"Authorization": "Bearer internal"}, json={"model": "kimi-model"})
        assert str(calls[-1].url) == ENDPOINT
        assert calls[-1].headers["authorization"] == "Bearer kimi-private-key"
    assert "qwen" not in model_catalog_json(targets)  # Camera assignment is not a director profile.
    assert "flower-private-key" not in repr(targets)
    with pytest.raises(ValueError, match="Flower camera"):
        load_provider_targets({**env, "FLWR_MODEL_API_ENDPOINT": ENDPOINT})


def test_successful_provider_timing_is_correlatable_and_contains_no_prompt_or_secret() -> None:
    events: list[dict] = []
    response_body = {"id": "resp-timing-1", "status": "completed", "output_text": "private prompt answer"}
    app = create_app(
        {"kimi-model": ProviderTarget("kimi", "kimi-model", ENDPOINT, "never-log-this-key", "Kimi")},
        "internal-token",
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=response_body)),
        timing_sink=events.append,
    )
    request_payload = {"model": "kimi-model", "input": "do-not-log-this-prompt"}
    expected_hash = hashlib.sha256(json.dumps(
        request_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False,
    ).encode("utf-8")).hexdigest()
    with TestClient(app) as client:
        response = client.post("/v1/responses", headers={"Authorization": "Bearer internal-token"}, json=request_payload)
    assert response.status_code == 200
    assert response.json() == response_body
    assert "upstream;dur=" in response.headers["server-timing"]
    assert len(events) == 1
    event = events[0]
    assert event["event"] == "provider_success"
    assert event["response_id"] == "resp-timing-1"
    assert event["requested_model"] == "kimi-model" and event["profile"] == "kimi"
    assert event["request_sha256"] == expected_hash
    assert event["request_received_ns"] <= event["upstream_started_ns"] <= event["upstream_completed_ns"]
    assert event["elapsed_ms"] >= 0 and event["upstream_ms"] >= 0 and event["gateway_processing_ms"] >= 0
    serialized = json.dumps(event)
    assert "do-not-log-this-prompt" not in serialized
    assert "private prompt answer" not in serialized
    assert "never-log-this-key" not in serialized and "internal-token" not in serialized
    assert set(event) == {
        "event", "request_received_ns", "upstream_started_ns", "upstream_completed_ns", "response_id",
        "requested_model", "profile", "elapsed_ms", "upstream_ms", "gateway_processing_ms", "request_sha256",
    }


def test_provider_error_timing_has_status_and_type_only_without_response_id() -> None:
    events: list[dict] = []
    app = create_app(
        {"kimi-model": ProviderTarget("kimi", "kimi-model", ENDPOINT, "never-log-this-key", "Kimi")},
        "internal-token",
        transport=httpx.MockTransport(lambda _request: httpx.Response(429, text="sensitive provider error")),
        timing_sink=events.append,
    )
    with TestClient(app) as client:
        response = client.post("/v1/responses", headers={"Authorization": "Bearer internal-token"}, json={"model": "kimi-model", "input": "private"})
    assert response.status_code == 502
    assert len(events) == 1
    event = events[0]
    assert event["event"] == "provider_error" and event["status"] == 429 and event["type"] == "upstream_status"
    assert "response_id" not in event
    assert "private" not in json.dumps(event) and "sensitive provider error" not in json.dumps(event)
    assert set(event) == {"event", "status", "type"}


def test_timing_path_is_opt_in_and_confined_to_private_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gateway, "ROOT", tmp_path)
    sink = _timing_sink_from_env({gateway.TIMING_PATH_ENV: ".runtime/timing/gateway.jsonl"})
    assert sink is not None
    sink({"event": "provider_success", "response_id": "resp-safe"})
    path = tmp_path / ".runtime" / "timing" / "gateway.jsonl"
    assert json.loads(path.read_text(encoding="utf-8")) == {"event": "provider_success", "response_id": "resp-safe"}

    outside = tmp_path / "tracked-timing.jsonl"
    assert _timing_sink_from_env({gateway.TIMING_PATH_ENV: str(outside)}) is None
    assert not outside.exists()


def test_unkeyed_optional_profile_does_not_block_kimi() -> None:
    values = _env()
    values["NEBIUS_MINIMAX_API_KEY"] = ""
    targets = load_provider_targets(values)
    assert set(targets) == {"kimi-model"}
    assert "minimax" not in model_catalog_json(targets)
    values["NEBIUS_KIMI_API_ENDPOINT"] = ""
    with pytest.raises(ValueError, match="incomplete"):
        load_provider_targets(values)


def test_gateway_auth_and_provider_failures_never_echo_credentials_or_body() -> None:
    targets = {
        "kimi-model": ProviderTarget("kimi", "kimi-model", ENDPOINT, "private-kimi-key", "Kimi"),
    }

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="provider said private-kimi-key should never escape")

    app = create_app(targets, "internal-token", transport=httpx.MockTransport(handler))
    with TestClient(app) as client:
        unauthorized = client.post("/v1/responses", json={"model": "kimi-model"})
        assert unauthorized.status_code == 401
        failed = client.post("/v1/responses", headers={"Authorization": "Bearer internal-token"}, json={"model": "kimi-model"})
        assert failed.status_code == 502
        assert "private-kimi-key" not in failed.text
        assert "provider said" not in failed.text
        assert client.get("/health").json() == {"ok": True}


def test_token_file_is_random_persistent_and_not_printed(tmp_path) -> None:
    path = tmp_path / "gateway-token"
    first = ensure_gateway_token(path, {})
    second = ensure_gateway_token(path, {})
    assert first == second
    assert len(first) == 64
    int(first, 16)
    assert path.read_text(encoding="utf-8") == first
