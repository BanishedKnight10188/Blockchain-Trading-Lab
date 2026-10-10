"""The offline command requires an explicit style and writes a reviewable report."""

import json
import subprocess
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]
FIXTURE = PROJECT / "tests" / "fixtures" / "btc_events.jsonl"


def command(*arguments):
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "agent_platform.cli",
            "replay",
            "--input",
            str(FIXTURE),
            "--threshold",
            "60000",
            *arguments,
        ],
        cwd=PROJECT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_replay_command_writes_an_advisory_report(tmp_path):
    target = tmp_path / "report.json"
    result = command("--style", "72", "--output", str(target))
    assert result.returncode == 0, result.stderr
    report = json.loads(target.read_text(encoding="utf-8"))
    assert report["mode"] == "advisory"
    assert report["events_processed"] == 3
    assert report["decisions"][0]["snapshot"]["style"]["strength"] == 72
    assert report["real_orders"] == report["network_calls"] == 0


def test_command_requires_style_and_never_overwrites_an_existing_report(tmp_path):
    target = tmp_path / "report.json"
    assert command("--output", str(target)).returncode != 0
    assert not target.exists()
    target.write_text("existing personal result", encoding="utf-8")
    assert command("--style", "72", "--output", str(target)).returncode != 0
    assert target.read_text(encoding="utf-8") == "existing personal result"


def test_bad_jsonl_cli_reports_the_line_and_keeps_raw_content_private(tmp_path):
    source, target = tmp_path / "bad.jsonl", tmp_path / "report.json"
    first = FIXTURE.read_text(encoding="utf-8").splitlines()[0]
    source.write_text(first + '\n{"api_secret":"private-fixture-value"}\n', encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "agent_platform.cli",
            "replay",
            "--input",
            str(source),
            "--threshold",
            "60000",
            "--style",
            "72",
            "--output",
            str(target),
        ],
        cwd=PROJECT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "line 2" in result.stderr
    assert "private-fixture-value" not in result.stderr
    assert not target.exists()
