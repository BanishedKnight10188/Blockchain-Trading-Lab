"""Stable identity for an explicitly scheduled review; no automatic strategy change."""

from hashlib import sha256
from json import dumps

from .reviews import ReviewKind


def review_job_identity(group_id: str, kind: ReviewKind, due_at) -> str:
    canonical = dumps([group_id, str(kind), due_at.isoformat()], separators=(",", ":"))
    return "review-job:" + sha256(canonical.encode()).hexdigest()
