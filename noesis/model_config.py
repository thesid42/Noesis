"""Private Nebius profile selection and SuperLink-only credential routing."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit


PROFILE_ENV = {
    "kimi": {
        "endpoint": "NEBIUS_KIMI_API_ENDPOINT",
        "model": "NEBIUS_KIMI_MODEL",
        "api_key": "NEBIUS_KIMI_API_KEY",
    },
    "minimax": {
        "endpoint": "NEBIUS_MINIMAX_API_ENDPOINT",
        "model": "NEBIUS_MINIMAX_MODEL",
        "api_key": "NEBIUS_MINIMAX_API_KEY",
    },
}
PROFILE_MANIFEST = ".runtime/superlink-profile.json"


class ModelProfileError(ValueError):
    """A selected provider profile is missing or invalid."""


@dataclass(frozen=True)
class ModelProfile:
    name: str
    endpoint: str
    model: str
    api_key: str
    configured_key_fingerprint: str = ""

    @property
    def identity(self) -> dict[str, str]:
        """Non-secret identity used only to validate SuperLink reuse."""
        return {
            "profile": self.name,
            "endpoint": self.endpoint,
            "model": self.model,
            "api_key_fingerprint": self.configured_key_fingerprint or hashlib.sha256(self.api_key.encode("utf-8")).hexdigest()[:20],
        }


def load_model_profile(
    profile_name: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> ModelProfile | None:
    """Load `kimi`/`minimax`; an empty or `rules` selector means no LLM profile."""
    values = os.environ if environ is None else environ
    selected = (profile_name if profile_name is not None else values.get("NOESIS_MODEL_PROFILE", ""))
    selected = str(selected or "").strip().lower()
    if selected in ("", "rules", "default"):
        return None
    if selected not in PROFILE_ENV:
        raise ModelProfileError("NOESIS_MODEL_PROFILE must be 'rules', 'kimi', or 'minimax'.")

    names = PROFILE_ENV[selected]
    fingerprint = str(values.get("NOESIS_PROFILE_KEY_FINGERPRINT", "")).strip()
    required_names = (names["endpoint"], names["model"])
    missing = [env_name for env_name in required_names if not str(values.get(env_name, "")).strip()]
    if not str(values.get(names["api_key"], "")).strip() and not fingerprint:
        missing.append(names["api_key"])
    if missing:
        raise ModelProfileError(
            f"Model profile '{selected}' is incomplete; configure: {', '.join(missing)}."
        )

    endpoint = str(values[names["endpoint"]]).strip()
    parsed = urlsplit(endpoint)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ModelProfileError(f"{names['endpoint']} must be a complete HTTP(S) endpoint URL.")
    return ModelProfile(
        name=selected,
        endpoint=endpoint,
        model=str(values[names["model"]]).strip(),
        api_key=str(values.get(names["api_key"], "")).strip(),
        configured_key_fingerprint=fingerprint,
    )


def launcher_environment(
    environ: Mapping[str, str] | None = None,
    profile: ModelProfile | None = None,
) -> dict[str, str]:
    """Copy ordinary settings while stripping all provider keys from app/agent envs."""
    result = dict(os.environ if environ is None else environ)
    for key in tuple(result):
        if key.startswith("NEBIUS_KIMI_") or key.startswith("NEBIUS_MINIMAX_"):
            result.pop(key, None)
    result.pop("FLWR_MODEL_API_KEY", None)
    result.pop("FLWR_MODEL_API_ENDPOINT", None)
    result["NOESIS_MODEL_PROFILE"] = profile.name if profile else "rules"
    if profile:
        result["NOESIS_MODEL"] = profile.model
    else:
        result.pop("NOESIS_MODEL", None)
    return result


def superlink_environment(
    environ: Mapping[str, str] | None = None,
    profile: ModelProfile | None = None,
) -> dict[str, str]:
    """Return a sanitized environment with the selected key added for SuperLink only."""
    result = launcher_environment(environ, profile)
    if profile:
        if not profile.api_key:
            raise ModelProfileError("The selected profile key is available only to the SuperLink launcher.")
        result["FLWR_MODEL_API_ENDPOINT"] = profile.endpoint
        result["FLWR_MODEL_API_KEY"] = profile.api_key
    return result


def read_profile_manifest(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def write_profile_manifest(path: Path, profile: ModelProfile | None, pid: int | None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    identity = profile.identity if profile else {
        "profile": "rules",
        "endpoint": "",
        "model": "",
        "api_key_fingerprint": "",
    }
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({"identity": identity, "pid": pid}, indent=2), encoding="utf-8")
    temporary.replace(path)


def require_matching_profile(path: Path, profile: ModelProfile | None) -> dict:
    manifest = read_profile_manifest(path)
    if manifest is None or not isinstance(manifest.get("identity"), dict):
        raise ModelProfileError(
            "A Flower SuperLink is already running but has no trusted Noesis profile record. "
            "Stop that service and restart it with scripts/run_demo.py or scripts/flower_agents.py."
        )
    pid = manifest.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        raise ModelProfileError("The Flower SuperLink profile record is incomplete; restart it using a Noesis launcher.")
    try:
        os.kill(pid, 0)
    except OSError as exc:
        raise ModelProfileError("The Flower SuperLink profile record is stale; stop and restart the service.") from exc
    expected = profile.identity if profile else {
        "profile": "rules",
        "endpoint": "",
        "model": "",
        "api_key_fingerprint": "",
    }
    if manifest["identity"] != expected:
        actual_name = str(manifest["identity"].get("profile", "unknown"))
        requested_name = profile.name if profile else "rules"
        raise ModelProfileError(
            f"Flower SuperLink uses a different model profile ({actual_name}); "
            f"stop it before selecting {requested_name}."
        )
    return manifest
