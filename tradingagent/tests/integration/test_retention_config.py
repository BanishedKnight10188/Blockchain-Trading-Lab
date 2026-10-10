"""User retention choices reach real pruning and remain visible at assembly boundaries."""

import asyncio
import importlib
import json
import subprocess
import sys
from datetime import timedelta

import pytest

from agent_platform.bootstrap import build_application_services
from agent_platform.config import RuntimeConfig
from tests.adapters.test_retention import record
from tests.domain.test_decisions import NOW


def policy(**values):
    model = importlib.import_module("agent_platform.domain.market_archive").MarketRetentionPolicy
    return model(**values)


def test_default_and_custom_retention_are_strict_owned_configuration():
    default = RuntimeConfig().market_retention
    assert (default.raw_days, default.minute_days) == (7, 90)
    custom = RuntimeConfig(market_retention={"raw_days": 1, "minute_days": 365})
    assert custom.market_retention == policy(raw_days=1, minute_days=365)
    assert not custom.live_public and not custom.live_account


@pytest.mark.parametrize("field", ["raw_days", "minute_days"])
@pytest.mark.parametrize("value", [0, 366, True, 2.5, "3"])
def test_retention_rejects_coercion_and_unbounded_values(field, value):
    with pytest.raises(ValueError):
        policy(**{field: value})


@pytest.mark.asyncio
async def test_selected_retention_prunes_exact_boundaries_and_keeps_pins_after_reopen(tmp_path):
    cls = importlib.import_module("agent_platform.adapters.sqlite.retention").SqliteMarketArchive
    path = tmp_path / "custom.market.sqlite3"
    archive = cls(path, retention=policy(raw_days=1, minute_days=2))
    await archive.initialize()
    raw = record(NOW - timedelta(days=1))
    minute = record(NOW - timedelta(days=2), kind="minute")
    expired_raw = record(raw.event_at - timedelta(microseconds=1))
    expired_minute = record(minute.event_at - timedelta(microseconds=1), kind="minute")
    pinned = record(NOW - timedelta(days=400))
    await archive.append_many((raw, minute, expired_raw, expired_minute, pinned))
    await archive.pin(pinned.record_id, pinned.source_hash)
    result = await archive.prune(NOW)
    assert result.raw_removed == result.minute_removed == 1
    for retained in (raw, minute, pinned):
        assert await archive.get(retained.record_id) == retained
    # A new startup choice changes maintenance, not the schema or old evidence.
    reopened = cls(path, retention=policy(raw_days=10, minute_days=30))
    await reopened.initialize()
    assert (await reopened.prune(NOW + timedelta(days=5))).raw_removed == 0
    assert await reopened.get(pinned.record_id) == pinned


@pytest.mark.asyncio
async def test_public_assembly_uses_selected_pruning_and_reports_it(tmp_path, monkeypatch):
    import agent_platform.adapters.binance_direct.market_stream as streams
    import agent_platform.adapters.binance_direct.public_rest as rest
    from agent_platform.adapters.fake.market import FakeMarket
    from tests.runtime.test_read_only import observation

    class FakeRest:
        def __init__(self, clock):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr(rest, "PublicRestClient", FakeRest)
    monkeypatch.setattr(streams, "BinanceMarketStream", lambda *args: FakeMarket((observation(),)))
    config = RuntimeConfig(live_public=True, market_retention={"raw_days": 2, "minute_days": 3})
    async with build_application_services(tmp_path / "configured.sqlite3", config) as services:
        archive = services.system.archive.archive
        await archive.append_many(
            (record(NOW - timedelta(days=4)), record(NOW - timedelta(days=10), kind="minute"))
        )
        result = await archive.prune(NOW)
        assert result.raw_removed == result.minute_removed == 1
        status = await services.system.current()
        assert status["archive"]["raw_retention_days"] == 2
        assert status["archive"]["minute_retention_days"] == 3
    assert (await services.runtime.health()).status == "stopped"


def test_web_cli_selected_retention_reaches_actual_status(tmp_path, monkeypatch):
    from agent_platform import cli

    seen = []

    def server(app, **kwargs):
        async def inspect():
            async with app.router.lifespan_context(app):
                seen.append((await app.state.services.system.current())["archive"])

        asyncio.run(inspect())

    monkeypatch.setattr(cli.uvicorn, "run", server)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "btc-agent",
            "web",
            "--raw-retention-days",
            "5",
            "--minute-retention-days",
            "20",
            "--database",
            str(tmp_path / "web.sqlite3"),
        ],
    )
    cli.main()
    assert seen[0]["raw_retention_days"] == 5
    assert seen[0]["minute_retention_days"] == 20
    assert seen[0]["enabled"] is False


def command(tmp_path, mode, *extra):
    args = [
        sys.executable,
        "-m",
        "agent_platform.cli",
        mode,
        "--database",
        str(tmp_path / "cli.sqlite3"),
    ]
    if mode == "soak":
        args += ["--seconds", "1", "--output", str(tmp_path / "cli.json")]
    return subprocess.run(args + list(extra), capture_output=True, text=True, timeout=8)


def test_soak_cli_selected_retention_is_recorded_without_enabling_network(tmp_path):
    result = command(tmp_path, "soak", "--raw-retention-days", "5", "--minute-retention-days", "20")
    assert result.returncode == 0, result.stderr
    report = json.loads((tmp_path / "cli.json").read_text(encoding="utf-8"))
    assert report["archive"]["raw_retention_days"] == 5
    assert report["archive"]["minute_retention_days"] == 20
    assert report["mode"] == "disabled" and report["formal_acceptance"] is False


@pytest.mark.parametrize("mode", ["web", "soak"])
@pytest.mark.parametrize(
    "flag,value", [("--raw-retention-days", "0"), ("--minute-retention-days", "366")]
)
def test_invalid_retention_cli_fails_before_storage_or_server(tmp_path, mode, flag, value):
    result = command(tmp_path, mode, flag, value)
    assert result.returncode == 2
    assert "unrecognized arguments" not in result.stderr
    assert not (tmp_path / "cli.sqlite3").exists()
    assert not (tmp_path / "cli.json").exists()
