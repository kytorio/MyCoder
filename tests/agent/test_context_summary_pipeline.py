from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from agent.runner_helpers import make_run_spec
from nanobot.agent.context import TranscriptInput
from nanobot.agent.context_governance import (
    ContextCompactionState,
    ContextGovernanceConfig,
    ContextGovernor,
    ModelRequestState,
)
from nanobot.agent.context_plan import (
    ContextConsumption,
    ContextPlanError,
    ContextRequestOutcome,
    ContextSummaryCandidate,
    StructuredContextSummary,
    content_hash,
    history_message_hash,
    render_context_summary,
    validate_context_summary_candidate,
)
from nanobot.agent.runner import AgentRunner
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.config.schema import ContextConfig
from nanobot.providers.base import LLMProvider, LLMResponse
from nanobot.providers.conversation_state import ProviderConversationStateController


class SummaryProvider(LLMProvider):
    def __init__(self) -> None:
        super().__init__(provider_name="summary-test")

    async def chat(self, **kwargs) -> LLMResponse:
        return LLMResponse(content="unused")

    def get_default_model(self) -> str:
        return "summary-test"

    @staticmethod
    def estimate_prompt_tokens(messages, tools, model):
        del tools, model
        serialized = repr(messages)
        pressured = "accepted history" in serialized or "history_artifact" in serialized
        return (1_600 if pressured else 100), "summary-test"


def _structured_summary() -> StructuredContextSummary:
    return StructuredContextSummary(
        schema_version=1,
        tasks=["Continue the accepted task"],
        constraints=[],
        explicit_preferences=["Keep output concise."],
        decisions=[],
        file_changes=[],
        errors=[],
        evidence_refs=[],
        remaining_work=["Process the current delta"],
    )


def _state(
    tmp_path: Path,
    commit,
) -> tuple[ContextGovernor, ModelRequestState, list[dict]]:
    transcript_input = TranscriptInput(
        history=[
            {
                "role": "user",
                "content": "accepted history. Keep output concise.",
            }
        ],
        current_message="current delta must survive",
    )

    def build(transcript: TranscriptInput) -> list[dict]:
        system = "policy"
        if transcript.session_summary is not None:
            system += "\n" + transcript.session_summary["text"]
        messages = [{"role": "system", "content": system}, *deepcopy(transcript.history)]
        if transcript.current_message is not None:
            messages.append({"role": "user", "content": transcript.current_message})
        return messages

    candidate = ContextSummaryCandidate(
        text=render_context_summary(_structured_summary()),
        structured=_structured_summary(),
    )

    async def consolidate(messages, previous):
        del messages, previous
        return candidate

    raw, compaction = ContextCompactionState.from_transcript(
        transcript_input,
        build,
        consolidate,
        None,
        track_consumption=True,
    )
    assert compaction is not None
    compaction.accepted_messages = deepcopy(raw[:2])
    compaction.raw_accepted_boundary = 2
    compaction.accepted_raw_hashes = tuple(content_hash(message) for message in raw[:2])
    provider = SummaryProvider()
    config = ContextGovernanceConfig(
        provider=provider,
        model=provider.get_default_model(),
        tools=ToolRegistry(),
        workspace=tmp_path,
        session_key="test:summary",
        max_tool_result_chars=10_000,
        context_window_tokens=2_000,
        max_tokens=500,
        context=ContextConfig(
            mode="enforce",
            safety_margin_tokens=100,
            high_watermark=0.8,
        ),
        runtime_data_dir=tmp_path,
        summary_checkpoint_commit=commit,
    )
    state = ModelRequestState(
        config=config,
        conversation=ProviderConversationStateController(
            provider=provider,
            model=provider.get_default_model(),
            messages=raw,
            session_id="test:summary",
        ),
        compaction=compaction,
    )
    return ContextGovernor(), state, raw


def test_structured_summary_is_strict_and_complete() -> None:
    with pytest.raises(ValidationError):
        StructuredContextSummary.model_validate(
            {"schema_version": 1, "tasks": ["incomplete"]}
        )
    with pytest.raises(ValidationError):
        StructuredContextSummary.model_validate(
            {**_structured_summary().model_dump(), "untrusted": ["value"]}
        )


def test_summary_preferences_must_be_grounded_in_accepted_user_text() -> None:
    structured = _structured_summary().model_copy(
        update={"explicit_preferences": ["Use a preference that was never stated."]}
    )
    candidate = ContextSummaryCandidate(
        text=render_context_summary(structured),
        structured=structured,
    )
    assert not validate_context_summary_candidate(
        candidate,
        [{"role": "user", "content": "Keep output concise."}],
        artifact_store=None,
        session_key="test:summary",
    )


