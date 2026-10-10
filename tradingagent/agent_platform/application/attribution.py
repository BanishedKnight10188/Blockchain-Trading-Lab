"""Freeze a user's explicit correction and submit it to the atomic fact store."""

from agent_platform.domain.attribution import AttributionChange
from agent_platform.domain.common import live_account_ref
from agent_platform.domain.reviews import AttributionReceipt
from agent_platform.ports.attribution import AttributionStorePort
from agent_platform.ports.clock import ClockPort


class AttributionService:
    def __init__(self, *, store: AttributionStorePort, clock: ClockPort, account_ref: str):
        self.store, self.clock, self.account_ref = store, clock, live_account_ref(account_ref)

    async def record(self, change: AttributionChange) -> AttributionReceipt:
        checked = AttributionChange.model_validate_json(change.model_dump_json())
        if checked.attribution.account_ref != self.account_ref:
            raise ValueError("attribution belongs to another account")
        return await self.store.record_attribution(checked)
