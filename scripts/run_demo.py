"""Start the SuperGrid-backed Noesis demo (Kimi by default)."""
from __future__ import annotations
import argparse
from supergrid_agents import serve


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--obs", action="store_true")
    parser.add_argument("--model-profile", choices=("kimi", "minimax"), default="kimi")
    parser.add_argument("--duration-s", dest="agent_budget_s", type=int, default=3600)
    args = parser.parse_args()
    try:
        serve(args)
    except KeyboardInterrupt:
        print("Noesis stopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
