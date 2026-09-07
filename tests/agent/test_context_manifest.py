"""Truthful request ledgers after legacy governance, without content leakage."""

import json
from copy import deepcopy
from dataclasses import replace

import pytest

from nanobot.agent.context import ContextBuilder, TranscriptInput
from nanobot.agent.context_governance import (
    ContextGovernanceConfig,
    ContextGovernor,
    ModelRequestState,
)
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.providers.base import LLMProvider, LLMResponse, LLMUsage
from nanobot.providers.conversation_state import ProviderConversationStateController


class OfflineProvider(LLMProvider):
    async def chat(self, *args, **kwargs):
        raise AssertionError("no live calls")

    def get_default_model(self):
        return "n05-offline"


def state_for(tmp_path, messages, **overrides):
    provider = OfflineProvider(provider_name="n05-offline")
    config = ContextGovernanceConfig(
        provider=provider, model="n05-offline", tools=ToolRegistry(), workspace=tmp_path,
        session_key="n05:test", max_tool_result_chars=16000,
        context_window_tokens=100000, max_tokens=128,
    )
    config = replace(config, **overrides)
    return ModelRequestState(config=config, conversation=ProviderConversationStateController(
        provider=provider, model=config.model, messages=messages))


async def test_observe_ledger_matches_legacy_payload_and_redacts(tmp_path):
    from nanobot.agent.context_plan import ContextPlanner, content_hash

    builder = ContextBuilder(tmp_path)
    transcript = TranscriptInput(history=[
        {"role": "assistant", "content": "[Previous assistant message omitted.]"},
        {"role": "tool", "tool_call_id": "orphan", "content": "private-orphan"},
    ], current_message="private-current")
    seed = ContextPlanner().plan(builder.collect_sources(transcript), 100000)
    messages = builder.build_transcript(transcript)
    schemas = [{"type": "function", "function": {"name": "test", "parameters": {
        "type": "object", "properties": {"value": {"enum": ["private-schema"]}}}}}]
    state = state_for(tmp_path, messages, initial_plan=seed)
    expected = ContextGovernor().prepare_messages_for_model(state.config, deepcopy(messages))
    prepared, _ = await ContextGovernor().prepare_request(state, messages, tool_definitions=schemas)
    assert prepared == expected
    assert state.manifest.payload_hash == content_hash({"messages": prepared, "tools": schemas})
    decisions = {d.source_id: d for d in state.manifest.plan.decisions}
    assert decisions["history:0"].action == "defer"
    assert decisions["history:1"].action == "defer"
    assert decisions["current:0"].action == "keep"
    assert sum(s.kind == "mcp_schema" for s in state.manifest.plan.sources) == 1
    assert sum(s.kind == "envelope" for s in state.manifest.plan.sources) == 1
    assert state.manifest.actual_input_tokens is None
    encoded = json.dumps(state.manifest.to_log())
    assert "private-" not in encoded
    assert state.manifest.plan._catalog is None


@pytest.mark.parametrize("window,root,reason", [
    (None, True, "unknown_budget"), (100000, False, "runtime_root_required"),
    (1200, True, "irreducible_floor"),
])
async def test_enforce_failure_has_manifest(tmp_path, window, root, reason):
    from nanobot.agent.context_plan import ContextPlanError
    from nanobot.config.schema import ContextConfig

    messages = [{"role": "system", "content": "required-policy " * 1000},
                {"role": "user", "content": "required-current"}]
    state = state_for(tmp_path, messages, context_window_tokens=window,
                      context=ContextConfig(mode="enforce"),
                      runtime_data_dir=tmp_path / "runtime" if root else None)
    with pytest.raises(ContextPlanError) as caught:
        await ContextGovernor().prepare_request(state, messages, tool_definitions=[])
    assert state.manifest.status == "refused"
    assert state.manifest.reason_code == reason
    assert caught.value.manifest == state.manifest
    assert all(d.stage == "selection" for d in state.manifest.plan.decisions)


async def test_observe_unknown_images_and_usage_are_never_estimated_actuals(tmp_path):
    messages = [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,SECRET"}},
    ]}]
    state = state_for(tmp_path, messages, context_window_tokens=None)
    governor = ContextGovernor()
    await governor.prepare_request(state, messages, tool_definitions=[])
    assert state.manifest.plan.input_budget is None
    assert state.manifest.plan.rendered_estimate is None
    governor.record_response(state, LLMResponse(content="ok", usage=LLMUsage.estimated(
        input_tokens=100, output_tokens=1)))
    assert state.manifest.actual_input_tokens is None
    request_id = state.manifest.plan.request_id
    governor.record_response(state, LLMResponse(content="ok", usage=LLMUsage.reported(
        input_tokens=321, output_tokens=1)))
    assert state.manifest.plan.request_id == request_id
    assert state.manifest.actual_input_tokens == 321
    assert "SECRET" not in json.dumps(state.manifest.to_log())


