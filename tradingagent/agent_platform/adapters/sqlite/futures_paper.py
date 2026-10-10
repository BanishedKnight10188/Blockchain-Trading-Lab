"""Separate futures wallets and immutable commands, calculated inside a write transaction."""

from contextlib import closing
from hashlib import sha256
from json import dumps
from uuid import uuid4

from agent_platform.domain.agent_controls import AgentControlState
from agent_platform.domain.common import required_identifier, utc_datetime
from agent_platform.domain.futures_paper import (
    FuturesPaperFunding,
    FuturesPaperOrder,
    FuturesPaperQuote,
    FuturesPaperRecord,
    FuturesPaperState,
    FuturesPaperTransition,
)
from agent_platform.domain.futures_paper_engine import (
    create_account,
    execute,
    settle_funding,
    value,
)
from agent_platform.domain.sessions import AgentSession
from agent_platform.domain.trading_execution import TradeCommand
from agent_platform.ports.futures_paper import FuturesPaperGuardConflict
from agent_platform.ports.persistence import EventIdentityConflict
from agent_platform.ports.sessions import RevisionConflict

from .state import SqliteStateStore
from .trade_archive import append_archive, initialize_archive


class SqliteFuturesPaperStore(SqliteStateStore):
    async def initialize(self):
        await super().initialize()

        def initialize_tables():
            with closing(self._connect()) as db, db:
                db.execute("BEGIN IMMEDIATE")
                db.execute(
                    "CREATE TABLE IF NOT EXISTS futures_paper_wallets ("
                    "account_ref TEXT PRIMARY KEY, session_id TEXT UNIQUE NOT NULL "
                    "REFERENCES sessions(session_id), "
                    "revision INTEGER NOT NULL, body TEXT NOT NULL)"
                )
                db.execute(
                    "CREATE TABLE IF NOT EXISTS futures_paper_operations ("
                    "sequence INTEGER PRIMARY KEY AUTOINCREMENT, command_id TEXT UNIQUE NOT NULL, "
                    "account_ref TEXT NOT NULL REFERENCES futures_paper_wallets(account_ref), "
                    "fingerprint TEXT NOT NULL, body TEXT NOT NULL)"
                )
                db.execute(
                    "CREATE INDEX IF NOT EXISTS futures_paper_operations_wallet "
                    "ON futures_paper_operations(account_ref, sequence)"
                )
                db.execute(
                    "CREATE TABLE IF NOT EXISTS futures_paper_quote_watermarks ("
                    "account_ref TEXT PRIMARY KEY REFERENCES futures_paper_wallets(account_ref), "
                    "body TEXT NOT NULL)"
                )
                initialize_archive(db)

        await self._io(initialize_tables)

    @staticmethod
    def _account(db, account_ref):
        row = db.execute(
            "SELECT body FROM futures_paper_wallets WHERE account_ref=?",
            (required_identifier(account_ref),),
        ).fetchone()
        if row is None:
            raise LookupError("futures simulation wallet is unavailable")
        state = FuturesPaperState.model_validate_json(row["body"])
        watermark = db.execute(
            "SELECT body FROM futures_paper_quote_watermarks WHERE account_ref=?", (account_ref,)
        ).fetchone()
        if watermark is not None:
            return FuturesPaperState.model_validate(
                state.model_dump()
                | {"last_quote": FuturesPaperQuote.model_validate_json(watermark["body"])}
            )
        return state

    @staticmethod
    def _set_quote(db, account_ref, quote):
        row = db.execute(
            "SELECT body FROM futures_paper_quote_watermarks WHERE account_ref=?", (account_ref,)
        ).fetchone()
        if row is not None:
            previous = FuturesPaperQuote.model_validate_json(row["body"])
            if quote.book_at <= previous.book_at:
                quote = FuturesPaperQuote.model_validate(
                    quote.model_dump()
                    | {"book_at": previous.book_at, "bid": previous.bid, "ask": previous.ask}
                )
        db.execute(
            "INSERT INTO futures_paper_quote_watermarks(account_ref,body) VALUES(?,?) "
            "ON CONFLICT(account_ref) DO UPDATE SET body=excluded.body",
            (account_ref, quote.model_dump_json()),
        )
        return quote

    @staticmethod
    def _expected(account, revision):
        if type(revision) is not int or revision < 1 or account.revision != revision:
            raise RevisionConflict("futures wallet changed; reload before continuing")

    def _session(self, db, session_id, at, *, style=None, running=False):
        record = self._load(db, session_id)
        if record is None or not isinstance(record.state, AgentSession):
            raise FuturesPaperGuardConflict("futures simulation requires an owned session")
        session = record.state
        if session.analysis_target.market != "usdt_perpetual" or at < session.created_at:
            raise FuturesPaperGuardConflict("simulation requires a USDT perpetual session")
        if style is not None and (
            type(style) is not int or not session.matches_style_revision(style)
        ):
            raise FuturesPaperGuardConflict("session style changed before execution")
        if running and (session.status != "running" or at < session.updated_at):
            raise FuturesPaperGuardConflict(
                "futures simulation requires the current running session"
            )
        return session

    def _permission(self, db, account, at, style, trader):
        session = self._session(db, account.session_id, at, style=style, running=True)
        controls = self._load(db, "agent-controls")
        if controls is None or not isinstance(controls.state, AgentControlState):
            raise FuturesPaperGuardConflict("independent trader settings are unavailable")
        c = controls.state
        if (
            type(trader) is not int
            or trader != c.trader_revision
            or not c.trader.enabled
            or c.operation.mode != "auto"
            or c.operation.execution_environment != "paper"
            or at < c.updated_at
            or session.analysis_target.symbol != account.symbol
        ):
            raise FuturesPaperGuardConflict(
                "current independent trader does not permit this execution"
            )

    @staticmethod
    def _fingerprint(kind, payload):
        return sha256(
            dumps(
                {"kind": kind, **payload}, sort_keys=True, ensure_ascii=False, separators=(",", ":")
            ).encode()
        ).hexdigest()

    @staticmethod
    def _prior(db, command_id, fingerprint):
        checked_id = required_identifier(command_id)
        if len(checked_id) > 128:
            raise ValueError("simulation command ID is too long")
        row = db.execute(
            "SELECT fingerprint, body FROM futures_paper_operations WHERE command_id=?",
            (checked_id,),
        ).fetchone()
        if row is None:
            return None
        if row["fingerprint"] != fingerprint:
            raise EventIdentityConflict(
                "simulation command ID already refers to a different operation"
            )
        return FuturesPaperRecord.model_validate_json(row["body"])

    @staticmethod
    def _commit(
        db,
        state,
        kind,
        *,
        command_id=None,
        fingerprint=None,
        operation=None,
        quote=None,
        order=None,
        funding=None,
        save_wallet=True,
        execution_command=None,
        before_state=None,
    ):
        record = FuturesPaperRecord(
            command_id=command_id or "futures:" + uuid4().hex,
            fingerprint=fingerprint or sha256(state.model_dump_json().encode()).hexdigest(),
            account_ref=state.account_ref,
            kind=kind,
            state=state,
            operation=operation,
            quote=quote,
            order=order,
            funding=funding,
            execution_command=execution_command,
            before_state=before_state,
        )
        if save_wallet:
            db.execute(
                "INSERT INTO futures_paper_wallets(account_ref,session_id,revision,body) "
                "VALUES(?,?,?,?) ON CONFLICT(account_ref) DO UPDATE SET "
                "revision=excluded.revision,body=excluded.body",
                (state.account_ref, state.session_id, state.revision, state.model_dump_json()),
            )
        if state.last_quote is not None:
            SqliteFuturesPaperStore._set_quote(db, state.account_ref, state.last_quote)
        inserted = db.execute(
            "INSERT INTO futures_paper_operations(command_id,account_ref,fingerprint,body) "
            "VALUES(?,?,?,?)",
            (record.command_id, state.account_ref, record.fingerprint, record.model_dump_json()),
        )
        append_archive(db, record, inserted.lastrowid)
        return record

    async def create(self, session_id, settings, rules, at, *, expected_style_revision):
        at = utc_datetime(at)

        def write():
            with closing(self._connect()) as db, db:
                db.execute("BEGIN IMMEDIATE")
                session = self._session(db, session_id, at, style=expected_style_revision)
                if session.status == "closed":
                    raise FuturesPaperGuardConflict("closed session cannot create a new wallet")
                account = create_account(
                    session_id, session.analysis_target.symbol, settings, rules, at
                )
                previous = db.execute(
                    "SELECT body FROM futures_paper_wallets WHERE session_id=?", (session_id,)
                ).fetchone()
                if previous:
                    original = FuturesPaperState.model_validate_json(previous["body"])
                    initial_row = db.execute(
                        "SELECT body FROM futures_paper_operations WHERE account_ref=? "
                        "AND json_extract(body,'$.kind') IN ('configure','parameterize') "
                        "ORDER BY sequence DESC LIMIT 1",
                        (original.account_ref,),
                    ).fetchone()
                    if initial_row is None:
                        raise FuturesPaperGuardConflict("original configuration is unavailable")
                    initial = FuturesPaperRecord.model_validate_json(initial_row["body"])
                    if (
                        initial.kind not in ("configure", "parameterize")
                        or initial.state.settings != account.settings
                        or original.rules != account.rules
                        or original.symbol != account.symbol
                    ):
                        raise FuturesPaperGuardConflict(
                            "configured funds and hard limits cannot be reset"
                        )
                    return original
                self._commit(db, account, "configure")
                return account

        return await self._io(write)

    async def get(self, account_ref):
        def read():
            with closing(self._connect()) as db:
                return self._account(db, account_ref)

        return await self._io(read)

    async def maintenance_accounts(self):
        def read():
            with closing(self._connect()) as db:
                rows = db.execute(
                    "SELECT w.account_ref, s.status FROM futures_paper_wallets w "
                    "JOIN sessions s ON s.session_id=w.session_id"
                ).fetchall()
                states = [self._account(db, r["account_ref"]) for r in rows]
                return tuple(
                    s
                    for s, r in zip(states, rows, strict=True)
                    if s.quantity > 0 or r["status"] != "closed"
                )

        return await self._io(read)

    async def _toggle(self, account_ref, expected_revision, at, status, *, style=None, trader=None):
        at = utc_datetime(at)

        def write():
            with closing(self._connect()) as db, db:
                db.execute("BEGIN IMMEDIATE")
                account = self._account(db, account_ref)
                self._expected(account, expected_revision)
                if at < account.updated_at:
                    raise ValueError("wallet chronology cannot move backwards")
                if status == "running":
                    self._permission(db, account, at, style, trader)
                    if account.status == "liquidated":
                        raise FuturesPaperGuardConflict("liquidated wallet cannot be restarted")
                if account.status == status or account.status == "liquidated":
                    return account
                updated = FuturesPaperState.model_validate(
                    account.model_dump()
                    | {"status": status, "revision": account.revision + 1, "updated_at": at}
                )
                self._commit(db, updated, "start" if status == "running" else "pause")
                return updated

        return await self._io(write)

    async def start(
        self,
        account_ref,
        expected_revision,
        at,
        *,
        expected_style_revision,
        expected_trader_revision,
    ):
        return await self._toggle(
            account_ref,
            expected_revision,
            at,
            "running",
            style=expected_style_revision,
            trader=expected_trader_revision,
        )

    async def pause(self, account_ref, expected_revision, at):
        return await self._toggle(account_ref, expected_revision, at, "paused")

    async def _apply(
        self,
        account_ref,
        expected_revision,
        at,
        command_id,
        fingerprint,
        calculate,
        *,
        style=None,
        trader=None,
        trading=False,
        quote=None,
        order=None,
        funding=None,
        execution_command=None,
    ):
        def write():
            with closing(self._connect()) as db, db:
                db.execute("BEGIN IMMEDIATE")
                prior = self._prior(db, command_id, fingerprint)
                if prior is not None:
                    if prior.kind == "mark" and prior.operation is None:
                        return None
                    return FuturesPaperTransition(state=prior.state, operation=prior.operation)
                account = self._account(db, account_ref)
                self._expected(account, expected_revision)
                if trading:
                    self._permission(db, account, at, style, trader)
                else:
                    session = self._session(db, account.session_id, at)
                    if session.analysis_target.symbol != account.symbol:
                        raise FuturesPaperGuardConflict(
                            "wallet and session contract identities disagree"
                        )
                transition = calculate(account)
                if transition is None:
                    merged = self._set_quote(db, account_ref, quote)
                    observed = FuturesPaperState.model_validate(
                        account.model_dump() | {"last_quote": merged}
                    )
                    self._commit(
                        db,
                        observed,
                        "mark",
                        command_id=command_id,
                        fingerprint=fingerprint,
                        quote=quote,
                        save_wallet=False,
                    )
                    return None
                self._commit(
                    db,
                    transition.state,
                    transition.operation.kind,
                    command_id=command_id,
                    fingerprint=fingerprint,
                    operation=transition.operation,
                    quote=quote,
                    order=order,
                    funding=funding,
                    execution_command=execution_command,
                    before_state=account,
                )
                return transition

        return await self._io(write)

    async def execute(
        self,
        account_ref,
        expected_revision,
        order,
        quote,
        at,
        *,
        command_id,
        expected_style_revision,
        expected_trader_revision,
        execution_command=None,
    ):
        at = utc_datetime(at)
        order = FuturesPaperOrder.model_validate_json(order.model_dump_json())
        quote = FuturesPaperQuote.model_validate_json(quote.model_dump_json())
        if execution_command is not None:
            execution_command = TradeCommand.model_validate(execution_command)
        payload = dict(
            account_ref=account_ref,
            revision=expected_revision,
            order=order.model_dump(mode="json", exclude_none=True),
            quote=quote.model_dump(mode="json"),
            at=at.isoformat(),
            style=expected_style_revision,
            trader=expected_trader_revision,
        )
        if execution_command is not None:
            payload["execution_command"] = execution_command.model_dump(
                mode="json", exclude_none=True
            )
        fingerprint = self._fingerprint("execute", payload)
        return await self._apply(
            account_ref,
            expected_revision,
            at,
            command_id,
            fingerprint,
            lambda state: execute(state, order, quote, at),
            style=expected_style_revision,
            trader=expected_trader_revision,
            trading=True,
            quote=quote,
            order=order,
            execution_command=execution_command,
        )

    async def lookup_trade(self, command):
        checked = TradeCommand.model_validate(command)

        def read():
            with closing(self._connect()) as db:
                row = db.execute(
                    "SELECT body FROM futures_paper_operations WHERE command_id=?",
                    (checked.command_id,),
                ).fetchone()
                if row is None:
                    return None
                record = FuturesPaperRecord.model_validate_json(row["body"])
                if (
                    checked.scope.environment != "paper"
                    or record.kind not in ("trade", "liquidation")
                    or record.operation is None
                    or record.quote is None
                    or record.order is None
                    or record.state.session_id != checked.scope.session_id
                    or record.state.symbol != checked.scope.symbol
                    or record.execution_command != checked
                ):
                    raise EventIdentityConflict("execution command refers to different facts")
                fingerprint = self._fingerprint(
                    "execute",
                    dict(
                        account_ref=checked.scope.account_ref,
                        revision=checked.expected_account_revision,
                        order=FuturesPaperOrder(
                            action=checked.action,
                            quantity=checked.quantity,
                            target_leverage=checked.target_leverage,
                        ).model_dump(mode="json", exclude_none=True),
                        quote=record.quote.model_dump(mode="json"),
                        at=record.operation.occurred_at.isoformat(),
                        style=checked.style_revision,
                        trader=checked.trader_revision,
                        execution_command=checked.model_dump(mode="json", exclude_none=True),
                    ),
                )
                if fingerprint != record.fingerprint:
                    raise EventIdentityConflict("execution command refers to different inputs")
                return record

        return await self._io(read)

    async def funding(self, account_ref, expected_revision, funding, at, *, command_id):
        at = utc_datetime(at)
        funding = FuturesPaperFunding.model_validate_json(funding.model_dump_json())
        fingerprint = self._fingerprint(
            "funding",
            dict(
                account_ref=account_ref,
                revision=expected_revision,
                funding=funding.model_dump(mode="json"),
                at=at.isoformat(),
            ),
        )
        return await self._apply(
            account_ref,
            expected_revision,
            at,
            command_id,
            fingerprint,
            lambda state: settle_funding(state, funding, at),
            funding=funding,
        )

    async def mark(self, account_ref, expected_revision, quote, at, *, command_id):
        at = utc_datetime(at)
        quote = FuturesPaperQuote.model_validate_json(quote.model_dump_json())
        fingerprint = self._fingerprint(
            "mark",
            dict(
                account_ref=account_ref,
                revision=expected_revision,
                quote=quote.model_dump(mode="json"),
                at=at.isoformat(),
            ),
        )

        def maintain(state):
            if not value(state, quote, at).requires_liquidation:
                return None
            reduction = FuturesPaperOrder(action="reduce", quantity=state.quantity)
            return execute(state, reduction, quote, at)

        return await self._apply(
            account_ref, expected_revision, at, command_id, fingerprint, maintain, quote=quote
        )

    async def recover(self, at):
        at = utc_datetime(at)

        def write():
            with closing(self._connect()) as db, db:
                db.execute("BEGIN IMMEDIATE")
                rows = db.execute("SELECT account_ref FROM futures_paper_wallets").fetchall()
                for row in rows:
                    account = self._account(db, row["account_ref"])
                    if account.status == "running":
                        if at < account.updated_at:
                            raise ValueError("recovery time cannot precede wallet history")
                        updated = FuturesPaperState.model_validate(
                            account.model_dump()
                            | {
                                "status": "paused",
                                "revision": account.revision + 1,
                                "updated_at": at,
                            }
                        )
                        self._commit(db, updated, "recover")

        await self._io(write)

    async def recent(self, account_ref, limit=50):
        account_ref = required_identifier(account_ref)
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError("simulation history limit must be 1 through 200")

        def read():
            with closing(self._connect()) as db:
                self._account(db, account_ref)
                rows = db.execute(
                    "SELECT body FROM futures_paper_operations WHERE account_ref=? "
                    "ORDER BY sequence DESC LIMIT ?",
                    (account_ref, limit),
                ).fetchall()
                return tuple(FuturesPaperRecord.model_validate_json(row["body"]) for row in rows)

        return await self._io(read)
