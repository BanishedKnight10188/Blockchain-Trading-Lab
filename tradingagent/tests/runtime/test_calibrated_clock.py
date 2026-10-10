"""Runtime time corrections never weaken quote chronology or rewrite exchange times."""

import struct
from datetime import timedelta

import pytest

from agent_platform.runtime import clock as clocks
from tests.domain.test_futures_paper import NOW


class LocalTime:
    seconds = 0.0
    wall_shift = 0.0

    def wall(self):
        return NOW + timedelta(seconds=self.seconds + self.wall_shift)

    def mono(self):
        return self.seconds


def calibrated(local):
    assert hasattr(clocks, "CalibratedClock"), "live Paper needs a bounded runtime clock"
    return clocks.CalibratedClock(wall_now=local.wall, monotonic=local.mono)


def sample(local, *, correction=0.4, uncertainty=0.02):
    return clocks.TimeSample(
        source="ntp.aliyun.com",
        utc=local.wall() + timedelta(seconds=correction),
        monotonic=local.mono(),
        uncertainty_seconds=uncertainty,
        offset_seconds=correction,
    )


def test_corrects_skew_and_ignores_later_windows_clock_steps():
    local = LocalTime()
    clock = calibrated(local)
    assert not clock.public_status["ready"]
    clock.update([sample(local)])
    assert clock.utcnow() == NOW + timedelta(seconds=0.4)
    local.seconds = 10
    local.wall_shift = -0.8
    assert clock.utcnow() == NOW + timedelta(seconds=10.4)
    assert clock.public_status["ready"]
    assert clock.public_status["offset_ms"] == pytest.approx(1200)


def test_initial_calibration_also_corrects_a_windows_clock_that_is_ahead():
    local = LocalTime()
    clock = calibrated(local)
    clock.update([sample(local, correction=-0.4)])
    assert clock.utcnow() == NOW - timedelta(seconds=0.4)


def test_sources_must_agree_within_their_measured_uncertainty():
    from dataclasses import replace

    local = LocalTime()
    clock = calibrated(local)
    first = sample(local)
    second = replace(sample(local, correction=0.7), source="ntp1.aliyun.com")
    clock.update([first, second])
    assert not clock.public_status["ready"]
    assert clock.public_status["failure"] == "clock_source_disagreement"


def test_negative_correction_keeps_time_monotonic_and_accounts_for_error():
    local = LocalTime()
    clock = calibrated(local)
    clock.update([sample(local)])
    local.seconds = 1
    before = clock.utcnow()
    clock.update([sample(local, correction=0.39)])
    assert clock.utcnow() == before
    assert clock.public_status["uncertainty_ms"] == pytest.approx(30)
    local.seconds += 0.1
    assert clock.utcnow() > before


def test_old_or_uncertain_calibration_cannot_be_used_for_trading():
    local = LocalTime()
    clock = calibrated(local)
    clock.update([sample(local, uncertainty=0.051)])
    assert not clock.public_status["ready"]
    clock.update([sample(local)])
    local.seconds = 181
    assert not clock.public_status["ready"]
    assert clock.public_status["failure"] == "clock_calibration_stale"


def test_conflicting_time_source_blocks_quotes_without_stepping_the_clock():
    local = LocalTime()
    clock = calibrated(local)
    clock.update([sample(local)])
    local.seconds = 1
    before = clock.utcnow()
    clock.update([sample(local, correction=0.6)])
    assert clock.utcnow() == before
    assert not clock.public_status["ready"]
    assert clock.public_status["failure"] == "clock_source_disagreement"


@pytest.mark.asyncio
async def test_late_initial_sync_cannot_rewind_already_published_application_time():
    local = LocalTime()
    clock = clocks.CalibratedClock(
        wall_now=local.wall, monotonic=local.mono, probe=lambda *args: []
    )
    async with clock:
        published = clock.utcnow()
        clock.update([sample(local, correction=-0.4)])
        assert clock.utcnow() >= published
        assert not clock.public_status["ready"]
        assert clock.public_status["failure"] == "clock_calibration_uncertain"
    assert clock._task is None


def response(request, *, root_delay=0.01, dispersion=0.002):
    data = bytearray(48)
    data[0], data[1], data[3] = 0x24, 2, 236
    struct.pack_into("!iI", data, 4, int(root_delay * 65536), int(dispersion * 65536))
    data[24:32] = request[40:48]
    for offset, seconds in [(32, 0.415), (40, 0.416)]:
        stamp = NOW.timestamp() + 2208988800 + seconds
        whole = int(stamp)
        struct.pack_into("!II", data, offset, whole, int((stamp - whole) * 2**32))
    return bytes(data)


def test_ntp_uses_matched_originate_and_full_error_budget():
    assert hasattr(clocks, "decode_ntp_response"), "validate the actual NTP evidence"
    request = clocks.ntp_request(NOW)
    result = clocks.decode_ntp_response(
        response(request), request, NOW, 10, 10.032, "ntp.aliyun.com"
    )
    assert result.offset_seconds == pytest.approx(0.3995, abs=0.00001)
    # Half network delay + half root delay + dispersion + local timestamp precision.
    assert 0.038 < result.uncertainty_seconds < 0.04
    with pytest.raises(ValueError):
        clocks.decode_ntp_response(response(request), bytes(48), NOW, 10, 10.032, "ntp.aliyun.com")


@pytest.mark.parametrize("byte,value", [(0, 0xE4), (0, 0x23), (1, 0), (1, 16)])
def test_unsynchronized_or_wrong_ntp_response_is_rejected(byte, value):
    assert hasattr(clocks, "decode_ntp_response")
    request = clocks.ntp_request(NOW)
    data = bytearray(response(request))
    data[byte] = value
    with pytest.raises(ValueError):
        clocks.decode_ntp_response(bytes(data), request, NOW, 10, 10.032, "ntp.aliyun.com")
