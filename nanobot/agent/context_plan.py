"""Request-local context plans and their content-free audit projection."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from copy import deepcopy
from dataclasses import asdict, dataclass, field, replace
from hashlib import sha256
from typing import Any, Literal, Protocol, Sequence, cast
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, field_validator

from nanobot.agent.context_sources import ContextSourcePolicy
from nanobot.runtime_context import (
    RUNTIME_CONTEXT_MESSAGE_META,
    RuntimeContextBlock,
    detach_runtime_context,
)
from nanobot.utils.helpers import estimate_prompt_tokens

_SUMMARY_FIELDS = (
    "tasks",
    "constraints",
    "explicit_preferences",
    "decisions",
    "file_changes",
    "errors",
    "evidence_refs",
    "remaining_work",
)


class StructuredContextSummary(BaseModel):
    """Strict, host-validated semantic payload for one L4 checkpoint."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1]
    tasks: list[str]
    constraints: list[str]
    explicit_preferences: list[str]
    decisions: list[str]
    file_changes: list[str]
    errors: list[str]
    evidence_refs: list[str]
    remaining_work: list[str]

    @field_validator(*_SUMMARY_FIELDS)
    @classmethod
    def validate_items(cls, values: list[str]) -> list[str]:
        if any(not value.strip() for value in values):
            raise ValueError("summary list entries must be non-empty")
        return values


@dataclass(frozen=True, slots=True)
class ContextSummaryCandidate:
    """Readable summary plus the strict model-produced structure it represents."""

    text: str
    structured: StructuredContextSummary


class ArtifactResolver(Protocol):
    def resolve(self, ref_id: str, scope: str) -> object: ...


def render_context_summary(summary: StructuredContextSummary) -> str:
    """Render a stable human-readable checkpoint while retaining structured metadata."""
    labels = {
        "tasks": "Tasks",
        "constraints": "Constraints",
        "explicit_preferences": "Explicit preferences",
        "decisions": "Decisions",
        "file_changes": "File changes",
        "errors": "Errors",
        "evidence_refs": "Evidence references",
        "remaining_work": "Remaining work",
    }
    lines = ["[Structured Context Summary v1]"]
    payload = summary.model_dump()
    for field_name in _SUMMARY_FIELDS:
        lines.append(f"\n{labels[field_name]}:")
        values = cast(list[str], payload[field_name])
        lines.extend(f"- {value}" for value in values)
        if not values:
            lines.append("- (none)")
    return "\n".join(lines)


def _normalized_user_texts(messages: Sequence[dict[str, Any]]) -> tuple[str, ...]:
    texts: list[str] = []
    for message in messages:
        if message.get("role") != "user":
            continue
        content = message.get("content")
        parts: list[str] = []
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for raw_block in cast(list[object], content):
                if not isinstance(raw_block, dict):
                    continue
                text = cast(dict[str, Any], raw_block).get("text")
                if isinstance(text, str):
                    parts.append(text)
        normalized = re.sub(r"\s+", " ", "\n".join(parts)).strip()
        if normalized:
            texts.append(normalized)
    return tuple(texts)


def validate_context_summary_candidate(
    candidate: ContextSummaryCandidate,
    accepted_messages: Sequence[dict[str, Any]],
    *,
    artifact_store: ArtifactResolver | None,
    session_key: str | None,
) -> bool:
    """Reject preferences or artifact references not grounded in accepted H."""
    if not candidate.text.strip():
        return False
    user_texts = _normalized_user_texts(accepted_messages)
    for preference in candidate.structured.explicit_preferences:
        normalized = re.sub(r"\s+", " ", preference).strip()
        if not normalized or not any(normalized in text for text in user_texts):
            return False
    accepted_snapshot = json.dumps(accepted_messages, ensure_ascii=False, sort_keys=True)
    for ref_id in candidate.structured.evidence_refs:
        if (
            re.fullmatch(r"[0-9a-f]{64}", ref_id) is None
            or ref_id not in accepted_snapshot
            or artifact_store is None
            or not session_key
        ):
            return False
        try:
            artifact_store.resolve(ref_id, session_key)
        except Exception:
            return False
    return True