async def test_runner_collects_before_legacy_render_and_records_provider_request(tmp_path):
    from nanobot.agent.runner import AgentRunner
    from tests.agent.runner_helpers import make_run_spec

    class CapturingProvider(OfflineProvider):
        async def chat(self, *args, **kwargs):
            self.request = deepcopy(kwargs)
            return LLMResponse(content="finished", usage=LLMUsage.reported(input_tokens=42, output_tokens=1))

    class CapturingGovernor(ContextGovernor):
        def record_response(self, state, response):
            super().record_response(state, response)
            self.manifest = state.manifest

    provider = CapturingProvider(provider_name="n05-offline")
    builder = ContextBuilder(tmp_path)
    order = []

    def collect(transcript):
        order.append("collect")
        return builder.collect_sources(transcript)

    def render(transcript):
        order.append("render")
        return builder.build_transcript(transcript)

    runner = AgentRunner()
    governor = CapturingGovernor()
    runner.context_governor = governor
    result = await runner.run(make_run_spec(
        provider, model="n05-offline", initial_messages=None, tools=ToolRegistry(),
        max_iterations=1, max_tool_result_chars=16000,
        transcript_input=TranscriptInput(history=[], current_message="provider-payload"),
        transcript_builder=render, context_source_collector=collect,
    ))
    assert result.final_content == "finished"
    assert order == ["collect", "render"]
    assert provider.request["messages"][-1]["content"] == "provider-payload"
    assert governor.manifest.status == "responded"
    assert governor.manifest.actual_input_tokens == 42
    assert any(s.source_id == "current:0" for s in governor.manifest.plan.sources)


async def test_enforce_keeps_required_sources_and_rejects_images(tmp_path):
    from nanobot.agent.context_plan import ContextPlanError
    from nanobot.config.schema import ContextConfig

    messages = [{"role": "system", "content": "required policy"},
                {"role": "user", "content": "required current"}]
    state = state_for(tmp_path, messages, context=ContextConfig(mode="enforce"),
                      runtime_data_dir=tmp_path / "runtime")
    prepared, _ = await ContextGovernor().prepare_request(state, messages, tool_definitions=[])
    assert prepared == messages
    assert all(d.action == "keep" for d in state.manifest.plan.decisions)
    images = [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "unknown"}}]}]
    with pytest.raises(ContextPlanError, match="unknown_image_cost"):
        await ContextGovernor().prepare_request(state, images, tool_definitions=[])


async def test_archive_failure_is_visible_without_changing_legacy_payload(tmp_path, monkeypatch):
    import nanobot.agent.context_governance as governance

    def fail_archive(*args, **kwargs):
        raise OSError("private-archive-failure")

    monkeypatch.setattr(governance, "maybe_persist_tool_result", fail_archive)
    messages = [
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c", "type": "function",
         "function": {"name": "exec", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c", "name": "exec", "content": "x" * 100},
        {"role": "user", "content": "current"},
    ]
    state = state_for(tmp_path, messages)
    prepared, _ = await ContextGovernor().prepare_request(state, messages, tool_definitions=[])
    assert prepared == messages
    assert state.manifest.reason_code == "archive_failed"
    assert "private-archive-failure" not in json.dumps(state.manifest.to_log())


async def test_concurrent_catalogs_survive_legacy_compaction_await(tmp_path):
    import asyncio

    from nanobot.agent.context_plan import ContextPlanner, content_hash

    builder = ContextBuilder(tmp_path)
    arrived = asyncio.Event()
    release = asyncio.Event()

    class YieldingGovernor(ContextGovernor):
        async def _prepare_legacy_request(self, state, messages, **kwargs):
            if messages[-1]["content"] == "first":
                arrived.set()
                await release.wait()
            else:
                await arrived.wait()
                release.set()
            return await super()._prepare_legacy_request(state, messages, **kwargs)

    async def run(value):
        transcript = TranscriptInput(history=[], current_message=value)
        sources = builder.collect_sources(transcript)
        plan = ContextPlanner().plan(sources, 100000)
        messages = builder.render_plan(plan).messages
        state = state_for(tmp_path, messages, initial_plan=plan)
        prepared, _ = await YieldingGovernor().prepare_request(state, messages, tool_definitions=[])
        assert prepared[-1]["content"] == value
        assert state.manifest.payload_hash == content_hash({"messages": prepared, "tools": []})
        return state.manifest.plan.request_id

    first, second = await asyncio.gather(run("first"), run("second"))
    assert first != second


async def test_new_tool_batch_is_required_and_still_classified_as_tool_results(tmp_path):
    from nanobot.agent.context_plan import ContextPlanner

    builder = ContextBuilder(tmp_path)
    transcript = TranscriptInput(history=[], current_message="read both")
    seed = ContextPlanner().plan(builder.collect_sources(transcript), 100000)
    messages = builder.render_plan(seed).messages + [
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": key, "type": "function", "function": {"name": "read_file", "arguments": "{}"}}
            for key in ("a", "b")]},
        {"role": "tool", "tool_call_id": "a", "name": "read_file", "content": "first result"},
        {"role": "tool", "tool_call_id": "b", "name": "read_file", "content": "second result"},
    ]
    state = state_for(tmp_path, messages, initial_plan=seed)
    await ContextGovernor().prepare_request(state, messages, tool_definitions=[])
    results = [s for s in state.manifest.plan.sources if s.kind == "tool_result"]
    assert len(results) == 2
    assert all(s.required and s.priority == 95 for s in results)


