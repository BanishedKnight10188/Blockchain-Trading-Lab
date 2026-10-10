"""Strict values shared by domain models, independent of any provider or framework."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import MAX_EMAX, MIN_EMIN, Decimal, InvalidOperation, localcontext
from enum import StrEnum

type DecimalInput = Decimal | str | int


class DomainValidationError(ValueError):
    """Invalid owned input; messages never include the original input value."""


class RunMode(StrEnum):
    ADVISORY = "advisory"
    READ_ONLY = "read_only"
    PAPER = "paper"


class MarketType(StrEnum):
    SPOT = "spot"
    FUTURES = "futures"


class DecisionOrigin(StrEnum):
    HUMAN = "human"
    AGENT = "agent"
    UNCLASSIFIED = "unclassified"


class DecisionPurpose(StrEnum):
    ADVISORY = "advisory"
    REVIEW = "review"


def decimal_value(value: DecimalInput) -> Decimal:
    """Parse exact finite numbers. Floating-point values are intentionally rejected."""
    if isinstance(value, bool) or not isinstance(value, (Decimal, str, int)):
        raise DomainValidationError("financial values require Decimal, string or integer")
    try:
        result = value if isinstance(value, Decimal) else Decimal(value)
    except InvalidOperation:
        raise DomainValidationError("financial value is not a valid decimal") from None
    if not result.is_finite():
        raise DomainValidationError("financial value must be finite")
    return result


def positive_amount(value: DecimalInput) -> Decimal:
    """Validate quantities and prices that must be strictly positive."""
    result = decimal_value(value)
    if result <= 0:
        raise DomainValidationError("amount must be greater than zero")
    return result


def nonnegative_amount(value: DecimalInput) -> Decimal:
    """Validate balances, costs and cumulative fills without accepting a deficit."""
    result = decimal_value(value)
    if result < 0:
        raise DomainValidationError("amount must be nonnegative")
    return result


def exact_add(left: Decimal, right: Decimal) -> Decimal:
    """Add finite values with enough digits, regardless of the caller's context."""
    values = (decimal_value(left), decimal_value(right))
    nonzero = tuple(value for value in values if value)
    if not nonzero:
        return Decimal(0)
    lowest_exponent = min(value.as_tuple().exponent for value in nonzero)
    highest_digit = max(value.adjusted() for value in nonzero)
    with localcontext() as context:
        context.prec = max(1, highest_digit - lowest_exponent + 2)
        context.Emax = MAX_EMAX
        context.Emin = MIN_EMIN
        return values[0] + values[1]


def utc_datetime(value: datetime) -> datetime:
    """Require a known instant; adapters must parse transport timestamps first."""
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise DomainValidationError("timestamp must be a timezone-aware datetime")
    return value.astimezone(UTC)


def required_identifier(value: str) -> str:
    """Preserve opaque identity and reject missing or whitespace-ambiguous keys."""
    if not isinstance(value, str) or not value or value != value.strip():
        raise DomainValidationError("identifier must be a nonempty string without outer whitespace")
    return value


def live_account_ref(value: str) -> str:
    """Reserve the simulation prefix so actual facts cannot claim a paper account."""
    result = required_identifier(value)
    if result.startswith("paper:"):
        raise DomainValidationError("actual exchange facts cannot use a paper account namespace")
    return result
