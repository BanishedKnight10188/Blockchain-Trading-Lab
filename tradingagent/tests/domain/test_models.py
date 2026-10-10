"""Pydantic domain contracts prepared before implementation; require project dependencies."""

from __future__ import annotations

import importlib
import importlib.util
import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal, localcontext
from importlib.metadata import PackageNotFoundError, version


def pydantic_two_available() -> bool:
    try:
        return version("pydantic").split(".", 1)[0] == "2"
    except PackageNotFoundError:
        return False


@unittest.skipUnless(pydantic_two_available(), "requires user-installed Conda Pydantic 2")
class DomainModelsTest(unittest.TestCase):
    def setUp(self):
        for name in ("account", "market"):
            self.assertIsNotNone(importlib.util.find_spec(f"agent_platform.domain.{name}"))
        self.account = importlib.import_module("agent_platform.domain.account")
        self.market = importlib.import_module("agent_platform.domain.market")
        self.now = datetime(2026, 10, 5, tzinfo=UTC)

    def test_balance_total_preserves_decimal_precision(self):
        balance = self.account.Balance(asset="USDT", free="0.1", locked="0.2")
        self.assertEqual(balance.total, Decimal("0.3"))
        self.assertIsInstance(balance.total, Decimal)

    def test_balance_total_preserves_more_than_default_precision(self):
        balance = self.account.Balance(
            asset="BTC",
            free="0.12345678901234567890123456781",
            locked="0.00000000000000000000000000001",
        )
        self.assertEqual(balance.total, Decimal("0.12345678901234567890123456782"))

    def test_balance_total_is_independent_of_ambient_precision(self):
        balance = self.account.Balance(asset="USDT", free="1E+29", locked="1")
        with localcontext() as context:
            context.prec = 1
            self.assertEqual(balance.total, Decimal("100000000000000000000000000001"))

    def test_balance_rejects_a_deficit_or_float(self):
        for value in ("-0.01", 0.1, "NaN", "Infinity"):
            with self.subTest(value=str(value)):
                with self.assertRaises(ValueError):
                    self.account.Balance(asset="USDT", free=value, locked="0")

    def test_domain_balance_is_immutable(self):
        balance = self.account.Balance(asset="BTC", free="0.01", locked="0")
        with self.assertRaises(ValueError):
            balance.free = Decimal("9")
        self.assertEqual(balance.free, Decimal("0.01"))

    def test_unknown_sensitive_fields_are_not_stored(self):
        with self.assertRaises(ValueError):
            self.account.Balance(asset="BTC", free="0", locked="0", api_secret="do-not-save")

    def test_balance_json_keeps_money_as_strings(self):
        balance = self.account.Balance(asset="USDT", free="0.1", locked="0.2")
        self.assertEqual(balance.model_dump(mode="json")["free"], "0.1")

    def test_candle_rejects_inconsistent_ohlc(self):
        with self.assertRaises(ValueError):
            self.market.Candle(
                symbol="BTCUSDT",
                opened_at=self.now,
                closed_at=self.now + timedelta(minutes=1),
                open="100",
                high="90",
                low="80",
                close="95",
                volume="1",
            )

    def test_candle_rejects_naive_time(self):
        with self.assertRaises(ValueError):
            self.market.Candle(
                symbol="BTCUSDT",
                opened_at=datetime(2026, 10, 5),
                closed_at=self.now + timedelta(minutes=1),
                open="100",
                high="110",
                low="90",
                close="105",
                volume="1",
            )

    def test_candle_requires_a_positive_time_window(self):
        with self.assertRaises(ValueError):
            self.market.Candle(
                symbol="BTCUSDT",
                opened_at=self.now,
                closed_at=self.now,
                open="100",
                high="110",
                low="90",
                close="105",
                volume="1",
            )

    def test_candle_round_trip_preserves_utc_and_decimals(self):
        candle = self.market.Candle(
            symbol="BTCUSDT",
            opened_at=self.now,
            closed_at=self.now + timedelta(minutes=1),
            open="100",
            high="110",
            low="90",
            close="105",
            volume="1.25",
        )
        restored = self.market.Candle.model_validate_json(candle.model_dump_json())
        self.assertEqual(restored.close, Decimal("105"))
        self.assertEqual(restored.opened_at, self.now)


if __name__ == "__main__":
    unittest.main()
