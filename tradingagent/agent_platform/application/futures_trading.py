"""Independent typed JEV lane over replaceable account, market and execution ports."""

import asyncio
import json
from datetime import timedelta
from decimal import ROUND_FLOOR, Context, Decimal, localcontext
from uuid import uuid4

from agent_platform.application.shared_futures_risk import SharedFuturesRisk
from agent_platform.config import FuturesCadence
from agent_platform.domain.background import MultiScaleSnapshot
from agent_platform.domain.costs import RouteDecision
from agent_platform.domain.decision_models import (
    DecisionCriterion,
    DecisionModelRequest,
    DecisionModelResponse,
    DecisionQuestion,
)
from agent_platform.domain.futures_market import FuturesContractRules
from agent_platform.domain.futures_values import FUTURES_CLOCK_UNCERTAINTY_MS
from agent_platform.domain.session_market import HistoricalMarketData
from agent_platform.domain.trade_evidence import TradeDecisionEvidence
from agent_platform.domain.trading_execution import ExecutionScope, TradeCommand
from agent_platform.domain.trading_plans import build_plans
from agent_platform.domain.trading_runtime import (
    TradingCycle,
    TradingLimits,
    TradingPolicy,
    TradingRun,
)
from agent_platform.ports.futures_market import FuturesMarketUnavailable
from agent_platform.ports.model import ModelCallFailed
from agent_platform.ports.persistence import BudgetExceeded, BudgetFrozen, HourlyCallLimitExceeded
from agent_platform.ports.session_analysis import HistoricalDataUnavailable
from agent_platform.ports.trading_execution import ExecutionRejected


class TradingGuard(ValueError):
    pass


JEV_EXIT_INSTRUCTIONS = (
    "For a held position, decide the timing of take-profit and stop-loss yourself using "
    "current market evidence, entry price, unrealized PnL, funding, fees and confirmed style. "
    "No fixed profit/loss percentage or price trigger is configured. Select a reduction or "
    "full close when appropriate, including when the position is profitable; WAIT retains "
    "the current exposure. Do not wait for a fixed trigger or assume an exchange protective "
    "order exists. Hard limits and execution guards always prevail. "
)


MODEL_PAUSE_REASONS = frozenset(
    {
        "provider_access_denied",
        "provider_error",
        "provider_timeout",
        "provider_transport_error",
        "invalid_model_response",
        "invalid_model_usage",
        "invalid_model_assessment",
        "invalid_model_metadata",
        "model_read_only",
        "model_budget_exhausted",
        "model_billing_frozen",
        "model_hourly_limit",
    }
)


def _model_pause_required(reason, usage):
    return (
        reason in MODEL_PAUSE_REASONS
        or (type(reason) is str and reason.startswith("provider_http_"))
        or (usage is not None and usage.billing_status != "confirmed")
    )


