"""Versioned, deliberately narrow protocol for expert-confirmed COMSOL tasks."""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ProtocolModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ParameterTarget(ProtocolModel):
    parameter: str = Field(min_length=1)
    expected_current_expression: str = Field(min_length=1)
    new_expression: str = Field(min_length=1)


class ParameterExpectation(ProtocolModel):
    parameter_expression: str = Field(min_length=1)


class ParameterTask(ProtocolModel):
    """Only a single existing parameter may change in protocol version 1.0."""

    protocol_version: Literal["1.0"]
    task_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    )
    goal: str = Field(min_length=1)
    model_tag: str = Field(min_length=1)
    target: ParameterTarget
    allowed_operations: list[Literal["param_set"]] = Field(min_length=1, max_length=1)
    expected_result: ParameterExpectation

    @model_validator(mode="after")
    def validate_scope(self) -> "ParameterTask":
        if self.allowed_operations != ["param_set"]:
            raise ValueError("Version 1.0 only permits one param_set operation.")
        if self.expected_result.parameter_expression != self.target.new_expression:
            raise ValueError("Expected result must equal the requested new expression.")
        return self


def task_fingerprint(task: ParameterTask) -> str:
    """Bind expert confirmation to every field in the reviewed task."""
    canonical = json.dumps(
        task.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
