"""Paper balances, claims and audit commit under the core database write lock."""

from contextlib import closing
from datetime import datetime
from decimal import Decimal, localcontext
from uuid import uuid4

from agent_platform.domain.agent_controls import AgentControlState
from agent_platform.domain.common import required_identifier, utc_datetime
from agent_platform.domain.costs import ModelUsage
from agent_platform.domain.events import JournalEvent, StateRecord, StateType
from agent_platform.domain.paper import PaperFill
from agent_platform.domain.paper_trading import PaperAccountState, PaperCycleState, PaperSettings
from agent_platform.domain.paper_trials import (
    PaperTrialPolicy,
    PaperTrialState,
    effective_continuous_policies,
)
from agent_platform.domain.sessions import AgentSession
from agent_platform.ports.paper import PaperGuardConflict
from agent_platform.ports.sessions import RevisionConflict

from .state import SqliteStateStore


class SqlitePaperStore(SqliteStateStore):
    async def ensure_trial(self, policy, at):
        policy = PaperTrialPolicy.model_validate_json(policy.model_dump_json())
        policy.validate_active(utc_datetime(at))

        def write():
            with closing(self._connect()) as connection, connection:
                connection.execute("BEGIN IMMEDIATE")
                previous = self._load(connection, policy.budget_key)
                if previous is not None:
                    if (
                        previous.state_type != StateType.PAPER_TRIAL
                        or previous.state.policy != policy
                    ):
                        raise PaperGuardConflict("first trial policy cannot be replaced or raised")
                    return previous.state
                if policy.budget_change_confirmed:
                    policies = tuple(
                        StateRecord.model_validate_json(body).state.policy
                        for (body,) in connection.execute(
                            "SELECT body FROM domain_states WHERE state_type=?",
                            (StateType.PAPER_TRIAL.value,),
                        )
                    )
                    active = effective_continuous_policies(policies)
                    if policy.supersedes_budget_key not in {p.budget_key for p in active}:
                        raise PaperGuardConflict("budget parent is missing or already superseded")
                    try:
                        effective_continuous_policies((*policies, policy))
                    except ValueError:
                        raise PaperGuardConflict("budget amendment lineage is invalid") from None
                elif policy.grant_id is not None:
                    first = self._load(connection, policy.initial_budget_key)
                    if (
                        first is None
                        or first.state_type != StateType.PAPER_TRIAL
                        or policy.trial_total_usd > first.state.policy.trial_total_usd
                        or policy.single_call_usd > first.state.policy.single_call_usd
                        or (
                            first.state.policy.expires_at is not None
                            and policy.issued_at < first.state.policy.expires_at
                        )
                    ):
                        raise PaperGuardConflict(
                            "renewal requires an expired first policy and cannot raise its caps"
                        )
                    if policy.expires_at is None:
                        for (body,) in connection.execute(
                            "SELECT body FROM domain_states WHERE key LIKE ?",
                            (policy.initial_budget_key + ":%",),
                        ):
                            prior_grant = StateRecord.model_validate_json(body)
                            if (
                                prior_grant.state_type != StateType.PAPER_TRIAL
                                or policy.trial_total_usd > prior_grant.state.policy.trial_total_usd
                                or policy.single_call_usd > prior_grant.state.policy.single_call_usd
                            ):
                                raise PaperGuardConflict(
                                    "continuous policy cannot raise an existing cap"
                                )
                trial = PaperTrialState(
                    aggregate_id=policy.budget_key, policy=policy, created_at=at, updated_at=at
                )
                return self._commit(connection, trial, StateType.PAPER_TRIAL)

        return await self._io(write)

    @staticmethod
    def _expected(account, revision):
        if type(revision) is not int or revision < 1 or account.revision != revision:
            raise RevisionConflict("paper state changed; reload before continuing")

    def _account(self, connection, account_ref):
        record = self._load(connection, required_identifier(account_ref))
        if record is None or record.state_type != StateType.PAPER_ACCOUNT:
            raise LookupError("paper account is unavailable")
        return record.state

    def _guards(self, connection, session_id):
        session_record = self._load(connection, session_id)
        control_record = self._load(connection, "agent-controls")
        if session_record is None or not isinstance(session_record.state, AgentSession):
            raise PaperGuardConflict("paper requires an active confirmed session")
        session = session_record.state
        if (
            session.status == "closed"
            or control_record is None
            or not isinstance(control_record.state, AgentControlState)
        ):
            raise PaperGuardConflict("paper requires active session and persisted trader settings")
        controls = control_record.state
        if (
            not controls.trader.enabled
            or controls.operation.mode != "auto"
            or controls.operation.execution_environment != "paper"
        ):
            raise PaperGuardConflict("paper trader must be enabled in auto/paper mode")
        return session, controls

    def _commit(self, connection, state, tag):
        record = StateRecord.model_validate_json(
            StateRecord(
                key=state.account_ref if tag == StateType.PAPER_ACCOUNT else state.aggregate_id,
                revision=state.revision,
                state_type=tag,
                state=state,
                updated_at=state.updated_at,
            ).model_dump_json()
        )
        prior = self._load(connection, record.key)
        if (prior.revision if prior else 0) != record.revision - 1:
            raise RevisionConflict("paper projection changed")
        if prior is not None:
            self._validate_update(prior, record)
        audit = JournalEvent(
            event_id="paper-event:" + str(uuid4()),
            aggregate_id=record.key,
            kind="state_changed",
            payload=record,
            occurred_at=record.updated_at,
        )
        self._append(connection, audit)
        connection.execute(
            "INSERT INTO domain_states(key,revision,state_type,body,event_id) VALUES(?,?,?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET revision=excluded.revision,"
            "state_type=excluded.state_type,body=excluded.body,event_id=excluded.event_id",
            (
                record.key,
                record.revision,
                record.state_type.value,
                record.model_dump_json(),
                audit.event_id,
            ),
        )
        connection.execute("INSERT INTO state_commits(event_id) VALUES(?)", (audit.event_id,))
        return record.state

    @staticmethod
    def _change(state, at, **updates):
        return type(state).model_validate(
            state.model_dump()
            | updates
            | {"revision": state.revision + 1, "updated_at": utc_datetime(at)}
        )

    async def create(
        self,
        session_id,
        settings,
        at,
        *,
        decision_source="offline_mock",
        market_source="offline_demo",
        expected_style_revision=None,
    ):
        session_id = required_identifier(session_id)
        policy = PaperSettings.model_validate_json(settings.model_dump_json())
        timestamp = utc_datetime(at)
        account = PaperAccountState(
            account_ref="paper:" + session_id,
            session_id=session_id,
            settings=policy,
            usdt=policy.initial_usdt,
            btc="0",
            created_at=timestamp,
            updated_at=timestamp,
            decision_source=decision_source,
            market_source=market_source,
        )

        def write():
            with closing(self._connect()) as connection, connection:
                connection.execute("BEGIN IMMEDIATE")
                session, _ = self._guards(connection, session_id)
                if (
                    expected_style_revision is not None
                    and session.style_revision != expected_style_revision
                ):
                    raise PaperGuardConflict("confirmed session style changed")
                if self._load(connection, account.account_ref) is not None:
                    raise RevisionConflict(
                        "paper wallet already exists; virtual funds cannot be recharged"
                    )
                return self._commit(connection, account, StateType.PAPER_ACCOUNT)

        return await self._io(write)

    async def get(self, account_ref):
        def read():
            with closing(self._connect()) as connection:
                return self._account(connection, account_ref)

        return await self._io(read)

    async def latest(self):
        def read():
            with closing(self._connect()) as connection:
                row = connection.execute(
                    "SELECT d.body FROM domain_states d JOIN sessions s "
                    "ON d.key='paper:'||s.session_id WHERE d.state_type=? AND s.status<>'closed'",
                    (StateType.PAPER_ACCOUNT.value,),
                ).fetchone()
                return StateRecord.model_validate_json(row[0]).state if row else None

        return await self._io(read)

    async def start(self, account_ref, expected_revision, at, *, expected_style_revision=None):
        def write():
            with closing(self._connect()) as connection, connection:
                connection.execute("BEGIN IMMEDIATE")
                account = self._account(connection, account_ref)
                self._expected(account, expected_revision)
                session, _ = self._guards(connection, account.session_id)
                if (
                    expected_style_revision is not None
                    and session.style_revision != expected_style_revision
                ):
                    raise PaperGuardConflict("confirmed session style changed")
                if account.status == "running":
                    return account
                return self._commit(
                    connection,
                    self._change(
                        account,
                        at,
                        status="running",
                        activation_revision=account.activation_revision + 1,
                    ),
                    StateType.PAPER_ACCOUNT,
                )

        return await self._io(write)

    def _pause(self, connection, account, at):
        if account.status == "paused":
            return account
        if account.pending_request_id:
            record = self._load(
                connection, account.account_ref + ":cycle:" + account.pending_request_id
            )
            if record is None or record.state.status != "pending":
                raise PaperGuardConflict("paper pending projection is inconsistent")
            self._commit(
                connection,
                self._change(record.state, at, status="discarded", reason="paused"),
                StateType.PAPER_CYCLE,
            )
        return self._commit(
            connection,
            self._change(
                account,
                at,
                status="paused",
                pending_request_id=None,
                activation_revision=account.activation_revision + 1,
            ),
            StateType.PAPER_ACCOUNT,
        )

    async def pause(self, account_ref, expected_revision, at, *, expected_activation_revision=None):
        def write():
            with closing(self._connect()) as connection, connection:
                connection.execute("BEGIN IMMEDIATE")
                account = self._account(connection, account_ref)
                if expected_activation_revision is None:
                    self._expected(account, expected_revision)
                elif (
                    type(expected_activation_revision) is not int
                    or account.activation_revision != expected_activation_revision
                    or type(expected_revision) is not int
                    or not 1 <= expected_revision <= account.revision
                ):
                    raise RevisionConflict("paper activation changed")
                return self._pause(connection, account, at)

        return await self._io(write)

    async def claim(self, account_ref, request_id, expected_revision, at, deadline):
        def write():
            with closing(self._connect()) as connection, connection:
                connection.execute("BEGIN IMMEDIATE")
                account = self._account(connection, account_ref)
                self._expected(account, expected_revision)
                session, controls = self._guards(connection, account.session_id)
                if account.status != "running" or account.pending_request_id is not None:
                    raise RevisionConflict("paper account cannot claim another cycle")
                cycle = PaperCycleState(
                    aggregate_id=account_ref + ":cycle:" + request_id,
                    account_ref=account_ref,
                    session_id=account.session_id,
                    request_id=request_id,
                    activation_revision=account.activation_revision,
                    trader_revision=controls.trader_revision,
                    style_revision=session.style_revision,
                    style=session.style,
                    created_at=at,
                    updated_at=at,
                    deadline=deadline,
                )
                if self._load(connection, cycle.aggregate_id) is not None:
                    raise RevisionConflict("paper request was already claimed")
                self._commit(connection, cycle, StateType.PAPER_CYCLE)
                self._commit(
                    connection,
                    self._change(account, at, pending_request_id=request_id),
                    StateType.PAPER_ACCOUNT,
                )
                return cycle

        return await self._io(write)

    async def complete(
        self, account_ref, request_id, decision, confidence, fill, usage, at, *, reason=None
    ):
        timestamp = utc_datetime(at)
        checked_fill = PaperFill.model_validate_json(fill.model_dump_json()) if fill else None
        checked_usage = ModelUsage.model_validate_json(usage.model_dump_json()) if usage else None

        def write():
            with closing(self._connect()) as connection, connection:
                connection.execute("BEGIN IMMEDIATE")
                account = self._account(connection, account_ref)
                record = self._load(connection, account_ref + ":cycle:" + request_id)
                if record is None or record.state_type != StateType.PAPER_CYCLE:
                    raise LookupError("paper request was not claimed")
                cycle = record.state
                if cycle.response_recorded:
                    return cycle
                valid = (
                    cycle.status == "pending"
                    and account.status == "running"
                    and account.pending_request_id == request_id
                    and account.activation_revision == cycle.activation_revision
                    and timestamp <= cycle.deadline
                )
                try:
                    session, controls = self._guards(connection, account.session_id)
                    valid = (
                        valid
                        and session.style_revision == cycle.style_revision
                        and controls.trader_revision == cycle.trader_revision
                    )
                except PaperGuardConflict:
                    valid = False
                status = "discarded"
                usdt, btc = account.usdt, account.btc
                actual_fill = None
                if valid:
                    status = "unavailable" if decision is None else "rejected"
                    if reason is None and (
                        confidence is not None
                        and Decimal(confidence) >= account.settings.min_confidence
                    ):
                        if decision == "WAIT" and checked_fill is None:
                            status = "wait"
                        elif checked_fill is not None:
                            if (
                                checked_fill.intent_id != request_id
                                or checked_fill.account_ref != account_ref
                                or checked_fill.symbol != "BTCUSDT"
                                or checked_fill.side != {"BUY": "buy", "SELL": "sell"}.get(decision)
                                or checked_fill.quantity != account.settings.order_quantity
                                or checked_fill.fee_asset != "USDT"
                                or checked_fill.model_version != "paper-top-of-book-v1"
                                or not cycle.created_at
                                <= checked_fill.filled_at
                                <= timestamp
                                <= cycle.deadline
                            ):
                                raise ValueError("paper fill does not match claimed policy")
                            with localcontext() as ctx:
                                ctx.prec = 34
                                notional = checked_fill.price * checked_fill.quantity
                                if (
                                    checked_fill.fee
                                    != notional * account.settings.fee_bps / Decimal(10000)
                                    or checked_fill.slippage_bps != account.settings.slippage_bps
                                ):
                                    raise ValueError(
                                        "paper fill costs differ from confirmed policy"
                                    )
                                if checked_fill.side == "buy":
                                    usdt, btc = (
                                        usdt - notional - checked_fill.fee,
                                        btc + checked_fill.quantity,
                                    )
                                else:
                                    usdt, btc = (
                                        usdt + notional - checked_fill.fee,
                                        btc - checked_fill.quantity,
                                    )
                            if usdt >= 0 and 0 <= btc <= account.settings.max_position_quantity:
                                status, actual_fill = "filled", checked_fill
                            else:
                                usdt, btc = account.usdt, account.btc
                completed = self._change(
                    cycle,
                    max(timestamp, cycle.updated_at),
                    status=status,
                    decision=decision,
                    confidence=confidence,
                    fill=actual_fill,
                    usage=checked_usage,
                    response_recorded=True,
                    reason=reason if valid else "stale_activation",
                )
                self._commit(connection, completed, StateType.PAPER_CYCLE)
                if account.pending_request_id == request_id:
                    self._commit(
                        connection,
                        self._change(
                            account,
                            timestamp,
                            usdt=usdt,
                            btc=btc,
                            pending_request_id=None,
                            status=account.status if valid else "paused",
                        ),
                        StateType.PAPER_ACCOUNT,
                    )
                return completed

        return await self._io(write)

    async def recent(self, account_ref, limit=50):
        account_ref = required_identifier(account_ref)
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError("paper history limit must be an integer in 1..50")
        prefix = account_ref + ":cycle:"

        def read():
            with closing(self._connect()) as connection:
                rows = connection.execute(
                    "SELECT body FROM domain_states WHERE state_type=? AND substr(key,1,?)=? "
                    "ORDER BY json_extract(body,'$.state.created_at') DESC,key DESC LIMIT ?",
                    (StateType.PAPER_CYCLE.value, len(prefix), prefix, limit),
                ).fetchall()
                return tuple(StateRecord.model_validate_json(row[0]).state for row in rows)

        return await self._io(read)

    async def recover(self, at: datetime):
        def write():
            with closing(self._connect()) as connection, connection:
                connection.execute("BEGIN IMMEDIATE")
                rows = connection.execute(
                    "SELECT body FROM domain_states WHERE state_type=?",
                    (StateType.PAPER_ACCOUNT.value,),
                ).fetchall()
                for row in rows:
                    account = StateRecord.model_validate_json(row[0]).state
                    self._pause(connection, account, max(utc_datetime(at), account.updated_at))

        await self._io(write)
