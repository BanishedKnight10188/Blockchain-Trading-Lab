"""Explicit correction of local classifications; no exchange write methods."""

from typing import Protocol

from agent_platform.domain.attribution import AttributionChange
from agent_platform.domain.reviews import AttributionReceipt


class AttributionStorePort(Protocol):
    async def attribution_change(
        self, operation_id: str, *, account_ref: str
    ) -> AttributionReceipt | None: ...

    async def record_attribution(self, change: AttributionChange) -> AttributionReceipt: ...