async def test_real_tool_execution_second_request_has_tool_result_sources(tmp_path):
    from functools import partial

    from nanobot.agent.runner import AgentRunner
    from nanobot.agent.tools.filesystem import ReadFileTool
    from nanobot.providers.base import ToolCallRequest
    from tests.agent.runner_helpers import make_run_spec

    (tmp_path / "first.txt").write_text("first evidence", encoding="utf-8")
    (tmp_path / "second.txt").write_text("second evidence", encoding="utf-8")

    class ToolProvider(OfflineProvider):
        requests = None

        async def chat(self, *args, **kwargs):
            if self.requests is None:
                self.requests = []
            self.requests.append(deepcopy(kwargs))
            if len(self.requests) == 1:
                return LLMResponse(content=None, finish_reason="tool_calls", tool_calls=[
                    ToolCallRequest(id="first", name="read_file", arguments={"path": "first.txt"}),
                    ToolCallRequest(id="second", name="read_file", arguments={"path": "second.txt"}),
                ])
            assert len(self.requests) == 2
            return LLMResponse(content="both read")

    class CaptureGovernor(ContextGovernor):
        def __init__(self):
            self.manifests = []

        async def prepare_request(self, state, messages, **kwargs):
            result = await super().prepare_request(state, messages, **kwargs)
            self.manifests.append(state.manifest)
            return result

    provider = ToolProvider(provider_name="n05-offline")
    tools = ToolRegistry()
    tools.register(ReadFileTool(workspace=tmp_path, restrict_to_workspace=True))
    builder = ContextBuilder(tmp_path)
    runner = AgentRunner()
    governor = CaptureGovernor()
    runner.context_governor = governor
    result = await runner.run(make_run_spec(
        provider, model="n05-offline", initial_messages=None, tools=tools,
        max_iterations=2, max_tool_result_chars=16000, workspace=tmp_path,
        transcript_input=TranscriptInput(history=[], current_message="read first.txt and second.txt"),
        transcript_builder=builder.build_transcript,
        context_source_collector=partial(builder.collect_sources, tool_definitions=tools.get_definitions()),
    ))
    assert result.final_content == "both read"
    assert len(provider.requests) == len(governor.manifests) == 2
    tail = provider.requests[1]["messages"][-2:]
    assert [message["role"] for message in tail] == ["tool", "tool"]
    assert "first evidence" in tail[0]["content"]
    assert "second evidence" in tail[1]["content"]
    sources = [source for source in governor.manifests[1].plan.sources if source.kind == "tool_result"]
    assert len(sources) == 2
    assert all(source.required and source.priority == 95 for source in sources)
    assert sum(source.kind == "user_current" for source in governor.manifests[1].plan.sources) == 1


def _parallel_pair():
    return [
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": key, "type": "function", "function": {"name": "read_file", "arguments": "{}"}}
            for key in ("a", "b")]},
        {"role": "tool", "tool_call_id": "b", "content": "second"},
        {"role": "tool", "tool_call_id": "a", "content": "first"},
    ]


@pytest.mark.parametrize("defect", ["missing", "orphan", "duplicate_result", "duplicate_call", "interrupted"])
async def test_enforce_refuses_malformed_pairing_before_dispatch(tmp_path, defect):
    from nanobot.agent.context_plan import ContextPlanError
    from nanobot.config.schema import ContextConfig

    messages = _parallel_pair()
    if defect == "missing":
        messages.pop()
    elif defect == "orphan":
        messages[1]["tool_call_id"] = "unknown"
    elif defect == "duplicate_result":
        messages.append(deepcopy(messages[-1]))
    elif defect == "duplicate_call":
        messages[0]["tool_calls"][1]["id"] = "a"
    else:
        messages.insert(1, {"role": "user", "content": "interrupted"})
    original = deepcopy(messages)
    state = state_for(tmp_path, messages, context=ContextConfig(mode="enforce"),
                      runtime_data_dir=tmp_path / "runtime")
    with pytest.raises(ContextPlanError, match="malformed_tool_transcript"):
        await ContextGovernor().prepare_request(state, messages, tool_definitions=[])
    assert state.manifest.status == "refused"
    assert state.manifest.payload_hash is None
    assert state.messages is None
    assert messages == original


