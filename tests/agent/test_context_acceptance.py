"""Host evidence, never response text, controls the accepted history boundary."""

from copy import deepcopy
from dataclasses import replace

import pytest

from nanobot.providers.base import LLMProvider, LLMResponse, ProviderCallContext


class EvidenceProvider(LLMProvider):
    """Offline transport double; the real base owns exception/retry/receipt logic."""

    _CHAT_RETRY_DELAYS = [0]

    def __init__(self, responses):
        super().__init__(provider_name="n07-offline")
        self.responses = iter(responses)

    async def chat(self, **kwargs):
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response

    def get_default_model(self):
        return "offline"


def bound_context(messages, tools=None):
    from nanobot.agent.context_plan import ContextRequestOutcome, content_hash

    return ProviderCallContext(
        context_request=ContextRequestOutcome("r1", "l1", 0, "unknown"),
        context_payload_hash=content_hash({"messages": messages, "tools": tools}))


def compaction_for(history=None):
    from nanobot.agent.context import TranscriptInput
    from nanobot.agent.context_governance import ContextCompactionState

    async def consolidate(messages, summary):
        return "summary"

    def build(transcript):
        return [{"role": "system", "content": "policy"}, *deepcopy(transcript.history),
                {"role": "user", "content": transcript.current_message}]

    return ContextCompactionState.from_transcript(
        TranscriptInput(history=history or [], current_message="current"), build, consolidate, None)


@pytest.mark.parametrize("status,expected", [
    ("accepted", True), ("rejected", False), ("unknown", False),
])
def test_acceptance_requires_evidence(status, expected):
    from nanobot.agent.context_plan import ContextRequestOutcome, should_accept

    assert should_accept(ContextRequestOutcome("r1", "lineage1", 0, status)) is expected


def test_invalid_outcome_status_cannot_become_evidence():
    from nanobot.agent.context_plan import ContextRequestOutcome

    with pytest.raises(ValueError):
        ContextRequestOutcome("r1", "lineage1", 0, "stop")


def test_imported_history_is_unknown_on_every_rebuild():
    history = [{"role": "user", "content": "old"}, {"role": "assistant", "content": "answer"}]
    for _ in range(2):
        raw, state = compaction_for(history)
        assert state.raw_accepted_boundary == 0
        assert state.accepted_messages == []
        assert state.request_messages(raw) == raw


@pytest.mark.parametrize("change", [
    {"status": "rejected"}, {"status": "unknown"}, {"request_id": "stale"},
    {"lineage_id": "other"}, {"attempt": 1}, {},
])
def test_only_matching_receipt_advances_frozen_dispatch_snapshot(change):
    from nanobot.agent.context_plan import ContextRequestOutcome

    raw, state = compaction_for()
    request = ContextRequestOutcome("r1", "l1", 0, "unknown")
    state.begin_request(raw, raw_boundary=len(raw), request=request)
    outcome = replace(request, status="accepted", **change) if "status" not in change else replace(request, **change)
    raw.append({"role": "assistant", "content": "unsent"})
    state.accept_request(raw[:2], raw_boundary=2, outcome=outcome)
    assert state.raw_accepted_boundary == (0 if change else 2)
    assert state.request_messages(raw) == raw


def test_receipt_cannot_accept_a_rewritten_payload_or_raw_boundary():
    from nanobot.agent.context_plan import ContextRequestOutcome

    raw, state = compaction_for()
    request = ContextRequestOutcome("r1", "l1", 0, "unknown")
    state.begin_request(raw, raw_boundary=2, request=request)
    state.accept_request(raw[:1], raw_boundary=2, outcome=replace(request, status="accepted"))
    state.accept_request(raw, raw_boundary=3, outcome=replace(request, status="accepted"))
    assert state.raw_accepted_boundary == 0


