"""Independent typed decision loop: fresh market, hard policy, atomic virtual fill."""

import asyncio
import json
from datetime import timedelta
from decimal import Decimal, localcontext
from uuid import uuid4

from agent_platform.domain.costs import RouteDecision
from agent_platform.domain.decision_models import (
    DecisionCriterion,
    DecisionModelRequest,
    DecisionModelResponse,
    DecisionQuestion,
)
from agent_platform.domain.paper import PaperIntent
from agent_platform.domain.paper_trading import PaperSettings
from agent_platform.ports.model import ModelCallFailed
from agent_platform.ports.paper import PaperGuardConflict
from agent_platform.ports.sessions import RevisionConflict


class PaperTradingService:
    def __init__(
        self,
        *,
        store,
        sessions,
        clock,
        market,
        model,
        execution,
        price_version,
        decision_source,
        market_source,
    ):
        if decision_source not in ("offline_mock", "real_jev") or market_source not in (
            "offline_demo",
            "binance_public",
        ):
            raise ValueError("paper sources must be explicit")
        if decision_source == "real_jev" and market_source != "binance_public":
            raise ValueError("real paper decisions require public market observations")
        self.store, self.sessions, self.clock = store, sessions, clock
        self.market, self.model, self.execution = market, model, execution
        self.price_version, self.decision_source, self.market_source = (
            price_version,
            decision_source,
            market_source,
        )
        self._step_lock = asyncio.Lock()

    async def configure(self, settings: PaperSettings, *, session_id, style_revision):
        session = await self.sessions.active()
        if session is None:
            raise PaperGuardConflict("select and confirm session style first")
        if session.session_id != session_id or session.style_revision != style_revision:
            raise PaperGuardConflict("confirmed paper session or style changed")
        if not session.analysis_target.legacy_spot:
            raise PaperGuardConflict("futures_paper_not_configured")
        return await self.store.create(
            session.session_id,
            settings,
            self.clock.utcnow(),
            decision_source=self.decision_source,
            market_source=self.market_source,
            expected_style_revision=style_revision,
        )

    async def start(self, account_ref, expected_revision, *, style_revision=None):
        session = await self.sessions.active()
        if session is None or not session.analysis_target.legacy_spot:
            raise PaperGuardConflict("futures_paper_not_configured")
        account = await self.store.get(account_ref)
        self._scope(account)
        if hasattr(self.model, "validate_active"):
            self.model.validate_active()
        result = await self.store.start(
            account_ref,
            expected_revision,
            self.clock.utcnow(),
            expected_style_revision=style_revision,
        )
        if hasattr(self.model, "set_enabled"):
            self.model.set_enabled(True)
        return result

    async def pause(self, account_ref, expected_revision, *, activation_revision=None):
        result = await self.store.pause(
            account_ref,
            expected_revision,
            self.clock.utcnow(),
            expected_activation_revision=activation_revision,
        )
        if hasattr(self.model, "set_enabled"):
            self.model.set_enabled(False)
        return result

    def _scope(self, account):
        if (
            account.decision_source != self.decision_source
            or account.market_source != self.market_source
        ):
            raise PaperGuardConflict("paper account source differs from this configured process")

    def _quote(self, market):
        now = self.clock.utcnow()
        if (
            market is None
            or market.symbol != "BTCUSDT"
            or market.status != "ready"
            or market.book is None
        ):
            raise PaperGuardConflict("market_unavailable")
        for timestamp in (
            market.as_of,
            market.latest_received_at,
            market.latest_quote_at,
            market.book_as_of,
        ):
            if timestamp is None or not timedelta(0) <= now - timestamp <= timedelta(seconds=5):
                raise PaperGuardConflict("market_stale")
        # Bound arithmetic on provider amounts before using Decimal operations.
        for price in (market.book.bid, market.book.ask):
            if len(price.as_tuple().digits) > 20 or not -16 <= price.as_tuple().exponent <= 16:
                raise PaperGuardConflict("market_invalid")
        return market

    def _request(self, account, cycle, market, features=None):
        state = {
            "environment": "paper",
            "symbol": "BTCUSDT",
            "market_source": self.market_source,
            "captured_at": cycle.created_at.isoformat(),
            "deadline": cycle.deadline.isoformat(),
            "quote_at": market.latest_quote_at.isoformat(),
            "bid": str(market.book.bid),
            "ask": str(market.book.ask),
            "virtual_holdings": {"BTC": str(account.btc), "USDT": str(account.usdt)},
            "limits": account.settings.model_dump(mode="json"),
            "style": cycle.style.context.model_dump(mode="json"),
            "style_revision": cycle.style_revision,
            "trader_revision": cycle.trader_revision,
            "candles": [
                c.model_dump(mode="json")
                for c in market.candles[-12:]
                if c.is_closed and c.closed_at <= cycle.created_at
            ],
            "features": features.model_dump(mode="json") if features is not None else None,
        }
        return DecisionModelRequest(
            request_id=cycle.request_id,
            captured_at=cycle.created_at,
            deadline=cycle.deadline,
            route=RouteDecision(
                route_id="paper-route:" + cycle.request_id,
                kind="economy",
                purpose="advisory",
                reason="paper_trader",
                model_version="typesafe/jev-1.13",
                price_version=self.price_version,
                paid=True,
            ),
            question_set_version="paper-action-v1",
            state_json=json.dumps(state, ensure_ascii=False),
            questions=(
                DecisionQuestion(
                    question_id="action",
                    kind="choice",
                    instructions="Choose BUY, SELL or WAIT for BTCUSDT using the supplied "
                    "strategy, style and virtual holdings. Never exceed hard limits. "
                    "Missing evidence requires WAIT. Quantity is fixed by code. "
                    "Confidence is not a win rate.",
                    criteria=tuple(
                        DecisionCriterion(key=key, description=description)
                        for key, description in (
                            ("BUY", "Increase the virtual BTC position"),
                            ("SELL", "Reduce the virtual BTC position"),
                            ("WAIT", "Make no trade"),
                        )
                    ),
                ),
            ),
        )

    def _risk(self, account, decision, before, after):
        policy = account.settings
        with localcontext() as ctx:
            ctx.prec = 34
            old = before.book.ask if decision == "BUY" else before.book.bid
            new = after.book.ask if decision == "BUY" else after.book.bid
            if abs(new - old) / old * 10000 > policy.max_price_drift_bps:
                raise PaperGuardConflict("price_drift")
            equity = account.usdt + account.btc * after.book.bid
            if policy.initial_usdt - equity >= policy.max_run_loss_usdt:
                raise PaperGuardConflict("run_loss_limit")
            quantity = policy.order_quantity
            # Explicit Paper filters, never reported as Binance exchangeInfo.
            if quantity % Decimal("0.000001") != 0 or quantity * new < 10:
                raise PaperGuardConflict("paper_filter")
            if decision == "BUY":
                if account.btc + quantity > policy.max_position_quantity:
                    raise PaperGuardConflict("position_limit")
                price = new * (1 + policy.slippage_bps / 10000)
                debit = quantity * price * (1 + policy.fee_bps / 10000)
                if debit > account.usdt:
                    raise PaperGuardConflict("insufficient_funds")
                post_equity = account.usdt - debit + (account.btc + quantity) * after.book.bid
            else:
                if quantity > account.btc:
                    raise PaperGuardConflict("insufficient_btc")
                price = new * (1 - policy.slippage_bps / 10000)
                proceeds = quantity * price * (1 - policy.fee_bps / 10000)
                post_equity = account.usdt + proceeds + (account.btc - quantity) * after.book.bid
            if policy.initial_usdt - post_equity >= policy.max_run_loss_usdt:
                raise PaperGuardConflict("run_loss_limit")

    async def step(self):
        if self._step_lock.locked():
            return None
        async with self._step_lock:
            session = await self.sessions.active()
            if session is None or not session.analysis_target.legacy_spot:
                return None
            account = await self.store.latest()
            if account is None or account.status != "running":
                return None
            self._scope(account)
            try:
                market = self._quote(await self.market.sample())
            except PaperGuardConflict:
                return None
            try:
                at = self.clock.utcnow()
                cycle = await self.store.claim(
                    account.account_ref,
                    uuid4().hex,
                    account.revision,
                    at,
                    at + timedelta(seconds=15),
                )
            except PaperGuardConflict:
                try:
                    await self.pause(account.account_ref, account.revision)
                except RevisionConflict:
                    pass
                return None
            except RevisionConflict:
                return None
            decision = confidence = fill = usage = reason = None
            cancelled = False
            try:
                features = (
                    await self.market.features() if hasattr(self.market, "features") else None
                )
                if features is not None and (
                    features.symbol != "BTCUSDT"
                    or features.as_of != market.as_of
                    or not timedelta(0)
                    <= self.clock.utcnow() - features.as_of
                    <= timedelta(seconds=60)
                ):
                    features = None
                request = self._request(account, cycle, market, features)
                async with asyncio.timeout(15):
                    response = DecisionModelResponse.model_validate_json(
                        (await self.model.decide(request)).model_dump_json()
                    )
                response.bind_to(request)
                usage = response.usage
                answer = response.answers[0]
                decision, confidence = answer.choice, str(answer.confidence)
                latest = self._quote(await self.market.sample())
                if self.clock.utcnow() > cycle.deadline:
                    raise PaperGuardConflict("decision_expired")
                if answer.confidence < account.settings.min_confidence:
                    reason = "low_confidence"
                elif decision in ("BUY", "SELL"):
                    self._risk(account, decision, market, latest)
                    # The simulation timestamp is this decision's current observation window.
                    latest = type(latest).model_validate(
                        latest.model_dump() | {"as_of": self.clock.utcnow()}
                    )
                    fill = await self.execution.simulate(
                        account,
                        PaperIntent(
                            intent_id=cycle.request_id,
                            mode="paper",
                            account_ref=account.account_ref,
                            symbol="BTCUSDT",
                            side=decision.lower(),
                            quantity=account.settings.order_quantity,
                            created_at=cycle.created_at,
                        ),
                        latest,
                    )
            except asyncio.CancelledError:
                cancelled, reason, fill = True, "cancelled", None
            except ModelCallFailed as error:
                usage, reason, fill = error.usage, error.reason, None
            except PaperGuardConflict as error:
                reason, fill = str(error), None
            except Exception:
                reason, fill = "decision_unavailable", None
            completion = asyncio.create_task(
                self.store.complete(
                    account.account_ref,
                    cycle.request_id,
                    decision,
                    confidence,
                    fill,
                    usage,
                    self.clock.utcnow(),
                    reason=reason,
                )
            )
            while True:
                try:
                    result = await asyncio.shield(completion)
                    break
                except asyncio.CancelledError:
                    cancelled = True
            if cancelled:
                raise asyncio.CancelledError
            return result

    async def public_view(self):
        account = await self.store.latest()
        session = await self.sessions.active()
        result = {
            "enabled": True,
            "environment": "paper",
            "paper_market": "spot",
            "real_orders_enabled": False,
            "decision_source": self.decision_source,
            "market_source": self.market_source,
            "paid_models_enabled": self.decision_source == "real_jev"
            and bool(getattr(self.model, "enabled", False)),
            "real_model_configured": self.decision_source == "real_jev",
            "account": None,
            "cycles": [],
        }
        result["equity_usdt"] = None
        result["analysis_target"] = (
            session.analysis_target.model_dump(mode="json") if session else None
        )
        result["market_compatible"] = session is None or session.analysis_target.legacy_spot
        result["session"] = (
            {
                "session_id": session.session_id,
                "style_revision": session.style_revision,
                "style_strength": session.style.strength,
            }
            if session
            else None
        )
        result["source_compatible"] = account is None or (
            account.decision_source == self.decision_source
            and account.market_source == self.market_source
        )
        result["configured_sources"] = {
            "decision_source": self.decision_source,
            "market_source": self.market_source,
        }
        if hasattr(self.model, "trial"):
            result["trial"] = self.model.trial.model_dump(mode="json")
        if not result["market_compatible"]:
            return result
        if account is not None:
            result["decision_source"], result["market_source"] = (
                account.decision_source,
                account.market_source,
            )
            result["account"] = account.model_dump(mode="json")
            result["cycles"] = [
                cycle.model_dump(mode="json")
                for cycle in await self.store.recent(account.account_ref)
            ]
            try:
                if not result["source_compatible"]:
                    raise PaperGuardConflict("source_conflict")
                quote = self._quote(await self.market.sample())
                with localcontext() as ctx:
                    ctx.prec = 34
                    result["equity_usdt"] = str(account.usdt + account.btc * quote.book.bid)
            except PaperGuardConflict:
                pass
        return result
