"""Project only user-facing local records, never the account namespace or raw audits."""

from agent_platform.domain.common import live_account_ref
from agent_platform.ports.ledger import LedgerReadPort
from agent_platform.ports.overview import OverviewPort


def feedback_view(feedback):
    return feedback.model_dump(mode="json")


def attribution_view(attribution, revision, updated_at=None):
    return dict(
        original_author=attribution.original_author.value,
        final_decision_maker=attribution.final_decision_maker.value,
        executor=attribution.executor,
        recommendation_id=attribution.recommendation_id,
        user_confirmed=attribution.user_confirmed,
        revision=revision,
        updated_at=updated_at.isoformat() if updated_at else None,
    )


def report_view(receipt):
    state, report = receipt.state, receipt.state.report
    return dict(
        report_id=report.report_id,
        source=report.source,
        side=report.side.value,
        price=str(report.price),
        quantity=str(report.quantity),
        executed_at=report.executed_at.isoformat(),
        reported_at=report.reported_at.isoformat(),
        exchange_trade_id=report.exchange_trade_id,
        status=state.status,
        reasons=state.reasons,
        matched_trade_id=state.matched_trade_id,
        checked_at=state.checked_at.isoformat() if state.checked_at else None,
        revision=receipt.revision,
    )


class LedgerQueryService:
    def __init__(self, *, store: LedgerReadPort, source: OverviewPort, account_ref: str):
        self.store, self.source, self.account_ref = store, source, live_account_ref(account_ref)

    async def current(self, *, after_sequence=0, limit=50):
        data = await self.store.ledger(self.account_ref, after_sequence=after_sequence, limit=limit)
        mode = (await self.source.latest()).mode
        trades = []
        for entry in data.trades:
            trade, attribution = entry.trade, entry.attribution
            trades.append(
                dict(
                    trade_id=trade.trade_id,
                    side=trade.side.value,
                    price=str(trade.price),
                    quantity=str(trade.quantity),
                    quote_quantity=str(trade.quote_quantity)
                    if trade.quote_quantity is not None
                    else None,
                    fee=str(trade.fee),
                    fee_asset=trade.fee_asset,
                    executed_at=trade.executed_at.isoformat(),
                    attribution=attribution_view(
                        attribution.state, attribution.revision, attribution.updated_at
                    ),
                )
            )
        return dict(
            mode=mode,
            trades=trades,
            feedback=[feedback_view(item.feedback) for item in data.feedback],
            reports=[report_view(item) for item in data.reports],
            next_trade_sequence=data.next_trade_sequence,
            has_more_trades=data.has_more_trades,
            record_limit=limit,
        )
