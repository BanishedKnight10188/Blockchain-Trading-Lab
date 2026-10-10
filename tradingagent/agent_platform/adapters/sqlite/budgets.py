"""Transactional daily/cumulative reservations and rolling call limits, without network IO."""

import sqlite3
from contextlib import closing
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from agent_platform.domain.common import exact_add, required_identifier, utc_datetime
from agent_platform.domain.costs import (
    BillingStatus,
    BudgetBalance,
    BudgetRequest,
    BudgetReservation,
    ModelUsage,
    ReservationStatus,
)
from agent_platform.domain.events import JournalEvent, StateRecord, StateType
from agent_platform.domain.paper_trials import effective_continuous_policies
from agent_platform.ports.persistence import (
    BudgetExceeded,
    BudgetFrozen,
    DispatchAlreadyReserved,
    HourlyCallLimitExceeded,
    RequestIdentityConflict,
    ReservationNotFound,
    SettlementConflict,
)

from .events import SqliteStore


class SqliteBudgetStore(SqliteStore):
    async def budget_balance(self, at, *, daily_limit_usd, cumulative=False):
        if type(cumulative) is not bool:
            raise ValueError("cumulative budget mode must be boolean")
        at = utc_datetime(at)
        checked = BudgetRequest(
            request_id="local-budget-read",
            route_id="local-budget-read",
            purpose="advisory",
            price_version="read-only",
            estimated_cost_usd="0",
            daily_limit_usd=daily_limit_usd,
            requested_at=at,
        )

        def read():
            with closing(self._connect()) as connection:
                connection.execute("BEGIN")
                return self._balance(connection, checked, at, cumulative=cumulative)

        return await self._io(read)

    @staticmethod
    def _frozen(connection: sqlite3.Connection) -> bool:
        return bool(
            connection.execute(
                "SELECT billing_frozen FROM budget_settings WHERE singleton=1"
            ).fetchone()[0]
        )

    @staticmethod
    def _hour_count(connection: sqlite3.Connection, at: datetime) -> int:
        # Count future records too if the host clock moves backwards.
        cutoff = (at - timedelta(hours=1)).isoformat()
        return connection.execute(
            "SELECT COUNT(*) FROM budget_requests WHERE requested_at>?", (cutoff,)
        ).fetchone()[0]

    @classmethod
    def _balance(
        cls,
        connection: sqlite3.Connection,
        request: BudgetRequest,
        at: datetime,
        *,
        cumulative=False,
    ) -> BudgetBalance:
        rows = (
            connection.execute("SELECT body FROM budget_requests").fetchall()
            if cumulative
            else connection.execute(
                "SELECT body FROM budget_requests WHERE budget_day=?",
                (request.budget_day.isoformat(),),
            ).fetchall()
        )
        spent, reserved = Decimal(0), Decimal(0)
        for row in rows:
            reservation = BudgetReservation.model_validate_json(row["body"])
            reserved = exact_add(reserved, reservation.held_cost_usd)
            if reservation.actual_cost_usd is not None:
                spent = exact_add(spent, reservation.actual_cost_usd)
        exposure = exact_add(spent, reserved)
        effective_limit = request.daily_limit_usd
        if cumulative and not request.provider_managed:
            continuous_cap = cls._continuous_cap(connection)
            if continuous_cap is not None:
                effective_limit = min(effective_limit, continuous_cap)
        return BudgetBalance(
            budget_day=request.budget_day,
            daily_limit_usd=effective_limit,
            spent_usd=spent,
            reserved_usd=reserved,
            hourly_call_count=cls._hour_count(connection, at),
            billing_frozen=not request.provider_managed
            and (cls._frozen(connection) or exposure > effective_limit),
            provider_managed=request.provider_managed,
        )

    @staticmethod
    def _continuous_cap(connection):
        if connection.execute("PRAGMA user_version").fetchone()[0] < 4:
            return None
        policies = []
        for (body,) in connection.execute(
            "SELECT body FROM domain_states WHERE state_type=?", (StateType.PAPER_TRIAL.value,)
        ):
            record = StateRecord.model_validate_json(body)
            policies.append(record.state.policy)
        limits = [p.trial_total_usd for p in effective_continuous_policies(tuple(policies))]
        return min(limits) if limits else None

    def _reserve_on(self, connection, checked, *, require_new=False, enforce_hourly=True):
        previous = connection.execute(
            "SELECT body FROM budget_requests WHERE request_id=?", (checked.request_id,)
        ).fetchone()
        if previous is not None:
            if require_new:
                raise DispatchAlreadyReserved("request already has durable budget evidence")
            existing = BudgetReservation.model_validate_json(previous["body"])
            if existing.request != checked:
                raise RequestIdentityConflict("request identifier has different budget inputs")
            return existing
        if not checked.provider_managed and self._frozen(connection):
            raise BudgetFrozen("paid calls are frozen pending billing review")
        balance = (
            None
            if checked.provider_managed
            else self._balance(connection, checked, checked.requested_at)
        )
        hourly_count = (
            self._hour_count(connection, checked.requested_at)
            if balance is None
            else balance.hourly_call_count
        )
        if enforce_hourly and hourly_count >= checked.hourly_call_limit:
            raise HourlyCallLimitExceeded("rolling hourly model call allowance exhausted")
        exposure = (
            Decimal(0)
            if balance is None
            else exact_add(
                exact_add(balance.spent_usd, balance.reserved_usd), checked.estimated_cost_usd
            )
        )
        # v3 budget writers are used before the v4 owned-state migration.
        trial_row = (
            connection.execute(
                "SELECT body FROM domain_states WHERE key=?",
                ("paper-trial:" + checked.budget_day.isoformat(),),
            ).fetchone()
            if not checked.provider_managed
            and connection.execute("PRAGMA user_version").fetchone()[0] >= 4
            else None
        )
        effective_limit = checked.daily_limit_usd
        continuous_cap = None if checked.provider_managed else self._continuous_cap(connection)
        if trial_row is not None and continuous_cap is None:
            trial = StateRecord.model_validate_json(trial_row[0])
            if trial.state_type != StateType.PAPER_TRIAL:
                raise ValueError("trial budget has an invalid owned type")
            effective_limit = min(effective_limit, trial.state.policy.trial_total_usd)
            # Explicit continuations can only reduce the durable cap.
            # Keep the lowest confirmed ceiling even after expiry.
            for (body,) in connection.execute(
                "SELECT body FROM domain_states WHERE key LIKE ?",
                ("paper-trial:" + checked.budget_day.isoformat() + ":%",),
            ):
                grant = StateRecord.model_validate_json(body)
                if grant.state_type != StateType.PAPER_TRIAL:
                    raise ValueError("renewal budget has an invalid owned type")
                effective_limit = min(effective_limit, grant.state.policy.trial_total_usd)
        if exposure > effective_limit:
            raise BudgetExceeded("insufficient daily model budget")
        if continuous_cap is not None:
            total = self._balance(connection, checked, checked.requested_at, cumulative=True)
            if (
                exact_add(
                    exact_add(total.spent_usd, total.reserved_usd),
                    checked.estimated_cost_usd,
                )
                > total.daily_limit_usd
            ):
                raise BudgetExceeded("insufficient cumulative model budget")
        reservation = BudgetReservation(
            reservation_id=uuid4().hex,
            request=checked,
            updated_at=checked.requested_at,
        )
        connection.execute(
            "INSERT INTO budget_requests"
            "(reservation_id, request_id, budget_day, requested_at, body) "
            "VALUES(?,?,?,?,?)",
            (
                reservation.reservation_id,
                checked.request_id,
                checked.budget_day.isoformat(),
                checked.requested_at.isoformat(),
                reservation.model_dump_json(),
            ),
        )
        self._append(
            connection,
            JournalEvent(
                event_id="budget-reserved:" + checked.request_id,
                aggregate_id=reservation.reservation_id,
                kind="budget_reserved",
                payload=reservation,
                occurred_at=checked.requested_at,
            ),
        )
        return reservation

    async def reserve(self, request: BudgetRequest, *, require_new: bool = False):
        if type(require_new) is not bool:
            raise ValueError("dispatch reservation mode must be an explicit bool")
        checked = BudgetRequest.model_validate_json(request.model_dump_json())

        def write():
            with closing(self._connect()) as connection, connection:
                connection.execute("BEGIN IMMEDIATE")
                return self._reserve_on(connection, checked, require_new=require_new)

        return await self._io(write)

    async def reserve_agent(self, request, grant):
        from agent_platform.domain.event_agent import AgentBudgetGrant

        checked = BudgetRequest.model_validate_json(request.model_dump_json())
        grant = AgentBudgetGrant.model_validate_json(grant.model_dump_json())
        if (
            not grant.valid_from <= checked.requested_at < grant.expires_at
            or checked.price_version != grant.price_version
            or checked.estimated_cost_usd > grant.max_single_cost_usd
        ):
            raise BudgetExceeded("agent grant does not permit this request")

        def write():
            with closing(self._connect()) as db, db:
                db.execute("BEGIN IMMEDIATE")
                if db.execute(
                    "SELECT 1 FROM budget_requests WHERE request_id=?", (checked.request_id,)
                ).fetchone():
                    raise DispatchAlreadyReserved("request already has durable budget evidence")
                lane_exposure = Decimal(0)
                for row in db.execute(
                    "SELECT b.body FROM budget_requests b JOIN agent_budget_links a "
                    "ON a.request_id=b.request_id WHERE a.lane_id=?",
                    (grant.lane_id,),
                ):
                    item = BudgetReservation.model_validate_json(row[0])
                    lane_exposure = exact_add(
                        lane_exposure,
                        exact_add(item.held_cost_usd, item.actual_cost_usd or Decimal(0)),
                    )
                parent = self._balance(db, checked, checked.requested_at, cumulative=True)
                if (
                    exact_add(lane_exposure, checked.estimated_cost_usd) > grant.lane_total_usd
                    or exact_add(
                        exact_add(parent.spent_usd, parent.reserved_usd), checked.estimated_cost_usd
                    )
                    > grant.parent_total_usd
                ):
                    raise BudgetExceeded("agent lane or parent cumulative budget exhausted")
                cutoff = (checked.requested_at - timedelta(hours=1)).isoformat()
                calls = db.execute(
                    "SELECT count(*) FROM agent_budget_links a JOIN budget_requests b "
                    "ON b.request_id=a.request_id WHERE a.lane_id=? AND b.requested_at>?",
                    (grant.lane_id, cutoff),
                ).fetchone()[0]
                if calls >= grant.hourly_call_limit:
                    raise HourlyCallLimitExceeded("agent hourly budget exhausted")
                # This grant's hourly allowance belongs to its lane. Legacy calls
                # retain the existing global hourly check in reserve().
                reservation = self._reserve_on(db, checked, require_new=True, enforce_hourly=False)
                db.execute(
                    "INSERT INTO agent_budget_links VALUES(?,?,?)",
                    (checked.request_id, grant.lane_id, grant.grant_id),
                )
                return reservation

        return await self._io(write)

    async def settle(self, reservation_id: str, usage: ModelUsage) -> BudgetBalance:
        checked = ModelUsage.model_validate_json(usage.model_dump_json())

        def write():
            with closing(self._connect()) as connection, connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT body, usage_body FROM budget_requests WHERE reservation_id=?",
                    (reservation_id,),
                ).fetchone()
                if row is None:
                    raise ReservationNotFound("budget reservation not found")
                reservation = BudgetReservation.model_validate_json(row["body"])
                request = reservation.request
                continuous_cap = (
                    None if request.provider_managed else self._continuous_cap(connection)
                )
                cumulative = request.provider_managed or continuous_cap is not None
                # Settlement uses the current authorization, while the original
                # request and its quoted estimate remain immutable audit evidence.
                balance_request = (
                    request.model_copy(update={"daily_limit_usd": continuous_cap})
                    if continuous_cap is not None
                    else request
                )
                if checked.request_id != request.request_id or checked.route_id != request.route_id:
                    raise SettlementConflict("usage belongs to another budget request")
                if checked.estimated_cost_usd != request.estimated_cost_usd:
                    raise SettlementConflict("usage cannot replace the original cost estimate")
                if checked.recorded_at < reservation.updated_at:
                    raise SettlementConflict("billing observations cannot move back in time")
                previous = (
                    ModelUsage.model_validate_json(row["usage_body"]) if row["usage_body"] else None
                )
                if previous is not None and previous.billing_status == BillingStatus.CONFIRMED:
                    if checked != previous:
                        raise SettlementConflict("confirmed billing cannot be replaced")
                    return self._balance(
                        connection, balance_request, checked.recorded_at, cumulative=cumulative
                    )
                if previous == checked:
                    return self._balance(
                        connection, balance_request, checked.recorded_at, cumulative=cumulative
                    )
                confirmed = checked.billing_status == BillingStatus.CONFIRMED
                changed = BudgetReservation(
                    reservation_id=reservation.reservation_id,
                    request=request,
                    status=ReservationStatus.SETTLED if confirmed else ReservationStatus.UNKNOWN,
                    actual_cost_usd=checked.actual_cost_usd,
                    updated_at=checked.recorded_at,
                )
                connection.execute(
                    "UPDATE budget_requests SET body=?, usage_body=? WHERE reservation_id=?",
                    (changed.model_dump_json(), checked.model_dump_json(), reservation_id),
                )
                balance = self._balance(
                    connection, balance_request, checked.recorded_at, cumulative=cumulative
                )
                if (
                    not request.provider_managed
                    and confirmed
                    and (
                        checked.actual_cost_usd > request.estimated_cost_usd
                        or exact_add(balance.spent_usd, balance.reserved_usd)
                        > balance.daily_limit_usd
                    )
                ):
                    connection.execute(
                        "UPDATE budget_settings SET billing_frozen=1 WHERE singleton=1"
                    )
                    balance = balance.model_copy(update={"billing_frozen": True})
                self._append(
                    connection,
                    JournalEvent(
                        event_id="model-usage:" + uuid4().hex,
                        aggregate_id=request.request_id,
                        kind="model_usage_recorded",
                        payload=checked,
                        occurred_at=checked.recorded_at,
                    ),
                )
                return balance

        return await self._io(write)

    async def session_usage(self, session_id, *, wallet_database=None):
        session_id = required_identifier(session_id)

        def read():
            uri = self.path.as_uri() + "?mode=ro"
            with closing(sqlite3.connect(uri, uri=True, timeout=5)) as db:
                db.row_factory = sqlite3.Row
                clauses = ["json_extract(body,'$.request.session_id')=?"]
                params = [session_id]
                if wallet_database is not None:
                    path = Path(wallet_database).resolve()
                    db.execute("ATTACH DATABASE ? AS task_wallet", (path.as_uri() + "?mode=ro",))
                    clauses.append(
                        "request_id IN (SELECT request_id FROM task_wallet.futures_trading_cycles "
                        "WHERE json_extract(body,'$.scope.session_id')=?)"
                    )
                    params.append(session_id)
                    background = path.with_suffix(".background.sqlite3")
                    if background.exists():
                        db.execute(
                            "ATTACH DATABASE ? AS task_background",
                            (background.as_uri() + "?mode=ro",),
                        )
                        clauses.append(
                            "request_id IN (SELECT request_id "
                            "FROM task_background.background_records "
                            "WHERE session_id=?)"
                        )
                        params.append(session_id)
                db.execute("BEGIN")
                rows = db.execute(
                    "SELECT body FROM budget_requests WHERE " + " OR ".join(clauses), params
                ).fetchall()
                spent, unconfirmed = Decimal(0), Decimal(0)
                for row in rows:
                    reservation = BudgetReservation.model_validate_json(row["body"])
                    spent = exact_add(spent, reservation.actual_cost_usd or Decimal(0))
                    unconfirmed = exact_add(unconfirmed, reservation.held_cost_usd)
                return {
                    "spent_usd": str(spent),
                    "unconfirmed_usd": str(unconfirmed),
                    "call_count": len(rows),
                }

        return await self._io(read)
