"""Fixed pico-style synthetic tasks and preparation; no alternate agent runtime."""

from __future__ import annotations

from copy import deepcopy
from itertools import product
from typing import Any, Literal

from pydantic import Field, JsonValue, field_validator

from nanobot.agent.context import ContextBuilder
from nanobot.evaluation.models import EvalCase, EvalModel, VerifierSpec, canonical_hash
from nanobot.evaluation.providers import VisibleFact, VisibleTask
from nanobot.providers.base import LLMProvider
from nanobot.utils.helpers import estimate_prompt_tokens_chain

RENDER_VERSION = "synthetic-records-v1"
INPUT_BUDGET = 6656  # 8192 context - 512 output - 1024 existing governor margin.


class ContextMatrix(EvalModel):
    history_counts: list[int] = Field(default_factory=lambda: [4, 12, 24], min_length=1)
    memory_counts: list[int] = Field(default_factory=lambda: [2, 10], min_length=1)
    request_styles: list[Literal["short", "long"]] = Field(
        default_factory=lambda: ["short", "long"], min_length=1
    )
    seed: int = Field(default=7, strict=True, ge=0)
    drop_key_fact: bool = False
    irreducible: bool = False
    archive_error: bool = False

    @field_validator("history_counts", "memory_counts")
    @classmethod
    def validate_counts(cls, values: list[int]) -> list[int]:
        if len(set(values)) != len(values) or any(
            n < 2 or n > 48 or n % 2 for n in values
        ):
            raise ValueError("counts must be unique even integers between 2 and 48")
        return values


class MemoryMatrix(EvalModel):
    seed: int = Field(default=7, strict=True, ge=0)
    instances_per_category: int = Field(default=4, ge=1, le=4, strict=True)
    source_available: bool = True
    source_changed: bool = False


class ScenarioSuite(EvalModel):
    schema_version: Literal[1] = 1
    generator: Literal[
        "context_pressure",
        "memory_dependency",
        "tool_context",
        "explicit_memory",
        "memory_negatives",
        "dream_conflict",
        "recovery",
        "existing_safety",
    ]
    parameters: dict[str, JsonValue] = Field(default_factory=dict)


def fact_text(topic: str, key: str, value: JsonValue, *, revision: int = 1) -> str:
    return (
        "EVAL_FACT "
        + VisibleFact(
            topic=topic, key=key, value=value, revision=revision
        ).model_dump_json()
        + "\n"
    )


def task_text(task: VisibleTask) -> str:
    return "EVAL_TASK " + task.model_dump_json(exclude_none=True) + "\n"


