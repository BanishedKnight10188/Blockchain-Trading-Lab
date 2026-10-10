"""One ordered offline pass; live state and simulated balances never share a DTO."""

from collections.abc import Iterable, Mapping
from datetime import timedelta

from agent_platform.adapters.fake.clock import FakeClock
from agent_platform.adapters.fake.market import FakeMarket
from agent_platform.adapters.paper.execution import PaperSimulator
from agent_platform.domain.account import AccountSnapshot, PositionView
from agent_platform.domain.common import RunMode, required_identifier
from agent_platform.domain.decisions import DecisionSnapshot, DecisionTrigger
from agent_platform.domain.market import FeatureSnapshot, MarketEvent
from agent_platform.domain.paper import PaperIntent
from agent_platform.domain.sessions import AgentSession
from agent_platform.ports.advisory import AdvisoryPort

from .report import ReplayDecision, ReplayReport


class ReplayRunner:
    def __init__(
        self,
        session: AgentSession,
        account: AccountSnapshot,
        *,
        run_id: str = "offline-v1",
        paper: PaperSimulator | None = None,
        intents: Mapping[str, PaperIntent] | None = None,
    ):
        self.session = session
        self.account = account
        self.run_id = required_identifier(run_id)
        self.paper = paper
        self.intents = dict(intents or {})

    async def run(
        self,
        events: Iterable[MarketEvent],
        advisory: AdvisoryPort,
        mode: RunMode = RunMode.ADVISORY,
    ) -> ReplayReport:
        selected_mode = RunMode(mode)
        if selected_mode == RunMode.PAPER and self.paper is None:
            raise ValueError("explicit paper replay requires a separate simulator")
        clock, market = FakeClock(self.session.updated_at), FakeMarket()
        seen: dict[str, MarketEvent] = {}
        decisions, fills = [], []
        duplicates = 0
        initial_balances = self.paper.balances if self.paper else ()
        previous_occurred_at = None
        for raw in events:
            event = MarketEvent.model_validate_json(raw.model_dump_json())
            if event.symbol != "BTCUSDT":
                raise ValueError("M0 replay supports only BTCUSDT")
            if event.event_id in seen:
                if seen[event.event_id] != event:
                    raise ValueError("replay event identity refers to changed inputs")
                duplicates += 1
                continue
            if previous_occurred_at is not None and event.occurred_at < previous_occurred_at:
                raise ValueError("replay event time cannot regress")
            previous_occurred_at = event.occurred_at
            timestamp = max(event.occurred_at, event.received_at)
            clock.advance_to(timestamp)
            current = market.observe(event)
            snapshot_id = f"{self.run_id}:snapshot:{event.event_id}"
            evidence = (event.event_id, snapshot_id + ":features", snapshot_id + ":account")
            quantity = next(
                (item.total for item in self.account.balances if item.asset == "BTC"), "0"
            )
            snapshot = DecisionSnapshot(
                snapshot_id=snapshot_id,
                session_id=self.session.session_id,
                session_revision=self.session.revision,
                style_revision=self.session.style_revision,
                style=self.session.style,
                captured_at=clock.utcnow(),
                market=current,
                features=FeatureSnapshot(
                    symbol=event.symbol,
                    as_of=timestamp,
                    snapshot_id=evidence[1],
                    algorithm_version="m0-uncomputed",
                ),
                account=self.account,
                position=PositionView(
                    symbol=event.symbol, quantity=quantity, cost_status="unknown"
                ),
                trigger=DecisionTrigger(
                    trigger_id=snapshot_id + ":trigger",
                    session_id=self.session.session_id,
                    kind="manual",
                    requested_at=timestamp,
                    expires_at=timestamp + timedelta(seconds=60),
                    event_ids=(event.event_id,),
                ),
                evidence_ids=evidence,
            )
            assessment = advisory.evaluate(snapshot)
            if set(assessment.evidence_ids) - set(evidence):
                raise ValueError("offline advice references evidence outside its snapshot")
            decisions.append(
                ReplayDecision(event_id=event.event_id, snapshot=snapshot, assessment=assessment)
            )
            if selected_mode == RunMode.PAPER and event.event_id in self.intents:
                fill = await self.paper.simulate(self.intents[event.event_id], current)
                if fill not in fills:
                    fills.append(fill)
            seen[event.event_id] = event
        return ReplayReport(
            run_id=self.run_id,
            mode=selected_mode,
            rule_version=getattr(advisory, "rule_version", "unspecified"),
            events_processed=len(decisions),
            duplicate_count=duplicates,
            decisions=tuple(decisions),
            paper_fills=tuple(fills),
            paper_initial_balances=initial_balances,
            paper_final_balances=self.paper.balances if self.paper else (),
        )