def test_provider_contract_defaults_to_no_evidence():
    from nanobot.providers.base import LLMResponse, ProviderCallContext

    response = LLMResponse(content="I consumed everything")
    context = ProviderCallContext()
    assert response.context_acceptance == "unknown"
    assert response.context_outcome is None
    assert context.context_request is None
    assert context.context_payload_hash is None


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("proof,expected", [("accepted", "accepted"), ("rejected", "rejected"), ("unknown", "unknown")])
async def test_base_binds_only_adapter_evidence(stream, proof, expected):
    messages = [{"role": "user", "content": "read this"}]
    provider = EvidenceProvider([LLMResponse(content="accepted", context_acceptance=proof)])
    call = provider.chat_stream_with_retry if stream else provider.chat_with_retry
    response = await call(messages=messages, provider_context=bound_context(messages))
    assert response.context_outcome == replace(bound_context(messages).context_request, status=expected)


@pytest.mark.parametrize("failure", [TimeoutError("expired"), RuntimeError("failed")])
async def test_exception_conversion_does_not_accept(failure):
    messages = [{"role": "user", "content": "read this"}]
    provider = EvidenceProvider([failure, failure])
    response = await provider.chat_with_retry(messages=messages, provider_context=bound_context(messages))
    assert response.context_outcome.status == "unknown"


async def test_base_refuses_evidence_for_different_payload():
    messages = [{"role": "user", "content": "original"}]
    provider = EvidenceProvider([LLMResponse(content="done", context_acceptance="accepted")])
    response = await provider.chat_with_retry(
        messages=[{"role": "user", "content": "rewritten"}], provider_context=bound_context(messages))
    assert response.context_outcome.status == "unknown"


async def test_retry_success_binds_final_evidence_not_failed_attempt():
    messages = [{"role": "user", "content": "original"}]
    provider = EvidenceProvider([
        LLMResponse(content="busy", finish_reason="error", error_status_code=429),
        LLMResponse(content="done", context_acceptance="accepted"),
    ])
    response = await provider.chat_with_retry(messages=messages, provider_context=bound_context(messages))
    assert response.context_outcome.status == "accepted"


