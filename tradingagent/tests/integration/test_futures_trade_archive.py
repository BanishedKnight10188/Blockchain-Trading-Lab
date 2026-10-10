"""Per-fill evidence must survive retries, restarts and failed archive writes."""

import csv
import io
import json
import sqlite3
from datetime import timedelta
from decimal import Decimal

import pytest

from agent_platform.domain.trading_runtime import TradingLimits
from tests.domain.test_futures_paper import NOW, settings_data
from tests.integration import test_futures_trading_core as core
from tests.integration.test_jev_parameter_trading import parameter_policy

assembled = core.assembled


@pytest.mark.asyncio
async def test_transport_evidence_survives_fill_archive_and_public_view(assembled):
    from agent_platform.domain.model_diagnostics import ModelLocalTiming, ModelTransportEvidence

    svc, _, _, _ = assembled
    run = await configure(svc)
    svc.model.choices = ("OPEN_LONG_M20_L10",)
    original_decide = svc.model.decide
    transport = ModelTransportEvidence(
        attempts=2,
        pre_request_retries=1,
        request_started=True,
        connect_ms=20,
        proxy_connect_ms=1,
        tls_ms=200,
        send_ms=1,
        response_wait_ms=600,
        body_ms=2,
        total_ms=824,
    )
    timing = ModelLocalTiming(
        validation_us=400,
        reserve_us=19000,
        provider_us=824000,
        fee_validation_us=100,
        settle_us=22000,
        binding_us=200,
        total_us=866000,
    )

    async def traced(request):
        response = await original_decide(request)
        return response.model_copy(update={"transport_evidence": transport, "local_timing": timing})

    svc.model.decide = traced
    cycle = await svc.step()
    assert cycle.status == "filled"
    archived = (await svc.archive.page(run.scope.account_ref)).entries[0]
    evidence = archived.record.execution_command.decision_evidence.response
    assert evidence.transport_evidence == transport
    assert evidence.local_timing == timing
    view = await svc.public_view()
    assert view["cycles"][0]["transport_evidence"] == transport.model_dump(mode="json")
    assert view["cycles"][0]["local_timing"] == timing.model_dump(mode="json")


@pytest.mark.asyncio
async def test_probability_adjustment_survives_fill_archive_and_public_view(assembled):
    from agent_platform.domain.decision_models import DecisionModelResponse

    svc, _, _, _ = assembled
    run = await configure(svc)
    svc.model.choices = ("OPEN_LONG_M20_L10",)
    original_decide = svc.model.decide

    async def rounded(request):
        response = await original_decide(request)
        data = response.model_dump(mode="json")
        answer = response.answers[0]
        data["choice_probability_adjustments"] = [
            {
                "question_id": answer.question_id,
                "original_total": "0.99",
                "original_probabilities": [
                    {"key": p.key, "probability": "0.99" if p.key == answer.choice else "0"}
                    for p in answer.probabilities
                ],
            }
        ]
        return DecisionModelResponse.model_validate_json(json.dumps(data))

    svc.model.decide = rounded
    cycle = await svc.step()
    assert cycle.status == "filled"
    archived = (await svc.archive.page(run.scope.account_ref)).entries[0]
    evidence = archived.record.execution_command.decision_evidence.response
    assert evidence.choice_probability_adjustments[0].original_total == Decimal("0.99")
    view = await svc.public_view()
    assert view["cycles"][0]["choice_probability_adjustments"][0]["original_total"] == "0.99"


async def configure(svc):
    run = await svc.configure(
        TradingLimits(**(settings_data() | {"max_leverage": 10})),
        parameter_policy(),
        session_id="s1",
        style_revision=1,
    )
    a = await svc.backend.account(run.scope, svc.clock.utcnow())
    await svc.start(
        account_ref=run.scope.account_ref,
        expected_revision=a.revision,
        style_revision=1,
        trader_revision=1,
    )
    return run


