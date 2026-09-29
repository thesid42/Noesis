"""Local, model-allowlisted Open Responses proxy for Nebius credentials.

Flower SuperLink talks only to this service. Provider keys remain in the private
project environment and are selected by the requested, configured model ID.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass, field
import hmac
import hashlib
import json
import logging
import os
from pathlib import Path
import secrets
import stat
import threading
import time
from typing import Any, Callable, Mapping
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
TIMING_PATH_ENV = "NOESIS_GATEWAY_TIMING_LOG_PATH"

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
        # A profile without credentials is unavailable, even when its model and
        # endpoint placeholders are present. Keyed but invalid profiles fail closed.
        if not key:
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


def _request_sha256(payload: Mapping[str, Any]) -> str | None:
    try:
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError):
        return None
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _timing_sink_from_env(environ: Mapping[str, str] | None = None) -> Callable[[dict[str, Any]], None] | None:
    values = os.environ if environ is None else environ
    configured = str(values.get(TIMING_PATH_ENV, "")).strip()
    if not configured:
        return None
    candidate = Path(configured).expanduser()
    if not candidate.is_absolute():
        candidate = ROOT / candidate
    path = candidate.resolve()
    runtime_dir = (ROOT / ".runtime").resolve()
    if runtime_dir not in path.parents:
        LOGGER.warning("Ignoring %s outside the private .runtime directory.", TIMING_PATH_ENV)
        return None
    lock = threading.Lock()

    def append(record: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with lock:
            with path.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(json.dumps(record, sort_keys=True, separators=(",", ":"), allow_nan=False))
                stream.write("\n")
    return append


def _response_id_from_body(body: bytes) -> str | None:
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    response_id = payload.get("id") if isinstance(payload, dict) else None
    return response_id if isinstance(response_id, str) and 0 < len(response_id) <= 200 else None


def create_app(
    targets: Mapping[str, ProviderTarget] | None = None,
    token: str | None = None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    timing_sink: Callable[[dict[str, Any]], None] | None = None,
) -> FastAPI:
    configured = dict(targets if targets is not None else load_provider_targets())
    expected_token = token or os.environ.get(GATEWAY_TOKEN_ENV) or ensure_gateway_token()
    timing = timing_sink if timing_sink is not None else _timing_sink_from_env()

    def record_timing(record: dict[str, Any]) -> None:
        if timing is None:
            return
        try:
            timing(record)
        except Exception as exc:
            LOGGER.warning("Gateway timing write failed (%s).", type(exc).__name__)

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
        request_received_ns = time.monotonic_ns()
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
        request_sha256 = _request_sha256(payload)
        headers = {"Authorization": f"Bearer {target.api_key}", "Content-Type": "application/json"}
        accept = request.headers.get("accept")
        if accept:
            headers["Accept"] = accept[:200]
        upstream_started_ns: int | None = None
        upstream_completed_ns: int | None = None
        try:
            client: httpx.AsyncClient = request.app.state.http_client
            upstream_request = client.build_request("POST", target.endpoint, content=body, headers=headers)
            upstream_started_ns = time.monotonic_ns()
            upstream = await client.send(upstream_request)
            if not upstream.is_success:
                status_code = upstream.status_code
                await upstream.aclose()
                record_timing({
                    "event": "provider_error",
                    "status": status_code,
                    "type": "upstream_status",
                })
                LOGGER.warning("Nebius request failed (status=%s, type=upstream_status).", status_code)
                return JSONResponse(status_code=502, content={"error": {"message": "Configured model provider request failed.", "type": "upstream_error"}})
            response_body = await upstream.aread()
            upstream_completed_ns = time.monotonic_ns()
            content_type = upstream.headers.get("content-type", "application/json")
            status_code = upstream.status_code
            await upstream.aclose()
            response_id = _response_id_from_body(response_body)
            finished_ns = time.monotonic_ns()
            upstream_ms = max(0.0, (upstream_completed_ns - upstream_started_ns) / 1_000_000)
            elapsed_ms = max(0.0, (finished_ns - request_received_ns) / 1_000_000)
            gateway_processing_ms = max(0.0, elapsed_ms - upstream_ms)
            record_timing({
                "event": "provider_success",
                "request_received_ns": request_received_ns,
                "upstream_started_ns": upstream_started_ns,
                "upstream_completed_ns": upstream_completed_ns,
                "response_id": response_id,
                "requested_model": model,
                "profile": target.profile,
                "elapsed_ms": round(elapsed_ms, 3),
                "upstream_ms": round(upstream_ms, 3),
                "gateway_processing_ms": round(gateway_processing_ms, 3),
                "request_sha256": request_sha256,
            })
            timing_header = f"upstream;dur={upstream_ms:.3f}, gateway;dur={gateway_processing_ms:.3f}"
            return Response(content=response_body, status_code=status_code,
                            headers={"content-type": content_type, "server-timing": timing_header})
        except Exception as exc:
            record_timing({
                "event": "provider_error",
                "status": 502,
                "type": type(exc).__name__,
            })
            # Do not include provider exceptions, request bodies, or credential values.
            LOGGER.warning("Nebius request failed (status=502, type=%s).", type(exc).__name__)
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
