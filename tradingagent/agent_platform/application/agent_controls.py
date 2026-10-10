"""Persist confirmed settings with CAS and audit; selection never assembles a provider."""

from uuid import uuid4

from agent_platform.domain.agent_controls import (
    AdviceSelection,
    AgentControlState,
    ControlSelection,
    TraderSelection,
)
from agent_platform.domain.events import JournalEvent, StateRecord, StateType
from agent_platform.domain.model_modules import JevModuleSettings, JevTraderSettings
from agent_platform.domain.operating_modes import OperatingSettings
from agent_platform.ports.sessions import PersistenceUnavailable, RevisionConflict


class ControlEnvironmentConflict(ValueError):
    """Stored intent is incompatible with this process's current data source."""


def _require_revision(current: AgentControlState, expected_revision: int):
    if type(expected_revision) is not int or expected_revision < 0:
        raise ValueError("settings revision must be a nonnegative integer")
    if current.revision != expected_revision:
        raise RevisionConflict("operating settings changed; reload before saving")


class AgentControlService:
    def __init__(
        self, *, store, clock, operation=None, jev=None, trader=None, production_reads=False
    ):
        self.store, self.clock = store, clock
        self.production_reads = production_reads
        self.defaults = AgentControlState(
            revision=0,
            updated_at=clock.utcnow(),
            operation=operation or OperatingSettings(),
            jev=jev or JevModuleSettings(),
            trader=trader or JevTraderSettings(),
        )

    async def current(self) -> AgentControlState:
        record = await self.store.load("agent-controls")
        if record is None:
            state = self.defaults
        else:
            if record.state_type != StateType.AGENT_CONTROLS:
                raise PersistenceUnavailable("operating settings have an invalid owned type")
            state = AgentControlState.model_validate_json(record.state.model_dump_json())
        if (
            state.operation.mode == "auto"
            and state.operation.execution_environment == "testnet"
            and self.production_reads
        ):
            raise ControlEnvironmentConflict(
                "testnet auto mode cannot use production read switches"
            )
        return state

    async def update(self, selection: ControlSelection, *, expected_revision: int):
        selection = ControlSelection.model_validate_json(selection.model_dump_json())
        if (
            selection.mode == "auto"
            and selection.execution_environment == "testnet"
            and self.production_reads
        ):
            raise ValueError("testnet auto mode cannot use production read switches")
        current = await self.current()
        _require_revision(current, expected_revision)
        operation, jev, trader = (
            OperatingSettings(
                mode=selection.mode, execution_environment=selection.execution_environment
            ),
            JevModuleSettings(enabled=selection.jev_enabled),
            JevTraderSettings(enabled=selection.trader_enabled),
        )
        if (
            current.revision > 0
            and current.operation == operation
            and current.jev == jev
            and current.trader == trader
        ):
            return current
        changed = AgentControlState(
            revision=current.revision + 1,
            updated_at=self.clock.utcnow(),
            operation=operation,
            jev=jev,
            trader=trader,
            advice_revision=current.advice_revision + (current.jev != jev),
            trader_revision=current.trader_revision
            + (current.operation != operation or current.trader != trader),
        )
        record = StateRecord(
            key=changed.aggregate_id,
            revision=changed.revision,
            state_type=StateType.AGENT_CONTROLS,
            state=changed,
            updated_at=changed.updated_at,
        )
        event = JournalEvent(
            event_id=uuid4().hex,
            aggregate_id=record.key,
            kind="state_changed",
            payload=record,
            occurred_at=record.updated_at,
        )
        return (await self.store.save(record, expected_revision, event)).state

    async def update_advice(self, selection: AdviceSelection, *, expected_revision: int):
        selection = AdviceSelection.model_validate_json(selection.model_dump_json())
        current = await self.current()
        _require_revision(current, expected_revision)
        return await self.update(
            ControlSelection(
                mode=current.operation.mode,
                execution_environment=current.operation.execution_environment,
                jev_enabled=selection.enabled,
                trader_enabled=current.trader.enabled,
                confirmed=selection.confirmed,
            ),
            expected_revision=expected_revision,
        )

    async def update_trader(self, selection: TraderSelection, *, expected_revision: int):
        selection = TraderSelection.model_validate_json(selection.model_dump_json())
        current = await self.current()
        _require_revision(current, expected_revision)
        return await self.update(
            ControlSelection(
                mode=selection.mode,
                execution_environment=selection.execution_environment,
                jev_enabled=current.jev.enabled,
                trader_enabled=selection.enabled,
                confirmed=selection.confirmed,
            ),
            expected_revision=expected_revision,
        )

    async def public_view(self):
        state = await self.current()
        return {
            "revision": state.revision,
            "updated_at": state.updated_at.isoformat() if state.revision else None,
            "operation": state.operation.public_state(trader_enabled=state.trader.enabled),
            "flash": {"mode": "continuous", "connection_state": "not_connected"},
            "jev_advice": {
                **state.jev.model_dump(mode="json"),
                "revision": state.advice_revision,
                "connection_state": "not_connected",
                "decision": None,
            },
            "jev_trader": {
                **state.trader.model_dump(mode="json"),
                "revision": state.trader_revision,
                "connection_state": "not_connected",
                "decision": None,
            },
            "jev": {
                **state.jev.model_dump(mode="json"),
                "connection_state": "not_connected",
                "decision": None,
            },
        }
