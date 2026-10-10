"""System clock for assembly; domain code does not import this module."""

import asyncio
import math
import socket
import struct
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from time import monotonic, perf_counter

NTP_EPOCH = 2208988800
CLOCK_ERROR_LIMIT = 0.05
CLOCK_MAX_AGE = 180
CLOCK_DRIFT = 0.000015
TIME_SERVERS = ("ntp.aliyun.com", "ntp1.aliyun.com")


class SystemClock:
    def utcnow(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return monotonic()


@dataclass(frozen=True)
class TimeSample:
    source: str
    utc: datetime
    monotonic: float
    uncertainty_seconds: float
    offset_seconds: float


def ntp_request(wall: datetime) -> bytes:
    packet = bytearray(48)
    packet[0] = 0x23  # NTPv4 client, with a nonzero transmit/originate identity.
    stamp = wall.timestamp() + NTP_EPOCH
    whole = int(stamp)
    struct.pack_into("!II", packet, 40, whole, int((stamp - whole) * 2**32))
    return bytes(packet)


def decode_ntp_response(data, request, wall, started, received, source) -> TimeSample:
    if (
        source not in TIME_SERVERS
        or len(data) != 48
        or data[0] >> 6 == 3
        or (data[0] >> 3) & 7 not in (3, 4)
        or data[0] & 7 != 4
        or not 1 <= data[1] <= 15
        or data[24:32] != request[40:48]
    ):
        raise ValueError("invalid NTP identity or synchronization")
    words = struct.unpack("!12I", data)
    t2 = words[8] + words[9] / 2**32 - NTP_EPOCH
    t3 = words[10] + words[11] / 2**32 - NTP_EPOCH
    elapsed = received - started
    if not 0 <= t3 - t2 <= elapsed <= 2 or not words[8] or not words[10]:
        raise ValueError("invalid NTP response chronology")
    t1 = wall.timestamp()
    t4 = t1 + elapsed
    offset = ((t2 - t1) + (t3 - t4)) / 2
    root_delay = struct.unpack("!i", data[4:8])[0] / 65536
    dispersion = words[2] / 65536
    precision = 2 ** struct.unpack("!b", data[3:4])[0]
    # Include upstream distance and Windows wall timestamp granularity, not just RTT.
    uncertainty = (elapsed - (t3 - t2) + max(0, root_delay)) / 2 + dispersion + precision + 0.016
    if root_delay < 0 or abs(offset) > 2 or not math.isfinite(uncertainty):
        raise ValueError("NTP correction is outside supported bounds")
    return TimeSample(
        source=source,
        utc=wall + timedelta(seconds=elapsed + offset),
        monotonic=received,
        uncertainty_seconds=uncertainty,
        offset_seconds=offset,
    )


def _probe_server(source, wall_now, monotonic_now):
    samples = []
    # A connected UDP socket pins the response to the resolved peer and ephemeral port.
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as peer:
            peer.settimeout(1)
            peer.connect((source, 123))
            for _ in range(3):
                wall, began = wall_now(), monotonic_now()
                request = ntp_request(wall)
                try:
                    peer.send(request)
                    data = peer.recv(512)
                    samples.append(
                        decode_ntp_response(data, request, wall, began, monotonic_now(), source)
                    )
                except (OSError, ValueError, OverflowError):
                    continue
    except (OSError, RuntimeError):
        pass
    return samples


class CalibratedClock:
    """Bounded UTC anchored to monotonic time; does not change the Windows clock.

    Only the live Paper assembly owns this resource. All its task runtimes share
    it. A failed refresh can retain recent bounded evidence, never indefinitely.
    """

    def __init__(self, *, wall_now=None, monotonic=perf_counter, probe=_probe_server):
        self.wall_now = wall_now or (lambda: datetime.now(UTC))
        self._monotonic, self._probe = monotonic, probe
        self._anchor, self._began = self.wall_now(), self._monotonic()
        self._sample = None
        self._uncertainty = float("inf")
        self._blocked = None
        self._last = self._anchor
        self._task = None
        self._released = False
        self.last_failure = "clock_not_calibrated"

    def monotonic(self):
        return self._monotonic()

    def utcnow(self):
        current = self._anchor + timedelta(seconds=self.monotonic() - self._began)
        self._last = max(self._last, current)
        return self._last

    @property
    def public_status(self):
        age = max(0, self.monotonic() - self._sample.monotonic) if self._sample else None
        uncertainty = self._uncertainty + (age or 0) * CLOCK_DRIFT
        ready = bool(
            self._sample
            and not self._blocked
            and age <= CLOCK_MAX_AGE
            and uncertainty <= CLOCK_ERROR_LIMIT
        )
        failure = self._blocked or (
            "clock_calibration_stale"
            if age is not None and age > CLOCK_MAX_AGE
            else "clock_calibration_uncertain"
            if self._sample and uncertainty > CLOCK_ERROR_LIMIT
            else self.last_failure
        )
        return {
            "ready": ready,
            "source": self._sample.source if self._sample else None,
            "offset_ms": round((self.utcnow() - self.wall_now()).total_seconds() * 1000, 3),
            "uncertainty_ms": round(uncertainty * 1000, 3) if math.isfinite(uncertainty) else None,
            "age_seconds": round(age, 3) if age is not None else None,
            "synchronized_at": self._sample.utc.isoformat() if self._sample else None,
            "failure": None if ready else failure,
            "last_refresh_failure": self.last_failure,
        }

    def update(self, samples):
        now = self.monotonic()
        valid = [
            s
            for s in samples
            if s.source in TIME_SERVERS
            and math.isfinite(s.uncertainty_seconds)
            and math.isfinite(s.offset_seconds)
            and abs(s.offset_seconds) <= 2
            and 0 <= now - s.monotonic <= 10
            and 0 <= s.uncertainty_seconds + (now - s.monotonic) * CLOCK_DRIFT <= CLOCK_ERROR_LIMIT
            and s.utc.tzinfo == UTC
        ]
        if not valid:
            self.last_failure = "clock_calibration_unavailable"
            return
        selected = min(valid, key=lambda s: s.uncertainty_seconds)
        candidate = selected.utc + timedelta(seconds=now - selected.monotonic)
        if any(
            abs((s.utc + timedelta(seconds=now - s.monotonic) - candidate).total_seconds())
            > s.uncertainty_seconds + selected.uncertainty_seconds
            for s in valid
        ):
            self._blocked = "clock_source_disagreement"
            return
        current = self.utcnow()
        if self._sample and abs((candidate - current).total_seconds()) > CLOCK_ERROR_LIMIT:
            self._blocked = "clock_source_disagreement"
            return
        # Negative corrections remain in the error budget; UTC never moves backwards.
        keep_continuity = self._sample is not None or self._released
        retained_error = max(0, (current - candidate).total_seconds()) if keep_continuity else 0
        if selected.uncertainty_seconds + retained_error > CLOCK_ERROR_LIMIT:
            self._blocked = "clock_calibration_uncertain"
            return
        self._anchor = max(current, candidate) if keep_continuity else candidate
        if not keep_continuity:
            self._last = self._anchor  # Startup calibration precedes all application timestamps.
        self._began, self._sample = now, selected
        self._uncertainty = selected.uncertainty_seconds + retained_error
        self._blocked = self.last_failure = None

    async def refresh(self):
        batches = await asyncio.gather(
            *(
                asyncio.to_thread(self._probe, server, self.wall_now, self.monotonic)
                for server in TIME_SERVERS
            )
        )
        self.update([sample for batch in batches for sample in batch])

    async def _refresh_loop(self):
        while True:
            await asyncio.sleep(30)
            await self.refresh()

    async def __aenter__(self):
        await self.refresh()
        self._released = True
        self._task = asyncio.create_task(self._refresh_loop(), name="paper-clock-calibration")
        return self

    async def __aexit__(self, *args):
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
