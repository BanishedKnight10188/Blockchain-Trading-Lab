"""Bounded structured tool envelopes, with immutable durable snapshots."""

import json
from typing import Annotated, Literal, Self

from pydantic import Field, JsonValue, model_validator

from .models import DomainModel, Identifier

ToolName = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")]


def bounded_json(value, limit=8192):
    encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    if len(encoded.encode()) > limit:
        raise ValueError("structured tool data exceeds its byte limit")
    return encoded


class ToolSpec(DomainModel):
    name: ToolName
    description: str = Field(min_length=1, max_length=512)
    parameters: dict[str, JsonValue]

    @model_validator(mode="after")
    def object_schema(self) -> Self:
        bounded_json(self.parameters, 16384)
        if self.parameters.get("type") != "object":
            raise ValueError("tools require an object parameter schema")
        return self


class ToolCall(DomainModel):
    tool_call_id: Identifier = Field(max_length=128)
    name: ToolName
    arguments: dict[str, JsonValue]

    @model_validator(mode="after")
    def bounded_arguments(self) -> Self:
        bounded_json(self.arguments)
        return self


class ToolResult(DomainModel):
    tool_call_id: Identifier
    name: ToolName
    status: Literal["ok", "rejected", "unavailable"]
    data: dict[str, JsonValue] = {}
    reason: str | None = Field(default=None, max_length=256)

    @model_validator(mode="after")
    def bounded_result(self) -> Self:
        bounded_json(self.data)
        return self
