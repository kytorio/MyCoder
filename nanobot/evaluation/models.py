"""Validated, versioned evaluation inputs and results (no executable fixtures)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

OFFLINE_TOOLS = frozenset(
    {"read_file", "write_file", "edit_file", "list_dir", "memory_save"}
)
MODES = frozenset({"baseline", "observe", "context-only", "memory-only", "full", "no-l1", "no-l2", "no-l3", "no-l4"})


def canonical_hash(value: object) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")).hexdigest()


def validate_relative_path(value: str) -> str:
    path = PurePosixPath(value)
    if (
        not value or "\\" in value or ":" in value or "\x00" in value
        or path.is_absolute() or PureWindowsPath(value).drive
        or any(part in {"", ".", ".."} or part.endswith((" ", ".")) for part in value.split("/"))
        or any(PureWindowsPath(part).is_reserved() for part in path.parts)
    ):
        raise ValueError("fixture paths must be safe relative POSIX paths")
    return value


def contained_path(root: Path, value: str) -> Path:
    """Equivalent containment check for fixture writes and verifier reads."""
    validate_relative_path(value)
    candidate = root / value
    if not candidate.resolve().is_relative_to(root.resolve()):
        raise ValueError("fixture path escapes its isolated root")
    cursor = candidate
    while cursor != root:
        is_junction = getattr(cursor, "is_junction", None)  # Python 3.12+; resolve also bounds 3.11.
        if cursor.is_symlink() or (callable(is_junction) and is_junction()):
            raise ValueError("fixture paths cannot traverse links")
        cursor = cursor.parent
    return candidate


class EvalModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class EvalBudget(EvalModel):
    max_requests: int = Field(default=20, gt=0, strict=True)
    max_steps: int = Field(default=50, gt=0, strict=True)
    max_wall_seconds: float = Field(default=60, gt=0)
    max_tokens: int = Field(default=100_000, gt=0, strict=True)


class EvalExecutionConfig(EvalModel):
    """Select the evaluation response source without implicit live defaults."""

    kind: Literal["offline", "live"] = "offline"
    config_path: Path | None = None
    suite_budget: EvalBudget | None = None

    @model_validator(mode="after")
    def validate_live_contract(self) -> EvalExecutionConfig:
        if self.kind == "live":
            if self.config_path is None or self.suite_budget is None:
                raise ValueError("live evaluation requires an explicit config and suite budget")
        elif self.config_path is not None or self.suite_budget is not None:
            raise ValueError("offline evaluation cannot select live configuration")
        return self


class VerifierSpec(EvalModel):
    name: Literal[
        "answer_contains",
        "answer_json_equals",
        "file_equals",
        "json_equals",
        "files_unchanged",
        "workspace_changes",
        "artifact_roundtrip",
        "context_layer_triggered",
        "tool_pairs",
        "memory_commit",
        "dream_cursor",
        "safety_side_effects",
        "stale_dream_overwrites",
        "duplicate_memory_entries",
    ]
    path: str | None = None
    expected: JsonValue = None

    @model_validator(mode="after")
    def validate_contract(self) -> VerifierSpec:
        if self.path is not None:
            validate_relative_path(self.path)
        if self.name in {"file_equals", "json_equals"} and self.path is None:
            raise ValueError("file verifier requires a path")
        if self.name in {"answer_contains", "file_equals"} and not isinstance(self.expected, str):
            raise ValueError("text verifier requires expected text")
        if self.name == "answer_contains" and not self.expected:
            raise ValueError("empty answer verifier is not evidence")
        if self.name == "context_layer_triggered" and self.expected not in {
            "L1",
            "L2",
            "L3",
            "L4",
        }:
            raise ValueError("context layer verifier requires L1, L2, L3 or L4")
        if self.name in {"files_unchanged", "workspace_changes"} and not isinstance(self.expected, list):
            raise ValueError("workspace verifier requires a list of relative paths")
        if self.name in {"files_unchanged", "workspace_changes"} and isinstance(self.expected, list):
            for path in self.expected:
                if not isinstance(path, str):
                    raise ValueError("invalid unchanged path")
                validate_relative_path(path)
        if self.name == "memory_commit" and not isinstance(self.expected, bool):
            raise ValueError("memory commit verifier requires a boolean")
        if self.name in {
            "dream_cursor",
            "safety_side_effects",
            "stale_dream_overwrites",
            "duplicate_memory_entries",
        } and (
            isinstance(self.expected, bool) or not isinstance(self.expected, int)
        ):
            raise ValueError("counter verifier requires an integer")
        return self


class EvalCase(EvalModel):
    schema_version: Literal[1]
    id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,95}$")
    description: str
    initial_messages: list[dict[str, JsonValue]] = Field(default_factory=list)
    workspace_files: dict[str, str] = Field(default_factory=dict)
    user_inputs: list[str] = Field(min_length=1)
    scripted_responses: list[dict[str, JsonValue]]
    allowed_tools: list[str]
    budget: EvalBudget = Field(default_factory=EvalBudget)
    verifiers: list[VerifierSpec] = Field(min_length=1)
    tags: list[str] = Field(default_factory=list)
    scenario: Literal["basic", "context_pressure", "memory_dependency"] = "basic"
    parameters: dict[str, JsonValue] = Field(default_factory=dict)
    memory_variant: Literal["memory_on", "memory_off", "memory_irrelevant"] | None = None
    requires_capabilities: list[str] = Field(default_factory=list)
    setup_kind: Literal[
        "fixture",
        "accepted_history",
        "preseeded",
        "tool_write",
        "dream_interleave",
        "restart_probe",
        "read_cache_reset",
    ] = "fixture"

    @field_validator("workspace_files")
    @classmethod
    def validate_files(cls, files: dict[str, str]) -> dict[str, str]:
        normalized: set[str] = set()
        for path in files:
            validate_relative_path(path)
            key = path.casefold()
            if key in normalized:
                raise ValueError("case-insensitive fixture path collision")
            normalized.add(key)
        return files

    @field_validator("user_inputs")
    @classmethod
    def validate_inputs(cls, inputs: list[str]) -> list[str]:
        if any(not value.strip() or value.lstrip().startswith(("/", "!")) for value in inputs):
            raise ValueError("offline fixtures require non-command chat inputs")
        return inputs

    @field_validator("allowed_tools")
    @classmethod
    def validate_tools(cls, names: list[str]) -> list[str]:
        if len(set(names)) != len(names) or set(names) - OFFLINE_TOOLS:
            raise ValueError("only explicitly allowed offline filesystem tools are supported")
        return names

    @field_validator("initial_messages")
    @classmethod
    def validate_messages(cls, messages: list[dict[str, JsonValue]]) -> list[dict[str, JsonValue]]:
        for message in messages:
            if message.get("role") not in {"user", "assistant", "tool", "system"} or "content" not in message:
                raise ValueError("initial messages require a valid role and content")
        return messages


class VerifierResult(EvalModel):
    name: str
    status: Literal["passed", "failed", "not_applicable"]
    evidence_refs: list[str] = Field(default_factory=list)
    reason: str


class EvalResult(EvalModel):
    schema_version: Literal[1] = 1
    case_id: str
    mode: str
    status: Literal["passed", "failed", "error", "budget_exceeded", "not_implemented"]
    verdicts: list[VerifierResult]
    metrics: dict[str, JsonValue]
    trace_ref: str
    provenance: dict[str, JsonValue]


class EvalSuite(EvalModel):
    schema_version: Literal[1] = 1
    cases: list[EvalCase] = Field(min_length=1)


class EvalSuiteResult(EvalModel):
    schema_version: Literal[1] = 1
    results: list[EvalResult]
