"""Actual monotonic measurements; duration alone never grants formal acceptance."""

import asyncio
import ctypes
import sys
from datetime import UTC, datetime
from time import monotonic

from agent_platform.config import RuntimeConfig


def process_memory() -> int | None:
    if sys.platform != "win32":
        return None

    class Counters(ctypes.Structure):
        _fields_ = [
            ("cb", ctypes.c_ulong),
            ("PageFaultCount", ctypes.c_ulong),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    try:
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        kernel.GetCurrentProcess.restype = ctypes.c_void_p
        psapi.GetProcessMemoryInfo.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(Counters),
            ctypes.c_ulong,
        ]
        psapi.GetProcessMemoryInfo.restype = ctypes.c_int
        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        if psapi.GetProcessMemoryInfo(
            kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb
        ):
            return counters.WorkingSetSize
    except (OSError, AttributeError):
        pass
    return None


def assess_soak(elapsed, *, live_public, live_account, ready, faults):
    if not live_public and not live_account:
        return "offline_short_check"
    if elapsed < 86400:
        return "duration_not_met"
    if not live_public or not live_account or not ready:
        return "live_evidence_missing"
    if faults:
        return "runtime_faults_present"
    return "duration_complete_requires_review"


async def capture_soak(database_path, config: RuntimeConfig, *, seconds: int):
    if type(seconds) is not int or not 1 <= seconds <= 604800:
        raise ValueError("capture requires a 1–604800 second integer")
    from agent_platform.bootstrap import build_application_services

    started_at, started = datetime.now(UTC), monotonic()
    samples = faults = ready_samples = 0
    memory_first = memory_last = memory_peak = None
    longest_gap, previous = 0, None
    interrupted = False
    transport = None
    services = data = None
    try:
        async with build_application_services(database_path, config) as services:
            while True:
                data = await services.system.current()
                now = monotonic()
                if previous is not None:
                    longest_gap = max(longest_gap, now - previous)
                previous = now
                samples += 1
                faults += data["runtime"]["status"] == "degraded"
                ready_samples += (
                    data["market"]["status"] == "ready" and data["account"]["status"] == "fresh"
                )
                memory = process_memory()
                if memory is not None:
                    if memory_first is None:
                        memory_first = memory
                    memory_last = memory
                    memory_peak = max(memory_peak or 0, memory)
                remaining = seconds - (now - started)
                if remaining <= 0:
                    break
                await asyncio.sleep(min(10, remaining))
    except asyncio.CancelledError:
        interrupted = True
    # Assembly exit stops all owned workers before the report is produced.
    ended_at, elapsed = datetime.now(UTC), monotonic() - started
    health = await services.runtime.health() if services is not None else None
    for worker in services.runtime.workers if services is not None else ():
        market = getattr(worker, "market", None)
        stats = getattr(market, "stats", None)
        if stats is not None:
            transport = {
                key: getattr(stats, key)
                for key in (
                    "connections",
                    "reconnects",
                    "failures",
                    "reconciliation_failures",
                )
            }
    verdict = assess_soak(
        elapsed,
        live_public=config.live_public,
        live_account=config.live_account,
        ready=ready_samples > 0,
        faults=faults,
    )
    return dict(
        schema_version=1,
        mode="live_read_only" if config.live_public or config.live_account else "disabled",
        started_at=started_at.isoformat(),
        ended_at=ended_at.isoformat(),
        measurement="actual_monotonic",
        elapsed_seconds=elapsed,
        requested_seconds=seconds,
        samples=samples,
        ready_samples=ready_samples,
        degraded_samples=faults,
        longest_sample_gap_seconds=longest_gap,
        sampling_gap_detected=longest_gap > 20,
        interrupted=interrupted,
        shutdown_status=health.status if health is not None else "not_started",
        memory=dict(
            kind="windows_working_set" if memory_first is not None else "unavailable",
            first_bytes=memory_first,
            last_bytes=memory_last,
            peak_sampled_bytes=memory_peak,
        ),
        budget=data["budget"] if data is not None else None,
        budget_status=data["budget_status"] if data is not None else "unavailable",
        market_status=data["market"]["status"] if data is not None else "not_connected",
        account_status=data["account"]["status"] if data is not None else "not_connected",
        transport=transport,
        archive=data["archive"] if data is not None else None,
        verdict=verdict,
        formal_acceptance=False,
        real_orders=0,
        model_calls=0,
        review_required=(
            "连续行情与账户覆盖、采样缺口、内存趋势、未知费用、日志轮转、重启恢复和隐私仍须逐项复核"
        ),
    )
