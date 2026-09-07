"""Model-message governance and compaction for agent runner requests.

This module owns model-facing message shaping, request pressure, H/delta
compaction state, and tool-result content normalization. It may return copied
messages or persisted-result placeholders, but it must not mutate an existing
session history list in place.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import datetime
from math import ceil
from pathlib import Path
from time import perf_counter
from typing import TYPE_CHECKING, Any, Literal, cast
from uuid import uuid4

from loguru import logger

from nanobot.agent.context import TranscriptInput
from nanobot.agent.context_plan import (
    ContextConsumption,
    ContextDecision,
    ContextManifest,
    ContextPlan,
    ContextPlanError,
    ContextPlanner,
    ContextRequestOutcome,
    ContextSource,
    ContextSummaryCandidate,
    content_hash,
    history_message_hash,
    reconcile_plan,
    should_accept,
    validate_context_summary_candidate,
)
from nanobot.agent.context_sources import (
    append_deferred_schema_audit,
    apply_image_references,
    estimate_model_request_tokens,
    reestimate_plan_images,
    reference_verified_images,
)
from nanobot.config.schema import ContextConfig
from nanobot.providers.base import (
    LLMResponse,
    LLMUsage,
    ProviderCallContext,
    ProviderConversationState,
)
from nanobot.providers.conversation_state import (
    ProviderConversationStateController,
    allows_conversation_message_merge,
)
from nanobot.runtime_context import (
    RUNTIME_CONTEXT_MESSAGE_META,
    detach_runtime_context,
    reattach_runtime_context,
)
from nanobot.session.history_visibility import is_hidden_history_message
from nanobot.session.summary import (
    SUMMARY_CONTINUATION_TEXT,
    SessionSummary,
    SessionSummaryCheckpoint,
)
from nanobot.utils.helpers import (
    estimate_message_tokens,
    estimate_prompt_tokens,
    estimate_prompt_tokens_chain,
    find_legal_message_start,
    maybe_persist_tool_result,
    truncate_text,
)
from nanobot.utils.runtime import ensure_nonempty_tool_result

if TYPE_CHECKING:
    from nanobot.agent.context_artifacts import ToolResultArtifactStore
    from nanobot.agent.tools.registry import ToolRegistry
    from nanobot.providers.base import LLMProvider

TranscriptBuilder = Callable[[TranscriptInput], list[dict[str, Any]]]
HistoryConsolidator = Callable[
    [list[dict[str, Any]], str | None],
    Awaitable[ContextSummaryCandidate | str | None],
]
ProviderCompactionConsolidator = Callable[
    [ProviderConversationState, list[dict[str, Any]], str | None],
    Awaitable[ContextSummaryCandidate | str | None],
]
SummaryCheckpointCommitter = Callable[[SessionSummaryCheckpoint], Awaitable[None]]
ContextSourceCollector = Callable[[TranscriptInput], list[ContextSource]]

SNIP_SAFETY_BUFFER = 1024
_archive_failed: ContextVar[bool] = ContextVar("context_archive_failed", default=False)
# read_file is the recovery path for persisted results; exempting it prevents persist->read->persist loops.
TOOL_RESULT_OFFLOAD_EXEMPT_TOOLS = frozenset({"read_file"})
BACKFILL_CONTENT = "[Tool result unavailable — call was interrupted or lost]"
PLACEHOLDER_TEXTS = frozenset({
    "[Previous assistant message omitted.]",
})


class ContextWindowExceededError(RuntimeError):
    """Raised before a locally fitted request that still exceeds its budget."""

    def __init__(
        self,
        *,
        session_key: str | None,
        estimated_tokens: int,
        input_budget: int,
        source: str,
    ) -> None:
        self.session_key = session_key
        self.estimated_tokens = estimated_tokens
        self.input_budget = input_budget
        self.source = source
        super().__init__(
            "Model input still exceeds the local context budget after request fitting "
            f"for {session_key or 'default'}: {estimated_tokens}/{input_budget} via {source}"
        )


def _tool_call_name_is_valid(tool_call: Any) -> bool:
    """Whether a persisted OpenAI-style tool_call carries a usable name.

    Mirrors ``ToolCallRequest.has_valid_name`` for the dict shape stored in
    message history: a degenerate call with ``name=None`` / ``""`` cannot be
    executed and is rejected by upstream APIs if replayed.
    """
    if not isinstance(tool_call, dict):
        return False
    tool_call_data = cast(dict[str, Any], tool_call)
    fn = tool_call_data.get("function")
    name = cast(dict[str, Any], fn).get("name") if isinstance(fn, dict) else tool_call_data.get("name")
    return isinstance(name, str) and bool(name)


@dataclass(slots=True)
class ContextGovernanceConfig:
    provider: LLMProvider
    model: str
    tools: ToolRegistry
    workspace: Path | None
    session_key: str | None
    max_tool_result_chars: int
    context_window_tokens: int | None = None
    context_block_limit: int | None = None
    max_tokens: int | None = None
    context: ContextConfig = field(default_factory=ContextConfig)
    runtime_data_dir: Path | None = None
    artifact_store: ToolResultArtifactStore | None = field(default=None, repr=False)
    initial_plan: ContextPlan | None = field(default=None, repr=False)
    context_source_collector: ContextSourceCollector | None = field(
        default=None,
        repr=False,
    )
    summary_checkpoint_commit: SummaryCheckpointCommitter | None = field(default=None, repr=False)


@dataclass(slots=True)
class ContextCompactionState:
    """Track accepted provider input H separately from the unsent delta."""

    raw_messages: list[dict[str, Any]]
    accepted_messages: list[dict[str, Any]]
    raw_accepted_boundary: int
    active_summary: str | None
    transcript_input: TranscriptInput
    transcript_builder: TranscriptBuilder
    consolidate_history: HistoryConsolidator | None
    consolidate_provider_compaction: ProviderCompactionConsolidator | None
    summary_checkpoint: SessionSummaryCheckpoint | None = None
    pending_request: ContextRequestOutcome | None = field(default=None, repr=False)
    pending_messages: list[dict[str, Any]] | None = field(default=None, repr=False)
    pending_raw_boundary: int = 0
    # Observe-only compatibility view. This is not consumption evidence.
    legacy_messages: list[dict[str, Any]] = field(default_factory=list, repr=False)
    legacy_raw_boundary: int = 0
    accepted_raw_hashes: tuple[str, ...] = ()
    pending_raw_hashes: tuple[str, ...] = ()
    history_archives: dict[str, str] = field(default_factory=dict, repr=False)
    accepted_outcome: ContextRequestOutcome | None = field(default=None, repr=False)

    @classmethod
    def from_transcript(
        cls,
        transcript_input: TranscriptInput,
        transcript_builder: TranscriptBuilder,
        consolidate_history: HistoryConsolidator | None,
        consolidate_provider_compaction: ProviderCompactionConsolidator | None,
        *, consumption: ContextConsumption | None = None, track_consumption: bool = False,
    ) -> tuple[list[dict[str, Any]], ContextCompactionState | None]:
        """Build the raw transcript and its initial H/delta boundary."""
        messages = list(transcript_builder(transcript_input))
        if consolidate_history is None and not track_consumption:
            return messages, None
        state = cls(
            raw_messages=messages,
            accepted_messages=[],
            raw_accepted_boundary=0,
            legacy_messages=deepcopy(messages[:1 + len(transcript_input.history)]),
            legacy_raw_boundary=1 + len(transcript_input.history),
            active_summary=(
                transcript_input.session_summary["text"]
                if transcript_input.session_summary is not None
                else None
            ),
            transcript_input=transcript_input,
            transcript_builder=transcript_builder,
            consolidate_history=consolidate_history,
            consolidate_provider_compaction=consolidate_provider_compaction,
        )
        if consumption is not None and should_accept(consumption.outcome) and consumption.history_hashes:
            count = len(consumption.history_hashes)
            if (count <= len(transcript_input.history)
                    and tuple(history_message_hash(m) for m in transcript_input.history[:count]) == consumption.history_hashes
                    and messages[1:count + 1] == transcript_input.history[:count]):
                state.raw_accepted_boundary = count + 1
                # Coverage of raw history is not proof its full text was sent.
                # Preserve the accepted derived view, refreshing only system policy.
                accepted_view = [json.loads(payload) for payload in consumption.accepted_messages_json]
                state.accepted_messages = [
                    *deepcopy(messages[:1]), *(m for m in accepted_view if m.get("role") != "system")]
                state.accepted_raw_hashes = tuple(content_hash(m) for m in messages[:count + 1])
                state.accepted_outcome = consumption.outcome
        return messages, state

    def consumption(self) -> ContextConsumption | None:
        if self.accepted_outcome is None or self.raw_accepted_boundary <= 1:
            return None
        raw = self.raw_messages[:self.raw_accepted_boundary]
        if tuple(content_hash(m) for m in raw) != self.accepted_raw_hashes:
            return None
        return ContextConsumption(
            self.accepted_outcome, tuple(history_message_hash(m) for m in raw[1:]),
            tuple(json.dumps(m, ensure_ascii=False, sort_keys=True) for m in self.accepted_messages))

    def request_messages(
        self,
        raw_messages: list[dict[str, Any]],
        *, observe: bool = False,
    ) -> list[dict[str, Any]]:
        if observe:
            return [*deepcopy(self.legacy_messages), *deepcopy(raw_messages[self.legacy_raw_boundary:])]
        raw_prefix = raw_messages[:self.raw_accepted_boundary]
        if tuple(content_hash(m) for m in raw_prefix) != self.accepted_raw_hashes:
            return deepcopy(raw_messages)
        prepared = [
            *deepcopy(self.accepted_messages),
            *deepcopy(raw_messages[self.raw_accepted_boundary:]),
        ]
        if tuple(content_hash(m) for m in raw_prefix) == self.accepted_raw_hashes:
            errors: dict[str, bool] = {}
            for message in raw_prefix:
                meta = message.get("_meta")
                call_id = message.get("tool_call_id")
                if message.get("role") == "tool" and isinstance(meta, dict) and isinstance(call_id, str):
                    flag = cast(dict[str, Any], meta).get("tool_result_error")
                    if isinstance(flag, bool):
                        errors[call_id] = flag
            for message in prepared:
                call_id = message.get("tool_call_id")
                if message.get("role") == "tool" and call_id in errors:
                    message["_meta"] = {"tool_result_error": errors[call_id]}
        return prepared

    def delta_after_accepted(
        self,
        request_messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        return deepcopy(request_messages[len(self.accepted_messages):])

    def begin_request(
        self, model_messages: list[dict[str, Any]], *, raw_boundary: int,
        request: ContextRequestOutcome,
    ) -> None:
        """Snapshot the dispatch before provider retries or new input can alter it."""
        self.pending_request = request
        self.pending_messages = deepcopy(model_messages)
        self.pending_raw_boundary = raw_boundary
        self.pending_raw_hashes = tuple(content_hash(m) for m in self.raw_messages[:raw_boundary])

    def accept_request(
        self,
        model_messages: list[dict[str, Any]],
        *,
        raw_boundary: int,
        outcome: ContextRequestOutcome | None,
    ) -> None:
        """Advance H only for evidence bound to the current dispatch snapshot."""
        if (outcome is None or not should_accept(outcome)
                or self.pending_request != replace(outcome, status="unknown")
                or raw_boundary != self.pending_raw_boundary
                or model_messages != self.pending_messages
                or tuple(content_hash(m) for m in self.raw_messages[:raw_boundary]) != self.pending_raw_hashes):
            return
        self.accepted_messages = deepcopy(model_messages)
        self.raw_accepted_boundary = raw_boundary
        self.accepted_raw_hashes = self.pending_raw_hashes
        self.accepted_outcome = outcome
        self.pending_request = None


@dataclass(slots=True)
class ModelRequestState:
    """Context state shared by every provider request in one runner turn."""

    config: ContextGovernanceConfig
    conversation: ProviderConversationStateController
    usage: LLMUsage | None = None
    messages: list[dict[str, Any]] | None = None
    tool_definitions: list[dict[str, Any]] | None = None
    compaction: ContextCompactionState | None = None
    provider_compaction_applied: bool = False
    manifest: ContextManifest | None = None
    lineage_id: str = field(default_factory=lambda: uuid4().hex)
    request_attempt: int = -1
    force_l4_reason: Literal["overflow"] | None = None
    overflow_l4_attempted: bool = False


@dataclass(frozen=True, slots=True)
class ContextCompactionResult:
    """Typed result shared by explicit/manual L4 callers."""

    applied: bool
    reason: str
    checkpoint: SessionSummaryCheckpoint | None = None


class ContextGovernor:
    """Own model-request context while preserving persisted history."""

    @staticmethod
    def _merge_message_content(left: Any, right: Any) -> str | list[dict[str, Any]]:
        if isinstance(left, str) and isinstance(right, str):
            return f"{left}\n\n{right}" if left else right

        def _to_blocks(value: Any) -> list[dict[str, Any]]:
            if isinstance(value, list):
                return [
                    cast(dict[str, Any], item)
                    if isinstance(item, dict)
                    else {"type": "text", "text": str(item)}
                    for item in cast(list[Any], value)
                ]
            if value is None:
                return []
            return [{"type": "text", "text": str(value)}]

        return _to_blocks(left) + _to_blocks(right)

    @classmethod
    def _merge_adjacent_user_messages_for_model(
        cls,
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Merge adjacent visible user messages only in the model-facing copy."""
        prepared: list[dict[str, Any]] = []
        for source in messages:
            injection = deepcopy(source)
            if (
                prepared
                and injection.get("role") == "user"
                and prepared[-1].get("role") == "user"
                and injection.get("content") != SUMMARY_CONTINUATION_TEXT
                and prepared[-1].get("content") != SUMMARY_CONTINUATION_TEXT
                and not is_hidden_history_message(injection)
                and not is_hidden_history_message(prepared[-1])
                and allows_conversation_message_merge(injection)
                and allows_conversation_message_merge(prepared[-1])
            ):
                merged = dict(prepared[-1])
                left_meta = merged.get("_meta")
                right_meta = injection.get("_meta")
                left_meta_dict = (
                    cast(dict[str, Any], left_meta) if isinstance(left_meta, dict) else None
                )
                right_meta_dict = (
                    cast(dict[str, Any], right_meta) if isinstance(right_meta, dict) else None
                )
                left_marker = (
                    left_meta_dict.get(RUNTIME_CONTEXT_MESSAGE_META)
                    if left_meta_dict is not None
                    else None
                )
                right_marker = (
                    right_meta_dict.get(RUNTIME_CONTEXT_MESSAGE_META)
                    if right_meta_dict is not None
                    else None
                )
                left_marker_dict = (
                    cast(dict[str, Any], left_marker) if isinstance(left_marker, dict) else None
                )
                right_marker_dict = (
                    cast(dict[str, Any], right_marker) if isinstance(right_marker, dict) else None
                )
                empty_sources: list[str] = []
                empty_blocks: list[dict[str, Any]] = []
                detached_left = (
                    detach_runtime_context(merged.get("content"), left_marker_dict)
                    if left_marker_dict is not None
                    else (merged.get("content"), empty_sources, empty_blocks)
                )
                detached_right = (
                    detach_runtime_context(injection.get("content"), right_marker_dict)
                    if right_marker_dict is not None
                    else (injection.get("content"), empty_sources, empty_blocks)
                )
                if detached_left is not None and detached_right is not None:
                    left_content, left_sources, left_blocks = detached_left
                    right_content, right_sources, right_blocks = detached_right
                    merged_content = cls._merge_message_content(left_content, right_content)
                    context_blocks = [*left_blocks, *right_blocks]
                    if context_blocks:
                        merged_content, marker = reattach_runtime_context(
                            merged_content,
                            [*left_sources, *right_sources],
                            context_blocks,
                        )
                        internal_meta = (
                            dict(left_meta_dict) if left_meta_dict is not None else {}
                        )
                        if right_meta_dict is not None:
                            for key, value in right_meta_dict.items():
                                internal_meta.setdefault(key, value)
                        internal_meta[RUNTIME_CONTEXT_MESSAGE_META] = marker
                        merged["_meta"] = internal_meta
                    merged["content"] = merged_content
                else:
                    merged["content"] = cls._merge_message_content(
                        merged.get("content"),
                        injection.get("content"),
                    )
                prepared[-1] = merged
                continue
            prepared.append(injection)
        return prepared

    def prepare_messages_for_model(
        self,
        config: ContextGovernanceConfig,
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Build the normalized model-facing copy of a raw transcript."""
        governed = self.prepare_for_model(config, messages)
        return self._merge_adjacent_user_messages_for_model(governed)

    def prepare_for_model(
        self,
        config: ContextGovernanceConfig,
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        updated = self.strip_placeholder_assistant_messages(messages)
        updated = self.strip_malformed_tool_calls(updated)
        updated = self.drop_orphan_tool_results(updated)
        updated = self.backfill_missing_tool_results(updated)
        return self.apply_tool_result_budget(config, updated)

    def fit_to_budget(
        self,
        config: ContextGovernanceConfig,
        messages: list[dict[str, Any]],
        *,
        tool_definitions: list[dict[str, Any]] | None,
    ) -> list[dict[str, Any]]:
        """Fit a model-facing copy while keeping the source transcript intact."""
        updated = self.snip_history(
            config,
            messages,
            tool_definitions=tool_definitions,
            force=True,
        )
        updated = self.drop_orphan_tool_results(updated)
        updated = self.backfill_missing_tool_results(updated)
        return self.ensure_request_fits(
            config,
            updated,
            tool_definitions=tool_definitions,
        )

    def ensure_request_fits(
        self,
        config: ContextGovernanceConfig,
        messages: list[dict[str, Any]],
        *,
        tool_definitions: list[dict[str, Any]] | None,
    ) -> list[dict[str, Any]]:
        """Validate an exact model request without dropping any messages."""
        if not config.context_window_tokens:
            return messages
        budget = self.input_budget(config)
        estimated, source = estimate_prompt_tokens_chain(
            config.provider,
            config.model,
            messages,
            tool_definitions,
        )
        if budget > 0 and estimated <= budget:
            return messages
        raise ContextWindowExceededError(
            session_key=config.session_key,
            estimated_tokens=estimated,
            input_budget=budget,
            source=source,
        )

    def request_pressure(
        self,
        config: ContextGovernanceConfig,
        messages: list[dict[str, Any]],
        usage: LLMUsage | None,
        *,
        usage_matches_messages: bool,
        tool_definitions: list[dict[str, Any]] | None,
        request_context_tokens: int | None = None,
    ) -> tuple[int, str] | None:
        """Return the authoritative measurement when a request is pressured."""
        if not config.context_window_tokens:
            return None
        budget = self.input_budget(config)
        if request_context_tokens is not None:
            measured = request_context_tokens
            source = "resumed provider state plus pending messages"
        elif (
            usage_matches_messages
            and usage is not None
            and usage.context_tokens is not None
        ):
            measured = usage.context_tokens
            source = "matching provider usage"
        else:
            measured, source = estimate_prompt_tokens_chain(
                config.provider,
                config.model,
                messages,
                tool_definitions,
            )
        if budget > 0 and measured < budget:
            return None
        return measured, source

    def fit_request(
        self,
        config: ContextGovernanceConfig,
        messages: list[dict[str, Any]],
        usage: LLMUsage | None,
        *,
        usage_matches_messages: bool,
        tool_definitions: list[dict[str, Any]] | None,
        request_context_tokens: int | None = None,
    ) -> tuple[list[dict[str, Any]], bool]:
        """Fit the request when its measured or estimated input is pressured."""
        pressure = self.request_pressure(
            config,
            messages,
            usage,
            usage_matches_messages=usage_matches_messages,
            tool_definitions=tool_definitions,
            request_context_tokens=request_context_tokens,
        )
        if pressure is None:
            return messages, False
        return self.fit_to_budget(
            config,
            messages,
            tool_definitions=tool_definitions,
        ), True

    @staticmethod
    def _summary_transcript(
        compaction: ContextCompactionState,
        summary: str,
    ) -> list[dict[str, Any]]:
        """Rebuild only the stable system prefix around a replacement summary."""
        return compaction.transcript_builder(
            replace(
                compaction.transcript_input,
                history=[],
                current_message=None,
                media=None,
                session_summary={
                    "text": summary,
                    "last_active": datetime.now().astimezone().isoformat(),
                },
                runtime_context_blocks=None,
            )
        )

    async def summarize_provider_compaction(
        self,
        state: ModelRequestState,
        response: LLMResponse,
        *,
        current_request_boundary: int | None,
    ) -> None:
        """Materialize the exact input replaced by provider-native compaction."""
        compaction = state.compaction
        if (
            not response.provider_compaction_applied
            or response.provider_compaction_state is None
            or compaction is None
            or compaction.consolidate_provider_compaction is None
        ):
            return

        if state.config.context.mode == "enforce":
            outcome = response.context_outcome
            if (outcome is None or not should_accept(outcome)
                    or response.finish_reason in {"error", "cancelled"}
                    or compaction.pending_request != replace(outcome, status="unknown")
                    or state.messages != compaction.pending_messages):
                return
            if response.provider_compaction_scope == "prior_context" and not compaction.accepted_messages:
                return

        if response.provider_compaction_scope == "prior_context":
            observe = state.config.context.mode == "observe"
            accepted_messages = compaction.legacy_messages if observe else compaction.accepted_messages
            transcript_boundary = compaction.legacy_raw_boundary if observe else compaction.raw_accepted_boundary
        elif (
            response.provider_compaction_scope == "current_request"
            and state.messages is not None
            and current_request_boundary is not None
        ):
            accepted_messages = state.messages
            transcript_boundary = current_request_boundary
        else:
            logger.warning(
                "Ignoring provider compaction with missing request-boundary scope for {}",
                state.config.session_key or "default",
            )
            return

        result = await compaction.consolidate_provider_compaction(
            response.provider_compaction_state,
            deepcopy(accepted_messages),
            compaction.active_summary,
        )
        checkpoint = self._summary_checkpoint_from_result(
            state,
            result,
            accepted_messages,
            transcript_boundary,
        )
        if checkpoint is None:
            return
        try:
            committed = await self._commit_summary_checkpoint(state, checkpoint)
        except Exception:
            logger.exception(
                "Provider-compaction checkpoint commit failed for {}",
                state.config.session_key or "default",
            )
            return
        if committed and state.config.context.mode == "enforce":
            # The portable checkpoint, not the opaque pre-rewrite state, owns
            # subsequent requests after an enforce-mode L4 commit.
            response.provider_state = None

    @staticmethod
    def _summary_checkpoint_from_result(
        state: ModelRequestState,
        result: ContextSummaryCandidate | str | None,
        accepted_messages: list[dict[str, Any]],
        transcript_boundary: int,
    ) -> SessionSummaryCheckpoint | None:
        if isinstance(result, ContextSummaryCandidate):
            if not validate_context_summary_candidate(
                result,
                accepted_messages,
                artifact_store=state.config.artifact_store,
                session_key=state.config.session_key,
            ):
                return None
            return SessionSummaryCheckpoint(
                summary=result.text,
                transcript_boundary=transcript_boundary,
                structured=result.structured.model_dump(mode="json"),
            )
        if state.config.context.mode == "observe" and isinstance(result, str) and result.strip():
            return SessionSummaryCheckpoint(
                summary=result,
                transcript_boundary=transcript_boundary,
            )
        return None

    async def _commit_summary_checkpoint(
        self,
        state: ModelRequestState,
        checkpoint: SessionSummaryCheckpoint,
    ) -> bool:
        """Persist first; only then swap H and discard provider continuation."""
        compaction = state.compaction
        if compaction is None:
            return False
        callback = state.config.summary_checkpoint_commit
        if state.config.context.mode == "enforce" and state.config.session_key and callback is None:
            return False
        if callback is not None:
            await callback(checkpoint)
        state.conversation.replace_transcript(compaction.raw_messages)
        compaction.active_summary = checkpoint.summary
        compaction.summary_checkpoint = checkpoint
        state.usage = None
        return True

    async def _build_l4_candidate(
        self,
        state: ModelRequestState,
        compaction: ContextCompactionState,
        messages: list[dict[str, Any]],
        pressure: tuple[int, str],
    ) -> tuple[list[dict[str, Any]], SessionSummaryCheckpoint]:
        """Summarize accepted H only and retain the unsummarized raw delta."""
        observe = state.config.context.mode == "observe"
        prefix = compaction.legacy_messages if observe else compaction.accepted_messages
        boundary = compaction.legacy_raw_boundary if observe else compaction.raw_accepted_boundary
        if compaction.consolidate_history is None or (not observe and not prefix):
            raise ContextWindowExceededError(
                session_key=state.config.session_key,
                estimated_tokens=pressure[0],
                input_budget=self.input_budget(state.config),
                source="unknown_consumption",
            )
        consolidation_prefix = self.prepare_messages_for_model(state.config, prefix)
        result = await compaction.consolidate_history(
            deepcopy(consolidation_prefix),
            compaction.active_summary,
        )
        checkpoint = self._summary_checkpoint_from_result(
            state,
            result,
            prefix,
            boundary,
        )
        if checkpoint is None:
            raise ContextWindowExceededError(
                session_key=state.config.session_key,
                estimated_tokens=pressure[0],
                input_budget=self.input_budget(state.config),
                source="invalid_context_summary",
            )
        delta_messages = deepcopy(
            messages[len(prefix):]
            if observe
            else compaction.raw_messages[boundary:]
        )
        prepared = self.prepare_messages_for_model(
            state.config,
            [
                *self._summary_transcript(compaction, checkpoint.summary),
                {"role": "user", "content": SUMMARY_CONTINUATION_TEXT},
                *delta_messages,
            ],
        )
        return prepared, checkpoint

    async def _compact_request_history(
        self,
        state: ModelRequestState,
        compaction: ContextCompactionState,
        messages: list[dict[str, Any]],
        pressure: tuple[int, str],
        *,
        tool_definitions: list[dict[str, Any]] | None,
    ) -> list[dict[str, Any]]:
        """Replace accepted history H with a checkpoint while preserving delta."""
        prepared, checkpoint = await self._build_l4_candidate(
            state,
            compaction,
            messages,
            pressure,
        )
        prepared = self.ensure_request_fits(
            state.config,
            prepared,
            tool_definitions=tool_definitions,
        )
        if not await self._commit_summary_checkpoint(state, checkpoint):
            measured, source = pressure
            raise ContextWindowExceededError(
                session_key=state.config.session_key,
                estimated_tokens=measured,
                input_budget=self.input_budget(state.config),
                source="checkpoint_commit_unavailable" if state.config.session_key else source,
            )
        return prepared

    @staticmethod
    def _refresh_system_context(
        state: ModelRequestState,
        request_messages: list[dict[str, Any]],
    ) -> None:
        """Refresh file-derived system context and abandon stale provider state."""
        compaction = state.compaction
        collector = state.config.context_source_collector
        if compaction is None or collector is None or not compaction.raw_messages:
            return

        summary = compaction.transcript_input.session_summary
        if compaction.active_summary is not None and (
            summary is None or summary["text"] != compaction.active_summary
        ):
            refreshed_summary: SessionSummary = {
                "text": compaction.active_summary,
                "last_active": (
                    summary["last_active"]
                    if summary is not None
                    else datetime.now().astimezone().isoformat()
                ),
            }
            if (
                compaction.summary_checkpoint is not None
                and compaction.summary_checkpoint.structured is not None
            ):
                refreshed_summary["structured"] = deepcopy(
                    compaction.summary_checkpoint.structured
                )
            summary = refreshed_summary
        refreshed_input = replace(
            compaction.transcript_input,
            session_summary=summary,
        )
        system_probe = compaction.transcript_builder(
            replace(
                refreshed_input,
                history=[],
                current_message=None,
                media=None,
                runtime_context_blocks=None,
            )
        )
        if not system_probe or system_probe[0].get("role") != "system":
            return
        refreshed_system = deepcopy(system_probe[0])
        if compaction.raw_messages[0] == refreshed_system:
            return

        for target in (
            compaction.raw_messages,
            compaction.accepted_messages,
            compaction.legacy_messages,
            request_messages,
        ):
            if target and target[0].get("role") == "system":
                target[0] = deepcopy(refreshed_system)
        if (
            compaction.raw_accepted_boundary > 0
            and len(compaction.accepted_raw_hashes)
            == compaction.raw_accepted_boundary
        ):
            accepted_hashes = list(compaction.accepted_raw_hashes)
            accepted_hashes[0] = content_hash(refreshed_system)
            compaction.accepted_raw_hashes = tuple(accepted_hashes)
        compaction.transcript_input = refreshed_input
        state.config.initial_plan = ContextPlanner().plan(
            collector(refreshed_input),
            input_budget=None,
        )
        state.conversation.replace_transcript(compaction.raw_messages)
        logger.info(
            "Refreshed file-derived system context for {}",
            state.config.session_key or "default",
        )

    @classmethod
    def _reconcile_l4_plan(
        cls,
        previous: ContextPlan,
        messages: list[dict[str, Any]],
        tool_definitions: list[dict[str, Any]] | None,
        *,
        model: str,
    ) -> ContextPlan:
        """Retain prior layer decisions and identify sources replaced by L4."""
        reconciled = reconcile_plan(
            previous,
            messages,
            tool_definitions,
            input_budget=previous.input_budget,
            model=model,
            merge_messages=cls._merge_adjacent_user_messages_for_model,
        )
        source_kinds = {source.source_id: source.kind for source in previous.sources}
        summarizable_kinds = {"history", "user_current", "tool_result", "image"}
        current_decisions = tuple(
            replace(decision, stage="L4", reason_code="summarized")
            if (
                decision.reason_code == "legacy_replaced_or_removed"
                and source_kinds.get(decision.source_id) in summarizable_kinds
            )
            else decision
            for decision in reconciled.decisions
        )
        return replace(
            reconciled,
            plan_id=previous.plan_id,
            request_id=previous.request_id,
            decisions=(*previous.decisions, *current_decisions),
        )

    async def _prepare_enforced_l4(
        self,
        state: ModelRequestState,
        messages: list[dict[str, Any]],
        plan: ContextPlan,
        pressure: tuple[int, str],
        *,
        tool_definitions: list[dict[str, Any]] | None,
        deferred_tool_names: tuple[str, ...],
    ) -> tuple[list[dict[str, Any]], ContextPlan, str | None]:
        """Build, fit, and durably commit one enforce-mode L4 replacement."""
        compaction = state.compaction
        if compaction is None:
            return messages, plan, "unknown_consumption"
        try:
            prepared, checkpoint = await self._build_l4_candidate(
                state,
                compaction,
                messages,
                pressure,
            )
        except ContextWindowExceededError as exc:
            return messages, plan, exc.source

        plan = self._reconcile_l4_plan(
            plan,
            prepared,
            tool_definitions,
            model=state.config.model,
        )
        plan = reestimate_plan_images(plan, prepared, state.config.model)
        plan = append_deferred_schema_audit(
            plan,
            state.config.tools.get_definitions(),
            deferred_tool_names,
        )
        reason: str | None = None
        if "L1" in state.config.context.enabled_layers:
            prepared, plan, reason = self._fit_tool_results(
                state.config,
                prepared,
                plan,
                tool_definitions,
            )
        if reason is None and not self._tool_result_limits_fit(state.config, prepared):
            reason = "irreducible_floor"

        prepared = [
            {key: value for key, value in message.items() if key != "_meta"}
            for message in prepared
        ]
        estimate, _ = estimate_model_request_tokens(
            state.config.provider,
            state.config.model,
            prepared,
            tool_definitions,
        )
        plan = replace(
            plan,
            rendered_estimate=estimate if plan.predicted_total is not None else None,
        )
        if reason is None and estimate is None:
            reason = "unknown_image_cost"
        budget = plan.input_budget
        if reason is None and budget is not None and estimate is not None and estimate > budget:
            reason = "irreducible_floor"
        if reason is not None:
            return messages, plan, reason

        try:
            committed = await self._commit_summary_checkpoint(state, checkpoint)
        except Exception:
            logger.exception(
                "L4 checkpoint commit failed for {}",
                state.config.session_key or "default",
            )
            return messages, plan, "checkpoint_commit_failed"
        if not committed:
            return messages, plan, "checkpoint_commit_unavailable"
        state.force_l4_reason = None
        return prepared, plan, None

    async def compact_accepted_history(
        self,
        state: ModelRequestState,
        messages: list[dict[str, Any]],
        *,
        tool_definitions: list[dict[str, Any]] | None,
    ) -> ContextCompactionResult:
        """Run the same validated, atomic L4 path for an explicit request."""
        config = state.config
        compaction = state.compaction
        if config.context.mode != "enforce" or "L4" not in config.context.enabled_layers:
            return ContextCompactionResult(False, "l4_disabled")
        if compaction is None or not compaction.accepted_messages:
            return ContextCompactionResult(False, "no_accepted_history")
        budget = self.planning_input_budget(config)
        if budget is None or budget <= 0:
            return ContextCompactionResult(False, "unknown_budget")
        plan = reconcile_plan(
            config.initial_plan,
            messages,
            tool_definitions,
            input_budget=budget,
            model=config.model,
            merge_messages=self._merge_adjacent_user_messages_for_model,
        )
        plan = reestimate_plan_images(plan, messages, config.model)
        estimate, source = estimate_model_request_tokens(
            config.provider,
            config.model,
            messages,
            tool_definitions,
        )
        if estimate is None:
            return ContextCompactionResult(False, "unknown_image_cost")
        _, _, reason = await self._prepare_enforced_l4(
            state,
            messages,
            plan,
            (estimate, source),
            tool_definitions=tool_definitions,
            deferred_tool_names=(),
        )
        if reason is not None:
            return ContextCompactionResult(False, reason)
        checkpoint = compaction.summary_checkpoint
        if checkpoint is None:
            return ContextCompactionResult(False, "checkpoint_commit_unavailable")
        return ContextCompactionResult(True, "summarized", checkpoint)

    async def prepare_request(
        self,
        state: ModelRequestState,
        messages: list[dict[str, Any]],
        *,
        tool_definitions: list[dict[str, Any]] | None,
        transcript: list[dict[str, Any]] | None = None,
        deferred_tool_names: tuple[str, ...] = (),
    ) -> tuple[list[dict[str, Any]], ProviderCallContext | None]:
        """The sole logical request entry for both observation and enforced layers."""
        started = perf_counter()
        self._refresh_system_context(state, messages)
        config = state.config
        archive_failed = False
        budget = self.planning_input_budget(config)
        plan = reconcile_plan(config.initial_plan, messages, tool_definitions,
                              input_budget=budget if budget and budget > 0 else None,
                              model=config.model,
                              merge_messages=self._merge_adjacent_user_messages_for_model)
        plan = reestimate_plan_images(plan, messages, config.model)
        plan = append_deferred_schema_audit(
            plan,
            config.tools.get_definitions(),
            deferred_tool_names,
        )

        def record(status: Literal["prepared", "refused"], reason: str,
                   payload: list[dict[str, Any]] | None) -> None:
            state.manifest = ContextManifest(
                plan=replace(plan, _catalog=None), status=status, reason_code=reason,
                payload_hash=content_hash({"messages": payload, "tools": tool_definitions})
                if payload is not None else None, elapsed_ms=(perf_counter() - started) * 1000)
            logger.info("Context manifest {}", state.manifest.to_log())

        if config.context.mode == "enforce":
            prepared = deepcopy(messages)
            image_selection = reference_verified_images(
                prepared,
                verified_message_indexes=set(self._verified_positions(state.compaction, messages)),
                workspace=config.workspace,
            )
            prepared = image_selection.messages
            if image_selection.references:
                plan = apply_image_references(plan, image_selection)
            reason = (
                "unknown_budget" if budget is None else
                "irreducible_floor" if budget <= 0 else
                "runtime_root_required" if config.runtime_data_dir is None and config.artifact_store is None else
                "malformed_tool_transcript" if not self.has_valid_tool_pairs(messages) else
                "unknown_image_cost" if plan.predicted_total is None else None
            )
            if reason is None and "L1" in config.context.enabled_layers:
                prepared, plan, reason = self._fit_tool_results(config, prepared, plan, tool_definitions)
            if reason in {None, "irreducible_floor"} and "L2" in config.context.enabled_layers:
                prepared, plan, reason = self._snip_verified_history(
                    state, messages, prepared, plan, tool_definitions)
            if reason in {None, "irreducible_floor"} and "L3" in config.context.enabled_layers:
                prepared, plan, reason = self._shorten_verified_results(state, messages, prepared, plan)
            if (reason is None and "L1" in config.context.enabled_layers
                    and not self._tool_result_limits_fit(config, prepared)):
                reason = "irreducible_floor"
            provisional = [
                {key: value for key, value in message.items() if key != "_meta"}
                for message in prepared
            ]
            provisional_estimate, _ = estimate_model_request_tokens(
                config.provider,
                config.model,
                provisional,
                tool_definitions,
            )
            forced_l4 = state.force_l4_reason is not None
            l2_compacted = any(
                decision.stage == "L2" and decision.action == "reference"
                for decision in plan.decisions
            )
            automatic_l4 = (
                reason in {None, "irreducible_floor"}
                and state.compaction is not None
                and bool(state.compaction.accepted_messages)
                and budget is not None
                and provisional_estimate is not None
                and (
                    provisional_estimate > budget * config.context.high_watermark
                    or l2_compacted
                )
            )
            if (
                "L4" in config.context.enabled_layers
                and reason in {None, "irreducible_floor"}
                and (forced_l4 or automatic_l4)
            ):
                pre_l4_plan = plan
                prepared, plan, l4_reason = await self._prepare_enforced_l4(
                    state,
                    messages,
                    plan,
                    (
                        provisional_estimate or 0,
                        state.force_l4_reason or "high_watermark",
                    ),
                    tool_definitions=tool_definitions,
                    deferred_tool_names=deferred_tool_names,
                )
                if l4_reason is None:
                    reason = None
                elif reason == "irreducible_floor" and l4_reason == "unknown_consumption":
                    # With no accepted H there is nothing L4 may replace; retain
                    # the original, more specific required-context failure.
                    pass
                elif (
                    not forced_l4
                    and reason is None
                    and l4_reason in {
                        "unknown_consumption",
                        "invalid_context_summary",
                        "irreducible_floor",
                        "unknown_image_cost",
                    }
                    and provisional_estimate is not None
                    and budget is not None
                    and provisional_estimate <= budget
                ):
                    # Proactive compaction may safely no-op while the exact
                    # request still fits. Hard overflow remains fail closed.
                    prepared = provisional
                    plan = pre_l4_plan
                else:
                    reason = l4_reason
            prepared = [{key: value for key, value in message.items() if key != "_meta"}
                        for message in prepared]
            estimate, _ = estimate_model_request_tokens(
                config.provider,
                config.model,
                prepared,
                tool_definitions,
            )
            plan = replace(plan, rendered_estimate=estimate if plan.predicted_total is not None else None)
            if reason is None and estimate is None:
                reason = "unknown_image_cost"
            if reason is None and budget is not None and estimate is not None and estimate > budget:
                reason = "irreducible_floor"
            if reason is not None:
                record("refused", reason, None)
                assert state.manifest is not None
                raise ContextPlanError(state.manifest)
            provider_context = state.conversation.prepare_request(
                transcript, context_window_tokens=config.context_window_tokens,
                model_messages=prepared,
            ) if transcript is not None else state.conversation.independent_request_context(
                context_window_tokens=config.context_window_tokens)
            state.messages = deepcopy(prepared)
            state.tool_definitions = deepcopy(tool_definitions)
        else:
            archive_token = _archive_failed.set(False)
            try:
                prepared, provider_context = await self._prepare_legacy_request(
                    state, messages, tool_definitions=tool_definitions, transcript=transcript)
            except Exception as exc:
                record("refused", "archive_failed" if _archive_failed.get() else (
                    "irreducible_floor" if isinstance(exc, ContextWindowExceededError)
                    else "legacy_governance_failed"), None)
                raise
            finally:
                archive_failed = _archive_failed.get()
                _archive_failed.reset(archive_token)
            reconciled = reconcile_plan(config.initial_plan, prepared, tool_definitions,
                                        input_budget=plan.input_budget, model=config.model,
                                        merge_messages=self._merge_adjacent_user_messages_for_model)
            # Observation uses the local counter without invoking the provider counter again.
            reconciled = reestimate_plan_images(reconciled, prepared, config.model)
            estimate, _ = estimate_model_request_tokens(
                None,
                config.model,
                prepared,
                tool_definitions,
            )
            plan = replace(reconciled, plan_id=plan.plan_id, request_id=plan.request_id,
                           rendered_estimate=estimate if reconciled.predicted_total is not None else None)
            plan = append_deferred_schema_audit(
                plan,
                config.tools.get_definitions(),
                deferred_tool_names,
            )
        record("prepared", "archive_failed" if config.context.mode == "observe" and archive_failed
               else "unknown_budget" if budget is None else
               "image_reference" if any(d.reason_code == "image_reference" for d in plan.decisions) else
               "summarized" if any(d.stage == "L4" and d.reason_code == "summarized" for d in plan.decisions)
               else
               "offloaded" if any(d.stage == "L1" and d.action == "reference" for d in plan.decisions)
               else "snipped" if any(d.stage == "L2" and d.action == "reference" for d in plan.decisions)
               else "micro_compacted" if any(d.stage == "L3" and d.action == "reference" for d in plan.decisions)
               else "schema_deferred" if any(d.reason_code == "schema_deferred" for d in plan.decisions)
               else "selection_only", prepared)
        state.request_attempt += 1
        request = ContextRequestOutcome(plan.request_id, state.lineage_id, state.request_attempt, "unknown")
        provider_context = replace(
            provider_context or ProviderCallContext(), context_request=request,
            context_payload_hash=content_hash({"messages": prepared, "tools": tool_definitions}))
        if state.compaction is not None:
            if transcript is not None:
                state.compaction.begin_request(prepared, raw_boundary=len(transcript), request=request)
            else:
                state.compaction.pending_request = None
        return prepared, provider_context

    @staticmethod
    def has_valid_tool_pairs(messages: list[dict[str, Any]]) -> bool:
        """Require complete consecutive call/result batches without modifying the transcript."""
        pending: set[str] = set()
        declared: set[str] = set()
        for message in messages:
            role = message.get("role")
            calls = message.get("tool_calls")
            if role == "tool":
                call_id = message.get("tool_call_id")
                if calls or not isinstance(call_id, str) or call_id not in pending:
                    return False
                pending.remove(call_id)
                continue
            if pending:
                return False
            if not calls:
                continue
            if role != "assistant" or not isinstance(calls, list):
                return False
            for call in cast(list[object], calls):
                if not isinstance(call, dict) or not _tool_call_name_is_valid(call):
                    return False
                call_id = cast(dict[str, Any], call).get("id")
                if not isinstance(call_id, str) or not call_id or call_id in declared:
                    return False
                declared.add(call_id)
                pending.add(call_id)
        return not pending

    @staticmethod
    def planning_input_budget(config: ContextGovernanceConfig) -> int | None:
        window = config.context_window_tokens
        maximum = config.max_tokens
        if not isinstance(window, int) or window <= 0 or not isinstance(maximum, int) or maximum <= 0:
            return None
        margin = config.context.safety_margin_tokens or max(1024, ceil(window * 0.05))
        budget = window - maximum - margin
        return min(budget, config.context_block_limit) if config.context_block_limit else budget

    @staticmethod
    def record_dispatch(state: ModelRequestState) -> None:
        if state.manifest is not None:
            state.manifest = replace(state.manifest, status="dispatched")
            logger.info("Context manifest {}", state.manifest.to_log())

    @staticmethod
    def record_response(state: ModelRequestState, response: LLMResponse) -> None:
        compaction = state.compaction
        if (compaction is not None and state.messages is not None
                and response.finish_reason not in {"error", "cancelled"}):
            compaction.accept_request(state.messages, raw_boundary=compaction.pending_raw_boundary,
                                      outcome=response.context_outcome)
        if state.manifest is None:
            return
        usage = response.usage
        actual = usage.input_tokens if (usage is not None and usage.source == "reported"
                                       and usage.request_count == 1) else None
        estimate = state.manifest.plan.rendered_estimate
        state.manifest = replace(
            state.manifest, status="responded", actual_input_tokens=actual,
            reason_code="provider_error" if response.finish_reason == "error" else "response_received",
            estimation_error=actual - estimate if actual is not None and estimate is not None else None)
        logger.info("Context manifest {}", state.manifest.to_log())

    async def _prepare_legacy_request(
        self,
        state: ModelRequestState,
        messages: list[dict[str, Any]],
        *,
        tool_definitions: list[dict[str, Any]] | None,
        transcript: list[dict[str, Any]] | None = None,
    ) -> tuple[list[dict[str, Any]], ProviderCallContext | None]:
        """Prepare, compact or fit, and record the exact provider payload."""
        prepared = self.prepare_messages_for_model(state.config, messages)
        model_messages: list[dict[str, Any]] | None = prepared
        supplemental_messages: list[dict[str, Any]] | None = None
        request_context_tokens = None
        if transcript is not None:
            if tool_definitions is None:
                model_messages = None
                supplemental_messages = [prepared[-1]]
            request_context_tokens = state.conversation.estimate_request_context_tokens(
                transcript,
                model_messages=model_messages,
                supplemental_messages=supplemental_messages,
                tool_definitions=tool_definitions,
            )
        usage_matches_messages = (
            state.messages is not None
            and prepared == state.messages
            and tool_definitions == state.tool_definitions
        )
        request_was_fitted = False
        compaction = state.compaction
        if compaction is None:
            prepared, request_was_fitted = self.fit_request(
                state.config,
                prepared,
                state.usage,
                usage_matches_messages=usage_matches_messages,
                tool_definitions=tool_definitions,
                request_context_tokens=request_context_tokens,
            )
        else:
            pressure = self.request_pressure(
                state.config,
                prepared,
                state.usage,
                usage_matches_messages=usage_matches_messages,
                tool_definitions=tool_definitions,
                request_context_tokens=request_context_tokens,
            )
            if pressure is not None:
                prepared = await self._compact_request_history(
                    state,
                    compaction,
                    messages,
                    pressure,
                    tool_definitions=tool_definitions,
                )
                model_messages = prepared
                supplemental_messages = None
        provider_context = (
            state.conversation.prepare_request(
                transcript,
                context_window_tokens=state.config.context_window_tokens,
                model_messages=model_messages,
                supplemental_messages=supplemental_messages,
                resume_state=not request_was_fitted,
            )
            if transcript is not None
            else state.conversation.independent_request_context(
                context_window_tokens=state.config.context_window_tokens,
            )
        )
        state.messages = deepcopy(prepared)
        state.tool_definitions = deepcopy(tool_definitions)
        return prepared, provider_context

    @staticmethod
    def input_budget(config: ContextGovernanceConfig) -> int:
        if not config.context_window_tokens:
            return 0

        provider_max_tokens = getattr(
            getattr(config.provider, "generation", None),
            "max_tokens",
            4096,
        )
        max_output = config.max_tokens if isinstance(config.max_tokens, int) else (
            provider_max_tokens if isinstance(provider_max_tokens, int) else 4096
        )
        budget = config.context_block_limit or (
            config.context_window_tokens - max_output - SNIP_SAFETY_BUFFER
        )
        return budget if budget > 0 else 0

    @staticmethod
    def normalize_tool_result(
        config: ContextGovernanceConfig,
        tool_call_id: str,
        tool_name: str,
        result: Any,
    ) -> Any:
        result = ensure_nonempty_tool_result(tool_name, result)
        if config.context.mode == "enforce":
            # Keep raw evidence in transcripts/checkpoints; L1 works on a derived whole batch.
            return result
        if tool_name in TOOL_RESULT_OFFLOAD_EXEMPT_TOOLS:
            return result
        try:
            content = maybe_persist_tool_result(
                config.workspace,
                config.session_key,
                tool_call_id,
                result,
                max_chars=config.max_tool_result_chars,
            )
        except Exception:
            _archive_failed.set(True)
            logger.warning(
                "Tool result persist failed for {} in {}; using raw result",
                tool_call_id,
                config.session_key or "default",
            )
            content = result
        if isinstance(content, str) and len(content) > config.max_tool_result_chars:
            return truncate_text(content, config.max_tool_result_chars)
        return content

    @staticmethod
    def strip_placeholder_assistant_messages(
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Remove assistant messages that are compaction placeholders.

        Messages like ``[Previous assistant message omitted.]`` carry no useful
        context for the model and can cause it to repeatedly attempt tool calls
        that previously failed, producing malformed responses in a loop.
        Consecutive same-role messages that result from removal are handled
        downstream by the provider's merge-consecutive logic. Only the
        model-facing copy is repaired; the persisted transcript is untouched
        (a copy is returned, or the same list object when nothing changes).
        """
        updated: list[dict[str, Any]] | None = None
        for idx, msg in enumerate(messages):
            if msg.get("role") != "assistant":
                if updated is not None:
                    updated.append(msg)
                continue
            content = msg.get("content", "")
            text = content if isinstance(content, str) else ""
            is_placeholder = text.strip() in PLACEHOLDER_TEXTS
            has_tool_calls = bool(msg.get("tool_calls"))
            if is_placeholder and not has_tool_calls:
                if updated is None:
                    updated = list(messages[:idx])
                logger.debug(
                    "Stripping placeholder assistant message from history: {!r}",
                    text[:60],
                )
                continue
            if updated is not None:
                updated.append(msg)
        if updated is None:
            return messages
        return updated

    @staticmethod
    def strip_malformed_tool_calls(
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Drop persisted assistant tool_calls whose name is missing/non-string.

        A degenerate tool call (``name=None`` or ``""``) that slipped into the
        saved history before this guard existed gets replayed on every turn and
        makes upstream APIs reject the whole request
        (``messages.content.N.tool_use.name: Input should be a valid string``),
        permanently wedging the session. Removing the bad call here lets the
        existing orphan-result cleanup drop its now-dangling tool result, so a
        polluted session self-heals on its next turn. The persisted transcript
        is left untouched; only the model-facing copy is repaired (a copy is
        returned, or the same list object when nothing changes).
        """
        updated: list[dict[str, Any]] | None = None
        for idx, msg in enumerate(messages):
            if msg.get("role") != "assistant":
                if updated is not None:
                    updated.append(msg)
                continue
            calls = msg.get("tool_calls")
            if not calls:
                if updated is not None:
                    updated.append(msg)
                continue
            kept = [tc for tc in cast(list[Any], calls) if _tool_call_name_is_valid(tc)]
            if len(kept) == len(calls):
                if updated is not None:
                    updated.append(msg)
                continue
            if updated is None:
                updated = [dict(m) for m in messages[:idx]]
            logger.warning(
                "Stripping {} malformed tool_call(s) with missing/non-string "
                "name from assistant history before request",
                len(calls) - len(kept),
            )
            repaired = dict(msg)
            if kept:
                repaired["tool_calls"] = kept
            else:
                repaired.pop("tool_calls", None)
            # An assistant turn with neither content nor any valid tool call is
            # itself invalid upstream; drop it entirely in that case.
            has_content = bool(repaired.get("content"))
            if not kept and not has_content:
                continue
            updated.append(repaired)

        if updated is None:
            return messages
        return updated

    @staticmethod
    def drop_orphan_tool_results(
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Drop invalid tool results before history is sent back to providers."""
        declared: set[str] = set()
        fulfilled: set[str] = set()
        updated: list[dict[str, Any]] | None = None
        for idx, msg in enumerate(messages):
            role = msg.get("role")
            if role == "assistant":
                for tc in cast(list[Any], msg.get("tool_calls") or []):
                    if isinstance(tc, dict):
                        tool_call = cast(dict[str, Any], tc)
                        if tool_call.get("id"):
                            declared.add(str(tool_call["id"]))
            if role == "tool":
                tid = msg.get("tool_call_id")
                tid_str = str(tid) if tid else ""
                if not tid_str or tid_str not in declared or tid_str in fulfilled:
                    if updated is None:
                        updated = [dict(m) for m in messages[:idx]]
                    continue
                fulfilled.add(tid_str)
            if updated is not None:
                updated.append(dict(msg))

        if updated is None:
            return messages
        return updated

    @staticmethod
    def backfill_missing_tool_results(
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Insert synthetic error results for assistant tool_calls with missing tool outputs."""
        declared: list[tuple[int, str, str]] = []
        fulfilled: set[str] = set()
        for idx, msg in enumerate(messages):
            role = msg.get("role")
            if role == "assistant":
                for tc in cast(list[Any], msg.get("tool_calls") or []):
                    if isinstance(tc, dict):
                        name = ""
                        tool_call = cast(dict[str, Any], tc)
                        if tool_call.get("id"):
                            func = tool_call.get("function")
                            if isinstance(func, dict):
                                func_data = cast(dict[str, Any], func)
                                raw_name = func_data.get("name", "")
                                name = raw_name if isinstance(raw_name, str) else str(raw_name)
                            declared.append((idx, str(tool_call["id"]), name))
            elif role == "tool":
                tid = msg.get("tool_call_id")
                if tid:
                    fulfilled.add(str(tid))

        missing = [(ai, cid, name) for ai, cid, name in declared if cid not in fulfilled]
        if not missing:
            return messages

        updated = list(messages)
        offset = 0
        for assistant_idx, call_id, name in missing:
            insert_at = assistant_idx + 1 + offset
            while insert_at < len(updated) and updated[insert_at].get("role") == "tool":
                insert_at += 1
            updated.insert(insert_at, {
                "role": "tool",
                "tool_call_id": call_id,
                "name": name,
                "content": BACKFILL_CONTENT,
            })
            offset += 1
        return updated

    def apply_tool_result_budget(
        self,
        config: ContextGovernanceConfig,
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        updated = messages
        for idx, message in enumerate(messages):
            if message.get("role") != "tool":
                continue
            normalized = self.normalize_tool_result(
                config,
                str(message.get("tool_call_id") or f"tool_{idx}"),
                str(message.get("name") or "tool"),
                message.get("content"),
            )
            if normalized != message.get("content"):
                if updated is messages:
                    updated = [dict(m) for m in messages]
                updated[idx]["content"] = normalized
        return updated

    @staticmethod
    def _tool_result_limits_fit(config: ContextGovernanceConfig, messages: list[dict[str, Any]]) -> bool:
        """Later layers may remove a failed L1 group; no-op layers cannot waive its limits."""
        batch_cost = 0
        for message in messages:
            if message.get("role") != "tool":
                batch_cost = 0
                continue
            cost = estimate_prompt_tokens([message]) - 4
            batch_cost += cost
            if (cost > config.context.tool_result_token_budget
                    or batch_cost > config.context.tool_batch_token_budget):
                return False
        return True

    @staticmethod
    def _share_budget(costs: dict[int, int], budget: int, protected: set[int]) -> dict[int, int] | None:
        """Water-fill adjustable results, retaining small results and protected pages intact."""
        fixed = {index: cost for index, cost in costs.items() if index in protected}
        available = budget - sum(fixed.values())
        pending: dict[int, int] = {index: cost for index, cost in costs.items() if index not in protected}
        if available < 0:
            return None
        while pending:
            share, remainder = divmod(available, len(pending))
            small: dict[int, int] = {index: cost for index, cost in pending.items() if cost <= share}
            if not small:
                fixed.update({index: share + (offset < remainder)
                              for offset, index in enumerate(pending)})
                break
            fixed.update(small)
            available -= sum(small.values())
            pending = {index: cost for index, cost in pending.items() if index not in small}
        return fixed

    def _fit_tool_results(
        self, config: ContextGovernanceConfig, messages: list[dict[str, Any]],
        plan: ContextPlan, tool_definitions: list[dict[str, Any]] | None,
    ) -> tuple[list[dict[str, Any]], ContextPlan, str | None]:
        """L1 edits only the request copy, after durable archival of the complete result."""
        costs = {i: estimate_prompt_tokens([message]) - 4 for i, message in enumerate(messages)
                 if message.get("role") == "tool"}
        if not costs:
            return messages, plan, None
        protected = {i for i in costs if messages[i].get("name") == "context_artifact_read"}
        single = config.context.tool_result_token_budget
        if any(costs[i] > single for i in protected):
            return messages, plan, "irreducible_floor"
        allocations = {i: min(cost, single) for i, cost in costs.items()}
        # Pair validation runs before L1, so consecutive tool messages form complete batches.
        for start in costs:
            if start - 1 in costs:
                continue
            indexes = [start]
            while indexes[-1] + 1 in costs:
                indexes.append(indexes[-1] + 1)
            shared = self._share_budget({i: allocations[i] for i in indexes},
                                        config.context.tool_batch_token_budget, protected)
            if shared is None:
                return messages, plan, "irreducible_floor"
            allocations.update(shared)
        # Each separately counted result also introduces a joining separator in the full
        # request. Reserve it here; the final provider-aware count remains authoritative.
        fixed = estimate_prompt_tokens([m for i, m in enumerate(messages) if i not in costs],
                                       tool_definitions) + 5 * len(costs)
        assert plan.input_budget is not None
        shared = self._share_budget(allocations, plan.input_budget - fixed, protected)
        if shared is None:
            return messages, plan, "irreducible_floor"
        allocations = shared
        decisions = list(plan.decisions)
        used: set[str] = set()
        failure: str | None = None
        for index, cost in costs.items():
            if cost <= allocations[index]:
                continue
            message = messages[index]
            body = {key: value for key, value in message.items() if key != "_meta"}
            source = next(s for s in plan.sources if s.kind == "tool_result"
                          and s.content_hash == content_hash(body) and s.source_id not in used)
            used.add(source.source_id)
            content = message.get("content")
            serialized = content if isinstance(content, str) else json.dumps(
                {"schema_version": 1, "kind": "tool_result_blocks", "blocks": content},
                ensure_ascii=False, separators=(",", ":"))
            try:
                if not config.session_key:
                    raise ValueError("Artifact session scope required")
                if config.artifact_store is None:
                    from nanobot.agent.context_artifacts import ToolResultArtifactStore

                    assert config.runtime_data_dir is not None
                    config.artifact_store = ToolResultArtifactStore(
                        config.runtime_data_dir / "context-artifacts",
                        max_bytes=config.context.artifact_max_bytes)
                ref = config.artifact_store.put(config.session_key,
                                                str(message["tool_call_id"]), serialized)
            except (OSError, ValueError):
                decisions.append(ContextDecision(source.source_id, "keep", "L1", "archive_failed",
                                                 source.estimated_tokens, source.estimated_tokens))
                failure = "archive_failed"
                continue
            meta = message.get("_meta")
            preview: dict[str, Any] = {
                "schema_version": 1, "kind": "tool_result_artifact", "tool": message.get("name"),
                "artifact_id": ref.artifact_id, "content_hash": ref.content_hash,
                "byte_length": ref.byte_length, "format": "text" if isinstance(content, str) else "blocks-json-v1",
                "is_error": bool(cast(dict[str, Any], meta).get("tool_result_error")) if isinstance(meta, dict) else False,
                "preview": "", "read_tool": "context_artifact_read",
            }

            def render(length: int) -> dict[str, Any]:
                return {**message, "content": json.dumps({**preview, "preview": serialized[:length]},
                                                         ensure_ascii=False, separators=(",", ":"))}

            minimum_prefix = min(256, len(serialized))
            if estimate_prompt_tokens([render(minimum_prefix)]) - 4 > allocations[index]:
                decisions.append(ContextDecision(source.source_id, "keep", "L1", "irreducible_floor",
                                                 source.estimated_tokens, source.estimated_tokens))
                failure = failure or "irreducible_floor"
                continue
            low, high = minimum_prefix, len(serialized)
            while low < high:
                middle = (low + high + 1) // 2
                if estimate_prompt_tokens([render(middle)]) - 4 <= allocations[index]:
                    low = middle
                else:
                    high = middle - 1
            messages[index] = render(low)
            after = estimate_prompt_tokens([messages[index]]) - 4
            decisions.append(ContextDecision(source.source_id, "reference", "L1", "offloaded",
                                             source.estimated_tokens, after, ref.artifact_id))
        final = {decision.source_id: decision.after_tokens for decision in decisions}
        total = sum(value for value in final.values() if value is not None) if all(
            value is not None for value in final.values()) else None
        return messages, replace(plan, decisions=tuple(decisions), predicted_total=total), failure

    @staticmethod
    def _verified_positions(
        compaction: ContextCompactionState | None, messages: list[dict[str, Any]],
    ) -> dict[int, int]:
        """Map unchanged request messages into an authenticated ordered raw prefix."""
        if compaction is None or not compaction.accepted_raw_hashes:
            return {}
        raw = compaction.raw_messages[:compaction.raw_accepted_boundary]
        if tuple(content_hash(m) for m in raw) != compaction.accepted_raw_hashes:
            return {}
        public_hashes = [content_hash({k: v for k, v in m.items() if k != "_meta"}) for m in raw]
        accepted_hashes = {content_hash({k: v for k, v in m.items() if k != "_meta"})
                           for m in compaction.accepted_messages}
        cursor = 0
        positions: dict[int, int] = {}
        for index, message in enumerate(messages):
            digest = content_hash({k: v for k, v in message.items() if k != "_meta"})
            if index >= len(compaction.accepted_messages) or digest not in accepted_hashes:
                continue
            match = next((i for i in range(cursor, len(raw)) if public_hashes[i] == digest), None)
            if match is not None:
                positions[index] = match
                cursor = match + 1
        return positions

    @staticmethod
    def _layer_plan(
        plan: ContextPlan, originals: list[dict[str, Any]], indexes: list[int],
        replacement: list[dict[str, Any]], stage: Literal["L2", "L3"], reason: str,
        artifact_id: str | None, message_count: int,
    ) -> ContextPlan:
        """Keep source identities and append transformations; charge final forms once."""
        decisions = list(plan.decisions)
        sources = list(plan.sources)
        used: set[str] = set()
        removed = {d.source_id for d in decisions
                   if d.stage == "selection" and d.reason_code == "legacy_replaced_or_removed"}
        replacement_cost = sum(estimate_prompt_tokens([m]) - 4 for m in replacement)
        # Resolve occurrences across the whole original request, not just this group.
        for index, original in enumerate(originals):
            digest = content_hash({k: v for k, v in original.items() if k != "_meta"})
            matches = [s for s in sources if s.content_hash == digest
                       and s.source_id not in used and s.source_id not in removed]
            for source in matches[:1]:
                used.add(source.source_id)
                if index not in indexes:
                    continue
                current = next(d.after_tokens for d in reversed(decisions) if d.source_id == source.source_id)
                after = current if reason == "archive_failed" else replacement_cost
                decisions.append(ContextDecision(source.source_id,
                                                 "keep" if reason == "archive_failed" else "reference",
                                                 stage, reason, current, after, artifact_id))
                if reason != "archive_failed":
                    replacement_cost = 0
                    sources[sources.index(source)] = replace(source, required=False, priority=50,
                                                            allowed_strategies=("keep", "reference"))
        previous = next(d.after_tokens for d in reversed(decisions) if d.source_id == "envelope")
        decisions.append(ContextDecision("envelope", "keep", stage, reason, previous, 4 * message_count))
        final = {d.source_id: d.after_tokens for d in decisions}
        total = sum(v for v in final.values() if v is not None) if all(v is not None for v in final.values()) else None
        return replace(plan, sources=tuple(sources), decisions=tuple(decisions), predicted_total=total)

    @staticmethod
    def _history_artifact(config: ContextGovernanceConfig, compaction: ContextCompactionState,
                          key: str, text: str) -> str:
        if not config.session_key:
            raise ValueError("Artifact scope required")
        if config.artifact_store is None:
            from nanobot.agent.context_artifacts import ToolResultArtifactStore

            if config.runtime_data_dir is None:
                raise ValueError("Artifact root required")
            config.artifact_store = ToolResultArtifactStore(
                config.runtime_data_dir / "context-artifacts", max_bytes=config.context.artifact_max_bytes)
        cached = compaction.history_archives.get(key)
        if cached is not None:
            config.artifact_store.resolve(cached, config.session_key)
            return cached
        ref = config.artifact_store.put(config.session_key, key, text)
        compaction.history_archives[key] = ref.artifact_id
        return ref.artifact_id

    def _snip_verified_history(
        self, state: ModelRequestState, originals: list[dict[str, Any]],
        prepared: list[dict[str, Any]], plan: ContextPlan,
        tools: list[dict[str, Any]] | None,
    ) -> tuple[list[dict[str, Any]], ContextPlan, str | None]:
        config, compaction = state.config, state.compaction
        budget = plan.input_budget
        if compaction is None or budget is None:
            return prepared, plan, None
        estimate, _ = estimate_prompt_tokens_chain(config.provider, config.model, prepared, tools)
        if estimate <= budget * config.context.high_watermark:
            return prepared, plan, None
        positions = self._verified_positions(compaction, originals)
        starts = [i for i, m in enumerate(originals) if m.get("role") == "user"]
        removed = 0
        for start, end in zip(starts, starts[1:]):
            indexes = list(range(start, end))
            raw_indexes = [positions.get(i, -1) for i in indexes]
            if (not raw_indexes or raw_indexes[0] < 0
                    or raw_indexes != list(range(raw_indexes[0], raw_indexes[0] + len(indexes)))
                    or any(originals[i].get("role") == "system" for i in indexes)
                    or not self.has_valid_tool_pairs(originals[start:end])
                    or find_legal_message_start(originals[end:]) != 0):
                continue
            raw_start, raw_end = raw_indexes[0], raw_indexes[-1] + 1
            # Synthetic user suffixes do not close the actual current transcript turn.
            if raw_end > 1 + len(compaction.transcript_input.history):
                continue
            archived = compaction.raw_messages[raw_start:raw_end]
            key = f"history:{raw_start}:{raw_end}:{content_hash(archived)}"
            try:
                ref = self._history_artifact(config, compaction, key, json.dumps(archived, ensure_ascii=False))
            except (OSError, ValueError):
                plan = self._layer_plan(plan, originals, indexes, [], "L2", "archive_failed", None, len(prepared))
                return prepared, plan, "archive_failed"
            marker = {"role": "user", "content": json.dumps({
                "kind": "history_artifact", "artifact_id": ref, "read_tool": "context_artifact_read",
                "raw_start": raw_start, "raw_end": raw_end,
                "preview": json.dumps(archived, ensure_ascii=False)[:256],
            }, separators=(",", ":"))}
            prepared[start - removed:end - removed] = [marker]
            removed += end - start - 1
            plan = self._layer_plan(plan, originals, indexes, [marker], "L2", "snipped", ref, len(prepared))
            estimate, _ = estimate_prompt_tokens_chain(config.provider, config.model, prepared, tools)
            if estimate <= budget * config.context.target_ratio:
                break
        return prepared, plan, None

    def _shorten_verified_results(
        self, state: ModelRequestState, originals: list[dict[str, Any]],
        prepared: list[dict[str, Any]], plan: ContextPlan,
    ) -> tuple[list[dict[str, Any]], ContextPlan, str | None]:
        compaction = state.compaction
        if compaction is None:
            return prepared, plan, None
        positions = self._verified_positions(compaction, originals)
        paths: dict[str, str] = {}
        for message in originals:
            # prepare_request validated the call/result shape before entering L3.
            for call in cast(list[dict[str, Any]], message.get("tool_calls") or []):
                fn_value = call.get("function", {})
                if not isinstance(fn_value, dict):
                    continue
                fn = cast(dict[str, Any], fn_value)
                if fn.get("name") != "read_file":
                    continue
                try:
                    args = fn.get("arguments", {})
                    args = json.loads(args) if isinstance(args, str) else args
                except ValueError:
                    continue
                if isinstance(args, dict):
                    path = cast(dict[str, Any], args).get("path")
                    if isinstance(path, str):
                        paths[call["id"]] = path
        seen: set[tuple[str, str]] = set()
        for index, original in enumerate(originals):
            call_id = original.get("tool_call_id")
            content = original.get("content")
            if (index not in positions or original.get("role") != "tool"
                    or original.get("name") != "read_file" or call_id not in paths
                    or not isinstance(content, str)):
                continue
            raw_meta = compaction.raw_messages[positions[index]].get("_meta")
            if isinstance(raw_meta, dict) and cast(dict[str, Any], raw_meta).get("tool_result_error") is True:
                continue
            target = next((i for i, m in enumerate(prepared)
                           if m.get("role") == "tool" and m.get("tool_call_id") == call_id), None)
            if target is None:
                continue
            version = (paths[call_id], content_hash(content))
            duplicate = version in seen
            seen.add(version)
            if not duplicate or len(content) <= 512:
                continue
            key = f"result:{positions[index]}:{version[1]}:{content_hash(version[0])}"
            try:
                ref = self._history_artifact(state.config, compaction, key, content)
            except (OSError, ValueError):
                plan = self._layer_plan(plan, originals, [index], [], "L3", "archive_failed", None, len(prepared))
                return prepared, plan, "archive_failed"
            shortened = {**prepared[target], "content": json.dumps({
                "kind": "tool_result_artifact", "artifact_id": ref,
                "read_tool": "context_artifact_read", "preview": content[:256],
                "content_version": version[1], "reason": "same_path_and_version",
            }, ensure_ascii=False, separators=(",", ":"))}
            if estimate_prompt_tokens([shortened]) >= estimate_prompt_tokens([prepared[target]]):
                continue
            prepared[target] = shortened
            plan = self._layer_plan(plan, originals, [index], [shortened], "L3", "micro_compacted", ref, len(prepared))
        return prepared, plan, None

    def snip_history(
        self,
        config: ContextGovernanceConfig,
        messages: list[dict[str, Any]],
        *,
        tool_definitions: list[dict[str, Any]] | None,
        force: bool = False,
    ) -> list[dict[str, Any]]:
        if not messages or not config.context_window_tokens:
            return messages

        budget = self.input_budget(config)
        if budget <= 0:
            return messages

        if not force:
            estimate, _ = estimate_prompt_tokens_chain(
                config.provider,
                config.model,
                messages,
                tool_definitions,
            )
            if estimate <= budget:
                return messages

        system_messages = [dict(msg) for msg in messages if msg.get("role") == "system"]
        non_system = [dict(msg) for msg in messages if msg.get("role") != "system"]
        if not non_system:
            return messages

        system_tokens = sum(estimate_message_tokens(msg) for msg in system_messages)
        fixed_tokens, _ = estimate_prompt_tokens_chain(
            config.provider,
            config.model,
            system_messages,
            tool_definitions,
        )
        remaining_budget = max(0, budget - max(system_tokens, fixed_tokens))
        kept: list[dict[str, Any]] = []
        kept_tokens = 0
        for message in reversed(non_system):
            msg_tokens = estimate_message_tokens(message)
            if kept and kept_tokens + msg_tokens > remaining_budget:
                break
            kept.append(message)
            kept_tokens += msg_tokens
        kept.reverse()

        return system_messages + self._legal_history_tail(kept, non_system)

    def _legal_history_tail(
        self,
        kept: list[dict[str, Any]],
        non_system: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        fallback = kept if kept else (non_system[-1:] if non_system else [])
        kept = self._user_tail(kept) or self._user_tail(non_system, last=True) or fallback

        start = find_legal_message_start(kept)
        return kept[start:] if start else kept

    @staticmethod
    def _user_tail(messages: list[dict[str, Any]], *, last: bool = False) -> list[dict[str, Any]]:
        indexes = range(len(messages) - 1, -1, -1) if last else range(len(messages))
        for idx in indexes:
            if messages[idx].get("role") == "user":
                return messages[idx:]
        return []