@pytest.mark.asyncio
async def test_enforced_pipeline_commits_l4_then_keeps_raw_delta(tmp_path: Path) -> None:
    committed = []
    state_holder = {}

    async def commit(checkpoint) -> None:
        state = state_holder["state"]
        assert state.compaction.active_summary is None
        committed.append(checkpoint)

    governor, state, raw = _state(tmp_path, commit)
    state_holder["state"] = state
    prepared, _ = await governor.prepare_request(
        state,
        state.compaction.request_messages(raw),
        tool_definitions=[],
        transcript=raw,
    )

    assert len(committed) == 1
    assert state.compaction.active_summary == committed[0].summary
    assert any(message.get("content") == "current delta must survive" for message in prepared)
    assert not any(message.get("content") == "accepted history. Keep output concise." for message in prepared)
    assert state.manifest is not None
    assert state.manifest.reason_code == "summarized"
    assert any(decision.stage == "L4" for decision in state.manifest.plan.decisions)
    source_kinds = {
        source.source_id: source.kind for source in state.manifest.plan.sources
    }
    assert not any(
        decision.stage == "L4"
        and source_kinds.get(decision.source_id)
        in {"system_policy", "memory", "skill_index", "skill_full", "mcp_schema"}
        for decision in state.manifest.plan.decisions
    )


@pytest.mark.asyncio
async def test_checkpoint_failure_does_not_swap_active_summary(tmp_path: Path) -> None:
    async def commit(checkpoint) -> None:
        del checkpoint
        raise OSError("checkpoint unavailable")

    governor, state, raw = _state(tmp_path, commit)
    with pytest.raises(ContextPlanError) as exc_info:
        await governor.prepare_request(
            state,
            state.compaction.request_messages(raw),
            tool_definitions=[],
            transcript=raw,
        )

    assert exc_info.value.manifest.reason_code == "checkpoint_commit_failed"
    assert state.compaction.active_summary is None
    assert state.compaction.summary_checkpoint is None
    assert state.compaction.raw_accepted_boundary == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("retry_succeeds", [True, False])
async def test_provider_overflow_gets_only_one_l4_retry_per_lineage(
    tmp_path: Path,
    retry_succeeds: bool,
) -> None:
    provider = SummaryProvider()
    provider.estimate_prompt_tokens = lambda *_args: (100, "summary-test")  # type: ignore[method-assign]
    overflow = LLMResponse(
        content="provider rejected input",
        finish_reason="error",
        error_code="context_length_exceeded",
    )
    provider.chat_with_retry = AsyncMock(  # type: ignore[method-assign]
        side_effect=[
            overflow,
            LLMResponse(content="done") if retry_succeeds else overflow,
        ]
    )
    history = [{"role": "user", "content": "accepted history. Keep output concise."}]
    accepted_messages = [
        {"role": "system", "content": "policy"},
        *history,
    ]
    consumption = ContextConsumption(
        outcome=ContextRequestOutcome("accepted", "prior", 0, "accepted"),
        history_hashes=(history_message_hash(history[0]),),
        accepted_messages_json=tuple(
            json.dumps(message, ensure_ascii=False, sort_keys=True)
            for message in accepted_messages
        ),
    )
    structured = _structured_summary()

    async def consolidate(messages, previous):
        del messages, previous
        return ContextSummaryCandidate(
            text=render_context_summary(structured),
            structured=structured,
        )

    committed = []

    async def commit(checkpoint) -> None:
        committed.append(checkpoint)

    def build(transcript: TranscriptInput) -> list[dict]:
        system = "policy"
        if transcript.session_summary is not None:
            system += "\n" + transcript.session_summary["text"]
        messages = [{"role": "system", "content": system}, *transcript.history]
        if transcript.current_message is not None:
            messages.append({"role": "user", "content": transcript.current_message})
        return messages

    result = await AgentRunner().run(
        make_run_spec(
            provider,
            initial_messages=None,
            transcript_input=TranscriptInput(
                history=history,
                current_message="current delta must survive",
            ),
            transcript_builder=build,
            consolidate_history=consolidate,
            summary_checkpoint_callback=commit,
            context_consumption=consumption,
            context_config=ContextConfig(
                mode="enforce",
                safety_margin_tokens=100,
            ),
            runtime_data_dir=tmp_path,
            tools=ToolRegistry(),
            workspace=tmp_path,
            session_key="test:overflow",
            model=provider.get_default_model(),
            context_window_tokens=2_000,
            max_tokens=500,
            max_iterations=1,
            max_tool_result_chars=10_000,
        )
    )

    assert provider.chat_with_retry.await_count == 2
    assert len(committed) == 1
    assert result.summary_checkpoint is not None
    assert result.stop_reason == ("completed" if retry_succeeds else "error")