@dataclass(frozen=True, slots=True)
class ContextRequestOutcome:
    """Host-bound evidence for one logical request, never model-generated text."""

    request_id: str
    lineage_id: str
    attempt: int
    status: str

    def __post_init__(self) -> None:
        if self.status not in {"accepted", "rejected", "unknown"}:
            raise ValueError("Invalid context request status")
        if not self.request_id or not self.lineage_id or type(self.attempt) is not int or self.attempt < 0:
            raise ValueError("Invalid context request identity")


def should_accept(outcome: ContextRequestOutcome) -> bool:
    return outcome.status == "accepted"


@dataclass(frozen=True, slots=True)
class ContextConsumption:
    """Transient host evidence; never reconstructed from serialized session metadata."""

    outcome: ContextRequestOutcome
    history_hashes: tuple[str, ...]
    accepted_messages_json: tuple[str, ...]


def history_message_hash(message: dict[str, Any]) -> str:
    """Hash only replay fields, retaining the existing host tool-error boolean."""
    public = {key: message[key] for key in (
        "role", "content", "tool_calls", "tool_call_id", "name", "reasoning_content", "thinking_blocks",
    ) if key in message}
    meta = message.get("_meta")
    if message.get("role") == "tool" and isinstance(meta, dict):
        tool_error = cast(dict[str, Any], meta).get("tool_result_error")
        if isinstance(tool_error, bool):
            public["_meta"] = {"tool_result_error": tool_error}
    return content_hash(public)


@dataclass(frozen=True, slots=True)
class ContextSource:
    source_id: str
    kind: str
    revision: str | None
    content_hash: str | None
    estimated_tokens: int | None
    estimate_method: str
    priority: int
    required: bool
    allowed_strategies: tuple[str, ...]
    render_key: str

    def __post_init__(self) -> None:
        if not self.revision and not self.content_hash:
            raise ValueError("source requires revision or content_hash")
        if self.estimated_tokens is not None and self.estimated_tokens < 0:
            raise ValueError("estimated_tokens must be nonnegative or unknown")


@dataclass(frozen=True, slots=True)
class ContextDecision:
    source_id: str
    action: Literal["keep", "shorten", "defer", "reference"]
    stage: Literal["selection", "L1", "L2", "L3", "L4"]
    reason_code: str
    before_tokens: int | None
    after_tokens: int | None
    artifact_ref: str | None = None


@dataclass(frozen=True, slots=True)
class ContextPlan:
    schema_version: int
    plan_id: str
    request_id: str
    model: str
    input_budget: int | None
    sources: tuple[ContextSource, ...]
    decisions: tuple[ContextDecision, ...]
    predicted_total: int | None
    rendered_estimate: int | None = None
    _catalog: RenderCatalog | None = field(default=None, repr=False, compare=False)

    def render_catalog(self) -> RenderCatalog | None:
        """Private payload custody for the renderer; excluded from audit projections."""
        return self._catalog


@dataclass(frozen=True, slots=True, repr=False)
class RenderCatalog:
    """Immutable private custody: JSON snapshots, never a shared mutable renderer."""

    system_parts: tuple[tuple[str, str], ...]
    messages: tuple[tuple[str, str], ...]
    tool_definitions: str = "[]"


class SourceCatalog(list[ContextSource]):
    def __init__(self, sources: list[ContextSource], catalog: RenderCatalog):
        super().__init__(sources)
        self.catalog = catalog


@dataclass(frozen=True, slots=True)
class RenderedContext:
    messages: list[dict[str, Any]]
    tool_definitions: list[dict[str, Any]]


def content_hash(value: object) -> str:
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def source_for(key: str, kind: str, value: object, *, required: bool = False,
               tokens: int | None = None, unknown: bool = False) -> ContextSource:
    policy = ContextSourcePolicy.for_source(kind, current=required)
    return ContextSource(
        source_id=key, kind=kind, revision=None, content_hash=content_hash(value),
        estimated_tokens=None if unknown else (
            tokens if tokens is not None else estimate_prompt_tokens(
                [{"role": "user", "content": value if isinstance(value, str)
                  else json.dumps(value, ensure_ascii=False)}]) - 4),
        estimate_method="unknown_image" if unknown else "local_text",
        priority=100 if required else policy.priority,
        required=required or policy.required,
        allowed_strategies=("keep",) if required else policy.allowed_strategies,
        render_key=key,
    )


