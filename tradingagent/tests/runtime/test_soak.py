"""Real elapsed measurements cannot be replaced by a short or synthetic soak claim."""

import asyncio
import importlib
import json
import subprocess
import sys
from contextlib import asynccontextmanager

import pytest

from agent_platform.config import RuntimeConfig


def module():
    return importlib.import_module("agent_platform.runtime.soak")


def test_duration_and_live_evidence_are_separate_from_formal_acceptance():
    assess = module().assess_soak
    assert (
        assess(5, live_public=False, live_account=False, ready=False, faults=0)
        == "offline_short_check"
    )
    assert (
        assess(86399.9, live_public=True, live_account=True, ready=True, faults=0)
        == "duration_not_met"
    )
    assert (
        assess(86400, live_public=True, live_account=False, ready=True, faults=0)
        == "live_evidence_missing"
    )
    assert (
        assess(86400, live_public=True, live_account=True, ready=True, faults=1)
        == "runtime_faults_present"
    )
    assert (
        assess(86400, live_public=True, live_account=True, ready=True, faults=0)
        == "duration_complete_requires_review"
    )


@pytest.mark.asyncio
async def test_actual_offline_short_capture_reports_memory_fees_and_owned_shutdown(tmp_path):
    report = await module().capture_soak(tmp_path / "short.sqlite3", RuntimeConfig(), seconds=1)
    assert report["verdict"] == "offline_short_check" and report["formal_acceptance"] is False
    assert report["elapsed_seconds"] >= 1 and report["samples"] >= 2
    assert report["shutdown_status"] == "stopped"
    assert report["budget"]["spent_usd"] == "0" and report["budget"]["reserved_usd"] == "0"
    assert report["memory"]["kind"] in ("windows_working_set", "unavailable")
    assert report["real_orders"] == report["model_calls"] == 0
    assert "account_ref" not in str(report)


def test_process_memory_is_measured_or_explicitly_unavailable():
    value = module().process_memory()
    assert value is None or (type(value) is int and value > 0)


@pytest.mark.asyncio
async def test_cancel_during_assembly_still_returns_truthful_partial_report(tmp_path, monkeypatch):
    started = asyncio.Event()

    @asynccontextmanager
    async def assembling(*args):
        started.set()
        await asyncio.Event().wait()
        yield None

    monkeypatch.setattr("agent_platform.bootstrap.build_application_services", assembling)
    task = asyncio.create_task(
        module().capture_soak(tmp_path / "partial.sqlite3", RuntimeConfig(), seconds=1)
    )
    await started.wait()
    task.cancel()
    report = await task
    assert report["interrupted"] is True and report["samples"] == 0
    assert report["shutdown_status"] == "not_started"
    assert report["budget"] is None and report["budget_status"] == "unavailable"


@pytest.mark.asyncio
async def test_cli_offline_short_report_is_explicit_and_existing_output_not_overwritten(tmp_path):
    target = tmp_path / "short.json"
    command = [
        sys.executable,
        "-m",
        "agent_platform.cli",
        "soak",
        "--seconds",
        "1",
        "--database",
        str(tmp_path / "short.sqlite3"),
        "--output",
        str(target),
    ]
    completed = await asyncio.to_thread(
        subprocess.run, command, capture_output=True, text=True, check=False
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(target.read_text(encoding="utf-8"))["verdict"] == "offline_short_check"
    before = target.read_bytes()
    repeated = await asyncio.to_thread(
        subprocess.run, command, capture_output=True, text=True, check=False
    )
    assert repeated.returncode != 0 and target.read_bytes() == before
