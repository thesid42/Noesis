"""Verify Nebius inference through the default local Flower runtime."""
import sys
from verify_supergrid import main

if __name__ == "__main__":
    if not any(arg == "--runtime" or arg.startswith("--runtime=") for arg in sys.argv[1:]):
        sys.argv.extend(["--runtime", "local"])
    main()
