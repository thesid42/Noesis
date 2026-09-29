"""Explicitly download the small English ASR model for local use.

This command is separate from application startup: Noesis never downloads a
model implicitly. The model is stored in ignored `.runtime/models/` state.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from noesis.perception import DEFAULT_MODEL_DIR  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare the local faster-whisper tiny.en model.")
    parser.add_argument("--model-dir", type=Path, default=DEFAULT_MODEL_DIR,
                        help="Exact local model directory (default: .runtime/models/whisper-tiny.en)")
    args = parser.parse_args()
    model_dir = args.model_dir.resolve()
    if (model_dir / "model.bin").is_file() and (model_dir / "config.json").is_file():
        print(f"Model already prepared: {model_dir}")
        return 0
    try:
        from faster_whisper.utils import download_model
    except ImportError:
        print("Install optional speech support first: uv sync --extra speech", file=sys.stderr)
        return 2
    model_dir.mkdir(parents=True, exist_ok=True)
    print("Downloading Systran/faster-whisper-tiny.en to the requested local directory...")
    try:
        result = Path(download_model("tiny.en", output_dir=str(model_dir)))
    except Exception as exc:
        print(f"Model preparation failed ({type(exc).__name__}). Retry after checking network access.", file=sys.stderr)
        return 1
    if not (result / "model.bin").is_file():
        print("Download finished without the expected model.bin file.", file=sys.stderr)
        return 1
    print(f"Model prepared: {result}")
    print("Application startup will use only these local files; no automatic download is performed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
