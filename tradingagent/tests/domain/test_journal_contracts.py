"""Typed state and journal records reject arbitrary secret-bearing payloads."""

import importlib
from datetime import UTC, datetime

import pytest

from agent_platform.domain.sessions import AgentSession

NOW = datetime(2026, 10, 5, tzinfo=UTC)


def session():
    return AgentSession(
        session_id="session-1", style={"strength": 67}, created_at=NOW, updated_at=NOW
    )


def test_state_and_journal_round_trip_only_owned_types():
    events = importlib.import_module("agent_platform.domain.events")
    record = events.StateRecord(
        key="session-1", revision=1, updated_at=NOW, state_type="session", state=session()
    )
    event = events.JournalEvent(
        event_id="event-1",
        aggregate_id=record.key,
        kind="state_changed",
        payload=record,
        occurred_at=NOW,
    )
    restored = events.JournalEvent.model_validate_json(event.model_dump_json())
    assert restored == event
    assert isinstance(restored.payload.state, AgentSession)


def test_event_kind_scope_and_payload_cannot_disagree():
    events = importlib.import_module("agent_platform.domain.events")
    base = {"event_id": "event-1", "aggregate_id": "session-1", "occurred_at": NOW}
    with pytest.raises(ValueError):
        events.JournalEvent(**base, kind="session_changed", payload={"api_secret": "never-store"})
    with pytest.raises(ValueError):
        events.JournalEvent(**base, kind="trades_imported", payload=session())
    with pytest.raises(ValueError):
        events.JournalEvent(
            **{**base, "aggregate_id": "another-session"}, kind="session_changed", payload=session()
        )


def test_state_tag_must_match_the_owned_state():
    events = importlib.import_module("agent_platform.domain.events")
    with pytest.raises(ValueError):
        events.StateRecord(
            key="session-1",
            revision=1,
            updated_at=NOW,
            state_type="recommendation",
            state=session(),
        )


def test_event_page_is_a_forward_sequence_not_an_unordered_list():
    events = importlib.import_module("agent_platform.domain.events")
    event = events.JournalEvent(
        event_id="event-1",
        aggregate_id="session-1",
        kind="session_changed",
        payload=session(),
        occurred_at=NOW,
    )
    item = events.EventRecord(sequence=2, event=event)
    with pytest.raises(ValueError):
        events.EventPage(records=(item, item), next_sequence=2)
    with pytest.raises(ValueError):
        events.EventPage(records=(item,), next_sequence=1)
    page = events.EventPage(records=(item,), next_sequence=2)
    assert page.next_sequence == 2


def test_state_envelope_cannot_lie_about_an_existing_internal_revision():
    events = importlib.import_module("agent_platform.domain.events")
    with pytest.raises(ValueError):
        events.StateRecord(
            key="session-1", revision=2, updated_at=NOW, state_type="session", state=session()
        )


def test_attribution_aggregate_is_distinct_for_each_exchange_scope():
    events = importlib.import_module("agent_platform.domain.events")
    reviews = importlib.import_module("agent_platform.domain.reviews")
    first = reviews.TradeAttribution(account_ref="account-1", symbol="BTCUSDT", trade_id="1")
    other = reviews.TradeAttribution(account_ref="account-2", symbol="ETHUSDT", trade_id="1")
    assert first.aggregate_id != other.aggregate_id
    assert first.identity != other.identity
    for index, attribution in enumerate((first, other)):
        recorded = events.JournalEvent(
            event_id=f"attribution-{index}",
            aggregate_id=attribution.aggregate_id,
            kind="attribution_recorded",
            payload=attribution,
            occurred_at=NOW,
        )
        assert events.JournalEvent.model_validate_json(recorded.model_dump_json()) == recorded
        with pytest.raises(ValueError):
            events.JournalEvent(**{**recorded.model_dump(), "aggregate_id": "1"})


def test_frozen_decision_evidence_has_a_typed_durable_journal_path():
    from agent_platform.domain.decisions import DecisionSnapshot
    from tests.domain.test_decisions import snapshot_data

    events = importlib.import_module("agent_platform.domain.events")
    snapshot = DecisionSnapshot(**snapshot_data())
    recorded = events.JournalEvent(
        event_id="snapshot-record-1",
        aggregate_id=snapshot.snapshot_id,
        kind="decision_snapshot_recorded",
        payload=snapshot,
        occurred_at=NOW,
    )
    restored = events.JournalEvent.model_validate_json(recorded.model_dump_json())
    assert restored.payload == snapshot
    assert restored.payload.style.strength == 67
    assert restored.payload.captured_at == NOW
    with pytest.raises(ValueError):
        events.JournalEvent(**{**recorded.model_dump(), "kind": "recommendation_recorded"})


@pytest.mark.parametrize(
    "changes", [{"key": "another-session"}, {"updated_at": NOW.replace(hour=1)}]
)
def test_state_identity_and_state_timestamp_cannot_disagree(changes):
    events = importlib.import_module("agent_platform.domain.events")
    with pytest.raises(ValueError):
        events.StateRecord(
            **{
                "key": "session-1",
                "revision": 1,
                "updated_at": NOW,
                "state_type": "session",
                "state": session(),
                **changes,
            }
        )
