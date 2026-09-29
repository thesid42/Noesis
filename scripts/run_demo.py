"""Start Noesis with local Flower orchestration and Nebius Kimi by default."""
from __future__ import annotations
import argparse


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--obs", action="store_true")
    parser.add_argument("--model-profile", choices=("kimi", "minimax"), default="kimi")
    parser.add_argument("--runtime", choices=("local", "supergrid"), default="local")
    parser.add_argument("--duration-s", dest="agent_budget_s", type=int, default=3600)
    args = parser.parse_args()
    if args.runtime == "supergrid":
        from supergrid_agents import serve
    else:
        from local_agents import serve
    try:
        serve(args)
    except KeyboardInterrupt:
        print("Noesis stopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