def message_sources(key: str, message: dict[str, Any], *, current: bool = False,
                    runtime_blocks: Sequence[RuntimeContextBlock] | None = None,
                    ) -> list[ContextSource]:
    """Split at real content boundaries, excluding images and runtime from text cost."""
    body = deepcopy(message)
    body.pop("_meta", None)
    sources: list[ContextSource] = []
    meta = message.get("_meta")
    marker = cast(dict[str, Any], meta).get(RUNTIME_CONTEXT_MESSAGE_META) if isinstance(meta, dict) else None
    detached = detach_runtime_context(message.get("content"), cast(dict[str, Any], marker)) if isinstance(marker, dict) else None
    if detached is not None:
        body["content"], _, blocks = detached
        if runtime_blocks is not None:
            for index, block in enumerate(runtime_blocks):
                kind = "skill_full" if block.source == "explicit_skills" else "runtime"
                sources.append(source_for(f"{key}:runtime:{index}", kind, block.content, required=current))
        else:
            sources.append(source_for(key + ":runtime", "runtime", blocks, required=current))
    content = body.get("content")
    if isinstance(content, list):
        text_blocks: list[object] = []
        for index, block in enumerate(cast(list[object], content)):
            if isinstance(block, dict) and cast(dict[str, Any], block).get("type") in {"image_url", "image", "input_image"}:
                sources.append(source_for(f"{key}:image:{index}", "image", cast(dict[str, Any], block),
                                          required=current, unknown=True))
            else:
                text_blocks.append(cast(object, block))
        body["content"] = text_blocks
    kind = "tool_result" if body.get("role") == "tool" else (
        "user_current" if current and body.get("role") == "user" else "history")
    source = source_for(key, kind, body, required=current,
                        tokens=estimate_prompt_tokens([body]) - 4)
    if kind == "tool_result":
        source = replace(source, allowed_strategies=(*source.allowed_strategies, "reference"))
    sources.insert(0, replace(source, priority=95) if current and kind != "user_current" else source)
    return sources


class ContextPlanner:
    """N05 selects intact sources; future layers own any lossy transformations."""

    def plan(self, sources: list[ContextSource], input_budget: int | None) -> ContextPlan:
        if input_budget is not None and input_budget <= 0:
            raise ValueError("input_budget must be positive or unknown")
        if len({s.source_id for s in sources}) != len(sources):
            raise ValueError("source IDs must be unique")
        total = (sum(s.estimated_tokens for s in sources if s.estimated_tokens is not None)
                 if all(s.estimated_tokens is not None for s in sources) else None)
        selection_reason = ("within_budget" if total is not None and input_budget is not None
                            and total <= input_budget else "selection_only")
        return ContextPlan(
            schema_version=1, plan_id=uuid4().hex, request_id=uuid4().hex, model="",
            input_budget=input_budget, sources=tuple(sources),
            decisions=tuple(ContextDecision(
                source_id=source.source_id, action="keep", stage="selection",
                reason_code="kept_required" if source.required else selection_reason,
                before_tokens=source.estimated_tokens, after_tokens=source.estimated_tokens,
            ) for source in sources),
            predicted_total=(
                sum(source.estimated_tokens for source in sources
                    if source.estimated_tokens is not None)
                if all(source.estimated_tokens is not None for source in sources) else None
            ),
            _catalog=sources.catalog if isinstance(sources, SourceCatalog) else None,
        )


def _matching_merged_group(
    catalog: RenderCatalog,
    available: list[tuple[str, str]],
    message: dict[str, Any],
    merge_messages: Callable[[list[dict[str, Any]]], list[dict[str, Any]]],
) -> list[tuple[str, str]]:
    """Prove provenance by replaying the existing lossless merge on contiguous snapshots."""
    if message.get("role") != "user":
        return []
    available_keys = {key for key, _ in available}
    for start in range(len(catalog.messages)):
        group: list[tuple[str, str]] = []
        inputs: list[dict[str, Any]] = []
        for key, payload in catalog.messages[start:]:
            candidate: dict[str, Any] = json.loads(payload)
            if key not in available_keys or candidate.get("role") != "user":
                break
            group.append((key, payload))
            inputs.append(candidate)
            if len(group) > 1 and merge_messages(inputs) == [message]:
                return group
    return []


