"""Explicit offline event-agent workspace; loopback only, no paid model."""

# ruff: noqa: E402 -- standalone workspace tool.
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import uvicorn

from agent_platform.config import RuntimeConfig
from agent_platform.web.app import create_app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8781)
    parser.add_argument(
        "--database", type=Path, default=ROOT / "output/event-agent-offline.sqlite3"
    )
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be 1..65535")
    app = create_app(args.database.resolve(), runtime_config=RuntimeConfig(event_agent=True))
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
