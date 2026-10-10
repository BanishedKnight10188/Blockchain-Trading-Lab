"""Local persistence adapters with one four-port assembly entry point."""

from .store import SqliteStore as SqliteStore
from .store import open_store as open_store
