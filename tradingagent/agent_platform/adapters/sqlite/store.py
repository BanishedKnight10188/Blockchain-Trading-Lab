"""One concrete adapter exposes the four owned persistence ports."""

from pathlib import Path

from .budgets import SqliteBudgetStore
from .review_jobs import SqliteReviewJobStore


class SqliteStore(SqliteReviewJobStore, SqliteBudgetStore):
    """A single connection policy, IO lock and migration registry for all operations."""


async def open_store(path: Path) -> SqliteStore:
    store = SqliteStore(path)
    await store.initialize()
    return store
