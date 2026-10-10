"""Bounded internal facts used to produce browser-safe record projections."""

from pydantic import Field, StrictBool

from .account import ObservedTrade
from .decisions import FeedbackReceipt
from .events import StateRecord
from .models import DomainModel
from .reports import ReportReceipt

MAX_LEDGER_SEQUENCE = 2**63 - 1


class TradeLedgerEntry(DomainModel):
    trade: ObservedTrade
    attribution: StateRecord


class LedgerData(DomainModel):
    trades: tuple[TradeLedgerEntry, ...] = Field(default=(), max_length=50)
    feedback: tuple[FeedbackReceipt, ...] = Field(default=(), max_length=50)
    reports: tuple[ReportReceipt, ...] = Field(default=(), max_length=50)
    next_trade_sequence: int = Field(default=0, strict=True, ge=0, le=MAX_LEDGER_SEQUENCE)
    has_more_trades: StrictBool = False