async def test_image_retry_preserves_identity_but_cannot_accept_original_context():
    from nanobot.providers.base import ProviderConversationState

    messages = [{"role": "user", "content": [
        {"type": "text", "text": "describe"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,fake"}},
    ]}]
    context = replace(bound_context(messages), conversation_state=ProviderConversationState(
        provider="offline", model="offline", kind="test", version=1, payload={}, pending_messages=messages))
    provider = EvidenceProvider([
        LLMResponse(content="unsupported image", finish_reason="error", error_status_code=400),
        LLMResponse(content="done", context_acceptance="accepted"),
    ])
    response = await provider.chat_with_retry(messages=messages, provider_context=context)
    assert response.context_outcome == replace(context.context_request, status="unknown")


@pytest.mark.parametrize("route", ["ordinary", "no_tools", "finalization"])
@pytest.mark.parametrize("proof,expected", [("accepted", 2), ("unknown", 0), ("rejected", 0)])
async def test_runner_routes_advance_only_verified_dispatch(tmp_path, route, proof, expected):
    from nanobot.agent.context_governance import ContextGovernor
    from nanobot.agent.runner import AgentRunner
    from nanobot.agent.tools.registry import ToolRegistry
    from nanobot.config.schema import ContextConfig
    from tests.agent.runner_helpers import make_run_spec
    from tests.agent.test_context_manifest import state_for

    raw, compaction = compaction_for()
    provider = EvidenceProvider([LLMResponse(content="done", context_acceptance=proof)])
    spec = make_run_spec(provider, model="offline", initial_messages=None, tools=ToolRegistry(),
                         transcript_input=compaction.transcript_input,
                         transcript_builder=compaction.transcript_builder,
                         consolidate_history=compaction.consolidate_history,
                         context_config=ContextConfig(mode="enforce"), runtime_data_dir=tmp_path,
                         max_iterations=1 if route == "ordinary" else 0, max_tool_result_chars=1000)
    runner = AgentRunner()
    boundaries = []

    class RecordingGovernor(ContextGovernor):
        def record_response(self, state, response):
            super().record_response(state, response)
            boundaries.append(state.compaction.raw_accepted_boundary)

    runner.context_governor = RecordingGovernor()
    if route == "no_tools":
        state = state_for(tmp_path, raw, provider=provider, context=spec.context_config,
                          runtime_data_dir=tmp_path)
        state.compaction = compaction
        await runner._request_no_tools(spec, raw, request_state=state, transcript=raw)
    else:
        result = await runner.run(spec)
        assert result.final_content == "done"
    assert boundaries == [expected]


async def test_next_governor_request_has_new_id_and_rejects_stale_receipt(tmp_path):
    from nanobot.agent.context_governance import ContextGovernor
    from tests.agent.test_context_manifest import state_for

    raw, compaction = compaction_for()
    state = state_for(tmp_path, raw)
    state.compaction = compaction
    governor = ContextGovernor()
    _, first = await governor.prepare_request(state, raw, tool_definitions=[], transcript=raw)
    _, second = await governor.prepare_request(state, raw, tool_definitions=[], transcript=raw)
    assert first.context_request.request_id != second.context_request.request_id
    assert first.context_request.lineage_id == second.context_request.lineage_id
    assert second.context_request.attempt == first.context_request.attempt + 1
    governor.record_response(state, LLMResponse(content="done", context_outcome=replace(
        first.context_request, status="accepted")))
    assert compaction.raw_accepted_boundary == 0


async def test_observe_legacy_summary_view_does_not_forge_verified_history(tmp_path):
    from nanobot.agent.context_governance import ContextGovernor
    from tests.agent.test_context_manifest import state_for

    history = [{"role": "user", "content": "old question"},
               {"role": "assistant", "content": "old answer"}]
    raw, compaction = compaction_for(history)
    seen = []

    async def summarize(messages, previous):
        seen.extend(messages)
        return "short summary"

    compaction.consolidate_history = summarize
    state = state_for(tmp_path, raw)
    state.compaction = compaction
    await ContextGovernor()._compact_request_history(state, compaction, raw, (100, "test"),
                                                     tool_definitions=[])
    assert seen == raw[:3]
    assert compaction.summary_checkpoint.transcript_boundary == 3
    assert compaction.raw_accepted_boundary == 0


@pytest.mark.parametrize("proof", ["unknown", "rejected"])
async def test_enforce_native_checkpoint_cannot_replace_unknown_request(tmp_path, proof):
    from nanobot.agent.context_governance import ContextGovernor
    from nanobot.config.schema import ContextConfig
    from nanobot.providers.base import ProviderConversationState
    from tests.agent.test_context_manifest import state_for

    raw, compaction = compaction_for([{"role": "user", "content": "imported"}])
    calls = []

    async def summarize(*args):
        calls.append(args)
        return "summary"

    compaction.consolidate_provider_compaction = summarize
    state = state_for(tmp_path, raw, context=ContextConfig(mode="enforce"), runtime_data_dir=tmp_path)
    state.compaction = compaction
    governor = ContextGovernor()
    _, context = await governor.prepare_request(state, raw, tool_definitions=[], transcript=raw)
    response = LLMResponse(content="partial", context_outcome=replace(context.context_request, status=proof),
                           provider_compaction_applied=True, provider_compaction_scope="current_request",
                           provider_compaction_state=ProviderConversationState("test", "offline", "offline", 1))
    await governor.summarize_provider_compaction(state, response, current_request_boundary=len(raw))
    assert compaction.summary_checkpoint is None
    assert calls == []


async def test_enforce_legacy_compactor_has_no_summarizable_unknown_history(tmp_path):
    from nanobot.agent.context_governance import ContextGovernor, ContextWindowExceededError
    from nanobot.config.schema import ContextConfig
    from tests.agent.test_context_manifest import state_for

    raw, compaction = compaction_for([{"role": "user", "content": "unknown import"}])
    calls = []

    async def summarize(*args):
        calls.append(args)
        return "made-up proof"

    compaction.consolidate_history = summarize
    state = state_for(tmp_path, raw, context=ContextConfig(mode="enforce"), runtime_data_dir=tmp_path)
    with pytest.raises(ContextWindowExceededError):
        await ContextGovernor()._compact_request_history(state, compaction, raw, (100, "test"), tool_definitions=[])
    assert calls == []
    assert compaction.summary_checkpoint is None


def test_cross_turn_evidence_restores_only_exact_history_prefix():
    from nanobot.agent.context import TranscriptInput
    from nanobot.agent.context_governance import ContextCompactionState
    from nanobot.agent.context_plan import ContextRequestOutcome

    raw, state = compaction_for()
    request = ContextRequestOutcome("r1", "l1", 0, "unknown")
    state.begin_request(raw, raw_boundary=2, request=request)
    state.accept_request(raw, raw_boundary=2, outcome=replace(request, status="accepted"))
    proof = state.consumption()
    history = [*deepcopy(raw[1:]), {"role": "assistant", "content": "unsent answer"}]
    next_input = TranscriptInput(history=history, current_message="next")
    next_raw, next_state = ContextCompactionState.from_transcript(
        next_input, state.transcript_builder, state.consolidate_history, None, consumption=proof)
    assert next_state.raw_accepted_boundary == 2
    assert next_state.request_messages(next_raw)[-2:] == [
        {"role": "assistant", "content": "unsent answer"}, {"role": "user", "content": "next"}]
    changed = replace(next_input, history=[{"role": "user", "content": "changed"}])
    _, invalid = ContextCompactionState.from_transcript(
        changed, state.transcript_builder, state.consolidate_history, None, consumption=proof)
    assert invalid.raw_accepted_boundary == 0


def test_cross_turn_receipt_never_accepts_saved_but_unread_answer():
    from nanobot.agent.context_plan import ContextRequestOutcome

    raw, state = compaction_for()
    request = ContextRequestOutcome("r1", "l1", 0, "unknown")
    state.begin_request(raw, raw_boundary=2, request=request)
    state.accept_request(raw, raw_boundary=2, outcome=replace(request, status="accepted"))
    raw.append({"role": "assistant", "content": "new output"})
    assert len(state.consumption().history_hashes) == 1


@pytest.mark.parametrize("stream", [False, True])
async def test_standalone_provider_does_not_hash_without_request_identity(stream, monkeypatch):
    provider = EvidenceProvider([LLMResponse(content="legacy response")])

    def cannot_hash(*args):
        raise AssertionError("no context request to bind")

    monkeypatch.setattr(provider, "_context_payload_hash", cannot_hash)
    call = provider.chat_stream_with_retry if stream else provider.chat_with_retry
    response = await call(messages=[{"role": "user", "content": "legacy"}])
    assert response.content == "legacy response"
    assert response.context_outcome is None


async def test_binding_does_not_mutate_reused_adapter_response():
    shared = LLMResponse(content="done", context_acceptance="accepted")
    messages = [{"role": "user", "content": "task"}]
    provider = EvidenceProvider([shared, shared])
    first_context = bound_context(messages)
    second_context = replace(first_context, context_request=replace(
        first_context.context_request, request_id="r2", attempt=1))
    first = await provider.chat_with_retry(messages=messages, provider_context=first_context)
    second = await provider.chat_with_retry(messages=messages, provider_context=second_context)
    assert first.context_outcome == replace(first_context.context_request, status="accepted")
    assert second.context_outcome == replace(second_context.context_request, status="accepted")
    assert shared.context_outcome is None


async def test_enforce_runner_tracks_receipts_without_a_summary_callback(tmp_path):
    from nanobot.agent.runner import AgentRunner
    from nanobot.agent.tools.registry import ToolRegistry
    from nanobot.config.schema import ContextConfig
    from tests.agent.runner_helpers import make_run_spec

    _, state = compaction_for()
    result = await AgentRunner().run(make_run_spec(
        EvidenceProvider([LLMResponse(content="done", context_acceptance="accepted")]),
        initial_messages=None, transcript_input=state.transcript_input,
        transcript_builder=state.transcript_builder, model="offline", tools=ToolRegistry(),
        context_config=ContextConfig(mode="enforce"), runtime_data_dir=tmp_path,
        max_iterations=1, max_tool_result_chars=1000))
    assert result.context_consumption is not None


def test_changed_raw_prefix_is_replayed_as_unknown_not_hidden_by_old_view():
    from nanobot.agent.context_plan import ContextRequestOutcome

    raw, state = compaction_for()
    request = ContextRequestOutcome("r1", "l1", 0, "unknown")
    state.begin_request(raw, raw_boundary=2, request=request)
    state.accept_request(raw, raw_boundary=2, outcome=replace(request, status="accepted"))
    raw[1]["content"] = "new correction"
    assert state.request_messages(raw) == raw
    assert state.consumption() is None
