"""Synthetic protocol metadata only; no actual Binance tool names or permission claims."""

import asyncio
import importlib
import tracemalloc

import pytest

from agent_platform.adapters.fake.clock import FakeClock
from tests.domain.test_decisions import NOW


def module():
    return importlib.import_module("agent_platform.adapters.binance_agent_os.discovery")


@pytest.mark.asyncio
async def test_oversized_shared_string_schema_is_rejected_before_full_encoding():
    discovery = module()
    large = tool()
    large["inputSchema"]["examples"] = ["x" * 4096] * 4000
    session = FakeSession([{"tools": [large]}])
    tracemalloc.start()
    try:
        report = await discovery.McpCapabilityProbe(FakeClock(NOW), session).inspect()
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert report.reason == "invalid_metadata" and report.tools == ()
    assert peak < 2_000_000, f"metadata rejection allocated {peak} bytes"


def tool(name="fixture_read"):
    return {
        "name": name,
        "inputSchema": {"type": "object", "properties": {"symbol": {"type": "string"}}},
        "outputSchema": {"type": "object"},
        "annotations": {"readOnlyHint": True},
        "description": "private-value-not-exported",
    }


class FakeSession:
    def __init__(self, pages):
        self.pages, self.calls = pages, []

    async def list_tools(self, cursor=None):
        self.calls.append(cursor)
        page = self.pages[len(self.calls) - 1]
        if isinstance(page, Exception):
            raise page
        return page


@pytest.mark.asyncio
async def test_no_transport_is_disabled_and_cannot_invent_empty_account():
    report = await module().McpCapabilityProbe(FakeClock(NOW)).inspect()
    assert report.status == "disabled" and report.tools == ()
    assert report.verified_capabilities == ()
    with pytest.raises(module().CapabilityUnavailable):
        module().require_verified_mapping(report, "main_spot_account")


@pytest.mark.asyncio
async def test_schema_fingerprints_are_canonical_and_hints_do_not_grant_reads():
    first = (
        await module()
        .McpCapabilityProbe(FakeClock(NOW), FakeSession([{"tools": [tool()]}]))
        .inspect()
    )
    reordered = dict(reversed(list(tool().items())))
    second = (
        await module()
        .McpCapabilityProbe(FakeClock(NOW), FakeSession([{"tools": [reordered]}]))
        .inspect()
    )
    assert first.tools == second.tools
    assert len(first.tools[0].input_sha256) == 64
    assert first.status == "unverified" and first.verified_capabilities == ()
    assert "private-value" not in first.model_dump_json()
    with pytest.raises(module().CapabilityUnavailable):
        module().require_verified_mapping(first, "public_market")


@pytest.mark.asyncio
async def test_schema_change_missing_or_new_tool_invalidates_pinned_inventory():
    clock = FakeClock(NOW)
    original = (
        await module().McpCapabilityProbe(clock, FakeSession([{"tools": [tool()]}])).inspect()
    )
    same = (
        await module()
        .McpCapabilityProbe(clock, FakeSession([{"tools": [tool()]}]), expected=original.tools)
        .inspect()
    )
    assert same.status == "schema_matched" and same.verified_capabilities == ()
    changed = tool()
    changed["inputSchema"]["required"] = ["symbol"]
    for rows in ([], [changed], [tool(), tool("fixture_added")]):
        report = (
            await module()
            .McpCapabilityProbe(clock, FakeSession([{"tools": rows}]), expected=original.tools)
            .inspect()
        )
        assert report.status == "schema_changed"


@pytest.mark.asyncio
async def test_pagination_is_bounded_and_cursors_are_not_exported():
    session = FakeSession(
        [
            {"tools": [tool("fixture_a")], "nextCursor": "private-page-cursor"},
            {"tools": [tool("fixture_b")]},
        ]
    )
    report = await module().McpCapabilityProbe(FakeClock(NOW), session).inspect()
    assert len(report.tools) == 2 and session.calls == [None, "private-page-cursor"]
    assert "private-page-cursor" not in report.model_dump_json()
    truncated = (
        await module()
        .McpCapabilityProbe(
            FakeClock(NOW), FakeSession([{"tools": [tool()], "nextCursor": "more"}]), max_pages=1
        )
        .inspect()
    )
    assert truncated.status == "unavailable" and truncated.tools == ()
    assert truncated.reason == "incomplete_inventory"


@pytest.mark.parametrize(
    "page",
    [
        {"tools": [tool(), tool()]},
        {"tools": [], "nextCursor": ""},
        {"tools": [{"name": "fixture_bad", "inputSchema": {"type": "string"}}]},
        {"tools": [dict(tool(), name="unsafe\nname")]},
        {"tools": [dict(tool(), inputSchema={"type": "object", "default": float("nan")})]},
        {"tools": [dict(tool(), description="x" * 300000)]},
    ],
)
@pytest.mark.asyncio
async def test_invalid_metadata_does_not_publish_partial_inventory(page):
    report = await module().McpCapabilityProbe(FakeClock(NOW), FakeSession([page])).inspect()
    assert report.status == "unavailable" and report.tools == ()
    assert report.reason == "invalid_metadata"


@pytest.mark.asyncio
async def test_permissions_and_server_errors_are_sanitized():
    for error in (PermissionError("private-token"), RuntimeError("private-token")):
        report = await module().McpCapabilityProbe(FakeClock(NOW), FakeSession([error])).inspect()
        assert report.status == "unavailable" and report.tools == ()
        assert "private-token" not in report.model_dump_json()


@pytest.mark.asyncio
async def test_timeout_and_cancel_do_not_leave_authorized_capabilities():
    class Waiting:
        cancelled = False

        async def list_tools(self, cursor=None):
            try:
                await asyncio.Event().wait()
            finally:
                self.cancelled = True

    session = Waiting()
    report = (
        await module().McpCapabilityProbe(FakeClock(NOW), session, timeout_seconds=0.01).inspect()
    )
    assert report.reason == "timeout" and session.cancelled
    pending = asyncio.create_task(module().McpCapabilityProbe(FakeClock(NOW), session).inspect())
    await asyncio.sleep(0)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending


@pytest.mark.asyncio
async def test_changed_read_hint_invalidates_metadata_without_granting_a_tool():
    clock = FakeClock(NOW)
    original = (
        await module().McpCapabilityProbe(clock, FakeSession([{"tools": [tool()]}])).inspect()
    )
    changed = tool()
    changed["annotations"]["readOnlyHint"] = False
    report = (
        await module()
        .McpCapabilityProbe(clock, FakeSession([{"tools": [changed]}]), expected=original.tools)
        .inspect()
    )
    assert report.status == "schema_changed" and report.verified_capabilities == ()


@pytest.mark.asyncio
async def test_repeated_cursor_and_cyclic_metadata_never_publish_partial_hashes():
    pages = [
        {"tools": [tool("fixture_a")], "nextCursor": "loop"},
        {"tools": [tool("fixture_b")], "nextCursor": "loop"},
    ]
    report = await module().McpCapabilityProbe(FakeClock(NOW), FakeSession(pages)).inspect()
    assert report.reason == "invalid_metadata" and report.tools == ()
    recursive = tool()
    recursive["inputSchema"]["cycle"] = recursive
    report = (
        await module()
        .McpCapabilityProbe(FakeClock(NOW), FakeSession([{"tools": [recursive]}]))
        .inspect()
    )
    assert report.reason == "invalid_metadata" and report.tools == ()
