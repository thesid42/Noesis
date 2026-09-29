"""Select a configured Nebius model profile in the private project .env file."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

from dotenv import dotenv_values, set_key

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from noesis.model_config import ModelProfileError, load_model_profile  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=("kimi", "minimax"), required=True)
    args = parser.parse_args()
    env_path = ROOT / ".env"
    source = {key: value for key, value in dotenv_values(env_path).items() if value is not None}
    try:
        profile = load_model_profile(args.profile, source)
    except ModelProfileError as exc:
        print(f"Cannot select profile: {exc}", file=sys.stderr)
        return 2

    # set_key's return includes the selected value; deliberately ignore it.
    set_key(str(env_path), "NOESIS_MODEL_PROFILE", profile.name, quote_mode="always")
    print(f"Selected configured Nebius profile: {profile.name}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