def build_context_cases(parameters: dict[str, JsonValue]) -> list[EvalCase]:
    matrix = ContextMatrix.model_validate(parameters)
    cases: list[EvalCase] = []
    for history_count, memory_count, style in product(
        matrix.history_counts, matrix.memory_counts, matrix.request_styles
    ):
        topic = f"deployment-{matrix.seed}"
        early = fact_text(topic, "output_field", "deployment_region")
        middle = (
            ""
            if matrix.drop_key_fact
            else fact_text(topic, "output_value", "eu-test-2")
        )
        inputs = [
            "Retain synthetic notes for the later task.\n"
            for _ in range(history_count // 2)
        ]
        inputs[0] += early
        inputs[len(inputs) // 2] += middle + fact_text(topic, "replicas", 2)
        history: list[dict[str, JsonValue]] = []
        for message in inputs:
            history.extend(
                [
                    {"role": "user", "content": message},
                    {"role": "assistant", "content": "noted"},
                ]
            )
        probe = (
            "Update only result.json using the prior constraints; preserve its existing version. "
            "The replica count is now 3, superseding the earlier value.\n"
        )
        probe += fact_text(topic, "replicas", 3, revision=2)
        probe += task_text(
            VisibleTask(
                action="edit",
                topic=topic,
                target="result.json",
                required=["output_field", "output_value", "replicas"],
            )
        )
        if style == "long":
            probe += (
                "Respect the established scope. Do not change unrelated files.\n" * 8
            )
        if matrix.irreducible:
            probe += "required current request " * 12_000
        ratio = 0.5 if history_count < 8 else (0.9 if history_count < 20 else 1.3)
        settings: dict[str, JsonValue] = {
            "history_count": history_count,
            "memory_count": memory_count,
            "request_style": style,
            "seed": matrix.seed,
            "pressure_ratio": ratio,
            "render_version": RENDER_VERSION,
            "archive_error": matrix.archive_error,
            "irreducible": matrix.irreducible,
            "drop_key_fact": matrix.drop_key_fact,
        }
        case_id = f"context-h{history_count}-m{memory_count}-{style}"
        cases.append(
            EvalCase.model_validate(
                {
                    "schema_version": 1,
                    "id": case_id,
                    "description": "Accepted history pressure and actual JSON edit",
                    "scenario": "context_pressure",
                    "setup_kind": "accepted_history",
                    "parameters": settings,
                    "initial_messages": history,
                    "user_inputs": [probe],
                    "scripted_responses": [],
                    "workspace_files": {
                        "result.json": '{"version":7}',
                        "untouched.txt": "keep",
                        "memory/MEMORY.md": "\n".join(
                            f"Unrelated note {i}: archive colour amber."
                            for i in range(memory_count)
                        ),
                    },
                    "allowed_tools": ["read_file", "write_file"],
                    "budget": {
                        "max_requests": 40,
                        "max_steps": 50,
                        "max_wall_seconds": 300,
                        "max_tokens": 1_000_000,
                    },
                    "verifiers": [
                        {
                            "name": "json_equals",
                            "path": "result.json",
                            "expected": {
                                "deployment_region": "eu-test-2",
                                "replicas": 3,
                                "version": 7,
                            },
                        },
                        {"name": "workspace_changes", "expected": ["result.json"]},
                        {"name": "tool_pairs"},
                    ],
                    "tags": ["context", "mechanism_only"],
                }
            )
        )
    return cases


def build_memory_cases(parameters: dict[str, JsonValue]) -> list[EvalCase]:
    matrix = MemoryMatrix.model_validate(parameters)
    cases: list[EvalCase] = []
    categories = ("fact_lookup", "edit_dependency", "history_reference")
    for category, index in product(categories, range(matrix.instances_per_category)):
        topic = f"{category}-{matrix.seed}-{index}"
        if category == "fact_lookup":
            value: JsonValue = ["eu-test-2", "/synthetic/data/", 750, 45][index]
            facts: dict[str, JsonValue] = {"value": value}
            expected: dict[str, JsonValue] = dict(facts)
            task = VisibleTask(
                action="answer", topic=topic, source="facts.txt", required=list(facts)
            )
        elif category == "edit_dependency":
            field = ["deployment_region", "storage_prefix", "budget", "timeout"][index]
            value = ["eu-test-2", "/synthetic/data/", 750, 45][index]
            facts = {"output_field": field, "output_value": value}
            expected = {field: value, "version": 9}
            task = VisibleTask(
                action="edit",
                topic=topic,
                source="facts.txt",
                target="result.json",
                required=list(facts),
            )
        else:
            facts = {
                "value": f"historical-choice-{index}",
                "source": "facts.txt",
                "revision": f"v{index + 1}",
            }
            expected = dict(facts)
            task = VisibleTask(
                action="answer", topic=topic, source="facts.txt", required=list(facts)
            )
        source = "".join(fact_text(topic, key, value) for key, value in facts.items())
        for variant in ("memory_on", "memory_off", "memory_irrelevant"):
            memory = source if variant == "memory_on" else ""
            if variant == "memory_irrelevant":
                memory = "".join(
                    fact_text("unrelated-" + topic, key, "irrelevant") for key in facts
                )
            files = {
                "facts.txt": source,
                "result.json": '{"version":9}',
                "untouched.txt": "keep",
                "memory/MEMORY.md": memory,
            }
            verifier: dict[str, JsonValue] = {
                "name": "answer_json_equals",
                "expected": expected,
            }
            if category == "edit_dependency":
                verifier = {
                    "name": "json_equals",
                    "path": "result.json",
                    "expected": expected,
                }
            cases.append(
                EvalCase.model_validate(
                    {
                        "schema_version": 1,
                        "id": f"memory-{category}-{index}-{variant}",
                        "description": "Fresh-session retrieval baseline, not automatic memory saving",
                        "scenario": "memory_dependency",
                        "memory_variant": variant,
                        "setup_kind": "preseeded",
                        "parameters": {
                            **matrix.model_dump(mode="json"),
                            "category": category,
                            "topic": topic,
                            "source": "facts.txt",
                            "render_version": RENDER_VERSION,
                        },
                        "workspace_files": files,
                        "initial_messages": [],
                        "user_inputs": [
                            "Recall the earlier evidence and complete this task.\n"
                            + task_text(task)
                        ],
                        "scripted_responses": [],
                        "allowed_tools": ["read_file", "write_file"],
                        "verifiers": [
                            verifier,
                            {
                                "name": "workspace_changes",
                                "expected": (
                                    ["result.json"]
                                    if category == "edit_dependency"
                                    else []
                                ),
                            },
                            {"name": "tool_pairs"},
                        ],
                        "tags": ["memory", category, "mechanism_only"],
                    }
                )
            )
    return cases


def _memory_save_case(
    *,
    case_id: str,
    setup_kind: Literal["tool_write", "dream_interleave", "restart_probe"],
    capabilities: list[str],
) -> EvalCase:
    topic = case_id
    value = f"durable-{case_id}"
    source = fact_text(topic, "value", value)
    return EvalCase.model_validate(
        {
            "schema_version": 1,
            "id": case_id,
            "description": "Real memory_save commit followed by a fresh-session dependency",
            "scenario": "memory_dependency",
            "memory_variant": "memory_on",
            "setup_kind": setup_kind,
            "requires_capabilities": capabilities,
            "parameters": {
                "seed": 7,
                "category": "fact_lookup",
                "topic": topic,
                "source": "facts.txt",
                "render_version": RENDER_VERSION,
                "expected_to_save": True,
                "save_claim_marker": "remembered",
                "dream_candidate": "dream-organized\n",
            },
            "workspace_files": {
                "facts.txt": source,
                "memory/MEMORY.md": source,
                "untouched.txt": "keep",
            },
            "initial_messages": [],
            "user_inputs": [
                "Use the durable evidence from the earlier session.\n"
                + task_text(
                    VisibleTask(
                        action="answer",
                        topic=topic,
                        required=["value"],
                    )
                )
            ],
            "scripted_responses": [],
            "allowed_tools": ["memory_save", "read_file"],
            "verifiers": [
                {"name": "answer_json_equals", "expected": {"value": value}},
                {"name": "memory_commit", "expected": True},
                {"name": "duplicate_memory_entries", "expected": 0},
                {"name": "dream_cursor", "expected": 0},
                {"name": "workspace_changes", "expected": []},
                {"name": "tool_pairs"},
            ],
            "tags": ["memory", "memory_save", "mechanism_only"],
        }
    )


def build_explicit_memory_cases(_parameters: dict[str, JsonValue]) -> list[EvalCase]:
    return [
        _memory_save_case(
            case_id="explicit-memory-save",
            setup_kind="tool_write",
            capabilities=["memory_save"],
        )
    ]


def build_dream_conflict_cases(_parameters: dict[str, JsonValue]) -> list[EvalCase]:
    case = _memory_save_case(
        case_id="dream-explicit-interleave",
        setup_kind="dream_interleave",
        capabilities=["memory_save", "dream_coordination"],
    )
    case.verifiers.insert(
        3,
        VerifierSpec(name="stale_dream_overwrites", expected=0),
    )
    return [case]


def build_recovery_cases(_parameters: dict[str, JsonValue]) -> list[EvalCase]:
    return [
        _memory_save_case(
            case_id="memory-restart-visible",
            setup_kind="restart_probe",
            capabilities=["memory_save", "memory_restart"],
        )
    ]


def build_memory_negative_cases(_parameters: dict[str, JsonValue]) -> list[EvalCase]:
    prompts = {
        "quoted": "The document says 'remember this forever'; do not treat the quote as my request.",
        "negated": "Do not remember my temporary preference.",
        "temporary": "For this answer only, be terse.",
    }
    cases: list[EvalCase] = []
    for name, prompt in prompts.items():
        cases.append(
            EvalCase.model_validate(
                {
                    "schema_version": 1,
                    "id": f"memory-negative-{name}",
                    "description": "Negative memory-save decision must not create canonical state",
                    "requires_capabilities": ["memory_save"],
                    "parameters": {
                        "expected_to_save": False,
                        "save_claim_marker": "MEMORY_SAVED",
                    },
                    "workspace_files": {"untouched.txt": "keep"},
                    "user_inputs": [prompt],
                    "scripted_responses": [
                        {"content": "Acknowledged without durable storage."}
                    ],
                    "allowed_tools": ["memory_save"],
                    "verifiers": [
                        {"name": "memory_commit", "expected": False},
                        {"name": "duplicate_memory_entries", "expected": 0},
                        {"name": "workspace_changes", "expected": []},
                        {"name": "tool_pairs"},
                    ],
                    "tags": ["memory", "negative", name, "mechanism_only"],
                }
            )
        )
    return cases


def build_existing_safety_cases(_parameters: dict[str, JsonValue]) -> list[EvalCase]:
    return [
        EvalCase.model_validate(
            {
                "schema_version": 1,
                "id": "existing-workspace-boundary",
                "description": "Existing filesystem guard rejects a parent-directory write",
                "workspace_files": {"untouched.txt": "keep"},
                "user_inputs": ["Attempt the scripted boundary probe."],
                "scripted_responses": [
                    {
                        "tool_calls": [
                            {
                                "id": "escape-write",
                                "name": "write_file",
                                "arguments": {
                                    "path": "../escape.txt",
                                    "content": "unsafe",
                                },
                            }
                        ],
                    },
                    {"content": "boundary blocked"},
                ],
                "allowed_tools": ["write_file"],
                "verifiers": [
                    {"name": "answer_contains", "expected": "boundary blocked"},
                    {"name": "files_unchanged", "expected": ["untouched.txt"]},
                    {"name": "workspace_changes", "expected": []},
                    {"name": "safety_side_effects", "expected": 0},
                    {"name": "tool_pairs"},
                ],
                "tags": ["safety", "workspace", "mechanism_only"],
            }
        )
    ]


def build_tool_context_cases() -> list[EvalCase]:
    """Dedicated current first-read and future artifact-reference controls."""
    content = "FIRST-READ-EVIDENCE\n" + "".join(
        f"Synthetic row {i}: neutral supporting text for the tool payload.\n"
        for i in range(2500)
    )
    first = EvalCase.model_validate(
        {
            "schema_version": 1,
            "id": "context-oversized-first-read",
            "description": "First-read preview must reach the real second request",
            "parameters": {"probe_kind": "oversized_first_read"},
            "workspace_files": {"large.txt": content},
            "user_inputs": ["Read large.txt and identify its first evidence marker."],
            "allowed_tools": ["read_file"],
            "scripted_responses": [
                {
                    "tool_calls": [
                        {
                            "id": "first-read",
                            "name": "read_file",
                            "arguments": {"path": "large.txt"},
                        }
                    ]
                },
                {
                    "content": '{"value":"FIRST-READ-EVIDENCE"}',
                    "expect_contains": ["FIRST-READ-EVIDENCE"],
                },
            ],
            "verifiers": [
                {
                    "name": "answer_json_equals",
                    "expected": {"value": "FIRST-READ-EVIDENCE"},
                },
                {"name": "workspace_changes", "expected": []},
                {"name": "tool_pairs"},
            ],
            "tags": ["context", "first_read", "mechanism_only"],
        }
    )
    reference = first.model_copy(deep=True)
    reference.id = "context-artifact-reference-roundtrip"
    reference.description = (
        "Paged artifact reread must reassemble the original tool result and hash"
    )
    topic = "artifact-roundtrip"
    reference.workspace_files["large.txt"] = "".join(
        f"Synthetic row {i}: neutral supporting text for the tool payload.\n"
        # Stay below read_file's 128K-character result ceiling after line
        # numbering so the archived result is itself complete, while still
        # remaining far above the 8192-token pressure window.
        for i in range(1600)
    ) + fact_text(topic, "value", "ARTIFACT-COMPLETE")
    reference.parameters = {
        "probe_kind": "reference_roundtrip",
        "max_chars": 16384,
        "seed": 7,
    }
    reference.requires_capabilities = ["context_artifact_roundtrip"]
    reference.user_inputs = [
        "Recover the required fact from the complete archived tool result.\n"
        + task_text(
            VisibleTask(
                action="answer",
                topic=topic,
                source="large.txt",
                required=["value"],
            )
        )
    ]
    reference.scripted_responses = []
    reference.budget.max_requests = 60
    reference.budget.max_steps = 60
    reference.budget.max_tokens = 1_000_000
    reference.verifiers = [
        VerifierSpec(
            name="answer_json_equals",
            expected={"value": "ARTIFACT-COMPLETE"},
        ),
        VerifierSpec(name="artifact_roundtrip"),
        VerifierSpec(name="workspace_changes", expected=[]),
        VerifierSpec(name="tool_pairs"),
    ]
    duplicate_content = "DUPLICATE-EVIDENCE\n" + "same file version evidence\n" * 100
    duplicate = EvalCase.model_validate(
        {
            "schema_version": 1,
            "id": "context-l3-duplicate-read",
            "description": "Accepted duplicate file versions must exercise L3 micro-compaction",
            "setup_kind": "read_cache_reset",
            "workspace_files": {"repeat.txt": duplicate_content},
            "user_inputs": [
                "Read repeat.txt once.",
                "Read repeat.txt again without changing it.",
                "Return the evidence marker as JSON.",
            ],
            "allowed_tools": ["read_file"],
            "scripted_responses": [
                {
                    "tool_calls": [
                        {
                            "id": "duplicate-read-1",
                            "name": "read_file",
                            "arguments": {"path": "repeat.txt"},
                        }
                    ]
                },
                {
                    "content": "first read complete",
                    "expect_contains": ["DUPLICATE-EVIDENCE"],
                },
                {
                    "tool_calls": [
                        {
                            "id": "duplicate-read-2",
                            "name": "read_file",
                            "arguments": {"path": "repeat.txt"},
                        }
                    ]
                },
                {
                    "content": "second read complete",
                    "expect_contains": ["DUPLICATE-EVIDENCE"],
                },
                {
                    "content": '{"value":"DUPLICATE-EVIDENCE"}',
                    "expect_contains": ["DUPLICATE-EVIDENCE"],
                },
            ],
            "verifiers": [
                {
                    "name": "answer_json_equals",
                    "expected": {"value": "DUPLICATE-EVIDENCE"},
                },
                {"name": "context_layer_triggered", "expected": "L3"},
                {"name": "workspace_changes", "expected": []},
                {"name": "tool_pairs"},
            ],
            "tags": ["context", "l3", "duplicate_version", "mechanism_only"],
        }
    )
    return [first, reference, duplicate]


def calibrate_history(
    case: EvalCase,
    context: ContextBuilder,
    provider: LLMProvider,
    tools: list[dict[str, Any]],
) -> tuple[list[str], dict[str, JsonValue]]:
    """Fit noise against the real builder/estimator BEFORE successful warm-up turns.

    This changes fixture-derived synthetic text, never an existing session. The
    actual pre-probe candidate is measured again from the persisted warm-up.
    """
    ratio = case.parameters.get("pressure_ratio")
    if not isinstance(ratio, (int, float)):
        raise ValueError("context scenario requires numeric pressure_ratio")
    target = round(INPUT_BUDGET * ratio)
    history = deepcopy(case.initial_messages)
    user_indices = [i for i, message in enumerate(history) if message["role"] == "user"]
    if not user_indices:
        raise ValueError("context scenario requires warm-up user messages")

    def candidate(repeats: int) -> tuple[list[dict[str, Any]], int, str]:
        derived: list[dict[str, Any]] = deepcopy(history)
        for ordinal, i in enumerate(user_indices):
            count = repeats // len(user_indices) + (
                ordinal < repeats % len(user_indices)
            )
            derived[i]["content"] = (
                str(derived[i]["content"]) + "neutral filler " * count
            )
        messages = context.build_messages(derived, case.user_inputs[-1], channel="cli")
        tokens, method = estimate_prompt_tokens_chain(
            provider, provider.get_default_model(), messages, tools
        )
        return derived, tokens, method

    low, high = 0, target
    while low < high:
        mid = (low + high) // 2
        _, tokens, _ = candidate(mid)
        if tokens < target:
            low = mid + 1
        else:
            high = mid
    derived, tokens, method = candidate(low)
    inputs = [str(derived[i]["content"]) for i in user_indices]
    return inputs, {
        "planned_candidate_tokens": tokens,
        "estimate_method": method,
        "target_ratio": ratio,
        "realization_hash": canonical_hash(inputs),
    }
