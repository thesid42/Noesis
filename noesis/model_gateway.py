"""Local, model-allowlisted Open Responses proxy for Nebius credentials.

Flower SuperLink talks only to this service. Provider keys remain in the private
project environment and are selected by the requested, configured model ID.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass, field
import hmac
import json
import logging
import os
from pathlib import Path
import secrets
import stat
from typing import Any, Mapping
from urllib.parse import urlsplit

from dotenv import dotenv_values
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

import httpx


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_GATEWAY_URL = "http://127.0.0.1:8770/v1/responses"
GATEWAY_TOKEN_FILE = ROOT / ".runtime" / "supergrid" / "gateway-token"
GATEWAY_TOKEN_ENV = "NOESIS_GATEWAY_TOKEN"
DEFAULT_PROFILE = "kimi"
ALLOWED_PROVIDER_HOST = "api.tokenfactory.tf-ca1.nebius.com"
MAX_REQUEST_BYTES = 2 * 1024 * 1024
LOGGER = logging.getLogger("noesis.model_gateway")

PROFILE_ENV = {
    "kimi": ("NEBIUS_KIMI_API_ENDPOINT", "NEBIUS_KIMI_MODEL", "NEBIUS_KIMI_API_KEY", "Kimi"),
    "minimax": ("NEBIUS_MINIMAX_API_ENDPOINT", "NEBIUS_MINIMAX_MODEL", "NEBIUS_MINIMAX_API_KEY", "MiniMax"),
}


@dataclass(frozen=True)
class ProviderTarget:
    profile: str
    model: str
    endpoint: str
    api_key: str = field(repr=False)
    label: str


def _effective_environment(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    values = {key: value for key, value in dotenv_values(ROOT / ".env").items() if value is not None}
    values.update(os.environ if environ is None else environ)
    return values


def _validate_endpoint(endpoint: str, env_name: str) -> str:
    parsed = urlsplit(endpoint)
    if (
        parsed.scheme != "https"
        or parsed.hostname != ALLOWED_PROVIDER_HOST
        or parsed.path.rstrip("/") != "/v1/responses"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(f"{env_name} must be the configured Nebius /v1/responses URL.")
    return endpoint.rstrip("/")


def load_provider_targets(environ: Mapping[str, str] | None = None) -> dict[str, ProviderTarget]:
    """Load model allowlist and credentials without modifying process env."""
    values = _effective_environment(environ)
    targets: dict[str, ProviderTarget] = {}
    for profile, (endpoint_name, model_name, key_name, label) in PROFILE_ENV.items():
        endpoint = str(values.get(endpoint_name, "")).strip()
        model = str(values.get(model_name, "")).strip()
        key = str(values.get(key_name, "")).strip()
        # An entirely unconfigured profile is omitted; partial profiles fail closed.
        if not any((endpoint, model, key)):
            continue
        if not endpoint or not model or not key:
            raise ValueError(f"{profile} profile is incomplete; configure {endpoint_name}, {model_name}, and {key_name}.")
        endpoint = _validate_endpoint(endpoint, endpoint_name)
        if model in targets:
            raise ValueError("Configured Nebius profiles must use distinct model IDs.")
        targets[model] = ProviderTarget(profile, model, endpoint, key, label)
    if not targets:
        raise ValueError("No Nebius model profiles are configured.")
    return targets


def model_catalog_json(targets: Mapping[str, ProviderTarget] | None = None) -> str:
    """Serialize only safe UI fields; never expose endpoints or provider keys."""
    configured = targets if targets is not None else load_provider_targets()
    options = [
        {"id": item.profile, "profile": item.profile, "model": item.model, "label": item.label, "available": True}
        for item in sorted(configured.values(), key=lambda target: target.profile)
    ]
    return json.dumps(options, separators=(",", ":"), ensure_ascii=True)


def initial_model_profile(environ: Mapping[str, str] | None = None) -> str:
    values = _effective_environment(environ)
    requested = str(values.get("NOESIS_MODEL_PROFILE", DEFAULT_PROFILE)).strip().lower()
    return requested if requested in PROFILE_ENV else DEFAULT_PROFILE


def ensure_gateway_token(
    token_path: Path = GATEWAY_TOKEN_FILE,
    environ: Mapping[str, str] | None = None,
) -> str:
    """Read or create the private internal bearer token (never log or display it)."""
    values = os.environ if environ is None else environ
    configured = str(values.get(GATEWAY_TOKEN_ENV, "")).strip()
    if configured:
        return configured

    token_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        existing = token_path.read_text(encoding="utf-8").strip()
        if len(existing) >= 32:
            return existing
    except OSError:
        pass

    token = secrets.token_hex(32)
    try:
        with token_path.open("x", encoding="utf-8") as stream:
            stream.write(token)
        try:
            os.chmod(token_path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
        return token
    except FileExistsError:
        existing = token_path.read_text(encoding="utf-8").strip()
        if len(existing) >= 32:
            return existing
        raise RuntimeError("The local model gateway token file is invalid.") from None


def create_app(
    targets: Mapping[str, ProviderTarget] | None = None,
    token: str | None = None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    configured = dict(targets if targets is not None else load_provider_targets())
    expected_token = token or os.environ.get(GATEWAY_TOKEN_ENV) or ensure_gateway_token()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(25.0, connect=5.0),
            trust_env=False,
            transport=transport,
        )
        try:
            yield
        finally:
            await app.state.http_client.aclose()

    app = FastAPI(title="Noesis Model Gateway", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

    @app.get("/health")
    async def health() -> dict[str, bool]:
        return {"ok": True}

    @app.post("/v1/responses")
    async def responses(request: Request) -> Response:
        authorization = request.headers.get("authorization", "")
        supplied = authorization[7:] if authorization.lower().startswith("bearer ") else ""
        if not supplied or not hmac.compare_digest(supplied, expected_token):
            return JSONResponse(status_code=401, content={"error": {"message": "Unauthorized.", "type": "authentication_error"}})

        content_length = request.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > MAX_REQUEST_BYTES:
                    return JSONResponse(status_code=413, content={"error": {"message": "Request too large.", "type": "invalid_request_error"}})
            except ValueError:
                return JSONResponse(status_code=400, content={"error": {"message": "Invalid content length.", "type": "invalid_request_error"}})
        body = await request.body()
        if len(body) > MAX_REQUEST_BYTES:
            return JSONResponse(status_code=413, content={"error": {"message": "Request too large.", "type": "invalid_request_error"}})
        try:
            payload = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return JSONResponse(status_code=400, content={"error": {"message": "A JSON request body is required.", "type": "invalid_request_error"}})
        if not isinstance(payload, dict):
            return JSONResponse(status_code=400, content={"error": {"message": "A JSON object is required.", "type": "invalid_request_error"}})
        if payload.get("stream") is True:
            return JSONResponse(status_code=400, content={"error": {"message": "Streaming responses are not supported.", "type": "invalid_request_error"}})
        model = payload.get("model")
        if not isinstance(model, str) or model not in configured:
            return JSONResponse(status_code=400, content={"error": {"message": "Model is not in the configured allowlist.", "type": "invalid_request_error"}})

        target = configured[model]
        headers = {"Authorization": f"Bearer {target.api_key}", "Content-Type": "application/json"}
        accept = request.headers.get("accept")
        if accept:
            headers["Accept"] = accept[:200]
        try:
            client: httpx.AsyncClient = request.app.state.http_client
            upstream_request = client.build_request("POST", target.endpoint, content=body, headers=headers)
            upstream = await client.send(upstream_request)
            if not upstream.is_success:
                status_code = upstream.status_code
                await upstream.aclose()
                LOGGER.warning("Nebius request failed for configured profile %s (HTTP %s).", target.profile, status_code)
                return JSONResponse(status_code=502, content={"error": {"message": "Configured model provider request failed.", "type": "upstream_error"}})
            response_body = await upstream.aread()
            content_type = upstream.headers.get("content-type", "application/json")
            status_code = upstream.status_code
            await upstream.aclose()
            return Response(content=response_body, status_code=status_code, headers={"content-type": content_type})
        except Exception as exc:
            # Do not include provider exceptions, request bodies, or credential values.
            LOGGER.warning("Nebius request failed for configured profile %s (%s).", target.profile, type(exc).__name__)
            return JSONResponse(status_code=502, content={"error": {"message": "Configured model provider request failed.", "type": "upstream_error"}})

    return app


app = create_app(targets={}, token="not-configured")


def main() -> None:
    import uvicorn

    # These settings are safe for Noesis UI configuration and contain no secret.
    targets = load_provider_targets()
    os.environ["NOESIS_MODEL_CATALOG_JSON"] = model_catalog_json(targets)
    os.environ["NOESIS_MODEL_PROFILE"] = initial_model_profile()
    os.environ[GATEWAY_TOKEN_ENV] = ensure_gateway_token()
    gateway_app = create_app(targets, os.environ[GATEWAY_TOKEN_ENV])
    uvicorn.run(gateway_app, host="127.0.0.1", port=8770, log_level="warning", access_log=False)


if __name__ == "__main__":
    main()
