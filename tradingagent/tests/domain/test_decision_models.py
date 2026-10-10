import importlib
from datetime import timedelta

import pytest

from tests.domain.test_decisions import NOW


def module():
    return importlib.import_module("agent_platform.domain.decision_models")


def questions():
    return (
        {
            "question_id": "gate",
            "kind": "choice",
            "instructions": "Does the candidate follow the supplied strategy?",
            "criteria": (
                {"key": "PASS", "description": "Supported"},
                {"key": "REVIEW", "description": "Ambiguous"},
                {"key": "ABSTAIN", "description": "Insufficient evidence"},
            ),
        },
        {
            "question_id": "quality",
            "kind": "score",
            "instructions": "How well supported is the explanation?",
            "criteria": (
                {"key": "0", "description": "Unsupported"},
                {"key": "1", "description": "Partial"},
                {"key": "2", "description": "Supported"},
            ),
        },
        {
            "question_id": "conflict",
            "kind": "noul",
            "instructions": "Are the supplied signals in conflict?",
            "criteria": (
                {"key": "true", "description": "Conflicting"},
                {"key": "false", "description": "Consistent"},
            ),
        },
    )


def request_data(**changes):
    value = {
        "request_id": "jev-1",
        "route": {
            "route_id": "jev-route",
            "kind": "standard",
            "purpose": "advisory",
            "reason": "decision_check",
            "model_version": "typesafe/jev-1.13",
            "price_version": "jev-price-v1",
            "paid": True,
        },
        "captured_at": NOW,
        "deadline": NOW + timedelta(seconds=15),
        "question_set_version": "strategy-gate-v1",
        "state_json": '{"evidence": "offline", "candidate": "hold"}',
        "questions": questions(),
        "max_output_tokens": 1024,
    }
    value.update(changes)
    return value


def test_request_and_criteria_are_immutable_bounded_and_roundtrip():
    request = module().DecisionModelRequest(**request_data())
    assert module().DecisionModelRequest.model_validate_json(request.model_dump_json()) == request
    with pytest.raises(ValueError):
        request.questions[0].criteria[0].key = "OTHER"
    assert isinstance(request.questions, tuple)


@pytest.mark.parametrize(
    "change",
    [
        {"deadline": NOW},
        {"state_json": '{"x":NaN}'},
        {"state_json": '{"x":1,"x":2}'},
        {"questions": ()},
        {"questions": questions() + (questions()[0],)},
        {"api_key": "never-export"},
    ],
)
def test_invalid_state_questions_or_credentials_are_rejected(change):
    with pytest.raises(ValueError):
        module().DecisionModelRequest(**request_data(**change))


@pytest.mark.parametrize(
    "probabilities",
    [
        ({"key": "PASS", "probability": "0.8"}, {"key": "REVIEW", "probability": "0.1"}),
        ({"key": "PASS", "probability": "1.1"},),
        ({"key": "PASS", "probability": "NaN"},),
    ],
)
def test_invalid_probability_distributions_cannot_be_valid_answers(probabilities):
    with pytest.raises(ValueError):
        module().DecisionAnswer(
            question_id="gate",
            kind="choice",
            choice="PASS",
            probabilities=probabilities,
            confidence="0.8",
        )
