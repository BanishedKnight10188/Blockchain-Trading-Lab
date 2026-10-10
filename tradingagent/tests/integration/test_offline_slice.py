"""A smoke acceptance result preserves gaps and actual stopped lifecycle."""

import importlib
import sqlite3

import pytest

from agent_platform.domain.common import RunMode


@pytest.mark.asyncio
async def test_acceptance_checks_actual_offline_assembly_and_records_remaining_gaps(tmp_path):
    run = importlib.import_module("agent_platform.runtime.acceptance").run_acceptance
    report = await run(RunMode.ADVISORY, database_path=tmp_path / "acceptance.sqlite3")
    assert report.evidence_kind == "AUTOMATED_OFFLINE" and report.formal_acceptance is False
    assert report.outcome == "passed_with_gaps"
    assert all(check.passed for check in report.checks)
    assert {"manual_read_only_pending", "soak_pending", "model_provider_pending"} <= set(
        report.gaps
    )
    assert report.shutdown_status == "stopped"
    assert "account_ref" not in report.model_dump_json()


@pytest.mark.asyncio
async def test_acceptance_storage_failure_is_fixed_and_not_false_pass(tmp_path):
    run = importlib.import_module("agent_platform.runtime.acceptance").run_acceptance
    path = tmp_path / "unsupported.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA user_version=99")
    report = await run(RunMode.ADVISORY, database_path=path)
    assert report.outcome == "failed" and not report.formal_acceptance
    assert report.failure == "local_assembly_unavailable"
    assert report.shutdown_status == "not_started"


@pytest.mark.asyncio
async def test_offline_acceptance_does_not_impersonate_real_read_only_or_paper(tmp_path):
    run = importlib.import_module("agent_platform.runtime.acceptance").run_acceptance
    for mode in (RunMode.READ_ONLY, RunMode.PAPER):
        with pytest.raises(ValueError, match="offline"):
            await run(mode, database_path=tmp_path / "unused.sqlite3")
    assert not (tmp_path / "unused.sqlite3").exists()


@pytest.mark.asyncio
async def test_acceptance_detects_write_route_hidden_from_openapi(tmp_path, monkeypatch):
    import agent_platform.web.app as web

    original = web.create_app

    def hidden(*args, **kwargs):
        app = original(*args, **kwargs)

        @app.post("/api/orders", include_in_schema=False)
        async def unavailable():
            pytest.fail("fixture route must never be invoked")

        return app

    monkeypatch.setattr(web, "create_app", hidden)
    run = importlib.import_module("agent_platform.runtime.acceptance").run_acceptance
    report = await run(RunMode.ADVISORY, database_path=tmp_path / "routes.sqlite3")
    assert report.outcome == "failed"
    assert (
        next(item for item in report.checks if item.name == "funds_routes_absent").passed is False
    )
