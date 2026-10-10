"""Protect exact amounts, UTC event time and identifiers at domain boundaries."""

from __future__ import annotations

import importlib
import importlib.util
import unittest
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal


class CommonValuesTest(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec("agent_platform.domain.common"))
        self.common = importlib.import_module("agent_platform.domain.common")

    def test_decimal_preserves_exact_input(self):
        cases = [
            ("0.100000000000000000000000000001", Decimal("0.100000000000000000000000000001")),
            (7, Decimal("7")),
            (Decimal("123.4500"), Decimal("123.4500")),
            ("1e-8", Decimal("0.00000001")),
        ]
        for given, expected in cases:
            with self.subTest(given=given):
                result = self.common.decimal_value(given)
                self.assertIsInstance(result, Decimal)
                self.assertEqual(result, expected)

    def test_floats_and_booleans_do_not_enter_financial_values(self):
        for value in (0.1, 1.0, float("nan"), True, False, None, object()):
            with self.subTest(value=type(value).__name__):
                with self.assertRaises(self.common.DomainValidationError):
                    self.common.decimal_value(value)

    def test_nonfinite_and_malformed_amounts_are_rejected(self):
        for value in ("NaN", "sNaN", "Infinity", "-Infinity", "", "not-a-price", Decimal("NaN")):
            with self.subTest(value=str(value)):
                with self.assertRaises(self.common.DomainValidationError):
                    self.common.decimal_value(value)

    def test_positive_amount_requires_strictly_greater_than_zero(self):
        self.assertEqual(self.common.positive_amount("0.00000001"), Decimal("0.00000001"))
        for value in ("0", "-0", "-0.00000001"):
            with self.subTest(value=value):
                with self.assertRaises(self.common.DomainValidationError):
                    self.common.positive_amount(value)

    def test_nonnegative_amount_allows_zero_but_not_negative_balance(self):
        self.assertEqual(self.common.nonnegative_amount("0"), Decimal("0"))
        self.assertEqual(self.common.nonnegative_amount("1.25"), Decimal("1.25"))
        with self.assertRaises(self.common.DomainValidationError):
            self.common.nonnegative_amount("-0.01")

    def test_error_does_not_echo_untrusted_input(self):
        secret_like_input = "private-token-do-not-log"
        with self.assertRaises(self.common.DomainValidationError) as captured:
            self.common.decimal_value(secret_like_input)
        self.assertNotIn(secret_like_input, str(captured.exception))

    def test_aware_time_is_normalized_without_changing_the_instant(self):
        source = datetime(2026, 10, 5, 8, 30, tzinfo=timezone(timedelta(hours=8)))
        expected = datetime(2026, 10, 5, 0, 30, tzinfo=UTC)
        normalized = self.common.utc_datetime(source)
        self.assertEqual(normalized, expected)
        self.assertEqual(normalized.utcoffset(), timedelta(0))

    def test_naive_and_unparsed_time_are_rejected(self):
        for value in (datetime(2026, 10, 5), "2026-10-05T00:00:00Z", 1791158400, None):
            with self.subTest(value=type(value).__name__):
                with self.assertRaises(self.common.DomainValidationError):
                    self.common.utc_datetime(value)

    def test_identifiers_cannot_be_silently_merged_by_trimming(self):
        self.assertEqual(self.common.required_identifier("trade-123"), "trade-123")
        for value in ("", " ", " trade-123", "trade-123 ", None, 123):
            with self.subTest(value=type(value).__name__):
                with self.assertRaises(self.common.DomainValidationError):
                    self.common.required_identifier(value)


if __name__ == "__main__":
    unittest.main()
