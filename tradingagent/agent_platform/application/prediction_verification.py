"""A bounded diagnostic lane over the unchanged model and cumulative budget."""

import asyncio
from datetime import UTC, datetime

from agent_platform.ports.model import ModelCallFailed


class BoundedDecisionModel:
    def __init__(self, model, *, max_calls):
        if type(max_calls) is not int or not 1 <= max_calls <= 60:
            raise ValueError("verification call limit must be 1..60")
        self.model, self.max_calls = model, max_calls
        self.calls = self.in_flight = 0
        self.observations = []

    def __getattr__(self, name):
        return getattr(self.model, name)

    @property
    def limit_reached(self):
        return self.calls >= self.max_calls

    async def decide(self, request):
        # No await between the limit check and increment: concurrent coroutines
        # cannot reserve the same final slot. A canceled slot is never recycled.
        if self.limit_reached:
            raise ModelCallFailed("verification_complete")
        self.calls += 1
        self.in_flight += 1
        loop = asyncio.get_running_loop()
        started = loop.time()
        observation = {
            "request_id": request.request_id,
            "started_at": datetime.now(UTC).isoformat(),
            "started_monotonic": started,
            "outcome": "failed",
        }
        self.observations.append(observation)
        try:
            result = await self.model.decide(request)
            observation["outcome"] = "returned"
            return result
        except asyncio.CancelledError:
            observation["outcome"] = "cancelled"
            raise
        finally:
            observation["elapsed_ms"] = round((loop.time() - started) * 1000, 1)
            self.in_flight -= 1
