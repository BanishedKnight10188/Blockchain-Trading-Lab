"""Stateless simulation from a frozen persisted virtual wallet."""

from .execution import PaperSimulator


class PaperExecution:
    async def simulate(self, account, intent, market):
        simulator = PaperSimulator(
            account.account_ref,
            balances={"USDT": account.usdt, "BTC": account.btc},
            fee_bps=account.settings.fee_bps,
            slippage_bps=account.settings.slippage_bps,
        )
        return await simulator.simulate(intent, market)