async def test_enforce_valid_parallel_pair_is_intact_and_observe_still_repairs(tmp_path):
    from nanobot.config.schema import ContextConfig

    messages = _parallel_pair()
    state = state_for(tmp_path, messages, context=ContextConfig(mode="enforce"),
                      runtime_data_dir=tmp_path / "runtime")
    prepared, _ = await ContextGovernor().prepare_request(state, messages, tool_definitions=[])
    assert prepared == messages
    missing = messages[:-1]
    observe = state_for(tmp_path, missing)
    expected = ContextGovernor().prepare_messages_for_model(observe.config, missing)
    prepared, _ = await ContextGovernor().prepare_request(observe, missing, tool_definitions=[])
    assert prepared == expected
    assert {m["tool_call_id"] for m in prepared if m["role"] == "tool"} == {"a", "b"}


async def test_merged_user_final_manifest_preserves_exact_skill_provenance(tmp_path):
    from nanobot.agent.runner import AgentRunner
    from nanobot.runtime_context import RuntimeContextBlock
    from tests.agent.runner_helpers import make_run_spec

    skill_dir = tmp_path / "skills" / "review-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: review-skill\ndescription: review fixture\n---\nEXACT-SKILL-BODY", encoding="utf-8")
    builder = ContextBuilder(tmp_path)
    history = builder.build_current_message("earlier", runtime_context_blocks=[
        RuntimeContextBlock(source="older_clock", content="older runtime")])
    transcript = TranscriptInput(history=[history], current_message="$review-skill please",
                                 runtime_context_blocks=[RuntimeContextBlock(source="clock", content="new runtime")])

    class CaptureProvider(OfflineProvider):
        async def chat(self, *args, **kwargs):
            self.request = deepcopy(kwargs)
            return LLMResponse(content="done")

    class CaptureGovernor(ContextGovernor):
        def record_response(self, state, response):
            super().record_response(state, response)
            self.manifest = state.manifest

    provider = CaptureProvider(provider_name="n05-offline")
    spec = make_run_spec(provider, model="n05-offline", initial_messages=None,
                         tools=ToolRegistry(), max_iterations=1, max_tool_result_chars=16000,
                         transcript_input=transcript, transcript_builder=builder.build_transcript,
                         context_source_collector=builder.collect_sources)
    runner = AgentRunner()
    governor = CaptureGovernor()
    runner.context_governor = governor
    await runner.run(spec)
    messages = provider.request["messages"]
    assert len(messages) == 2
    assert all(value in messages[-1]["content"] for value in ["earlier", "EXACT-SKILL-BODY", "older runtime", "new runtime"])
    initial = spec.initial_context_plan
    skill = next(s for s in initial.sources if s.kind == "skill_full")
    plan = governor.manifest.plan
    decisions = {d.source_id: d for d in plan.decisions}
    assert decisions[skill.source_id].action == "keep"
    assert next(s for s in plan.sources if s.source_id == skill.source_id) == skill
    assert sum(s.kind == "skill_full" and decisions[s.source_id].action == "keep" for s in plan.sources) == 1
    assert sum(s.kind == "runtime" and decisions[s.source_id].action == "keep" for s in plan.sources) == 2
    assert len(plan.sources) == len({s.source_id for s in plan.sources})
    assert plan.predicted_total == sum(d.after_tokens for d in plan.decisions)
    assert plan.predicted_total == initial.predicted_total - 4  # one fewer message envelope

    # An altered body, even with the same source labels, is not the known exact merge.
    altered = deepcopy(messages)
    altered[-1]["content"] = altered[-1]["content"].replace("EXACT-SKILL-BODY", "changed body")
    altered[-1]["_meta"]["runtime_context"]["suffix"] = altered[-1]["_meta"]["runtime_context"]["suffix"].replace("EXACT-SKILL-BODY", "changed body")
    state = state_for(tmp_path, altered, initial_plan=initial)
    await ContextGovernor().prepare_request(state, altered, tool_definitions=[])
    assert next(d for d in state.manifest.plan.decisions if d.source_id == skill.source_id).action == "defer"