class FuturesTradingService:
    def __init__(
        self,
        *,
        sessions,
        controls,
        store,
        backend,
        maintenance,
        execution,
        model,
        history,
        clock,
        market_source,
        decision_source,
        price_version,
        environment="paper",
        cadence=None,
        archive=None,
        advisory_only=False,
    ):
        self.sessions, self.controls, self.store = sessions, controls, store
        self.backend, self.maintenance, self.execution = backend, maintenance, execution
        self.model, self.history, self.clock = model, history, clock
        self.market_source, self.decision_source, self.price_version = (
            market_source,
            decision_source,
            price_version,
        )
        self.cadence = cadence or FuturesCadence()
        self.archive = archive
        self.advisory_only = advisory_only
        self.gate = asyncio.Lock()
        self._step_lock = asyncio.Semaphore(self.cadence.max_predictions)
        self._history_lock = asyncio.Lock()
        self.prediction_metrics = {
            "requests_started": 0,
            "last_model_ms": None,
            "skipped_dispatch": 0,
        }
        self._last_model_dispatch = None
        self._consecutive_provider_timeouts = 0
        self._provider_retry_until = 0.0
        self.ready = False
        self.environment = environment
        self.decision_requested = asyncio.Event()
        self._history = None
        self.last_failure = None
        self.multiscale = None
        self.context_gate = asyncio.Lock()
        self.context_epoch = 0

    def scope(self, session):
        return ExecutionScope(
            environment=self.environment,
            account_ref=self.environment + ":futures:" + session.session_id,
            session_id=session.session_id,
            symbol=session.analysis_target.symbol,
        )

    async def _current(self, *, session_id=None, style_revision=None, trader_revision=None):
        session = await self.sessions.active()
        controls = await self.controls.current()
        if (
            session is None
            or session.analysis_target.market != "usdt_perpetual"
            or (session_id is not None and session.session_id != session_id)
            or (style_revision is not None and session.style_revision != style_revision)
            or (trader_revision is not None and controls.trader_revision != trader_revision)
            or not controls.trader.enabled
            or controls.operation.mode != "auto"
            or controls.operation.execution_environment != self.environment
        ):
            raise TradingGuard("selection_changed")
        return session, controls

    async def configure(self, limits, policy, *, session_id, style_revision):
        limits = TradingLimits.model_validate_json(limits.model_dump_json())
        policy = TradingPolicy.model_validate_json(policy.model_dump_json())
        if policy.order_notional_usdt > limits.max_position_notional:
            raise TradingGuard("order_exceeds_position_limit")
        async with self.gate:
            if not self.ready:
                raise TradingGuard("runtime_not_owned")
            session, _ = await self._current(session_id=session_id, style_revision=style_revision)
            scope = self.scope(session)
            prior = await self.store.run(scope.account_ref)
            if prior is not None and (
                prior.policy != policy
                or prior.limits != limits
                or prior.market_source != self.market_source
                or prior.decision_source != self.decision_source
            ):
                raise TradingGuard("configured_policy_is_fixed")
            rules = FuturesContractRules.model_validate(
                await self.maintenance.market.rules(scope.symbol)
            )
            if (
                rules.contract.symbol != scope.symbol
                or rules.source != self.market_source
                or not timedelta(0)
                <= self.clock.utcnow() - rules.captured_at
                <= timedelta(seconds=300)
            ):
                raise TradingGuard("contract_rules_unavailable")
            wallet = await self.backend.configure(
                scope, limits, rules, self.clock.utcnow(), style_revision=style_revision
            )
            run = prior or TradingRun(
                scope=scope,
                policy=policy,
                limits=limits,
                qty_step=rules.market_step,
                min_qty=rules.market_min_qty,
                max_qty=min(rules.market_max_qty, Decimal("1e9")),
                min_notional=rules.min_notional,
                market_source=self.market_source,
                decision_source=self.decision_source,
                created_at=wallet.created_at,
            )
            await self.store.configure(run)
            await self.maintenance.maintain(scope, run.created_at)
            return run

    async def start(self, *, account_ref, expected_revision, style_revision, trader_revision):
        async with self.gate:
            if not self.ready:
                raise TradingGuard("runtime_not_owned")
            session, controls = await self._current(
                style_revision=style_revision, trader_revision=trader_revision
            )
            scope = self.scope(session)
            if scope.account_ref != account_ref or session.status != "running":
                raise TradingGuard("session_not_running")
            run = await self.store.run(account_ref)
            self._sources(run)
            if hasattr(self.model, "validate_active"):
                self.model.validate_active()
            await self.maintenance.maintain(scope, run.created_at)
            if await self.execution.journal.unresolved(account_ref):
                raise TradingGuard("execution_unresolved")
            await self.backend.start(
                scope,
                expected_revision,
                self.clock.utcnow(),
                style_revision=style_revision,
                trader_revision=controls.trader_revision,
            )
            if hasattr(self.model, "set_enabled"):
                self.model.set_enabled(True)
            self._consecutive_provider_timeouts = 0
            self._provider_retry_until = 0.0
            self.last_failure = None
            self.decision_requested.set()

    async def pause(self, *, account_ref, expected_revision):
        async with self.gate:
            run = await self.store.run(account_ref)
            if run is None:
                raise TradingGuard("run_unconfigured")
            session = await self.sessions.active()
            if session is None or run.scope.session_id != session.session_id:
                raise TradingGuard("selection_changed")
            await self.backend.pause(run.scope, expected_revision, self.clock.utcnow())
            if hasattr(self.model, "set_enabled"):
                self.model.set_enabled(False)

    def _sources(self, run):
        if run is None:
            raise TradingGuard("run_unconfigured")
        if (
            run.market_source != self.market_source
            or run.decision_source != self.decision_source
            or (
                self.decision_source == "real_jev"
                and self.market_source != "binance_futures_public"
            )
        ):
            raise TradingGuard("source_changed")

    async def maintain(self):
        async with self.context_gate:
            if self.multiscale is not None:
                current = await self.sessions.active()
                if current is not None and current.analysis_target.market == "usdt_perpetual":
                    await self.multiscale.prepare(
                        current, enabled=bool(getattr(self.model, "enabled", False))
                    )
        async with self.gate:
            session = await self.sessions.active()
            controls = await self.controls.current()
            accounts = await self.backend.maintenance_accounts()
            for scope, _ in accounts:
                account = await self.backend.account(scope, self.clock.utcnow())
                if account.status == "running" and (
                    session is None
                    or session.session_id != scope.session_id
                    or session.status != "running"
                    or not controls.trader.enabled
                    or controls.operation.mode != "auto"
                    or controls.operation.execution_environment != self.environment
                ):
                    await self.backend.pause(scope, account.revision, self.clock.utcnow())
            await self.maintenance.all()
            if (
                session is not None
                and session.analysis_target.market == "usdt_perpetual"
                and not any(s.session_id == session.session_id for s, _ in accounts)
            ):
                await self.maintenance.observe(session.analysis_target.symbol)

    async def recover(self):
        async with self.gate:
            await self.backend.recover(self.clock.utcnow())
            await self.store.recover(self.clock.utcnow())
            if hasattr(self.model, "set_enabled"):
                self.model.set_enabled(False)
            for scope, _ in await self.backend.maintenance_accounts():
                for record in await self.execution.journal.unresolved(scope.account_ref):
                    await self.execution.reconcile(record.command.command_id)

    async def _history_for(self, session):
        async with self.context_gate:
            if self.multiscale is not None:
                await self.multiscale.prepare(session, enabled=True)
                return self.multiscale.snapshot(session)
        async with self._history_lock:
            return await self._load_history(session)

    async def _load_history(self, session):
        target = session.analysis_target
        expected_end = self.clock.utcnow().replace(minute=0, second=0, microsecond=0)
        if (
            self._history is None
            or self._history.target != target
            or self._history.requested_end != expected_end
        ):
            if self.history is None:
                raise TradingGuard("history_unavailable")
            data = HistoricalMarketData.model_validate_json(
                (await self.history.history(target)).model_dump_json()
            )
            expected_source = (
                "fake" if self.market_source == "offline_replay" else "binance_futures_public"
            )
            if (
                data.target != target
                or data.source != expected_source
                or data.requested_end != expected_end
                or data.captured_at > self.clock.utcnow()
            ):
                raise TradingGuard("history_unavailable")
            self._history = data
        return self._history

    def _request(self, cycle, run, session, account, snapshot, history, plans=()):
        multiscale = isinstance(history, MultiScaleSnapshot)
        if multiscale and not history.ready_for(
            session.session_id,
            session.style_revision,
            session.analysis_target.symbol,
            self.clock.utcnow(),
        ):
            raise TradingGuard("short_history_pending")
        bars = history.short.candles if multiscale else history.candles
        with localcontext(Context(prec=80)):
            entry_price = (
                str(account.entry_notional / account.quantity) if account.quantity else None
            )
        state = {
            "environment": run.scope.environment,
            "operation_mode": "advice" if self.advisory_only else "auto_paper",
            "advice_only": self.advisory_only,
            "market": "usdt_perpetual",
            "currency": "USDT",
            "symbol": run.scope.symbol,
            "market_source": run.market_source,
            "decision_source": run.decision_source,
            "clock_evidence_policy": {
                "max_exchange_lead_ms": FUTURES_CLOCK_UNCERTAINTY_MS
                if run.market_source == "binance_futures_public"
                else 0,
                "original_timestamps_preserved": True,
                "max_quote_age_seconds": 5,
            },
            "style": session.style.context.model_dump(mode="json"),
            "style_revision": session.style_revision,
            "trader_revision": cycle.trader_revision,
            "account": account.model_dump(mode="json"),
            "position_management": {
                "policy_version": "jev-managed-exits-v1",
                "take_profit_mode": "jev_decision",
                "stop_loss_mode": "jev_decision",
                "fixed_price_triggers": None,
                "exchange_protective_orders": False,
                "entry_price": entry_price,
                "decision_interval_seconds": self.cadence.decision_seconds,
                "prediction_ttl_seconds": self.cadence.prediction_ttl_seconds,
            },
            "market_snapshot": snapshot.model_dump(mode="json", exclude={"recent_quotes"}),
            "recent_quote_ticks": {
                "source": snapshot.quote.source,
                "symbol": snapshot.quote.symbol,
                "columns": ["book_at", "mark_at", "received_at", "bid", "ask", "mark"],
                "samples": [
                    [
                        q.book_at.isoformat(),
                        q.mark_at.isoformat(),
                        q.received_at.isoformat(),
                        str(q.bid),
                        str(q.ask),
                        str(q.mark),
                    ]
                    for q in snapshot.recent_quotes
                ],
            },
            "hard_limits": run.limits.model_dump(mode="json"),
            "policy": run.policy.model_dump(mode="json"),
            "history": history.context_data()
            if multiscale
            else {
                "hash": history.content_hash,
                "days": history.target.history_days,
                "start": history.requested_start.isoformat(),
                "end": history.requested_end.isoformat(),
                "bar_count": len(bars),
                "first_close": str(bars[0].close),
                "high": str(max(b.high for b in bars)),
                "low": str(min(b.low for b in bars)),
                "recent_closed_1h": [
                    [
                        b.opened_at.isoformat(),
                        str(b.open),
                        str(b.high),
                        str(b.low),
                        str(b.close),
                        str(b.volume),
                    ]
                    for b in bars[-12:]
                ],
            },
        }
        if multiscale:
            state["market_context"] = state.pop("history")
            state.pop("recent_quote_ticks")
        parameterized = run.policy.decision_mode == "parameterized"
        if parameterized:
            if len(plans) < 2:
                raise TradingGuard("no_feasible_plan")
            state["trade_plans"] = [p.model_dump(mode="json") for p in plans]
        question = (
            DecisionQuestion(
                question_id="plan",
                kind="choice",
                instructions=JEV_EXIT_INSTRUCTIONS
                + "Choose a complete trade_plans candidate for the strategy and style. "
                "Sizes and leverage are jointly fixed in each candidate; never combine candidates. "
                "Entry percentages are margin budget as a fraction of net account equity. "
                "Add/reduce percentages are fractions of current contract quantity. "
                "Changed leverage affects the whole isolated position. Hard limits always prevail. "
                "Choose WAIT when evidence is insufficient. Confidence is not a measured win rate.",
                criteria=tuple(
                    DecisionCriterion(
                        key=p.candidate_id,
                        description=(
                            "Leave quantity and leverage unchanged"
                            if p.intent == "wait"
                            else f"{p.intent}; {p.percent}% of {p.sizing_basis}; "
                            f"quantity {p.quantity}; whole-position leverage {p.leverage}x; "
                            "use this exact precomputed plan"
                        ),
                    )
                    for p in plans
                ),
            )
            if parameterized
            else None
        )
        return DecisionModelRequest(
            request_id=cycle.request_id,
            route=RouteDecision(
                route_id="futures-jev:" + cycle.request_id,
                kind="economy",
                purpose="advisory",
                reason="independent_futures_trader",
                model_version="typesafe/jev-1.13",
                price_version=self.price_version,
                paid=True,
            ),
            captured_at=cycle.created_at,
            deadline=cycle.created_at + timedelta(seconds=self.cadence.prediction_ttl_seconds),
            response_deadline=cycle.created_at
            + timedelta(
                seconds=max(self.cadence.prediction_ttl_seconds, self.cadence.response_wait_seconds)
            ),
            question_set_version=("futures-six-layer-v1" if multiscale else "futures-plan-v3")
            if parameterized
            else "futures-action-v2",
            state_json=json.dumps(state, ensure_ascii=False, separators=(",", ":"))
            if multiscale
            else json.dumps(state, ensure_ascii=False),
            max_output_tokens=2048 if parameterized else 512,
            questions=(question,)
            if parameterized
            else (
                DecisionQuestion(
                    question_id="action",
                    kind="choice",
                    instructions=JEV_EXIT_INSTRUCTIONS
                    + "Choose one action for the selected isolated USDT position. "
                    "Follow the user's strategy and confirmed style. Hard limits always prevail. "
                    "Opposite direction requires a separate full close first. REDUCE closes the "
                    "current position. If evidence is insufficient choose WAIT. Confidence is "
                    "not a measured win rate. Historical bars are summarized; do not invent bars.",
                    criteria=tuple(
                        DecisionCriterion(key=k, description=d)
                        for k, d in (
                            ("OPEN_LONG", "Increase long exposure by the configured notional"),
                            ("OPEN_SHORT", "Increase short exposure by the configured notional"),
                            ("REDUCE", "Close the current position without reversing"),
                            ("WAIT", "Keep current position unchanged"),
                        )
                    ),
                ),
            ),
        )

    @staticmethod
    def _risk(run, original, current, account, action, quantity, leverage=None):
        return SharedFuturesRisk.evaluate(
            run, original, current, account, action, quantity, leverage
        )

    async def step(self):
        if (
            not self.ready
            or self.clock.monotonic() < self._provider_retry_until
            or self._step_lock.locked()
            or getattr(self.model, "limit_reached", False)
        ):
            return
        async with self._step_lock:
            try:
                session, controls = await self._current()
                if session.status != "running":
                    return
                scope = self.scope(session)
                run = await self.store.run(scope.account_ref)
                self._sources(run)
                account = await self.backend.account(scope, self.clock.utcnow())
                if account.status != "running":
                    return
                if hasattr(self.model, "validate_active"):
                    try:
                        self.model.validate_active()
                    except ValueError:
                        async with self.gate:
                            await self.backend.pause(scope, account.revision, self.clock.utcnow())
                            self.model.set_enabled(False)
                        self.last_failure = "model_policy_expired"
                        return
                # Fetch history before the model deadline and outside the funds gate.
                context_epoch = self.context_epoch
                history = await self._history_for(session)
                async with self.gate:
                    if context_epoch != self.context_epoch:
                        return  # A paused input switch invalidates preparation, too.
                    if getattr(self.model, "limit_reached", False):
                        return
                    snapshot = await self.maintenance.maintain(scope, run.created_at)
                    session, controls = await self._current(
                        session_id=scope.session_id,
                        style_revision=session.style_revision,
                        trader_revision=controls.trader_revision,
                    )
                    account = await self.backend.account(scope, self.clock.utcnow())
                    if account.status != "running":
                        return
                    pending = await self.execution.journal.unresolved(scope.account_ref)
                    if pending:
                        for r in pending:
                            await self.execution.reconcile(r.command.command_id)
                        return
                    cycle = TradingCycle(
                        request_id=uuid4().hex,
                        scope=scope,
                        style_revision=session.style_revision,
                        trader_revision=controls.trader_revision,
                        account_revision=account.revision,
                        created_at=self.clock.utcnow(),
                        quote=snapshot.quote,
                    )
                    await self.store.claim(cycle, max_pending=self.cadence.max_predictions)
            except (FuturesMarketUnavailable, HistoricalDataUnavailable) as error:
                self.last_failure = (
                    "history_unavailable"
                    if isinstance(error, HistoricalDataUnavailable)
                    else "market_unavailable"
                )
                return
            except (ValueError, LookupError) as error:
                reason = str(error)
                self.last_failure = (
                    reason
                    if reason
                    in {
                        "history_unavailable",
                        "source_changed",
                        "funding_publication_pending",
                        "funding_catchup_pending",
                        "execution_unresolved",
                        "background_model_unconfigured",
                        "background_pending",
                        "background_history_pending",
                        "background_model_failed",
                        "short_history_pending",
                    }
                    else None
                )
                return
            self.last_failure = None
            outcome = {"status": "rejected", "reason": "decision_unavailable"}
            cancelled = False
            retry_timeout = False
            try:
                plans = (
                    build_plans(run, account, snapshot.quote)
                    if run.policy.decision_mode == "parameterized"
                    else ()
                )
                request = self._request(cycle, run, session, account, snapshot, history, plans)
                cycle = TradingCycle.model_validate(cycle.model_dump() | {"model_request": request})
                # Persist the exact question before any paid dispatch or execution.
                await self.store.prepare(cycle)
                began = asyncio.get_running_loop().time()
                if (
                    self.decision_source == "real_jev"
                    and self._last_model_dispatch is not None
                    and began - self._last_model_dispatch < self.cadence.decision_seconds
                ):
                    # Preparation may wait on the funds/storage gate. Never let
                    # delayed slots burst into paid calls when that gate opens.
                    self.prediction_metrics["skipped_dispatch"] += 1
                    raise TradingGuard("prediction_tick_skipped")
                self._last_model_dispatch = began
                self.prediction_metrics["requests_started"] += 1
                try:
                    # The budgeted model bounds provider IO and owns fee writes.
                    # Timing its settlement as inference loses confirmed usage
                    # and mislabels local deadline expiry as provider_timeout.
                    # Alternate ports still need a service-level wait bound.
                    timeout = (
                        None
                        if getattr(self.model, "provider_deadline_managed", False) is True
                        else self.cadence.prediction_ttl_seconds + 0.25
                    )
                    async with asyncio.timeout(timeout):
                        response = DecisionModelResponse.model_validate_json(
                            (await self.model.decide(request)).model_dump_json()
                        )
                finally:
                    self.prediction_metrics["last_model_ms"] = round(
                        (asyncio.get_running_loop().time() - began) * 1000, 1
                    )
                    outcome["model_latency_ms"] = int(self.prediction_metrics["last_model_ms"])
                response.bind_to(request)
                self._consecutive_provider_timeouts = 0
                self._provider_retry_until = 0.0
                outcome["model_response"] = response
                answer = response.answers[0]
                plan = next((p for p in plans if p.candidate_id == answer.choice), None)
                if plans and plan is None:
                    raise ModelCallFailed("invalid_model_assessment", response.usage)
                outcome |= {
                    "decision": (
                        "WAIT"
                        if plan.action == "wait"
                        else {
                            "open_long": "OPEN_LONG",
                            "open_short": "OPEN_SHORT",
                            "reduce": "REDUCE",
                        }[plan.action]
                    )
                    if plan
                    else answer.choice,
                    "confidence": answer.confidence,
                    "usage": response.usage,
                    "plan": plan,
                }
                async with self.gate:
                    await self._current(
                        session_id=scope.session_id,
                        style_revision=cycle.style_revision,
                        trader_revision=cycle.trader_revision,
                    )
                    if self.clock.utcnow() >= request.deadline:
                        raise TradingGuard("decision_expired")
                    await self.maintenance.maintain(scope, run.created_at)
                    current = await self.backend.account(scope, self.clock.utcnow())
                    if current.revision != cycle.account_revision or current.status != "running":
                        raise TradingGuard("account_changed")
                    if answer.confidence < run.policy.min_confidence:
                        raise TradingGuard("low_confidence")
                    if not self.ready:
                        raise TradingGuard("runtime_not_owned")
                    if not await self.store.accept_result(cycle.request_id):
                        raise TradingGuard("prediction_superseded")
                    if outcome["decision"] == "WAIT":
                        outcome |= {"status": "wait", "reason": None}
                    elif self.advisory_only:
                        outcome |= {"status": "advised", "reason": None}
                    else:
                        action = {
                            "OPEN_LONG": "open_long",
                            "OPEN_SHORT": "open_short",
                            "REDUCE": "reduce",
                        }[outcome["decision"]]
                        with localcontext(Context(prec=80)):
                            quantity = (
                                plan.quantity
                                if plan
                                else (
                                    current.quantity
                                    if action == "reduce"
                                    else (
                                        run.policy.order_notional_usdt
                                        / snapshot.quote.ask
                                        / run.qty_step
                                    ).to_integral_value(rounding=ROUND_FLOOR)
                                    * run.qty_step
                                )
                            )
                        if quantity <= 0:
                            raise TradingGuard("order_below_step")
                        target_leverage = plan.leverage if plan and action != "reduce" else None
                        self._risk(
                            run,
                            snapshot.quote,
                            snapshot.quote,
                            current,
                            action,
                            quantity,
                            target_leverage,
                        )
                        checkpoint = await self.store.checkpoint(scope.account_ref)
                        expires = request.deadline
                        if checkpoint is not None and checkpoint.next_due is not None:
                            expires = min(expires, checkpoint.next_due - timedelta(milliseconds=1))
                        if self.clock.utcnow() >= expires:
                            raise TradingGuard("funding_publication_pending")
                        command = TradeCommand(
                            command_id="jev:" + cycle.request_id,
                            scope=scope,
                            action=action,
                            quantity=quantity,
                            target_leverage=target_leverage,
                            created_at=self.clock.utcnow(),
                            expires_at=expires,
                            expected_account_revision=current.revision,
                            style_revision=cycle.style_revision,
                            trader_revision=cycle.trader_revision,
                            decision_evidence=TradeDecisionEvidence(
                                decision_source=run.decision_source,
                                request=request,
                                response=response,
                                plan=plan,
                                elapsed_ms=outcome["model_latency_ms"],
                            ),
                        )
                        outcome["command_id"] = command.command_id
                        await self.store.prepare(
                            TradingCycle.model_validate(
                                cycle.model_dump()
                                | {
                                    k: v
                                    for k, v in outcome.items()
                                    if k
                                    in (
                                        "decision",
                                        "confidence",
                                        "usage",
                                        "command_id",
                                        "plan",
                                        "model_response",
                                        "model_latency_ms",
                                    )
                                }
                            )
                        )
                        record = await self.execution.submit(
                            command,
                            preflight=lambda q, a: self._risk(
                                run, snapshot.quote, q, a, action, quantity, target_leverage
                            ),
                        )
                        outcome |= {
                            "status": record.receipt.status
                            if record.receipt.status in ("filled", "rejected")
                            else "unknown",
                            "reason": record.receipt.reason,
                        }
            except asyncio.CancelledError:
                cancelled = True
                outcome |= {"status": "interrupted", "reason": "cancelled"}
            except ModelCallFailed as error:
                outcome |= {
                    "reason": error.reason,
                    "usage": error.usage,
                    "diagnostic": error.diagnostic,
                }
                if (
                    self.environment == "paper"
                    and getattr(self.model, "provider_deadline_managed", False) is True
                    and error.reason == "provider_timeout"
                    and error.usage is not None
                    and error.usage.billing_status == "unknown"
                    and error.diagnostic is not None
                    and error.diagnostic.stage == "transport"
                ):
                    self._consecutive_provider_timeouts += 1
                    if self._consecutive_provider_timeouts < 3:
                        # New request and fresh snapshot after cooldown; never
                        # replay this POST or release its unknown fee hold.
                        retry_timeout = True
                        self._provider_retry_until = self.clock.monotonic() + 5
                        self.last_failure = "provider_timeout_retry"
                elif error.reason == "decision_expired" and error.usage is not None:
                    self._consecutive_provider_timeouts = 0
                    self._provider_retry_until = 0.0
                if not retry_timeout and _model_pause_required(error.reason, error.usage):
                    self.last_failure = error.reason
                    if hasattr(self.model, "set_enabled"):
                        self.model.set_enabled(False)
            except TimeoutError:
                outcome["reason"] = self.last_failure = "provider_timeout"
                if hasattr(self.model, "set_enabled"):
                    self.model.set_enabled(False)
            except (BudgetExceeded, BudgetFrozen, HourlyCallLimitExceeded) as error:
                reason = (
                    "model_budget_exhausted"
                    if isinstance(error, BudgetExceeded)
                    else "model_billing_frozen"
                    if isinstance(error, BudgetFrozen)
                    else "model_hourly_limit"
                )
                outcome["reason"] = self.last_failure = reason
                if hasattr(self.model, "set_enabled"):
                    self.model.set_enabled(False)
            except (TradingGuard, ExecutionRejected) as error:
                outcome["reason"] = str(error)
            except Exception:
                outcome |= (
                    {"status": "unknown", "reason": "execution_unknown"}
                    if outcome.get("command_id")
                    else {"reason": "decision_unavailable"}
                )
            finished = TradingCycle.model_validate(
                cycle.model_dump() | outcome | {"completed_at": self.clock.utcnow()}
            )
            task = asyncio.create_task(self.store.finish(finished))
            while True:
                try:
                    await asyncio.shield(task)
                    break
                except asyncio.CancelledError:
                    cancelled = True
            if cancelled:
                raise asyncio.CancelledError
            if not retry_timeout and _model_pause_required(finished.reason, finished.usage):
                # Persist the failed decision/fee before pausing the owned
                # account. Paused positions still receive maintenance.
                async with self.gate:
                    current = await self.backend.account(scope, self.clock.utcnow())
                    if current.status == "running":
                        await self.backend.pause(scope, current.revision, self.clock.utcnow())
            return finished

    async def public_view(self):
        session = await self.sessions.active()
        controls = await self.controls.current()
        result = {
            "enabled": self.ready,
            "environment": self.environment,
            "operation_mode": "advice" if self.advisory_only else "auto_paper",
            "market": "usdt_perpetual",
            "real_orders_enabled": False,
            "decision_source": self.decision_source,
            "market_source": self.market_source,
            "paid_models_enabled": self.decision_source == "real_jev"
            and bool(getattr(self.model, "enabled", False)),
            "account": None,
            "cycles": [],
            "operations": [],
            "archive_summary": None,
            "quote": None,
            "policy": None,
            "limits": None,
            "maintenance_failure": None,
            "worker_failure": self.last_failure,
            "trader_revision": controls.trader_revision,
            "cadence": self.cadence.model_dump(mode="json"),
            "model_recovery": {
                "consecutive_timeouts": self._consecutive_provider_timeouts,
                "max_consecutive_timeouts": 3,
                "retrying": self.last_failure == "provider_timeout_retry",
                "cooldown_remaining_seconds": max(
                    0, round(self._provider_retry_until - self.clock.monotonic(), 1)
                ),
            },
            "prediction_metrics": dict(self.prediction_metrics),
            "runtime_metrics": dict(getattr(self, "runtime_metrics", {})),
            "market_status": getattr(self.maintenance.market, "public_status", None),
            "analysis_target": session.analysis_target.model_dump(mode="json") if session else None,
            "session": {
                "session_id": session.session_id,
                "style_revision": session.style_revision,
                "style_strength": session.style.strength,
                "status": session.status,
            }
            if session
            else None,
            **getattr(self.backend, "public_metadata", {}),
        }
        result["model_budget"] = (
            await self.model.budget_status() if hasattr(self.model, "budget_status") else None
        )
        result["model_read_only"] = bool(getattr(self.model, "read_only", False))
        if result["paid_models_enabled"] and hasattr(self.model, "validate_active"):
            try:
                self.model.validate_active()
            except ValueError:
                result["paid_models_enabled"] = False
        result["model_recovery"]["active"] = result["paid_models_enabled"]
        if session is None or session.analysis_target.market != "usdt_perpetual":
            return result
        result["context_mode"] = "multiscale" if self.multiscale is not None else "legacy"
        result["multiscale_context"] = (
            self.multiscale.status(session) if self.multiscale is not None else None
        )
        scope = self.scope(session)
        selected = self.maintenance.selected
        if selected is not None and selected.quote.symbol == scope.symbol:
            result["quote"] = selected.model_dump(mode="json")
            result["quote_fresh"] = (
                timedelta(0)
                <= self.clock.utcnow()
                - min(selected.quote.book_at, selected.quote.mark_at, selected.quote.received_at)
                <= timedelta(seconds=5)
            )
        run = await self.store.run(scope.account_ref)
        if run is None:
            return result
        result |= {
            "policy": run.policy.model_dump(mode="json"),
            "limits": run.limits.model_dump(mode="json"),
            "source_compatible": run.market_source == self.market_source
            and run.decision_source == self.decision_source,
            "cycles": [c.public_summary() for c in await self.store.recent(scope.account_ref)],
            "maintenance_failure": self.maintenance.failures.get(scope.account_ref),
        }
        for cycle in result["cycles"]:
            if cycle["command_id"]:
                try:
                    cycle["execution"] = (
                        await self.execution.journal.get(cycle["command_id"])
                    ).receipt.model_dump(mode="json", exclude={"command": {"decision_evidence"}})
                except LookupError:
                    cycle["execution"] = None
        try:
            account = await self.backend.account(scope, self.clock.utcnow())
            if self.archive is not None:
                result["archive_summary"] = await self.archive.summary(scope.account_ref)
            result["account"] = account.model_dump(mode="json")
            result["operations"] = [
                o.model_dump(
                    mode="json",
                    exclude={"before_state": True, "execution_command": {"decision_evidence"}},
                )
                for o in await self.backend.recent(scope)
            ]
        except (ValueError, LookupError):
            result["maintenance_failure"] = "account_unavailable"
        latest = self.maintenance.latest.get(scope.account_ref)
        if latest is not None:
            result["quote"] = latest.model_dump(mode="json")
            result["quote_fresh"] = (
                timedelta(0)
                <= self.clock.utcnow()
                - min(latest.quote.book_at, latest.quote.mark_at, latest.quote.received_at)
                <= timedelta(seconds=5)
            )
        return result
