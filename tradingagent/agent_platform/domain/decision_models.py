"""Immutable typed decision questions; probabilities are not trading win rates."""

import json
import re
from decimal import Context, Decimal, localcontext
from typing import Annotated, Literal, Self

from pydantic import BeforeValidator, Field, model_serializer, model_validator

from .costs import ModelUsage, RouteDecision
from .model_calls import ProviderMetadata
from .model_diagnostics import ModelLocalTiming, ModelTransportEvidence
from .models import DomainModel, FiniteDecimal, Identifier, UtcDateTime


def _probability(value):
    if type(value) not in (str, int, Decimal):
        raise ValueError("probability requires an exact number")
    result = Decimal(value)
    if (
        not result.is_finite()
        or not 0 <= result <= 1
        or len(result.as_tuple().digits) > 20
        or not -32 <= result.as_tuple().exponent <= 32
    ):
        raise ValueError("probability is outside bounds")
    return result


Probability = Annotated[Decimal, BeforeValidator(_probability)]
QuestionKey = Annotated[
    str, Field(strict=True, min_length=1, max_length=64, pattern=r"^[A-Za-z][A-Za-z0-9_-]*$")
]
DecisionKind = Literal["choice", "score", "noul"]


class DecisionCriterion(DomainModel):
    key: str = Field(strict=True, min_length=1, max_length=64)
    description: str = Field(strict=True, min_length=1, max_length=512)

    @model_validator(mode="after")
    def bounded_text(self) -> Self:
        if re.fullmatch(r"[A-Za-z0-9_-]+", self.key) is None or not self.description.strip():
            raise ValueError("invalid criterion")
        return self


class DecisionQuestion(DomainModel):
    question_id: QuestionKey
    kind: DecisionKind
    instructions: str = Field(strict=True, min_length=1, max_length=2048)
    criteria: tuple[DecisionCriterion, ...] = Field(default=(), max_length=255)

    @model_validator(mode="after")
    def coherent_criteria(self) -> Self:
        keys = tuple(item.key for item in self.criteria)
        if not self.instructions.strip() or len(set(keys)) != len(keys):
            raise ValueError("invalid decision question")
        if self.kind in ("choice", "score") and len(keys) < 2:
            raise ValueError("multiple criteria are required")
        if self.kind == "score" and (
            len(keys) > 10 or keys != tuple(str(i) for i in range(len(keys)))
        ):
            raise ValueError("score criteria must be ordered from zero")
        if self.kind == "noul" and keys and set(keys) != {"true", "false"}:
            raise ValueError("noul criteria require true and false")
        return self


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate state field")
        result[key] = value
    return result


def _constant(value):
    raise ValueError("nonfinite state")


class DecisionModelRequest(DomainModel):
    request_id: Identifier
    route: RouteDecision
    captured_at: UtcDateTime
    deadline: UtcDateTime
    response_deadline: UtcDateTime | None = None
    question_set_version: Identifier
    state_json: str = Field(strict=True, min_length=2, max_length=32768)
    questions: tuple[DecisionQuestion, ...] = Field(min_length=1, max_length=8)
    max_output_tokens: int = Field(default=1024, strict=True, ge=1, le=8192)

    @model_serializer(mode="wrap")
    def preserve_old_request_shape(self, handler):
        value = handler(self)
        if self.response_deadline is None:
            value.pop("response_deadline", None)
        return value

    @model_validator(mode="after")
    def bounded_request(self) -> Self:
        if (
            self.deadline <= self.captured_at
            or (self.deadline - self.captured_at).total_seconds() > 15
        ):
            raise ValueError("decision request must have a finite fifteen-second window")
        if self.response_deadline is not None and (
            self.response_deadline < self.deadline
            or (self.response_deadline - self.captured_at).total_seconds() > 15
        ):
            raise ValueError("response wait must cover execution and remain within fifteen seconds")
        if not self.route.paid or self.route.model_version != "typesafe/jev-1.13":
            raise ValueError("explicit fixed Jev route is required")
        ids = tuple(q.question_id for q in self.questions)
        if len(set(ids)) != len(ids):
            raise ValueError("question identities must be unique")
        try:
            state = json.loads(self.state_json, object_pairs_hook=_pairs, parse_constant=_constant)
            if type(state) is not dict or len(self.state_json.encode()) > 32768:
                raise ValueError("invalid state")
        except (ValueError, TypeError, UnicodeError, RecursionError):
            raise ValueError("invalid bounded decision state") from None
        return self


class DecisionProbability(DomainModel):
    key: str = Field(strict=True, min_length=1, max_length=64)
    probability: Probability


def normalized_choice_probabilities(probabilities):
    """Adapter compatibility for at most one percentage point of wire rounding.

    The domain still requires a unit distribution. Confidence and the selected
    option are never recalculated. Every original value must already be valid.
    """
    with localcontext(Context(prec=128)):
        total = sum((p.probability for p in probabilities), Decimal(0))
        if not total or abs(total - 1) > Decimal("0.01"):
            raise ValueError("choice rounding exceeds compatibility limit")
        return tuple(
            DecisionProbability(
                key=p.key,
                probability=(p.probability / total).quantize(Decimal("1e-18")),
            )
            for p in probabilities
        )


