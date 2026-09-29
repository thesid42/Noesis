"""Build a reviewed, secret-free Flower Hub source directory; never publishes."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil

from dotenv import dotenv_values
from flwr.cli.app_cmd.publish import _collect_file_paths, _validate_files
from flwr.cli.chat.chat_local_agent import build_local_agent

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "flower_apps"
DESTINATION = ROOT / ".runtime/hub/noesis-agents"


def main():
    candidates = _collect_file_paths(SOURCE)
    _validate_files(candidates)
    # Match configured secret values without ever writing or printing them.
    secrets = [value.encode() for key, value in dotenv_values(ROOT / ".env").items()
               if value and len(value) >= 8 and any(part in key for part in ("KEY", "TOKEN", "PASSWORD"))]
    relative_names = set()
    for path in candidates:
        relative = path.relative_to(SOURCE)
        payload = path.read_bytes()
        if any(secret in payload for secret in secrets) or b"-----BEGIN OPENSSH PRIVATE KEY-----" in payload:
            raise RuntimeError(f"Sensitive content detected in {relative}; publication preparation stopped.")
        relative_names.add(relative.as_posix())
    if DESTINATION.exists():
        previous = {p.relative_to(DESTINATION).as_posix() for p in _collect_file_paths(DESTINATION)}
        if previous - relative_names:
            raise RuntimeError("Staging directory contains extra public files; inspect them before rebuilding.")
    for path in candidates:
        target = DESTINATION / path.relative_to(SOURCE)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
    shutil.copyfile(SOURCE / ".gitignore", DESTINATION / ".gitignore")
    bundle = build_local_agent(DESTINATION)
    proof = {"app": bundle.app_spec, "fab_sha256": bundle.fab_hash,
             "sources": {str(p.relative_to(SOURCE)): hashlib.sha256(p.read_bytes()).hexdigest() for p in candidates}}
    (DESTINATION.parent / "review.json").write_text(json.dumps(proof, indent=2), encoding="utf-8")
    print(json.dumps({"ready_directory": str(DESTINATION), "public_files": sorted(relative_names),
                      "secret_matches": 0, "app": bundle.app_spec, "fab_sha256": bundle.fab_hash}, indent=2))


if __name__ == "__main__":
    main()
