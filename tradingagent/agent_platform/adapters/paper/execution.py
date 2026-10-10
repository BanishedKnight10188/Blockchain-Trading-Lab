"""A simple deterministic fill model, not an exchange matching engine."""

from collections.abc import Mapping
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext

from agent_platform.domain.account import Balance
from agent_platform.domain.common import exact_add, nonnegative_amount, required_identifier
from agent_platform.domain.market import MarketSnapshot
from agent_platform.domain.paper import PaperFill, PaperIntent


class PaperSimulator:
    model_version = "paper-top-of-book-v1"

    def __init__(
        self,
        account_ref: str,
        *,
        balances: Mapping[str, str | Decimal],
        fee_bps: str | Decimal = "10",
        slippage_bps: str | Decimal = "0",
    ):
        self.account_ref = required_identifier(account_ref)
        if not self.account_ref.startswith("paper:") or self.account_ref == "paper:":
            raise ValueError("simulation requires an independent paper account")
        self.fee_bps = nonnegative_amount(fee_bps)
        self.slippage_bps = nonnegative_amount(slippage_bps)
        if self.fee_bps >= 10000 or self.slippage_bps >= 10000:
            raise ValueError("simulation bps must be below 10000")
        if set(balances) - {"BTC", "USDT"}:
            raise ValueError("M0 simulation supports only BTC and USDT")
        self._balances = {
            asset: nonnegative_amount(balances.get(asset, "0")) for asset in ("BTC", "USDT")
        }
        self._committed: dict[str, tuple[PaperIntent, PaperFill]] = {}
        self._last_fill_at = None

    @property
    def balances(self) -> tuple[Balance, ...]:
        return tuple(
            Balance(asset=asset, free=value, locked="0")
            for asset, value in sorted(self._balances.items())
        )

    @property
    def fills(self) -> tuple[PaperFill, ...]:
        return tuple(item[1] for item in self._committed.values())

    def balance(self, asset: str) -> Decimal:
        return self._balances.get(required_identifier(asset), Decimal("0"))

    async def simulate(self, intent: PaperIntent, snapshot: MarketSnapshot) -> PaperFill:
        request = PaperIntent.model_validate_json(intent.model_dump_json())
        market = MarketSnapshot.model_validate_json(snapshot.model_dump_json())
        if request.account_ref != self.account_ref or request.symbol != "BTCUSDT":
            raise ValueError("paper request does not match this simulator scope")
        previous = self._committed.get(request.intent_id)
        if previous:
            if previous[0] != request:
                raise ValueError("paper intent identifier already refers to different inputs")
            return previous[1]
        if (
            market.symbol != request.symbol
            or market.status != "ready"
            or market.as_of < request.created_at
            or market.latest_quote_at is None
            or (self._last_fill_at is not None and market.as_of < self._last_fill_at)
            or (market.as_of - market.latest_received_at).total_seconds() > 5
            or (market.as_of - market.latest_quote_at).total_seconds() > 5
        ):
            raise ValueError("paper fill requires a current scoped quote after the intent")
        if market.book is not None and (
            market.book_as_of is None or (market.as_of - market.book_as_of).total_seconds() > 5
        ):
            raise ValueError("paper fill requires a current book timestamp")
        buying = request.side == "buy"
        reference = (
            (market.book.ask if buying else market.book.bid)
            if market.book
            else market.latest_trade.price
        )
        with localcontext(Context(prec=34, rounding=ROUND_HALF_EVEN)):
            direction = Decimal("1") if buying else Decimal("-1")
            price = reference * (1 + direction * self.slippage_bps / 10000)
            notional = price * request.quantity
            fee = notional * self.fee_bps / 10000
        if request.limit_price is not None and (
            (buying and price > request.limit_price) or (not buying and price < request.limit_price)
        ):
            raise ValueError("simulated fill would violate the limit price")
        debit = exact_add(notional, fee)
        if (buying and debit > self.balance("USDT")) or (
            not buying and request.quantity > self.balance("BTC")
        ):
            raise ValueError("paper balance is insufficient")
        fill = PaperFill(
            fill_id="paper-fill:" + request.intent_id,
            intent_id=request.intent_id,
            mode="paper",
            account_ref=self.account_ref,
            symbol=request.symbol,
            side=request.side,
            quantity=request.quantity,
            price=price,
            fee=fee,
            fee_asset="USDT",
            slippage_bps=self.slippage_bps,
            model_version=self.model_version,
            filled_at=market.as_of,
        )
        base_change = request.quantity if buying else request.quantity.copy_negate()
        quote_change = debit.copy_negate() if buying else exact_add(notional, fee.copy_negate())
        # No await occurs between validation and commit: one process cannot fill
        # the same intent twice through concurrent coroutines.
        self._balances["BTC"] = exact_add(self.balance("BTC"), base_change)
        self._balances["USDT"] = exact_add(self.balance("USDT"), quote_change)
        self._committed[request.intent_id] = request, fill
        self._last_fill_at = fill.filled_at
        return fill
