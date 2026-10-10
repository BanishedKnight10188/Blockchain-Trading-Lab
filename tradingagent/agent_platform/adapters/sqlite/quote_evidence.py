"""Keep first supplemental quote evidence without rewriting immutable trade bodies."""

import sqlite3

from agent_platform.domain.account import ObservedTrade
from agent_platform.domain.common import nonnegative_amount
from agent_platform.domain.events import JournalEvent
from agent_platform.ports.persistence import ObservationConflict


def record_quote(
    connection: sqlite3.Connection, sequence: int, trade: ObservedTrade, event_id: str
) -> None:
    if trade.quote_quantity is None:
        return
    previous = connection.execute(
        "SELECT quote_quantity FROM trade_quote_evidence WHERE trade_sequence=?", (sequence,)
    ).fetchone()
    if previous is not None:
        if nonnegative_amount(previous[0]) != trade.quote_quantity:
            raise ObservationConflict("trade quote conflicts with confirmed supplemental evidence")
        return
    connection.execute(
        "INSERT INTO trade_quote_evidence(trade_sequence, quote_quantity, source_event_id) "
        "VALUES(?,?,?)",
        (sequence, str(trade.quote_quantity), event_id),
    )


def backfill_quotes(connection: sqlite3.Connection) -> None:
    # Only completed atomic imports are evidence, not arbitrary standalone journal writes.
    audits = connection.execute(
        "SELECT e.event_id, e.body FROM journal_events e JOIN import_results r USING(event_id) "
        "WHERE e.kind='trades_imported' ORDER BY e.sequence"
    )
    for event_id, body in audits:
        event = JournalEvent.model_validate_json(body)
        for trade in event.payload.trades:
            if trade.quote_quantity is None:
                continue
            row = connection.execute(
                "SELECT sequence, body FROM observed_trades WHERE venue=? AND market_type=? "
                "AND account_ref=? AND symbol=? AND trade_id=?",
                (
                    trade.venue,
                    trade.market_type.value,
                    trade.account_ref,
                    trade.symbol,
                    trade.trade_id,
                ),
            ).fetchone()
            if row is None:
                raise ObservationConflict("quote proof lacks its committed trade")
            known = ObservedTrade.model_validate_json(row[1])
            if known.quote_quantity is not None and known.quote_quantity != trade.quote_quantity:
                raise ObservationConflict("historical quote proofs conflict")
            if known.model_copy(update={"quote_quantity": None}) != trade.model_copy(
                update={"quote_quantity": None}
            ):
                raise ObservationConflict("historical quote proof describes another fact")
            record_quote(connection, row[0], trade, event_id)
