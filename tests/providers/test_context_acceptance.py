"""Provider receipt evidence comes from protocol terminals, not parser defaults."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from nanobot.providers.openai_compat_provider import OpenAICompatProvider
from nanobot.providers.openai_responses.parsing import parse_response_output


@pytest.mark.parametrize("sdk", [False, True])
@pytest.mark.parametrize("terminal,expected", [(None, "unknown"), ("stop", "accepted"), ("length", "accepted")])
def test_chat_chunks_require_actual_terminal(sdk, terminal, expected):
    chunks = [{"choices": [{"delta": {"content": "partial evidence"}, "finish_reason": None}]}]
    if terminal:
        chunks.append({"choices": [{"delta": {}, "finish_reason": terminal}]})
    if sdk:
        chunks = [SimpleNamespace(choices=[SimpleNamespace(
            delta=SimpleNamespace(content=c["choices"][0]["delta"].get("content")),
            finish_reason=c["choices"][0]["finish_reason"],
        )]) for c in chunks]
    response = OpenAICompatProvider._parse_chunks(chunks)
    assert response.content == "partial evidence"
    assert response.finish_reason == (terminal or "stop")  # legacy output unchanged
    assert response.context_acceptance == expected


@pytest.mark.parametrize("terminal,expected", [(False, "unknown"), (True, "accepted")])
async def test_anthropic_stream_requires_message_stop_not_final_snapshot(terminal, expected):
    from nanobot.providers.anthropic_provider import AnthropicProvider
    from tests.providers.test_anthropic_stream_idle import _FakeAsyncStream

    chunks = [SimpleNamespace(type="content_block_delta", delta=SimpleNamespace(type="text_delta", text="Hi"))]
    if terminal:
        chunks.append(SimpleNamespace(type="message_stop"))
    stream = _FakeAsyncStream(chunks)
    provider = AnthropicProvider.__new__(AnthropicProvider)
    provider._build_kwargs = lambda *a, **k: {}
    provider._client = SimpleNamespace(messages=SimpleNamespace(stream=lambda **kwargs: stream))
    response = await provider.chat_stream(messages=[{"role": "user", "content": "test"}])
    assert response.content == "Hi"
    assert response.finish_reason == "stop"
    assert response.context_acceptance == expected


@pytest.mark.parametrize("terminal,expected", [(False, "unknown"), (True, "accepted")])
async def test_bedrock_stream_requires_message_stop(terminal, expected):
    from nanobot.providers.bedrock_provider import BedrockProvider
    from tests.providers.test_bedrock_provider import FakeClient

    events = [{"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": "Hi"}}}]
    if terminal:
        events.append({"messageStop": {"stopReason": "end_turn"}})
    provider = BedrockProvider.__new__(BedrockProvider)
    provider._build_kwargs = lambda *a, **k: {}
    provider._client = FakeClient(stream_events=events)
    response = await provider.chat_stream(messages=[{"role": "user", "content": "test"}])
    assert response.content == "Hi"
    assert response.finish_reason == "stop"
    assert response.context_acceptance == expected


@pytest.mark.parametrize("terminal,expected", [(None, "unknown"), ("stop", "accepted")])
def test_chat_completion_object_requires_finish_reason(terminal, expected):
    provider = OpenAICompatProvider.__new__(OpenAICompatProvider)
    response = provider._parse({"choices": [{"finish_reason": terminal,
        "message": {"content": "Hi"}}]})
    assert response.content == "Hi"
    assert response.context_acceptance == expected


@pytest.mark.parametrize("terminal,expected", [(None, "unknown"), ("end_turn", "accepted")])
def test_bedrock_nonstream_requires_stop_reason(terminal, expected):
    from nanobot.providers.bedrock_provider import BedrockProvider

    response = BedrockProvider._parse_response({"stopReason": terminal,
        "output": {"message": {"content": [{"text": "Hi"}]}}})
    assert response.content == "Hi"
    assert response.context_acceptance == expected


@pytest.mark.parametrize("terminal,expected", [(None, "unknown"), ("end_turn", "accepted")])
async def test_anthropic_nonstream_requires_stop_reason(terminal, expected):
    from nanobot.providers.anthropic_provider import AnthropicProvider
    from tests.providers.test_anthropic_stream_idle import _final_message_stub

    message = _final_message_stub()
    message.stop_reason = terminal
    provider = AnthropicProvider.__new__(AnthropicProvider)
    provider._build_kwargs = lambda *a, **k: {}
    provider._client = SimpleNamespace(messages=SimpleNamespace(create=AsyncMock(return_value=message)))
    response = await provider.chat(messages=[{"role": "user", "content": "test"}])
    assert response.content == "Hi"
    assert response.context_acceptance == expected


@pytest.mark.parametrize("status,expected", [(None, "unknown"), ("queued", "unknown"),
    ("completed", "accepted"), ("incomplete", "accepted"), ("failed", "unknown")])
def test_responses_object_requires_terminal_status(status, expected):
    payload = {"output": [{"type": "message", "content": [{"type": "output_text", "text": "evidence"}]}]}
    if status is not None:
        payload["status"] = status
    response = parse_response_output(payload)
    assert response.content == "evidence"
    assert response.context_acceptance == expected


@pytest.mark.parametrize("kind", ["codex", "xai", "azure", "compat"])
@pytest.mark.parametrize("terminal,expected", [(None, "unknown"),
    ("completed", "accepted"), ("incomplete", "accepted"), ("late_failure", "unknown")])
async def test_responses_stream_receipt_uses_terminal_event(monkeypatch, kind, terminal, expected):
    from nanobot.agent.context_plan import ContextRequestOutcome, content_hash
    from nanobot.providers.base import ProviderCallContext

    messages = [{"role": "user", "content": "test"}]
    context = ProviderCallContext(
        context_request=ContextRequestOutcome("r1", "line1", 0, "unknown"),
        context_payload_hash=content_hash({"messages": messages, "tools": None}),
    )
    events = [{"type": "response.output_text.delta", "delta": "Hi"}]
    if terminal:
        status = "completed" if terminal == "late_failure" else terminal
        events.append({"type": "response." + status, "response": {
            "status": status, "output": [],
            "incomplete_details": {"reason": "max_output_tokens"} if terminal == "incomplete" else None,
        }})
    if terminal == "late_failure":
        events.append({"type": "error", "error": "terminal followed by transport failure"})
    if kind in {"codex", "xai"}:
        original_client = httpx.AsyncClient
        body = "".join(f"data: {json.dumps(event)}\n\n" for event in events)
        transport = httpx.MockTransport(lambda request: httpx.Response(200, content=body))
        monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original_client(transport=transport))
        if kind == "codex":
            from nanobot.providers.openai_codex_provider import OpenAICodexProvider
            from tests.providers.test_openai_codex_provider import _mock_codex_token

            _mock_codex_token(monkeypatch)
            provider = OpenAICodexProvider()
        else:
            from nanobot.providers.xai_grok_provider import XAIGrokProvider
            from tests.providers.test_xai_grok_provider import _mock_token

            _mock_token(monkeypatch)
            provider = XAIGrokProvider(extra_body={"tools": []})
    else:
        async def stream():
            for event in events:
                yield SimpleNamespace(**event)

        if kind == "azure":
            from nanobot.providers.azure_openai_provider import AzureOpenAIProvider

            provider = AzureOpenAIProvider(api_key="test", api_base="https://mock.test")
        else:
            provider = OpenAICompatProvider(api_key="test", api_base="https://mock.test")
            monkeypatch.setattr(provider, "_should_use_responses_api", lambda *a, **kw: True)
            monkeypatch.setattr(provider, "_responses_is_required", lambda: True)
        provider._client = SimpleNamespace(responses=SimpleNamespace(create=AsyncMock(return_value=stream())))
    response = await provider.chat_stream_with_retry(messages=messages, provider_context=context)
    if terminal == "late_failure":
        assert response.finish_reason == "error"
    else:
        assert response.content == "Hi"
    assert response.context_acceptance == expected
    assert response.context_outcome.status == expected


async def test_fallback_preserves_context_receipt_identity():
    from nanobot.agent.context_plan import ContextRequestOutcome
    from nanobot.providers.base import LLMResponse, ProviderCallContext
    from nanobot.providers.fallback_provider import FallbackProvider
    from tests.agent.test_runner_fallback import _error_response, _FakeProvider, _fallback

    request = ContextRequestOutcome("r1", "line1", 2, "unknown")
    context = ProviderCallContext(context_request=request, context_payload_hash="hash1")
    primary = _FakeProvider(response=_error_response())
    fallback = _FakeProvider(response=LLMResponse(content="success"))
    provider = FallbackProvider(primary, [_fallback("fallback")], lambda _: fallback)
    response = await provider.chat_with_retry(
        messages=[{"role": "user", "content": "test"}], provider_context=context,
    )
    assert response.content == "success"
    assert primary.context_calls and fallback.context_calls
    for leaf_context in primary.context_calls + fallback.context_calls:
        assert leaf_context.context_request == request
        assert leaf_context.context_payload_hash == "hash1"


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("evidence", ["accepted", "unknown"])
async def test_fallback_returns_only_final_leaf_consumption_evidence(stream, evidence):
    from dataclasses import replace

    from nanobot.agent.context_plan import ContextRequestOutcome, content_hash
    from nanobot.providers.base import LLMResponse, ProviderCallContext
    from nanobot.providers.fallback_provider import FallbackProvider
    from tests.agent.test_runner_fallback import _error_response, _FakeProvider, _fallback

    messages = [{"role": "user", "content": "test"}]
    request = ContextRequestOutcome("r1", "line1", 0, "unknown")
    context = ProviderCallContext(context_request=request,
        context_payload_hash=content_hash({"messages": messages, "tools": None}))
    primary = _FakeProvider(response=_error_response())
    fallback = _FakeProvider(response=LLMResponse(content="success", context_acceptance=evidence))
    provider = FallbackProvider(primary, [_fallback("fallback")], lambda _: fallback)
    call = provider.chat_stream_with_retry if stream else provider.chat_with_retry
    response = await call(messages=messages, provider_context=context)
    assert response.content == "success"
    assert response.context_outcome == replace(request, status=evidence)


@pytest.mark.parametrize("kind", ["codex", "xai", "compat", "compat_stream"])
async def test_explicit_wire_input_override_does_not_attest_original_input(monkeypatch, kind):
    from nanobot.agent.context_plan import ContextRequestOutcome, content_hash
    from nanobot.providers.base import ProviderCallContext

    messages = [{"role": "user", "content": "original request"}]
    context = ProviderCallContext(
        context_request=ContextRequestOutcome("r1", "line1", 0, "unknown"),
        context_payload_hash=content_hash({"messages": messages, "tools": None}),
    )
    original_client = httpx.AsyncClient
    terminal = {"type": "response.completed", "response": {"status": "completed", "output": []}}
    wire_requests = []

    def handler(request):
        wire_requests.append(json.loads(request.content))
        return httpx.Response(200, content=f"data: {json.dumps(terminal)}\n\n")

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original_client(transport=httpx.MockTransport(handler)))
    override = {"input": [{"role": "user", "content": "replacement"}]}
    if kind == "codex":
        from nanobot.providers.openai_codex_provider import OpenAICodexProvider
        from tests.providers.test_openai_codex_provider import _mock_codex_token

        _mock_codex_token(monkeypatch)
        provider = OpenAICodexProvider(extra_body=override)
    elif kind == "xai":
        from nanobot.providers.xai_grok_provider import XAIGrokProvider
        from tests.providers.test_xai_grok_provider import _mock_token

        _mock_token(monkeypatch)
        provider = XAIGrokProvider(extra_body={**override, "tools": []})
    else:
        provider = OpenAICompatProvider(api_key="test", api_base="https://mock.test", extra_body=override)
        monkeypatch.setattr(provider, "_should_use_responses_api", lambda *a, **kw: True)

        async def create(**kwargs):
            wire_requests.append(kwargs)
            if kind == "compat":
                return terminal["response"]

            async def stream():
                yield SimpleNamespace(**terminal)

            return stream()

        provider._client = SimpleNamespace(responses=SimpleNamespace(create=create))
    call = provider.chat_stream_with_retry if kind == "compat_stream" else provider.chat_with_retry
    response = await call(messages=messages, provider_context=context)
    assert wire_requests[-1]["input"] == override["input"]  # user's override is unchanged
    assert response.finish_reason == "stop"
    assert response.context_outcome.status == "unknown"


@pytest.mark.parametrize("compact_again", [False, True])
@pytest.mark.parametrize("stream", [False, True])
async def test_restored_native_compaction_never_attests_unseen_raw_history(
    tmp_path, monkeypatch, compact_again, stream,
):
    from dataclasses import replace

    from nanobot.agent.context_governance import ContextGovernor
    from nanobot.config.schema import ContextConfig
    from nanobot.providers.base import ProviderConversationState
    from nanobot.providers.conversation_state import ProviderConversationStateController
    from nanobot.providers.openai_codex_provider import OpenAICodexProvider
    from nanobot.providers.openai_responses.state import build_responses_compaction_state
    from tests.agent.test_context_acceptance import compaction_for
    from tests.agent.test_context_manifest import state_for
    from tests.providers.test_openai_codex_provider import _mock_codex_token

    _mock_codex_token(monkeypatch)
    provider = OpenAICodexProvider(default_model="openai-codex/gpt-5.6-sol")
    prior = build_responses_compaction_state(
        provider=provider._responses_state_provider(), model="gpt-5.6-sol",
        output_items=[{"type": "compaction", "encrypted_content": "opaque-prior"}],
    )
    prior.payload["context_tokens"] = 99999 if compact_again else 10
    restored = ProviderConversationState.from_private_record(prior.to_private_record())
    raw, compaction = compaction_for([
        {"role": "user", "content": "RAW-UNSEEN-REQUIREMENT"},
        {"role": "assistant", "content": "RAW-UNSEEN-ANSWER"},
    ])
    state = state_for(tmp_path, raw, provider=provider, model=provider.get_default_model(),
                      context=ContextConfig(mode="enforce"), runtime_data_dir=tmp_path)
    state.compaction = compaction
    state.conversation = ProviderConversationStateController(
        provider=provider, model=state.config.model, messages=raw[:-1], state=restored)
    wire_requests = []
    original_client = httpx.AsyncClient

    def handler(request):
        body = json.loads(request.content)
        wire_requests.append(body)
        is_compact = body["input"][-1].get("type") == "compaction_trigger"
        output = [{"type": "compaction", "encrypted_content": "opaque-new"}] if is_compact else []
        terminal = {"type": "response.completed", "response": {
            "status": "completed", "output": output,
        }}
        return httpx.Response(200, content=f"data: {json.dumps(terminal)}\n\n")

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original_client(transport=httpx.MockTransport(handler)))
    governor = ContextGovernor()
    prepared, context = await governor.prepare_request(state, raw, tool_definitions=[], transcript=raw)
    assert context.conversation_state is not None
    call = provider.chat_stream_with_retry if stream else provider.chat_with_retry
    response = await call(messages=prepared, tools=[], provider_context=context)
    assert response.finish_reason == "stop"
    assert response.provider_state is not None  # native continuation remains available
    assert response.provider_compaction_applied is compact_again
    assert len(wire_requests) == (2 if compact_again else 1)
    assert "RAW-UNSEEN" not in json.dumps(wire_requests)
    assert "current" in json.dumps(wire_requests[-1])
    governor.record_response(state, response)
    assert response.context_outcome == replace(context.context_request, status="unknown")
    assert compaction.raw_accepted_boundary == 0
    assert governor._verified_positions(compaction, raw) == {}

    # Only an independent, actual full-transcript dispatch establishes full eligibility.
    state.conversation.replace_transcript(raw)
    prepared, context = await governor.prepare_request(state, raw, tool_definitions=[], transcript=raw)
    response = await call(messages=prepared, tools=[], provider_context=context)
    governor.record_response(state, response)
    assert "RAW-UNSEEN-REQUIREMENT" in json.dumps(wire_requests[-1])
    assert response.context_outcome.status == "accepted"
    assert compaction.raw_accepted_boundary == len(raw)


async def test_opaque_primary_does_not_downgrade_independent_fallback_receipt():
    from dataclasses import replace

    from nanobot.providers.base import LLMResponse, ProviderConversationState
    from nanobot.providers.fallback_provider import FallbackProvider
    from tests.agent.test_context_acceptance import bound_context
    from tests.agent.test_runner_fallback import _error_response, _FakeProvider, _fallback

    messages = [{"role": "user", "content": "entire logical context"}]
    context = replace(bound_context(messages), conversation_state=ProviderConversationState(
        "openai_responses", "primary", "primary", 1, {"items": []}))
    primary = _FakeProvider(response=_error_response())
    fallback = _FakeProvider(response=LLMResponse(content="done", context_acceptance="accepted"))
    provider = FallbackProvider(primary, [_fallback("fallback")], lambda _: fallback)
    response = await provider.chat_with_retry(messages=messages, provider_context=context)
    assert fallback.context_calls[-1].conversation_state is None
    assert response.context_outcome.status == "accepted"