def reconcile_plan(seed: ContextPlan | None, messages: list[dict[str, Any]],
                   tools: list[dict[str, Any]] | None, *, input_budget: int | None,
                   model: str,
                   merge_messages: Callable[[list[dict[str, Any]]], list[dict[str, Any]]] | None = None,
                   ) -> ContextPlan:
    """Account for the exact logical request, retaining replaced candidates as zero-cost decisions.

    Matching uses whole snapshots or an exact replay of the existing user merge,
    never substring guesses. Other rewrites get new sources rather than false keeps.
    System sections retain their pre-render identities only when their snapshot matches.
    """
    catalog = seed.render_catalog() if seed else None
    original = {s.source_id: s for s in seed.sources} if seed else {}
    sources: list[ContextSource] = []
    kept: set[str] = set()
    available = list(catalog.messages) if catalog else []
    system_used = False
    for index, message in enumerate(messages):
        if (catalog and not system_used and message.get("role") == "system"
                and message.get("content") == "".join(text for _, text in catalog.system_parts)):
            system_used = True
            for key, _ in catalog.system_parts:
                sources.append(original[key])
                kept.add(key)
            continue
        match = next(((key, payload) for key, payload in available
                      if json.loads(payload) == message), None)
        matched = [match] if match is not None else (
            _matching_merged_group(catalog, available, message, merge_messages)
            if catalog is not None and merge_messages is not None else [])
        if matched:
            for entry in matched:
                key, _ = entry
                available.remove(entry)
                group = [s for s in original.values()
                         if s.source_id == key or s.source_id.startswith(key + ":")]
                sources.extend(group)
                kept.update(s.source_id for s in group)
        else:
            key = f"request:{index}:{content_hash(message)[:12]}"
            if message.get("role") == "system":
                sources.append(source_for(key, "system_policy", message, required=True,
                                          tokens=estimate_prompt_tokens([message]) - 4))
            else:
                # Unknown/new messages are conservatively protected until N07 supplies consumption evidence.
                sources.extend(message_sources(key, message, current=True))
    sources.append(source_for("schema:tools", "mcp_schema", tools or [], required=True,
                              tokens=estimate_prompt_tokens([], tools)))
    sources.append(source_for("envelope", "envelope", len(messages), required=True,
                              tokens=4 * len(messages)))
    plan = ContextPlanner().plan(sources, input_budget)
    removed = [s for s in original.values()
               if s.source_id not in kept and s.source_id not in {"schema:tools", "envelope"}]
    return replace(plan, model=model,
                   sources=(*removed, *plan.sources),
                   decisions=(*(ContextDecision(
                       s.source_id, "defer", "selection", "legacy_replaced_or_removed",
                       s.estimated_tokens, 0) for s in removed), *plan.decisions))


@dataclass(frozen=True, slots=True)
class ContextManifest:
    plan: ContextPlan
    status: Literal["prepared", "dispatched", "responded", "refused"]
    reason_code: str
    payload_hash: str | None
    actual_input_tokens: int | None = None
    estimation_error: int | None = None
    elapsed_ms: float = 0

    def to_log(self) -> dict[str, object]:
        # Explicit whitelist: never serialize a plan's private catalog or a payload.
        plan = self.plan
        return {
            "schema_version": plan.schema_version, "plan_id": plan.plan_id,
            "request_id": plan.request_id, "model": plan.model,
            "input_budget": plan.input_budget,
            "sources": [asdict(source) for source in plan.sources],
            "decisions": [asdict(decision) for decision in plan.decisions],
            "predicted_total": plan.predicted_total, "rendered_estimate": plan.rendered_estimate,
            "status": self.status, "reason_code": self.reason_code,
            "payload_hash": self.payload_hash, "actual_input_tokens": self.actual_input_tokens,
            "estimation_error": self.estimation_error, "elapsed_ms": self.elapsed_ms,
        }


class ContextPlanError(ValueError):
    def __init__(self, manifest: ContextManifest):
        self.manifest = manifest
        super().__init__(f"Context request refused: {manifest.reason_code}")
