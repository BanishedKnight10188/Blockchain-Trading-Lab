"""Later committed market evidence belongs only to an explicit retrospective version."""

from datetime import timedelta

import pytest

from agent_platform.adapters.sqlite.decisions import SqliteDecisionStore
from agent_platform.domain.decision_requests import DecisionRequest
from tests.adapters.test_sqlite_decisions import completion, request, snapshot
from tests.adapters.test_sqlite_decisions import context as _context
from tests.application.test_reviews import service
from tests.domain.test_decisions import NOW

context = _context


async def publish(context, clock, second=11, identity="context-request"):
    at = NOW + timedelta(seconds=second)
    clock.advance_to(at)
    old = request(context[-1])
    candidate = DecisionRequest.model_validate(
        {
            **old.model_dump(),
            "request_id": identity,
            "snapshot": snapshot(context[-1], identity + "-original", at),
            "requested_at": at,
            "deadline": NOW + timedelta(seconds=30),
        }
    )
    completed = completion(context[-1], at)
    publication = snapshot(context[-1], identity + "-publication", at)
    result = completed.result.model_copy(
        update={
            "request_id": candidate.request_id,
            "snapshot_id": candidate.snapshot.snapshot_id,
            "publication_snapshot_id": publication.snapshot_id,
            "risk": completed.result.risk.model_copy(
                update={"snapshot_id": publication.snapshot_id}
            ),
            "recommendation": completed.result.recommendation.model_copy(
                update={
                    "recommendation_id": identity + "-advice",
                    "snapshot_id": candidate.snapshot.snapshot_id,
                }
            ),
        }
    )
    completed = completed.model_copy(update={"result": result, "publication_snapshot": publication})
    store = SqliteDecisionStore(context[0], clock=clock)
    await store.claim(candidate)
    result = await store.finish(candidate.request_id, completed)
    assert result.status == "published"
    return (await store.latest_decision("session-1")).completion.publication_snapshot


@pytest.mark.asyncio
async def test_followup_freezes_later_snapshot_and_keeps_initial_unchanged(context):
    value, store, clock = await service(context)
    await value.create_group("group-1", clock.utcnow())
    await value.generate("group-1", clock.utcnow(), "initial")
    initial = (await store.review_versions("group-1", account_ref="local-spot"))[0]
    later = await publish(context, clock)
    await value.generate("group-1", clock.utcnow(), "followup")
    versions = await store.review_versions("group-1", account_ref="local-spot")
    assert versions[0] == initial and initial.retrospective_context is None
    assert versions[1].retrospective_context.snapshot == later
    assert versions[1].retrospective_context.request_id == "context-request"
    assert versions[1].review.evidence_ids[-1] == "snapshot:" + later.snapshot_id
    assert versions[1].facts == initial.facts
    assert versions[1].review.model_participated is False


@pytest.mark.asyncio
@pytest.mark.parametrize("second", [12, 11.000001])
async def test_snapshot_after_cutoff_is_absent_even_when_generation_is_delayed(context, second):
    value, store, clock = await service(context)
    await value.create_group("group-1", clock.utcnow())
    await value.generate("group-1", clock.utcnow(), "initial")
    cutoff = NOW + timedelta(seconds=11)
    await publish(context, clock, second=second)
    await value.generate("group-1", cutoff, "followup")
    version = (await store.review_versions("group-1", account_ref="local-spot"))[-1]
    assert version.retrospective_context is None
    assert version.review.evidence_ids == ("trade:1", "trade:2")


@pytest.mark.asyncio
async def test_later_context_does_not_reselect_sources_for_old_version(context):
    value, store, clock = await service(context)
    await value.create_group("group-1", clock.utcnow())
    await value.generate("group-1", clock.utcnow(), "initial")
    first_snapshot = await publish(context, clock)
    cutoff = clock.utcnow()
    await value.generate("group-1", cutoff, "followup")
    frozen = (await store.review_versions("group-1", account_ref="local-spot"))[-1]
    second_snapshot = await publish(context, clock, second=12, identity="second-context")
    assert await value.generate("group-1", cutoff, "followup") == frozen.review
    await value.generate("group-1", clock.utcnow(), "manual")
    versions = await store.review_versions("group-1", account_ref="local-spot")
    assert versions[1] == frozen and versions[1].retrospective_context.snapshot == first_snapshot
    assert versions[2].retrospective_context.snapshot == second_snapshot
