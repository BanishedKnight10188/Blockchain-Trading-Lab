"""Bounded operator verification with explicit network opt-in and owned evidence."""

import asyncio
import importlib
import sys

import pytest

from agent_platform import cli
from tests.market.test_market_stream import Harness, book


def test_probe_captures_owned_snapshot_and_features_and_closes_the_stream():
    async def exercise():
        module = importlib.import_module("agent_platform.runtime.market_probe")
        harness = Harness([(61, book())])
        client = harness.client()
        report = await module.capture_market(client, seconds=1)
        assert report["mode"] == "public_market_probe"
        assert report["events_received"] == 1
        assert report["snapshot"]["status"] == "ready"
        assert report["snapshot"]["book"]["bid"] == "99"
        assert not report["features"]["warmup_ready"]
        assert report["account_calls"] == report["real_orders"] == report["model_calls"] == 0
        assert not client.running
        assert harness.sockets[0].closed

    asyncio.run(exercise())


def test_probe_without_events_records_no_evidence_instead_of_a_fabricated_snapshot():
    async def exercise():
        module = importlib.import_module("agent_platform.runtime.market_probe")
        harness = Harness([])
        report = await module.capture_market(harness.client(), seconds=1)
        assert report["events_received"] == 0
        assert report["snapshot"] is None
        assert report["features"] is None
        assert harness.sockets[0].closed

    asyncio.run(exercise())


def test_market_probe_cli_requires_explicit_live_public_flag_before_any_network(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.setattr(
        sys, "argv", ["btc-agent", "market-probe", "--output", str(tmp_path / "report.json")]
    )
    with pytest.raises(SystemExit) as stopped:
        cli.main()
    assert stopped.value.code == 2
    assert "requires --live-public" in capsys.readouterr().err
    assert not (tmp_path / "report.json").exists()


def test_market_probe_cli_does_not_replace_existing_output(monkeypatch, tmp_path, capsys):
    target = tmp_path / "report.json"
    target.write_text("existing fact", encoding="utf-8")
    monkeypatch.setattr(
        sys, "argv", ["btc-agent", "market-probe", "--live-public", "--output", str(target)]
    )
    with pytest.raises(SystemExit):
        cli.main()
    assert "output already exists" in capsys.readouterr().err
    assert target.read_text(encoding="utf-8") == "existing fact"
