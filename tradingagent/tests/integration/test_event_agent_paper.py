from datetime import timedelta

from agent_platform.adapters.fake.clock import FakeClock
from agent_platform.domain.agent_tools import ToolCall
from agent_platform.domain.futures_market import FuturesMarketSnapshot
from agent_platform.domain.futures_values import FuturesQuote
from tests.fixtures.event_agent_cases import lane, response
from tests.fixtures.watch_cases import NOW, definition, frame


class Market:
    def __init__(self, clock):
        self.clock, self.price = clock, "105"

    async def snapshot(self, symbol):
        q = FuturesQuote(
            symbol=symbol,
            source="offline_replay",
            bid=self.price,
            ask=self.price,
            mark=self.price,
            book_at=self.clock.utcnow(),
            mark_at=self.clock.utcnow(),
            received_at=self.clock.utcnow(),
        )
        return FuturesMarketSnapshot(
            quote=q,
            index_price=self.price,
            displayed_funding_rate="0",
            next_funding_at=NOW + timedelta(hours=8),
            bid_quantity="10",
            ask_quantity="10",
        )

    async def latest(self, *args):
        return frame()


async def prepared(tmp_path):
    from agent_platform.adapters.sqlite.agent_events import SqliteAgentEventStore
    from agent_platform.adapters.sqlite.event_agent import SqliteAgentRunStore
    from agent_platform.adapters.sqlite.event_paper import SqliteEventPaperBackend
    from agent_platform.adapters.sqlite.position_protection import SqliteProtectionStore
    from agent_platform.adapters.sqlite.trading_execution import SqliteExecutionJournal
    from agent_platform.adapters.sqlite.watches import SqliteWatchStore
    from agent_platform.application.agent_context import AgentContextBuilder
    from agent_platform.application.agent_intents import AgentIntentService
    from agent_platform.application.position_guardian import PositionGuardian
    from agent_platform.application.trade_authorization import EventTradeAuthorizer
    from agent_platform.application.trading_execution import TradeExecutionService
    from agent_platform.domain.watch_rules import evaluate_watch
    from tests.fixtures.event_agent_cases import request

    path = tmp_path / "paper.sqlite"
    runs, watches, events = (
        SqliteAgentRunStore(path),
        SqliteWatchStore(path),
        SqliteAgentEventStore(path),
    )
    await runs.initialize()
    value = lane()
    await runs.save_lane(value, 0)
    clock = FakeClock(NOW)
    market = Market(clock)
    protection = SqliteProtectionStore(path)
    backend = SqliteEventPaperBackend(path)
    await backend.configure(value, clock.utcnow())
    await backend.mark(value.scope, (await market.snapshot(value.symbol)).quote, clock.utcnow())
    journal = SqliteExecutionJournal(path)
    await journal.initialize()
    authorizer = EventTradeAuthorizer(
        runs=runs, watches=watches, events=events, data=market, protections=protection
    )
    execution = TradeExecutionService(
        journal=journal,
        execution=backend,
        accounts=backend,
        market=market,
        clock=clock,
        market_source="offline_replay",
        authorizer=authorizer,
    )
    intents = AgentIntentService(
        runs=runs,
        protections=protection,
        execution=execution,
        accounts=backend,
        market=market,
        clock=clock,
        authorizer=authorizer,
    )
    guardian = PositionGuardian(
        protections=protection,
        runs=runs,
        execution=execution,
        accounts=backend,
        market=market,
        clock=clock,
    )
    watch = await watches.create(definition(session_id=value.session_id))
    watch = await watches.commit_evaluation(
        watch.definition.watch_id, 1, frame(), evaluate_watch(watch, frame(), NOW)
    )
    lease = await events.claim(value.lane_id, NOW, 120)
    run = await runs.claim(lease.event, value, NOW)
    context = AgentContextBuilder(data=market, watches=watches, clock=clock)
    run = await runs.set_context(run.run_id, await context.for_event(lease.event, value))
    call = ToolCall(
        tool_call_id="open-1",
        name="submit_trade_intent",
        arguments={"action": "open_long", "quantity": "1", "protective_stop_mark": "95"},
    )
    req = request(run_id=run.run_id, request_id=run.run_id + ":1")
    run = await runs.record_turn(run.run_id, req, None, "SENT")
    run = await runs.record_turn(
        run.run_id, req, response(req.request_id, calls=(call,)), "RESPONDED"
    )
    run = await runs.begin_tool(run.run_id, call)
    return value, runs, protection, backend, execution, intents, call, run, market, clock, guardian
