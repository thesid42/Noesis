"""Start the local Noesis control room."""

from pathlib import Path
import os

from dotenv import load_dotenv
import uvicorn


def main() -> None:
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    uvicorn.run(
        "noesis.app:app",
        host=os.getenv("NOESIS_HOST", "127.0.0.1"),
        port=int(os.getenv("NOESIS_PORT", "8765")),
        log_level="info",
    )


if __name__ == "__main__":
    main()
