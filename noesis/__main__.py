"""Start the local Noesis control room."""

from pathlib import Path
import os

from dotenv import dotenv_values
import uvicorn


def main() -> None:
    settings = dotenv_values(Path(__file__).resolve().parents[1] / ".env")
    for key, value in settings.items():
        if value is not None and not key.startswith(("NEBIUS_", "FLWR_MODEL_", "FLWR_RUNTIME_")):
            os.environ.setdefault(key, value)
    for key in tuple(os.environ):
        if key.startswith(("NEBIUS_", "FLWR_MODEL_", "FLWR_RUNTIME_")):
            os.environ.pop(key, None)
    uvicorn.run(
        "noesis.app:app",
        host=os.getenv("NOESIS_HOST", "127.0.0.1"),
        port=int(os.getenv("NOESIS_PORT", "8765")),
        log_level="info",
    )


if __name__ == "__main__":
    main()
