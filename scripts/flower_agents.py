"""Compatibility entry point for Noesis's SuperGrid crew lifecycle."""
import sys
from supergrid_agents import main

if __name__ == "__main__":
    # The old local-only runs have been replaced with actual SuperGrid workers.
    if len(sys.argv) > 1 and sys.argv[1] == "start":
        sys.argv[1] = "serve"
    main()
