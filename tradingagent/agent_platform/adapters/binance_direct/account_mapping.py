"""Strict Spot REST mapping; exchange permissions are never execution capabilities."""

from datetime import datetime

from agent_platform.adapters.binance_direct.normalizer import _amount, _symbol, _timestamp
from agent_platform.domain.account import AccountSnapshot, ObservedOrder, ObservedTrade
from agent_platform.domain.common import required_identifier


class AccountMappingError(ValueError):
    pass


def exchange_id(value: int) -> str:
    if type(value) is not int or not 0 <= value <= 2**63 - 1:
        raise ValueError("expected bounded exchange integer identity")
    return str(value)


def _asset(value: str) -> str:
    result = required_identifier(value)
    if len(result) > 128 or any(ord(char) < 32 or ord(char) == 127 for char in result):
        raise ValueError("invalid asset identity")
    return result


def _side(value: str) -> str:
    return {"BUY": "buy", "SELL": "sell"}[value]


def map_account(raw: dict, account_ref: str, received_at: datetime) -> AccountSnapshot:
    try:
        if type(raw) is not dict or raw["accountType"] != "SPOT":
            raise ValueError("unsupported account type")
        balances = raw["balances"]
        if type(balances) is not list or len(balances) > 2000:
            raise ValueError("invalid balance collection")
        return AccountSnapshot(
            account_ref=account_ref,
            as_of=received_at,
            balances=tuple(
                {
                    "asset": _asset(row["asset"]),
                    "free": _amount(row["free"]),
                    "locked": _amount(row["locked"]),
                }
                for row in balances
            ),
        )
    except (ValueError, KeyError, TypeError, AttributeError, OverflowError):
        raise AccountMappingError("invalid Binance Spot account payload") from None


def map_order(raw: dict, account_ref: str) -> ObservedOrder:
    try:
        status = {
            "NEW": "new",
            "PARTIALLY_FILLED": "partially_filled",
            "FILLED": "filled",
            "CANCELED": "canceled",
            "REJECTED": "rejected",
            "EXPIRED": "expired",
            "EXPIRED_IN_MATCH": "expired",
        }[raw["status"]]
        return ObservedOrder(
            account_ref=account_ref,
            symbol=_symbol(raw["symbol"]),
            order_id=exchange_id(raw["orderId"]),
            side=_side(raw["side"]),
            status=status,
            quantity=_amount(raw["origQty"]),
            filled_quantity=_amount(raw["executedQty"]),
            price=_amount(raw["price"]),
            updated_at=_timestamp(raw["updateTime"]),
        )
    except (ValueError, KeyError, TypeError, AttributeError, OverflowError):
        raise AccountMappingError("invalid Binance Spot order payload") from None


def map_trade(raw: dict, account_ref: str) -> ObservedTrade:
    try:
        if type(raw["isBuyer"]) is not bool:
            raise ValueError("expected wire boolean")
        return ObservedTrade(
            account_ref=account_ref,
            symbol=_symbol(raw["symbol"]),
            trade_id=exchange_id(raw["id"]),
            order_id=exchange_id(raw["orderId"]),
            side="buy" if raw["isBuyer"] else "sell",
            price=_amount(raw["price"]),
            quantity=_amount(raw["qty"]),
            quote_quantity=_amount(raw["quoteQty"]),
            fee=_amount(raw["commission"]),
            fee_asset=_asset(raw["commissionAsset"]),
            executed_at=_timestamp(raw["time"]),
        )
    except (ValueError, KeyError, TypeError, AttributeError, OverflowError):
        raise AccountMappingError("invalid Binance Spot trade payload") from None