@pytest.mark.asyncio
@pytest.mark.parametrize("direction", ["LONG", "SHORT"])
async def test_each_fill_has_atomic_archive_and_full_model_evidence(assembled, direction):
    svc, clock, ctx, _ = assembled
    run = await configure(svc)
    svc.model.choices = (
        f"OPEN_{direction}_M20_L10",
        f"ADD_{direction}_Q20_L10",
        "REDUCE_Q20",
        "CLOSE",
    )
    previous = await svc.backend.account(run.scope, clock.utcnow())
    cycles = []
    for i in range(4):
        clock.advance_to(NOW + timedelta(seconds=i))
        cycle = await svc.step()
        assert cycle.status == "filled"
        operation = (await svc.backend.store.recent(run.scope.account_ref))[0]
        assert operation.before_state.revision == previous.revision
        assert operation.before_state.free_usdt == previous.free_usdt
        evidence = operation.execution_command.decision_evidence
        assert evidence.request.request_id == cycle.request_id
        assert evidence.response == cycle.model_response
        assert evidence.request == cycle.model_request
        assert evidence.response.answers[0].choice == cycle.plan.candidate_id
        assert evidence.decision_source == "offline_mock"
        assert json.loads(evidence.request.state_json)["account"]["revision"] == previous.revision
        assert cycle.model_latency_ms >= 0
        cycles.append(cycle)
        previous = await svc.backend.account(run.scope, clock.utcnow())
    page = await svc.archive.page(run.scope.account_ref, limit=2)
    assert page.total == 4 and len(page.entries) == 2
    assert page.next_after == page.entries[-1].sequence
    next_page = await svc.archive.page(
        run.scope.account_ref, after=page.next_after, limit=2, through=page.through
    )
    assert len(next_page.entries) == 2 and next_page.next_after is None
    assert next_page.entries[0].previous_hash == page.entries[-1].content_hash
    assert next_page.entries[-1].record.state.quantity == 0
    record = await svc.execution.journal.get(cycles[0].command_id)
    await svc.execution.submit(record.command)
    assert (await svc.archive.page(run.scope.account_ref)).total == 4
    from agent_platform.adapters.sqlite.trade_archive import SqliteTradeArchiveStore

    reopened = SqliteTradeArchiveStore(ctx[5])
    assert (await reopened.page(run.scope.account_ref)).entries[-1].record.state.quantity == 0
    exported = await reopened.export(run.scope.account_ref, format="jsonl")
    try:
        lines = [json.loads(line) for line in exported]
    finally:
        exported.close()
    assert lines[0]["type"] == "manifest" and lines[-1]["type"] == "complete"
    assert lines[-1]["count"] == 4 and len(lines) == 6
    assert lines[1]["entry"]["record"]["execution_command"]["decision_evidence"]["request"][
        "state_json"
    ]
    exported = await reopened.export(run.scope.account_ref, format="csv")
    try:
        rows = list(csv.DictReader(io.StringIO(exported.read().decode("utf-8-sig"))))
    finally:
        exported.close()
    assert len(rows) == 4 and rows[-1]["position_after"] == "0"
    assert rows[0]["model"] == "typesafe/jev-1.13"
    with pytest.raises(LookupError):
        await reopened.page("paper:futures:other")


@pytest.mark.asyncio
async def test_stable_page_cursor_excludes_later_fills_and_imports_legacy_honestly(assembled):
    svc, clock, ctx, _ = assembled
    run = await configure(svc)
    svc.model.choices = ("OPEN_LONG_M20_L10", "CLOSE")
    assert (await svc.step()).status == "filled"
    first = await svc.archive.page(run.scope.account_ref, limit=1)
    clock.advance_to(NOW + timedelta(seconds=1))
    assert (await svc.step()).status == "filled"
    assert (await svc.archive.page(run.scope.account_ref, through=first.through)).total == 1
    # Simulate a pre-archive database without fabricating absent model inputs.
    with sqlite3.connect(ctx[5]) as db:
        for table in ("futures_trade_archive", "futures_paper_operations"):
            for action in ("update", "delete"):
                db.execute(f"DROP TRIGGER {table}_no_{action}")
        db.execute("DROP TABLE futures_trade_archive")
        for sequence, text in db.execute("SELECT sequence,body FROM futures_paper_operations"):
            body = json.loads(text)
            body.pop("before_state", None)
            if body.get("execution_command"):
                body["execution_command"].pop("decision_evidence", None)
            db.execute(
                "UPDATE futures_paper_operations SET body=? WHERE sequence=?",
                (json.dumps(body), sequence),
            )
    await svc.backend.store.initialize()
    page = await svc.archive.page(run.scope.account_ref)
    assert page.total == 2 and all(e.origin == "legacy_import" for e in page.entries)
    assert all(e.record.before_state is None for e in page.entries)
    assert all(e.record.execution_command.decision_evidence is None for e in page.entries)


@pytest.mark.asyncio
async def test_archive_write_failure_rolls_back_the_fill(assembled):
    svc, _, ctx, _ = assembled
    run = await configure(svc)
    svc.model.choices = ("OPEN_LONG_M20_L10",)
    before = await svc.backend.account(run.scope, svc.clock.utcnow())
    with sqlite3.connect(ctx[5]) as db:
        db.execute(
            "CREATE TRIGGER fail_archive BEFORE INSERT ON futures_trade_archive "
            "BEGIN SELECT RAISE(ABORT,'archive unavailable'); END"
        )
    cycle = await svc.step()
    after = await svc.backend.account(run.scope, svc.clock.utcnow())
    assert cycle.status != "filled"
    assert after.quantity == before.quantity and after.free_usdt == before.free_usdt
    assert after.fees_usdt == before.fees_usdt and after.revision == before.revision
    assert (await svc.archive.page(run.scope.account_ref)).total == 0


@pytest.mark.asyncio
async def test_archive_is_immutable_and_detects_offline_tampering(assembled):
    svc, _, ctx, _ = assembled
    run = await configure(svc)
    svc.model.choices = ("OPEN_LONG_M20_L10",)
    assert (await svc.step()).status == "filled"
    with sqlite3.connect(ctx[5]) as db:
        for table in ("futures_trade_archive", "futures_paper_operations"):
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                db.execute(f"DELETE FROM {table}")
        db.execute("DROP TRIGGER futures_trade_archive_no_update")
        body = json.loads(db.execute("SELECT body FROM futures_trade_archive").fetchone()[0])
        body["record"]["state"]["symbol"] = "BTCUSDT"
        db.execute("UPDATE futures_trade_archive SET body=?", (json.dumps(body),))
    with pytest.raises(ValueError, match="archive"):
        await svc.archive.page(run.scope.account_ref)
