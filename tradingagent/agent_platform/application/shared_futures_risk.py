"""Common discipline extracted verbatim from the JEV execution preflight."""

from decimal import Context, Decimal, localcontext

from agent_platform.ports.trading_execution import ExecutionRejected


class SharedFuturesRisk:
    @staticmethod
    def evaluate(run, original, current, account, action, quantity, leverage=None):
        with localcontext(Context(prec=80)):
            if (
                abs(current.mark - original.mark) / original.mark * 10000
                > run.policy.max_price_drift_bps
            ):
                raise ExecutionRejected("price_drift")
            buying = action == "open_long" or (action == "reduce" and account.side == "short")
            old, new = (original.ask, current.ask) if buying else (original.bid, current.bid)
            if abs(new - old) / old * 10000 > run.policy.max_price_drift_bps:
                raise ExecutionRejected("price_drift")
            if action == "reduce":
                if not account.quantity or quantity > account.quantity:
                    raise ExecutionRejected("position_changed")
                return
            if account.side is not None and account.side != (
                "long" if action == "open_long" else "short"
            ):
                raise ExecutionRejected("close_before_reverse")
            price = current.ask * (1 + run.limits.slippage_bps / 10000)
            fee = quantity * price * run.limits.fee_bps / 10000
            if (
                quantity < run.min_qty
                or quantity > run.max_qty
                or quantity % run.qty_step
                or quantity * current.bid < run.min_notional
            ):
                raise ExecutionRejected("contract_filter")
            if (
                max(price, current.mark) * (account.quantity + quantity)
                > run.limits.max_position_notional
            ):
                raise ExecutionRejected("position_limit")
            target = leverage or account.leverage or run.limits.leverage
            ceiling = run.limits.max_leverage or run.limits.leverage
            if target > ceiling:
                raise ExecutionRejected("leverage_limit")
            adjustment = account.entry_notional * (
                Decimal(1) / target - Decimal(1) / (account.leverage or run.limits.leverage)
            )
            if adjustment + quantity * price / target + fee > account.free_usdt:
                raise ExecutionRejected("insufficient_funds")
            pnl = (account.quantity * current.mark - account.entry_notional) * (
                1 if account.side != "short" else -1
            )
            equity = account.free_usdt + account.margin_usdt + pnl
            if (
                account.equity_usdt is None
                or run.limits.initial_usdt - equity + fee >= run.limits.max_run_loss_usdt
            ):
                raise ExecutionRejected("run_loss_limit")
