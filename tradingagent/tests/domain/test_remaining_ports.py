"""Protocol consumers exchange owned DTOs with deterministic offline fixtures."""

import importlib
from datetime import UTC, datetime
from typing import get_type_hints

import pytest

from tests.domain.test_model_contracts import request_data


@pytest.mark.parametrize(
    "module,names",
    [
        ("model", ("ModelPort",)),
        ("advisory", ("AdvisoryPort",)),
        ("notification", ("NotificationPort",)),
        (
            "persistence",
            ("EventStorePort", "StateStorePort", "ObservationStorePort", "BudgetStorePort"),
        ),
    ],
)
def test_port_annotations_have_no_external_provider_types(module, names):
    ports = importlib.import_module(f"agent_platform.ports.{module}")
    for name in names:
        protocol = getattr(ports, name)
        for method_name, method in vars(protocol).items():
            if method_name.startswith("_") or not callable(method):
                continue
            for annotation in get_type_hints(method).values():
                origin = getattr(annotation, "__module__", "builtins")
                assert origin == "builtins" or origin.startswith(("agent_platform.domain", "types"))


@pytest.mark.asyncio
async def test_fake_model_rule_and_notice_obey_structural_ports():
    calls = importlib.import_module("agent_platform.domain.model_calls")
    decisions = importlib.import_module("agent_platform.domain.decisions")
    costs = importlib.import_module("agent_platform.domain.costs")
    events = importlib.import_module("agent_platform.domain.events")
    model_port = importlib.import_module("agent_platform.ports.model")
    advisory_port = importlib.import_module("agent_platform.ports.advisory")
    notification_port = importlib.import_module("agent_platform.ports.notification")
    notices = []

    class FakeModel:
        async def generate(self, request):
            return calls.ModelResponse(
                request_id=request.request_id,
                assessment=decisions.AdvisoryAssessment(
                    action="unavailable",
                    explanation="录制数据尚未预热",
                    source="fake",
                    unavailable_reasons=("warming",),
                ),
                usage=costs.ModelUsage(
                    request_id=request.request_id,
                    route_id=request.route.route_id,
                    model_version=request.route.model_version,
                    input_tokens=0,
                    output_tokens=0,
                    estimated_cost_usd="0",
                    actual_cost_usd="0",
                    billing_status="confirmed",
                    recorded_at=request.snapshot.captured_at,
                ),
            )

    class FakeRule:
        def evaluate(self, snapshot):
            return decisions.AdvisoryAssessment(
                action="unavailable",
                explanation="录制数据尚未预热",
                source="rule",
                unavailable_reasons=("warming",),
            )

    class FakeNotifier:
        async def publish(self, notice):
            notices.append(notice)

    request = calls.ModelRequest(**request_data())
    model, rule, notifier = FakeModel(), FakeRule(), FakeNotifier()
    assert isinstance(model, model_port.ModelPort)
    assert isinstance(rule, advisory_port.AdvisoryPort)
    assert isinstance(notifier, notification_port.NotificationPort)
    reply = await model.generate(request)
    assert reply.usage.actual_cost_usd == 0
    assert rule.evaluate(request.snapshot).action == reply.assessment.action
    notice = events.Notice(
        notice_id="notice-1",
        severity="warning",
        message="数据尚未预热",
        occurred_at=datetime(2026, 10, 5, tzinfo=UTC),
    )
    await notifier.publish(notice)
    assert notices == [notice]
