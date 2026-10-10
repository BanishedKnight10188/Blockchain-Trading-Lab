"""Mode selection is an audited local setting, never order or model permission."""

import asyncio
import importlib
import sqlite3

import pytest

from agent_platform.adapters.fake.clock import FakeClock
from agent_platform.adapters.sqlite import open_store
from agent_platform.domain.model_modules import JevModuleSettings
from agent_platform.domain.operating_modes import OperatingSettings
from agent_platform.ports.sessions import PersistenceUnavailable, RevisionConflict
from tests.domain.test_decisions import NOW


async def setup(path, *, operation=None, jev=None, production_reads=False):
    module = importlib.import_module("agent_platform.application.agent_controls")
    store = await open_store(path)
    service = module.AgentControlService(
        store=store,
        clock=FakeClock(NOW),
        operation=operation or OperatingSettings(),
        jev=jev or JevModuleSettings(),
        production_reads=production_reads,
    )
    return service, store


def selection(mode="auto", enabled=True):
    domain = importlib.import_module("agent_platform.domain.agent_controls")
    return domain.ControlSelection(
        mode=mode,
        jev_enabled=enabled,
        trader_enabled=False,
        confirmed=True,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["advice", "trader"])
async def test_future_revision_cannot_merge_an_old_snapshot_over_another_module(tmp_path, scope):
    domain = importlib.import_module("agent_platform.domain.agent_controls")
    path = tmp_path / "scope-race.sqlite3"
    first, _ = await setup(path)
    writer, store = await setup(path)
    loaded = asyncio.Event()
    release = asyncio.Event()
    current = first.current

    async def delayed_current():
        snapshot = await current()
        loaded.set()
        await release.wait()
        return snapshot

    first.current = delayed_current
    if scope == "advice":
        task = asyncio.create_task(
            first.update_advice(
                domain.AdviceSelection(enabled=True, confirmed=True),
                expected_revision=1,
            )
        )
        writer_selection = domain.ControlSelection(
            mode="auto",
            jev_enabled=False,
            trader_enabled=True,
            confirmed=True,
        )
    else:
        task = asyncio.create_task(
            first.update_trader(
                domain.TraderSelection(mode="auto", enabled=True, confirmed=True),
                expected_revision=1,
            )
        )
        writer_selection = domain.ControlSelection(
            mode="advisory",
            jev_enabled=True,
            trader_enabled=False,
            confirmed=True,
        )
    await loaded.wait()
    committed = await writer.update(writer_selection, expected_revision=0)
    release.set()
    with pytest.raises(RevisionConflict):
        await task
    assert await writer.current() == committed
    assert len((await store.scan(0, 100)).records) == 1


@pytest.mark.asyncio
async def test_modes_and_switch_commit_with_audit_and_recover_over_new_defaults(tmp_path):
    path = tmp_path / "agent.sqlite3"
    service, store = await setup(path)
    assert (await service.current()).revision == 0
    saved = await service.update(selection(), expected_revision=0)
    assert saved.revision == 1 and saved.operation.mode == "auto" and saved.jev.enabled
    events = (await store.scan(0, 100)).records
    assert len(events) == 1
    assert events[0].event.kind == "state_changed"
    assert events[0].event.payload.state == saved
    assert events[0].event.payload.state_type == "agent_controls"
    reopened, _ = await setup(path, operation=OperatingSettings(), jev=JevModuleSettings())
    assert await reopened.current() == saved
    disabled = await reopened.update(selection("advisory", False), expected_revision=1)
    assert disabled.revision == 2 and not disabled.jev.enabled
    assert len((await store.scan(0, 100)).records) == 2


@pytest.mark.asyncio
async def test_first_confirmation_of_defaults_is_recorded_and_noop_does_not_add_revision(tmp_path):
    service, store = await setup(tmp_path / "agent.sqlite3")
    first = await service.update(selection("advisory", False), expected_revision=0)
    assert first.revision == 1
    assert await service.update(selection("advisory", False), expected_revision=1) == first
    with pytest.raises(RevisionConflict):
        await service.update(selection("advisory", False), expected_revision=0)
    assert len((await store.scan(0, 100)).records) == 1


@pytest.mark.asyncio
async def test_concurrent_independent_writers_only_commit_one_setting(tmp_path):
    path = tmp_path / "agent.sqlite3"
    first, store = await setup(path)
    second, _ = await setup(path)
    results = await asyncio.gather(
        first.update(selection("auto", True), expected_revision=0),
        second.update(selection("advisory", True), expected_revision=0),
        return_exceptions=True,
    )
    assert sum(isinstance(value, RevisionConflict) for value in results) == 1
    assert (await first.current()).revision == 1
    assert len((await store.scan(0, 100)).records) == 1


@pytest.mark.asyncio
async def test_audit_write_failure_cannot_save_settings(tmp_path):
    service, store = await setup(tmp_path / "agent.sqlite3")

    def fail(*args):
        raise sqlite3.OperationalError("private-storage-diagnostic")

    store._append = fail
    with pytest.raises(PersistenceUnavailable):
        await service.update(selection(), expected_revision=0)
    assert (await service.current()).revision == 0
    assert not (await store.scan(0, 100)).records


@pytest.mark.asyncio
async def test_production_read_source_cannot_be_switched_to_testnet_auto(tmp_path):
    service, _ = await setup(tmp_path / "agent.sqlite3", production_reads=True)
    with pytest.raises(ValueError, match="testnet"):
        await service.update(selection(), expected_revision=0)
    assert (await service.current()).revision == 0
    saved = await service.update(selection("advisory", True), expected_revision=0)
    assert saved.operation.mode == "advisory"


@pytest.mark.asyncio
async def test_service_revalidates_bypassed_confirmation(tmp_path):
    service, _ = await setup(tmp_path / "agent.sqlite3")
    with pytest.raises(ValueError):
        await service.update(
            selection().model_copy(update={"confirmed": False}), expected_revision=0
        )
    assert (await service.current()).revision == 0


@pytest.mark.asyncio
async def test_corrupted_persisted_setting_fails_instead_of_resetting_to_defaults(tmp_path):
    path = tmp_path / "agent.sqlite3"
    service, _ = await setup(path)
    saved = await service.update(selection(), expected_revision=0)
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE domain_states SET body='{}' WHERE key=?", (saved.aggregate_id,))
    with pytest.raises(PersistenceUnavailable):
        await service.current()


@pytest.mark.asyncio
async def test_saved_auto_mode_rejects_production_bootstrap_before_clients_start(tmp_path):
    from agent_platform.bootstrap import build_application_services
    from agent_platform.config import RuntimeConfig

    path = tmp_path / "agent.sqlite3"
    service, _ = await setup(path)
    await service.update(selection(), expected_revision=0)
    with pytest.raises(ValueError, match="testnet"):
        async with build_application_services(path, RuntimeConfig(live_public=True)):
            pytest.fail("production client cannot start with saved testnet auto mode")


@pytest.mark.asyncio
async def test_existing_production_reader_rejects_auto_saved_by_another_process(tmp_path):
    path = tmp_path / "shared.sqlite3"
    reader, _ = await setup(path, production_reads=True)
    writer, _ = await setup(path)
    assert (await reader.current()).operation.mode == "advisory"
    await writer.update(selection(), expected_revision=0)
    with pytest.raises(ValueError, match="testnet"):
        await reader.current()
    with pytest.raises(ValueError, match="testnet"):
        await reader.public_view()
    await writer.update(selection("advisory", False), expected_revision=1)
    assert (await reader.current()).revision == 2
