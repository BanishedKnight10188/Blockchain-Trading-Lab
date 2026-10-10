"""Default assembly exposes local rule reviews and owns the explicit-job worker."""

from datetime import UTC, datetime, timedelta

import pytest

from agent_platform.bootstrap import build_application_services
from agent_platform.domain.account import AccountSnapshot, ObservedTrade, TradeBatch
from agent_platform.domain.events import JournalEvent


@pytest.mark.asyncio
async def test_default_assembly_review_slice_is_local_free_and_stops_worker(tmp_path):
    async with build_application_services(tmp_path / "review.sqlite3") as services:
        assert services.reviews is not None and services.review_jobs.running
        worker = services.review_jobs
        store = services.reviews.store
        scope = services.reviews.account_ref
        at = datetime.now(UTC) - timedelta(seconds=3)
        trades = tuple(
            ObservedTrade(
                account_ref=scope,
                symbol="BTCUSDT",
                trade_id=str(index + 1),
                order_id=str(index + 1),
                side=side,
                price="60000",
                quantity="0.01",
                quote_quantity="600",
                fee="0",
                fee_asset="USDT",
                executed_at=at + timedelta(seconds=index),
            )
            for index, side in enumerate(("buy", "sell"))
        )
        batch = TradeBatch(account_ref=scope, symbol="BTCUSDT", trades=trades)
        account = AccountSnapshot(account_ref=scope, as_of=at)
        await store.ingest(
            batch,
            account,
            JournalEvent(
                event_id="offline-bootstrap-import",
                aggregate_id=scope,
                kind="trades_imported",
                payload=batch,
                occurred_at=datetime.now(UTC),
            ),
        )
        group = await services.reviews.create_group("offline-group", datetime.now(UTC))
        review = await services.reviews.generate("offline-group", datetime.now(UTC), "initial")
        assert not review.model_participated and review.realized_pnl_usd is None
        assert group.group.cost_status != "known"
        assert await worker.schedule_followups("offline-group", ()) == ()
        job = await worker.schedule(
            "offline-group", datetime.now(UTC) + timedelta(hours=1), "followup"
        )
        assert job.status == "pending"
        assert await worker.run_due() == ()
        assert (await services.queries.overview()).mode == "disabled"
    assert not worker.running