class ChoiceProbabilityAdjustment(DomainModel):
    question_id: QuestionKey
    original_probabilities: tuple[DecisionProbability, ...] = Field(min_length=2, max_length=255)
    original_total: FiniteDecimal

    @model_validator(mode="after")
    def exact_original_total(self):
        keys = tuple(p.key for p in self.original_probabilities)
        with localcontext(Context(prec=128)):
            total = sum((p.probability for p in self.original_probabilities), Decimal(0))
        if (
            len(set(keys)) != len(keys)
            or total != self.original_total
            or not Decimal("0.000001") < abs(total - 1) <= Decimal("0.01")
        ):
            raise ValueError("invalid choice probability adjustment evidence")
        return self


class DecisionAnswer(DomainModel):
    question_id: QuestionKey
    kind: DecisionKind
    choice: str | None = Field(default=None, max_length=64)
    score: FiniteDecimal | None = None
    noul: Probability | None = None
    confidence: Probability | None = None
    probabilities: tuple[DecisionProbability, ...] = Field(default=(), max_length=255)

    @model_validator(mode="after")
    def coherent_answer(self) -> Self:
        if self.kind == "noul":
            if (
                self.noul is None
                or self.choice is not None
                or self.score is not None
                or self.confidence is not None
                or self.probabilities
            ):
                raise ValueError("noul is a standalone probability")
            return self
        if self.confidence is None or self.noul is not None or len(self.probabilities) < 2:
            raise ValueError("choice and score require a distribution and confidence")
        keys = tuple(p.key for p in self.probabilities)
        with localcontext(Context(prec=128)):
            total = sum((p.probability for p in self.probabilities), Decimal(0))
            if len(set(keys)) != len(keys) or abs(total - 1) > Decimal("0.000001"):
                raise ValueError("invalid decision probability distribution")
            if self.kind == "choice":
                selected = next(
                    (p.probability for p in self.probabilities if p.key == self.choice), None
                )
                if (
                    self.score is not None
                    or selected is None
                    or selected < max(p.probability for p in self.probabilities)
                ):
                    raise ValueError("choice is inconsistent with probabilities")
            else:
                if (
                    self.choice is not None
                    or len(keys) > 10
                    or keys != tuple(str(i) for i in range(len(keys)))
                ):
                    raise ValueError("score requires ordered criteria")
                expected = sum(
                    (Decimal(i) * p.probability for i, p in enumerate(self.probabilities)),
                    Decimal(0),
                )
                if self.score is None or abs(self.score - expected) > Decimal("0.000001"):
                    raise ValueError("score is inconsistent with probabilities")
        return self


class DecisionModelResponse(DomainModel):
    request_id: Identifier
    question_set_version: Identifier
    answers: tuple[DecisionAnswer, ...] = Field(min_length=1, max_length=8)
    usage: ModelUsage
    provider_metadata: ProviderMetadata
    choice_probability_adjustments: tuple[ChoiceProbabilityAdjustment, ...] = Field(
        default=(), max_length=8
    )
    transport_evidence: ModelTransportEvidence | None = None
    local_timing: ModelLocalTiming | None = None

    @model_serializer(mode="wrap")
    def preserve_original_response_shape(self, handler):
        value = handler(self)
        if not self.choice_probability_adjustments:
            value.pop("choice_probability_adjustments", None)
        if self.transport_evidence is None:
            value.pop("transport_evidence", None)
        if self.local_timing is None:
            value.pop("local_timing", None)
        return value

    @model_validator(mode="after")
    def coherent_identity(self) -> Self:
        if self.request_id != self.usage.request_id or len(
            {a.question_id for a in self.answers}
        ) != len(self.answers):
            raise ValueError("response identity does not match usage")
        seen = set()
        for adjustment in self.choice_probability_adjustments:
            answer = next(
                (a for a in self.answers if a.question_id == adjustment.question_id), None
            )
            original = adjustment.original_probabilities
            selected = next(
                (p.probability for p in original if p.key == getattr(answer, "choice", None)), None
            )
            if (
                adjustment.question_id in seen
                or answer is None
                or answer.kind != "choice"
                or selected is None
                or selected < max(p.probability for p in original)
                or answer.probabilities != normalized_choice_probabilities(original)
            ):
                raise ValueError("choice adjustment does not match the original winner")
            seen.add(adjustment.question_id)
        return self

    def bind_to(self, request: DecisionModelRequest) -> Self:
        if (
            self.request_id != request.request_id
            or self.question_set_version != request.question_set_version
            or tuple(a.question_id for a in self.answers)
            != tuple(q.question_id for q in request.questions)
        ):
            raise ValueError("response belongs to a different question set")
        for question, answer in zip(request.questions, self.answers, strict=True):
            if question.kind != answer.kind or (
                answer.kind != "noul"
                and tuple(p.key for p in answer.probabilities)
                != tuple(c.key for c in question.criteria)
            ):
                raise ValueError("answer does not match requested criteria")
        return self
