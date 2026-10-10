"""Bounded injected tools/list inspection; no SDK, login, or tools/call method."""

import asyncio
import json
import math
import re
from hashlib import sha256
from typing import Protocol

from agent_platform.domain.capabilities import CapabilityReport, ToolFingerprint
from agent_platform.ports.capabilities import CapabilityUnavailable
from agent_platform.ports.clock import ClockPort


class ToolInventorySession(Protocol):
    async def list_tools(self, cursor: str | None = None) -> dict: ...


def _bounded_json(value: object) -> bytes:
    stack, nodes = [(value, 0)], 0
    while stack:
        item, depth = stack.pop()
        nodes += 1
        if nodes > 32768 or depth > 20:
            raise ValueError("metadata bounds exceeded")
        if item is None or type(item) is bool:
            continue
        if type(item) is str:
            if len(item) > 4096:
                raise ValueError("metadata bounds exceeded")
        elif type(item) is int:
            if item.bit_length() > 256:
                raise ValueError("metadata bounds exceeded")
        elif type(item) is float:
            if not math.isfinite(item):
                raise ValueError("nonfinite metadata")
        elif type(item) is dict:
            if len(item) > 4096 or len(stack) + len(item) + nodes > 32768:
                raise ValueError("metadata bounds exceeded")
            if any(type(key) is not str or len(key) > 256 for key in item):
                raise ValueError("invalid metadata keys")
            stack.extend((child, depth + 1) for child in item.values())
        elif type(item) is list:
            if len(item) > 4096 or len(stack) + len(item) + nodes > 32768:
                raise ValueError("metadata bounds exceeded")
            stack.extend((child, depth + 1) for child in item)
        else:
            raise ValueError("unsupported metadata value")
    encoder = json.JSONEncoder(
        sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )
    payload = bytearray()
    for chunk in encoder.iterencode(value):
        encoded = chunk.encode("utf-8")
        if len(payload) + len(encoded) > 262144:
            raise ValueError("metadata bounds exceeded")
        payload.extend(encoded)
    return bytes(payload)


def _digest(value: dict) -> str:
    return sha256(_bounded_json(value)).hexdigest()


def _tool(value: dict) -> ToolFingerprint:
    if type(value) is not dict:
        raise ValueError("invalid tool descriptor")
    name, schema, output = value.get("name"), value.get("inputSchema"), value.get("outputSchema")
    if type(name) is not str or not re.fullmatch(r"[A-Za-z0-9_.:/-]{1,128}", name):
        raise ValueError("invalid tool name")
    if type(schema) is not dict or schema.get("type") != "object":
        raise ValueError("unsupported tool input schema")
    if output is not None and (type(output) is not dict or output.get("type") != "object"):
        raise ValueError("unsupported tool output schema")
    return ToolFingerprint(
        name=name,
        input_sha256=_digest(schema),
        output_sha256=_digest(output) if output is not None else None,
        metadata_sha256=_digest(
            {key: item for key, item in value.items() if key not in ("inputSchema", "outputSchema")}
        ),
    )


def require_verified_mapping(report: CapabilityReport, capability: str) -> None:
    # Schema equality alone is intentionally insufficient for any read capability.
    if capability not in report.verified_capabilities:
        raise CapabilityUnavailable("Agent OS read mapping or account scope is not verified")


class McpCapabilityProbe:
    def __init__(
        self,
        clock: ClockPort,
        session: ToolInventorySession | None = None,
        *,
        expected: tuple[ToolFingerprint, ...] | None = None,
        max_pages: int = 4,
        timeout_seconds: float = 10,
    ):
        if type(max_pages) is not int or not 1 <= max_pages <= 16:
            raise ValueError("inventory page limit must be 1..16")
        if type(timeout_seconds) not in (float, int) or not 0.001 <= timeout_seconds <= 15:
            raise ValueError("inventory timeout must be bounded")
        if expected is not None and (
            type(expected) is not tuple
            or any(not isinstance(item, ToolFingerprint) for item in expected)
            or len({item.name for item in expected}) != len(expected)
        ):
            raise ValueError("invalid pinned inventory")
        self.clock, self._session = clock, session
        self._expected = expected
        self._pages, self._seconds = max_pages, timeout_seconds

    def _report(self, status: str, *, tools=(), reason=None) -> CapabilityReport:
        return CapabilityReport(
            checked_at=self.clock.utcnow(), status=status, tools=tools, reason=reason
        )

    async def inspect(self) -> CapabilityReport:
        if self._session is None:
            return self._report("disabled")
        tools, cursors, cursor = {}, set(), None
        try:
            async with asyncio.timeout(self._seconds):
                for _ in range(self._pages):
                    page = await self._session.list_tools(cursor)
                    _bounded_json(page)
                    if (
                        type(page) is not dict
                        or type(page.get("tools")) is not list
                        or len(page["tools"]) > 256
                    ):
                        raise ValueError("invalid tool inventory")
                    for row in page["tools"]:
                        fingerprint = _tool(row)
                        if fingerprint.name in tools:
                            raise ValueError("duplicate tool names")
                        tools[fingerprint.name] = fingerprint
                    cursor = page.get("nextCursor")
                    if cursor is None:
                        inventory = tuple(tools[name] for name in sorted(tools))
                        status = "unverified"
                        if self._expected is not None:
                            status = (
                                "schema_matched"
                                if inventory
                                == tuple(sorted(self._expected, key=lambda item: item.name))
                                else "schema_changed"
                            )
                        return self._report(status, tools=inventory)
                    if (
                        type(cursor) is not str
                        or not 1 <= len(cursor) <= 512
                        or any(ord(character) < 32 or ord(character) == 127 for character in cursor)
                        or cursor in cursors
                    ):
                        raise ValueError("invalid pagination cursor")
                    cursors.add(cursor)
        except PermissionError:
            return self._report("unavailable", reason="permission")
        except TimeoutError:
            return self._report("unavailable", reason="timeout")
        except (ValueError, TypeError, UnicodeError, RecursionError):
            return self._report("unavailable", reason="invalid_metadata")
        except Exception:
            return self._report("unavailable", reason="transport")
        return self._report("unavailable", reason="incomplete_inventory")
