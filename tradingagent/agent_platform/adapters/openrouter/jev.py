"""Jev Decisions API payload and typed answers, without chat-model fallbacks."""

import json
from decimal import Context, Decimal, localcontext

from agent_platform.domain.decision_models import (
    ChoiceProbabilityAdjustment,
    DecisionAnswer,
    DecisionModelRequest,
    DecisionModelResponse,
    DecisionProbability,
    normalized_choice_probabilities,
)
from agent_platform.domain.model_diagnostics import ModelDiagnostic
from agent_platform.ports.model import ModelCallFailed

from .parsing import read_metadata, read_usage
from .transport import OpenRouterError


class OpenRouterDecisionModel:
    provider_deadline_managed = True

    def __init__(self, client, clock):
        self.client, self.clock = client, clock

    async def decide(self, request: DecisionModelRequest, quote) -> DecisionModelResponse:
        request = DecisionModelRequest.model_validate_json(request.model_dump_json())
        if (
            quote.route_id != request.route.route_id
            or quote.price_version != request.route.price_version
            or quote.max_output_tokens != request.max_output_tokens
        ):
            raise ModelCallFailed("quote_unavailable")
        questions = {}
        for question in request.questions:
            wire = {"type": question.kind, "instructions": question.instructions}
            if question.kind == "score":
                wire["criteria"] = [c.description for c in question.criteria]
            elif question.criteria:
                wire["criteria"] = {c.key: c.description for c in question.criteria}
            questions[question.question_id] = wire
        payload = {
            "model": request.route.model_version,
            "state": json.loads(request.state_json),
            "questions": questions,
        }
        try:
            value = await self.client.post(
                "/api/alpha/decisions", payload, request.response_deadline or request.deadline
            )
        except OpenRouterError as error:
            # ModelCallFailed admits only fixed codes and bounded HTTP status
            # codes, never arbitrary provider text or credential echoes.
            raise ModelCallFailed(
                str(error),
                diagnostic=ModelDiagnostic(
                    stage="transport", transport_evidence=error.transport_evidence
                ),
            ) from None
        try:
            usage = read_usage(value, request, quote, self.clock.utcnow(), decision=True)
        except ModelCallFailed as error:
            raise ModelCallFailed(
                error.reason, error.usage, ModelDiagnostic(stage="usage")
            ) from None
        try:
            metadata = read_metadata(value, request.route.model_version, usage)
        except ModelCallFailed as error:
            raise ModelCallFailed(
                error.reason, error.usage, ModelDiagnostic(stage="metadata")
            ) from None
        diagnostic = ModelDiagnostic(stage="question_set", expected_count=len(questions))
        try:
            raw_answers = value["answers"]
            diagnostic = ModelDiagnostic(
                stage="question_set",
                expected_count=len(questions),
                actual_count=min(len(raw_answers), 256) if type(raw_answers) is dict else None,
            )
            if type(raw_answers) is not dict or set(raw_answers) != set(questions):
                raise ValueError("response question set changed")
            answers = []
            adjustments = []
            for index, question in enumerate(request.questions):
                diagnostic = ModelDiagnostic(stage="answer_type", question_index=index)
                raw = raw_answers[question.question_id]
                if type(raw) is not dict or raw["type"] != question.kind:
                    raise ValueError("response type changed")
                if question.kind == "noul":
                    diagnostic = ModelDiagnostic(stage="answer_values", question_index=index)
                    answer = DecisionAnswer(
                        question_id=question.question_id, kind="noul", noul=raw["noul"]
                    )
                else:
                    diagnostic = ModelDiagnostic(
                        stage="criteria",
                        question_index=index,
                        expected_count=len(question.criteria),
                    )
                    probabilities = raw["probabilities"]
                    diagnostic = ModelDiagnostic(
                        stage="criteria",
                        question_index=index,
                        expected_count=len(question.criteria),
                        actual_count=min(len(probabilities), 256)
                        if type(probabilities) is dict
                        else None,
                    )
                    if type(probabilities) is not dict or set(probabilities) != {
                        c.key for c in question.criteria
                    }:
                        raise ValueError("response criteria changed")
                    diagnostic = ModelDiagnostic(stage="legend", question_index=index)
                    if (
                        question.kind == "score"
                        and "legend" in raw
                        and raw["legend"] != {c.key: c.description for c in question.criteria}
                    ):
                        raise ValueError("response legend changed")
                    diagnostic = ModelDiagnostic(stage="answer_values", question_index=index)
                    distribution = tuple(
                        DecisionProbability(key=c.key, probability=probabilities[c.key])
                        for c in question.criteria
                    )
                    if question.kind == "choice":
                        with localcontext(Context(prec=128)):
                            total = sum((p.probability for p in distribution), Decimal(0))
                        diagnostic = ModelDiagnostic(
                            stage="answer_values",
                            question_index=index,
                            issue="probability_sum",
                            probability_total=total,
                        )
                        if abs(total - 1) > Decimal("0.000001"):
                            original = distribution
                            distribution = normalized_choice_probabilities(original)
                            adjustments.append(
                                ChoiceProbabilityAdjustment(
                                    question_id=question.question_id,
                                    original_probabilities=original,
                                    original_total=total,
                                )
                            )
                    diagnostic = ModelDiagnostic(
                        stage="answer_values", question_index=index, issue="invalid_answer_values"
                    )
                    answer = DecisionAnswer(
                        question_id=question.question_id,
                        kind=question.kind,
                        choice=raw["choice"] if question.kind == "choice" else None,
                        score=str(raw["score"]) if question.kind == "score" else None,
                        confidence=raw["confidence"],
                        probabilities=distribution,
                    )
                answers.append(answer)
            diagnostic = ModelDiagnostic(stage="binding")
            return DecisionModelResponse(
                request_id=request.request_id,
                question_set_version=request.question_set_version,
                answers=tuple(answers),
                usage=usage,
                provider_metadata=metadata,
                transport_evidence=getattr(value, "transport_evidence", None),
                choice_probability_adjustments=tuple(adjustments),
            ).bind_to(request)
        except (ValueError, TypeError, KeyError, ArithmeticError):
            raise ModelCallFailed("invalid_model_assessment", usage, diagnostic) from None
