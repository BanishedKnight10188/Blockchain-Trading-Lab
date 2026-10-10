"""Offline replay is deterministic, explicit about simulation and free of lookahead."""

import importlib
import socket
from datetime import timedelta
from pathlib import Path

import pytest
import pytest_asyncio

from agent_platform.domain.account import AccountSnapshot
from agent_platform.domain.market import MarketEvent
from agent_platform.domain.sessions import AgentSession
from tests.adapters.test_paper import intent, simulator
from tests.domain.test_decisions import NOW


@pytest_asyncio.fixture(autouse=True)
async def deny_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("offline replay attempted network access")

    with monkeypatch.context() as patch:
        patch.setattr(socket.socket, "connect", forbidden)
        patch.setattr(socket, "create_connection", forbidden)
        yield


def event(identifier="market-1", offset=1, price="60000"):
    return MarketEvent(
        event_id=identifier,
        symbol="BTCUSDT",
        source="replay",
        occurred_at=NOW + timedelta(seconds=offset),
        received_at=NOW + timedelta(seconds=offset),
        time_quality="exchange",
        payload={
            "kind": "trade",
            "symbol": "BTCUSDT",
            "trade_id": identifier,
            "price": price,
            "quantity": "0.001",
        },
    )


def context(**extra):
    module = importlib.import_module("agent_platform.replay.runner")
    rules = importlib.import_module("agent_platform.adapters.rules.baseline")
    session = AgentSession(
        session_id="offline-session",
        style={"strength": 72},
        created_at=NOW,
        updated_at=NOW,
    )
    account = AccountSnapshot(
        account_ref="offline-observation",
        as_of=NOW,
        account_revision=1,
        balances=({"asset": "USDT", "free": "100", "locked": "0"},),
    )
    return module.ReplayRunner(session, account, **extra), rules.RuleAdvisoryProvider("60000")


@pytest.mark.asyncio
async def test_default_advisory_records_exact_style_and_does_not_fill():
    paper = simulator()
    runner, rules = context(paper=paper, intents={"market-1": intent()})
    before = paper.balances
    result = await runner.run((event(price="61000"),), rules)
    assert result.mode == "advisory"
    assert result.decisions[0].assessment.action == "buy"
    assert result.decisions[0].snapshot.style.strength == 72
    assert result.decisions[0].snapshot.style_revision == 1
    assert not result.decisions[0].snapshot.features.warmup_ready
    assert result.jev_status == "unspecified"
    assert paper.balances == before
    assert not result.paper_fills
    assert result.network_calls == result.real_orders == 0
    assert runner.account.account_ref == "offline-observation"


@pytest.mark.asyncio
async def test_explicit_paper_fills_the_separate_account():
    paper = simulator()
    runner, rules = context(paper=paper, intents={"market-1": intent()})
    report = await runner.run((event(),), rules, mode="paper")
    assert len(report.paper_fills) == 1
    assert report.paper_fills[0].account_ref == "paper:offline"
    assert runner.account.balances[0].free == 100
    assert report.real_orders == 0
    runner, rules = context()
    with pytest.raises(ValueError):
        await runner.run((event(),), rules, mode="paper")


@pytest.mark.asyncio
async def test_same_inputs_and_rules_produce_same_result_without_future_data():
    facts = (event(), event("market-2", 2, "61000"))
    runner, rules = context()
    first = await runner.run(iter(facts), rules)
    runner, rules = context()
    second = await runner.run(iter(facts), rules)
    assert first == second
    assert first.decisions[0].snapshot.market.latest_trade.price == 60000
    assert first.decisions[0].assessment.action == "hold"
    assert first.decisions[1].assessment.action == "buy"


@pytest.mark.asyncio
async def test_duplicate_identity_is_skipped_but_changed_content_and_out_of_order_fail():
    runner, rules = context()
    result = await runner.run((event(), event()), rules)
    assert result.events_processed == result.duplicate_count == 1
    assert len(result.decisions) == 1
    for events in (
        (event(), event(price="61000")),
        (event("later", 2), event("earlier", 1)),
    ):
        runner, rules = context()
        with pytest.raises(ValueError):
            await runner.run(events, rules)


@pytest.mark.asyncio
async def test_future_account_evidence_is_rejected_before_advisory_evaluation():
    runner, _ = context()
    runner.account = AccountSnapshot(
        account_ref="offline-observation", as_of=NOW + timedelta(seconds=2), status="unavailable"
    )

    class NeverCalled:
        def evaluate(self, snapshot):
            raise AssertionError("future evidence reached the advisory provider")

    with pytest.raises(ValueError):
        await runner.run((event(),), NeverCalled())


def test_jsonl_stream_reports_bad_line_without_exposing_raw_values(tmp_path):
    reader = importlib.import_module("agent_platform.replay.reader")
    path = tmp_path / "input.jsonl"
    path.write_text(event().model_dump_json() + '\n{"api_secret":"hidden"}\n', encoding="utf-8")
    stream = reader.read_events(path)
    assert next(stream) == event()
    with pytest.raises(reader.ReplayInputError, match="line 2") as failure:
        next(stream)
    assert "hidden" not in str(failure.value)


def test_bad_utf8_is_a_sanitized_replay_input_error_with_its_line(tmp_path):
    reader = importlib.import_module("agent_platform.replay.reader")
    source = tmp_path / "bad-encoding.jsonl"
    source.write_bytes(event().model_dump_json().encode("utf-8") + b"\n\xffsecret\n")
    stream = reader.read_events(source)
    assert next(stream) == event()
    with pytest.raises(reader.ReplayInputError, match="line 2") as failure:
        next(stream)
    assert "secret" not in str(failure.value)


def test_fixture_is_streamed_as_owned_events():
    reader = importlib.import_module("agent_platform.replay.reader")
    path = Path(__file__).parents[1] / "fixtures" / "btc_events.jsonl"
    facts = tuple(reader.read_events(path))
    assert len(facts) == 3
    assert all(item.source == "replay" for item in facts)


def test_fake_clock_advances_monotonically_and_rejects_backwards_time():
    module = importlib.import_module("agent_platform.adapters.fake.clock")
    clock = module.FakeClock(NOW)
    clock.advance_to(NOW + timedelta(seconds=2))
    assert clock.utcnow() == NOW + timedelta(seconds=2)
    assert clock.monotonic() == 2
    with pytest.raises(ValueError):
        clock.advance_to(NOW)
