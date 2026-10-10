"""Keep the complete fee write owned through lock waits and repeated cancellation."""

import asyncio


async def settle_owned(budgets, reservation_id, usage):
    task = asyncio.create_task(budgets.settle(reservation_id, usage))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not task.cancelled():
            task.exception()
        raise
