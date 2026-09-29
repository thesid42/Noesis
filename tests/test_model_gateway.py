from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from noesis.model_gateway import (
    ProviderTarget,
    create_app,
    ensure_gateway_token,
    load_provider_targets,
    model_catalog_json,
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
