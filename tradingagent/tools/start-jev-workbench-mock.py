"""Offline UI preview. Uses a separate database and never loads a paid model."""

# ruff: noqa: E402 -- standalone workspace tool.
import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import uvicorn

from agent_platform.config import RuntimeConfig
from agent_platform.web.app import create_app

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8778)
    args = parser.parse_args()
    database = (
        ROOT
        / "output/verification"
        / ("jev-workbench-mock-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f") + ".sqlite3")
    )
    uvicorn.run(
        create_app(
            database, runtime_config=RuntimeConfig(paper=True, paper_mock=True, jev_workbench=True)
        ),
        host="127.0.0.1",
        port=args.port,
    )
