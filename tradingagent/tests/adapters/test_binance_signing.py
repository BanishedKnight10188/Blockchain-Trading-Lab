"""HMAC correctness, exact timestamps and redacted credential boundaries."""

import importlib
from datetime import UTC, datetime, timedelta

import pytest

from tests.domain.test_decisions import NOW
from tests.market.test_normalizer import MS


def signing():
    return importlib.import_module("agent_platform.adapters.binance_direct.signing")


def test_hmac_sha256_matches_rfc4231_case_two():
    module = signing()
    credentials = module.HmacCredentials("fake-api-key", "Jefe")
    assert module.hmac_signature("what do ya want for nothing?", credentials.secret_key) == (
        "5bdcc146bf60754e6a042426089575c75a003f089d2739839dec58b964ec3843"
    )


def test_canonical_query_encoding_is_order_independent_and_signs_exact_wire_bytes():
    module = signing()
    secret = module.HmacCredentials("fake-api-key", "fake-secret").secret_key
    first = module.sign_query({"timestamp": MS, "symbol": "BTCUSDT", "note": "空 格/+"}, secret)
    second = module.sign_query({"note": "空 格/+", "symbol": "BTCUSDT", "timestamp": MS}, secret)
    assert first.query == second.query
    unsigned, signature = first.query.rsplit("&signature=", 1)
    assert unsigned == f"note=%E7%A9%BA%20%E6%A0%BC%2F%2B&symbol=BTCUSDT&timestamp={MS}"
    assert signature == module.hmac_signature(unsigned, secret)
    assert first.query not in repr(first)


def test_credentials_never_reveal_either_secret_in_repr_or_validation_failure():
    module = signing()
    credentials = module.HmacCredentials("fake-api-key", "private-fake-secret")
    assert "fake-api-key" not in repr(credentials)
    assert "private-fake-secret" not in str(credentials.secret_key)
    with pytest.raises(ValueError) as error:
        module.HmacCredentials("fake-api-key", "private-fake-secret\n")
    assert "private-fake-secret" not in str(error.value)


@pytest.mark.parametrize(
    "params",
    [
        {"timestamp": True},
        {"timestamp": 0.1},
        {"signature": "injected"},
        {"timestamp": -1},
        {"symbol": "x" * 257},
        {1: "BTCUSDT"},
    ],
)
def test_unsupported_signing_input_is_rejected_without_echoing_values(params):
    module = signing()
    credentials = module.HmacCredentials("fake-api-key", "fake-secret")
    with pytest.raises(ValueError):
        module.sign_query(params, credentials.secret_key)


def test_unix_millisecond_conversion_is_exact_and_requires_known_utc():
    module = signing()
    assert module.unix_milliseconds(NOW + timedelta(microseconds=999999)) == MS + 999
    assert module.unix_milliseconds(datetime(1970, 1, 1, tzinfo=UTC)) == 0
    with pytest.raises(ValueError):
        module.unix_milliseconds(datetime(2026, 10, 5))
    with pytest.raises(ValueError):
        module.unix_milliseconds(datetime(1969, 12, 31, tzinfo=UTC))
