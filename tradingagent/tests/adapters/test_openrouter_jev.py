import importlib
import json
from decimal import Decimal

import httpx
import pytest

from agent_platform.adapters.fake.clock import FakeClock
from agent_platform.adapters.openrouter.transport import OpenRouterClient, OpenRouterCredentials
from agent_platform.domain.routing import ModelCostQuote
from tests.domain.test_decision_models import request_data
from tests.domain.test_decisions import NOW


def module():
    return importlib.import_module("agent_platform.adapters.openrouter.jev")


def response():
    return {
        "id": "gen-dec-offline",
        "model": "typesafe/jev-1.13-20260917",
        "provider": "TypeSafe",
        "answers": {
            "gate": {
                "type": "choice",
                "choice": "PASS",
                "probabilities": {"PASS": 0.8, "REVIEW": 0.1, "ABSTAIN": 0.1},
                "confidence": 0.7,
            },
            "quality": {
                "type": "score",
                "score": 1.8,
                "probabilities": {"0": 0, "1": 0.2, "2": 0.8},
                "confidence": 0.75,
            },
            "conflict": {"type": "noul", "noul": 0.1},
        },
        "usage": {"input_tokens": 200, "output_tokens": 120, "cost": 0.0000084},
    }


async def run(payload, *, status=200, request_changes=None):
    models = importlib.import_module("agent_platform.domain.decision_models")
    request = models.DecisionModelRequest(**request_data(**(request_changes or {})))
    quote = ModelCostQuote(
        route_id="jev-route",
        price_version="jev-price-v1",
        input_token_upper_bound=3000,
        max_output_tokens=1024,
        estimated_cost_usd="0.000126",
    )
    seen = []

    def handler(r):
        seen.append(json.loads(r.content))
        return httpx.Response(status, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = OpenRouterClient(
            FakeClock(NOW),
            credentials=OpenRouterCredentials(api_key="sk-or-v1-offline-jev-key"),
            enabled=True,
            client=http,
        )
        reply = (
            await module().OpenRouterDecisionModel(client, FakeClock(NOW)).decide(request, quote)
        )
    return reply, seen


@pytest.mark.asyncio
async def test_choice_score_noul_are_typed_and_actual_version_is_retained():
    reply, seen = await run(response())
    assert reply.answers[0].choice == "PASS"
    assert reply.answers[1].score == Decimal("1.8")
    assert reply.answers[2].noul == Decimal("0.1")
    assert reply.provider_metadata.model_id == "typesafe/jev-1.13-20260917"
    assert reply.usage.actual_cost_usd == Decimal("0.0000084")
    assert seen[0]["questions"]["quality"]["criteria"] == ["Unsupported", "Partial", "Supported"]
    assert seen[0]["questions"]["conflict"]["criteria"]["true"] == "Conflicting"
    assert "request_id" not in seen[0] and "route" not in seen[0]


@pytest.mark.asyncio
async def test_forbidden_preserves_access_failure_without_inventing_fee_evidence():
    from agent_platform.ports.model import ModelCallFailed

    with pytest.raises(ModelCallFailed) as error:
        await run(
            {"error": {"code": 403, "message": "This model is not available in your region."}},
            status=403,
        )
    assert error.value.reason == "provider_access_denied"
    assert error.value.usage is None


@pytest.mark.parametrize("status", [401, 402, 429, 500, 503])
@pytest.mark.asyncio
async def test_provider_http_status_is_retained_without_body_or_false_zero_fee(status):
    from agent_platform.ports.model import ModelCallFailed

    with pytest.raises(ModelCallFailed) as error:
        await run({"error": {"message": "sk-or-v1-offline-do-not-export"}}, status=status)
    assert error.value.reason == f"provider_http_{status}"
    assert error.value.usage is None
    assert "sk-or-v1-" not in str(error.value)


@pytest.mark.parametrize(
    "change", ["missing", "extra", "choice", "type", "distribution", "score", "version"]
)
@pytest.mark.asyncio
async def test_invalid_judgment_keeps_fee_but_is_not_delivered(change):
    value = response()
    if change == "missing":
        value["answers"].pop("conflict")
    elif change == "extra":
        value["answers"]["other"] = value["answers"]["conflict"]
    elif change == "choice":
        value["answers"]["gate"]["choice"] = "UNKNOWN"
    elif change == "type":
        value["answers"]["gate"]["type"] = "noul"
    elif change == "distribution":
        value["answers"]["gate"]["probabilities"]["PASS"] = 0.5
    elif change == "score":
        value["answers"]["quality"]["score"] = 0.2
    else:
        value["model"] = "typesafe/jev-new"
    with pytest.raises(Exception) as error:
        await run(value)
    assert isinstance(
        error.value, importlib.import_module("agent_platform.ports.model").ModelCallFailed
    )
    assert error.value.usage.actual_cost_usd == Decimal("0.0000084")


@pytest.mark.parametrize(
    "change,stage,index",
    [
        ("questions", "question_set", None),
        ("type", "answer_type", 0),
        ("criteria", "criteria", 0),
        ("distribution", "answer_values", 0),
        ("legend", "legend", 1),
        ("choice", "answer_values", 0),
        ("metadata", "metadata", None),
        ("usage", "usage", None),
    ],
)
@pytest.mark.asyncio
async def test_failure_diagnostic_identifies_stage_without_exporting_provider_content(
    change, stage, index
):
    from agent_platform.ports.model import ModelCallFailed

    value = response()
    secret = "sk-or-v1-do-not-export-provider-text"
    if change == "questions":
        value["answers"][secret] = {}
    elif change == "type":
        value["answers"]["gate"]["type"] = secret
    elif change == "criteria":
        value["answers"]["gate"]["probabilities"][secret] = 0
    elif change == "distribution":
        value["answers"]["gate"]["probabilities"]["PASS"] = 0.5
    elif change == "legend":
        value["answers"]["quality"]["legend"] = {secret: secret}
    elif change == "choice":
        value["answers"]["gate"]["choice"] = secret
    elif change == "metadata":
        value["model"] = secret
    else:
        value["usage"]["cost"] = secret
    with pytest.raises(ModelCallFailed) as caught:
        await run(value)
    diagnostic = getattr(caught.value, "diagnostic", None)
    assert diagnostic is not None
    assert diagnostic.stage == stage and diagnostic.question_index == index
    assert secret not in diagnostic.model_dump_json()
    if change != "usage":
        assert caught.value.usage.actual_cost_usd == Decimal("0.0000084")


@pytest.mark.parametrize("total", ["0.99", "1.01"])
@pytest.mark.asyncio
async def test_rounded_choice_distribution_is_bounded_and_original_is_archived(total):
    value = response()
    value["answers"]["gate"]["probabilities"]["PASS"] = str(Decimal(total) - Decimal("0.2"))
    reply, _ = await run(value)
    assert reply.answers[0].choice == "PASS"
    assert reply.answers[0].confidence == Decimal("0.7")
    assert abs(sum(p.probability for p in reply.answers[0].probabilities) - 1) < Decimal("0.000001")
    adjustment = reply.choice_probability_adjustments[0]
    assert adjustment.question_id == "gate"
    assert adjustment.original_total == Decimal(total)
    assert adjustment.original_probabilities[0].probability == Decimal(total) - Decimal("0.2")
    assert type(reply).model_validate_json(reply.model_dump_json()) == reply


@pytest.mark.asyncio
async def test_valid_original_distribution_keeps_original_serialization():
    reply, _ = await run(response())
    assert "choice_probability_adjustments" not in json.loads(reply.model_dump_json())


@pytest.mark.parametrize("case", ["sum", "negative", "range", "choice"])
@pytest.mark.asyncio
async def test_rounding_never_repairs_invalid_values_or_a_different_winner(case):
    from agent_platform.ports.model import ModelCallFailed

    value = response()
    raw = value["answers"]["gate"]
    if case == "sum":
        raw["probabilities"]["PASS"] = "0.78"  # 0.98 exceeds the compatibility limit.
    elif case == "negative":
        raw["probabilities"]["REVIEW"] = "-0.01"
    elif case == "range":
        raw["probabilities"]["PASS"] = "1.01"
    else:
        raw["probabilities"] = {"PASS": "0.1", "REVIEW": "0.79", "ABSTAIN": "0.1"}
    with pytest.raises(ModelCallFailed) as caught:
        await run(value)
    assert caught.value.reason == "invalid_model_assessment"
    assert caught.value.usage.actual_cost_usd == Decimal("0.0000084")
    if case == "sum":
        assert caught.value.diagnostic.issue == "probability_sum"
        assert caught.value.diagnostic.probability_total == Decimal("0.98")


@pytest.mark.asyncio
async def test_real_nineteen_option_failure_replays_without_network():
    models = importlib.import_module("agent_platform.domain.decision_models")
    criteria = ["WAIT"] + [
        f"OPEN_{direction}_M{margin}_L{leverage}"
        for margin, leverages in [(5, (1, 2, 5, 10)), (10, (1, 2, 5)), (20, (1, 2))]
        for leverage in leverages
        for direction in ("LONG", "SHORT")
    ]
    weights = {key: "0" for key in criteria}
    weights.update(
        WAIT="0.81",
        OPEN_LONG_M5_L1="0.11",
        OPEN_SHORT_M5_L1="0.03",
        OPEN_LONG_M10_L1="0.01",
        OPEN_SHORT_M10_L1="0.01",
        OPEN_LONG_M20_L1="0.02",
    )
    value = response()
    value["answers"] = {
        "plan": {"type": "choice", "choice": "WAIT", "confidence": "0.79", "probabilities": weights}
    }
    question = models.DecisionQuestion(
        question_id="plan",
        kind="choice",
        instructions="Select an offline plan.",
        criteria=tuple({"key": key, "description": key} for key in criteria),
    )
    reply, _ = await run(value, request_changes={"questions": (question,)})
    assert reply.answers[0].choice == "WAIT" and reply.answers[0].confidence == Decimal("0.79")
    assert reply.choice_probability_adjustments[0].original_total == Decimal("0.99")
    assert len(reply.answers[0].probabilities) == 19


@pytest.mark.asyncio
async def test_adjustment_evidence_cannot_be_forged_or_dropped_from_nested_response():
    value = response()
    value["answers"]["gate"]["probabilities"]["PASS"] = "0.79"
    reply, _ = await run(value)
    data = json.loads(reply.model_dump_json())
    data["choice_probability_adjustments"][0]["original_total"] = "1"
    with pytest.raises(ValueError):
        type(reply).model_validate_json(json.dumps(data))
