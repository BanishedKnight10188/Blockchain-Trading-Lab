"""Exact recorded request/response pairs; no transport or paid fallback exists."""

from collections.abc import Iterable

from agent_platform.domain.model_calls import ModelRequest, ModelResponse


class FakeModel:
    network_calls = 0

    def __init__(self, recordings: Iterable[tuple[ModelRequest, ModelResponse]]):
        self._recordings: dict[str, tuple[ModelRequest, ModelResponse]] = {}
        for request, response in recordings:
            if request.request_id != response.request_id or request.request_id in self._recordings:
                raise ValueError("recording identities must be unique and consistent")
            if (
                response.usage.route_id != request.route.route_id
                or response.usage.model_version != request.route.model_version
                or response.usage.actual_cost_usd != 0
                or response.usage.estimated_cost_usd != 0
            ):
                raise ValueError("offline recording requires matching route and zero cost")
            self._recordings[request.request_id] = request, response

    async def generate(self, request: ModelRequest) -> ModelResponse:
        checked = ModelRequest.model_validate_json(request.model_dump_json())
        recorded = self._recordings.get(checked.request_id)
        if recorded is None or recorded[0] != checked:
            raise ValueError("no exact offline recording exists for this request")
        return recorded[1]
