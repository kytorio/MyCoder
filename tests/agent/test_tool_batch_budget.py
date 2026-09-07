"""L1 keeps raw evidence and fits complete tool-result batches."""

import json
import os
from copy import deepcopy

import pytest

from nanobot.utils import helpers


def test_artifact_atomic_writer_preserves_exact_unicode_and_newlines(tmp_path):
    content = "第一行\r\nsecond\n第三行\r最后😀"
    target = tmp_path / "artifact.txt"
    helpers.write_text_atomic(target, content, newline="")
    assert target.read_bytes() == content.encode("utf-8")


def test_legacy_atomic_writer_keeps_platform_newline_behavior(tmp_path):
    target = tmp_path / "legacy.txt"
    helpers._write_text_atomic(target, "first\nsecond")
    assert target.read_bytes() == f"first{os.linesep}second".encode("utf-8")


def test_atomic_writer_failure_preserves_previous_content(tmp_path, monkeypatch):
    target = tmp_path / "artifact.txt"
    target.write_bytes(b"old")

    def fail_replace(*args, **kwargs):
        raise OSError("synthetic disk error")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError):
        helpers.write_text_atomic(target, "new", newline="")
    assert target.read_bytes() == b"old"
    assert list(tmp_path.iterdir()) == [target]


def test_tool_context_does_not_enable_artifact_read_by_default():
    from nanobot.agent.tools.context import ToolContext
    from nanobot.config.schema import ToolsConfig

    context = ToolContext(config=ToolsConfig(), workspace="unused-test-root")
    assert context.context_artifact_store is None
    assert context.context_artifact_page_token_budget == 2048


@pytest.mark.parametrize("structured", [False, True])
def test_enforce_normalization_keeps_raw_results_for_transcript(tmp_path, structured):
    from nanobot.agent.context_governance import ContextGovernor
    from nanobot.config.schema import ContextConfig
    from tests.agent.test_context_manifest import state_for

    raw = [{"type": "text", "text": "原始证据\r\n" * 10_000},
           {"type": "image_url", "image_url": {"url": "data:image/png;base64,example"}}] if structured else "raw evidence\r\n" * 10_000
    original = deepcopy(raw)
    state = state_for(tmp_path, [], context=ContextConfig(mode="enforce"),
                      runtime_data_dir=tmp_path / "runtime")
    result = ContextGovernor().normalize_tool_result(state.config, "new-call", "custom", raw)
    assert result == original
    assert raw == original
    assert not (tmp_path / ".nanobot" / "tool-results").exists()


def _batch(contents, *, name="custom"):
    return [{"role": "system", "content": "Keep all current evidence."},
            {"role": "user", "content": "Complete the current task."},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": f"call-{i}", "type": "function", "function": {"name": name, "arguments": "{}"}}
                for i in range(len(contents))]},
            *({"role": "tool", "tool_call_id": f"call-{i}", "name": name, "content": content}
              for i, content in enumerate(contents))]


def _l1_state(tmp_path, messages, *, single=400, batch=900, window=100000, layers=None):
    from nanobot.agent.context_artifacts import ToolResultArtifactStore
    from nanobot.config.schema import ContextConfig
    from tests.agent.test_context_manifest import state_for

    store = ToolResultArtifactStore(tmp_path / "runtime" / "artifacts", max_bytes=1_048_576)
    state = state_for(tmp_path, messages, context=ContextConfig(
        mode="enforce", enabled_layers=["L1"] if layers is None else layers,
        tool_result_token_budget=single, tool_batch_token_budget=batch,
        safety_margin_tokens=1024), runtime_data_dir=tmp_path / "runtime", artifact_store=store,
        context_window_tokens=window)
    return state, store


async def test_l1_fits_complete_batch_and_reads_back_originals(tmp_path):
    from nanobot.agent.context_governance import ContextGovernor

    contents = [f"Important first evidence {i}.\r\n" * 600 for i in range(3)]
    raw = _batch(contents)
    original = deepcopy(raw)
    state, store = _l1_state(tmp_path, raw)
    prepared, _ = await ContextGovernor().prepare_request(state, raw, tool_definitions=[])
    results = [m for m in prepared if m["role"] == "tool"]
    assert len(results) == 3
    assert raw == original
    assert sum(helpers.estimate_prompt_tokens([m]) - 4 for m in results) <= 900
    for index, message in enumerate(results):
        assert helpers.estimate_prompt_tokens([message]) - 4 <= 400
        value = json.loads(message["content"])
        assert value["preview"]
        ref = store.resolve(value["artifact_id"], "n05:test")
        assert store.read(ref, "n05:test", max_bytes=1_048_576) == contents[index]
    decisions = [d for d in state.manifest.plan.decisions if d.stage == "L1"]
    assert len(decisions) == 3
    assert all(d.reason_code == "offloaded" and d.artifact_ref for d in decisions)
    final = {d.source_id: d.after_tokens for d in state.manifest.plan.decisions}
    assert state.manifest.plan.predicted_total == sum(final.values())


async def test_l1_first_read_is_archived_not_exempted(tmp_path):
    from nanobot.agent.context_governance import ContextGovernor

    raw = _batch(["first-read evidence\n" * 3000], name="read_file")
    state, store = _l1_state(tmp_path, raw)
    prepared, _ = await ContextGovernor().prepare_request(state, raw, tool_definitions=[])
    ref_id = json.loads(prepared[-1]["content"])["artifact_id"]
    assert store.read(store.resolve(ref_id, "n05:test"), "n05:test", max_bytes=1_048_576) == raw[-1]["content"]


async def test_l1_write_failure_refuses_without_losing_raw(tmp_path, monkeypatch):
    from nanobot.agent.context_governance import ContextGovernor
    from nanobot.agent.context_plan import ContextPlanError

    raw = _batch(["cannot lose this\n" * 3000])
    original = deepcopy(raw)
    state, store = _l1_state(tmp_path, raw)

    def fail_put(*args, **kwargs):
        raise OSError("private filesystem failure")

    monkeypatch.setattr(store, "put", fail_put)
    with pytest.raises(ContextPlanError, match="archive_failed"):
        await ContextGovernor().prepare_request(state, raw, tool_definitions=[])
    assert raw == original
    assert state.manifest.status == "refused"
    assert "private filesystem failure" not in json.dumps(state.manifest.to_log())
    assert any(d.reason_code == "archive_failed" and d.action == "keep" for d in state.manifest.plan.decisions)


async def test_runner_keeps_raw_checkpoints_and_budgets_parallel_errors(tmp_path):
    from agent.runner_helpers import make_run_spec
    from nanobot.agent.runner import AgentRunner
    from nanobot.agent.tools.base import Tool, ToolResult
    from nanobot.agent.tools.registry import ToolRegistry
    from nanobot.providers.base import LLMResponse, ToolCallRequest
    from tests.agent.test_context_manifest import OfflineProvider

    class EvidenceTool(Tool):
        name = "evidence"
        description = "Return original evidence"
        parameters = {"type": "object", "properties": {"error": {"type": "boolean"}}}
        read_only = True

        async def execute(self, **kwargs):
            return ToolResult.error("failed operation\r\n" * 2000) if kwargs.get("error") else ToolResult("evidence\r\n" * 2000)

    requests = []

    class Provider(OfflineProvider):
        async def chat(self, messages, **kwargs):
            requests.append(deepcopy(messages))
            return LLMResponse(content="", tool_calls=[
                ToolCallRequest(id="call-good", name="evidence", arguments={}),
                ToolCallRequest(id="call-bad", name="evidence", arguments={"error": True}),
            ]) if len(requests) == 1 else LLMResponse(content="done")

    state, store = _l1_state(tmp_path, [])
    registry = ToolRegistry()
    registry.register(EvidenceTool())
    checkpoints = []

    async def checkpoint(data):
        checkpoints.append(deepcopy(data))

    result = await AgentRunner().run(make_run_spec(
        Provider(provider_name="offline"), model="offline", max_iterations=2,
        initial_messages=[{"role": "user", "content": "Read both sources."}], tools=registry,
        max_tool_result_chars=100, max_tokens=128, context_window_tokens=100000,
        context_config=state.config.context, artifact_store=store, session_key="n05:test",
        checkpoint_callback=checkpoint, concurrent_tools=True))
    assert result.final_content == "done"
    assert len(requests) == 2
    previews = [json.loads(m["content"]) for m in requests[1] if m["role"] == "tool"]
    assert [p["is_error"] for p in previews] == [False, True]
    raw_results = [m for m in result.messages if m["role"] == "tool"]
    checkpoint_results = next(c["completed_tool_results"] for c in checkpoints if c["phase"] == "tools_completed")
    assert raw_results == checkpoint_results
    for raw, preview in zip(raw_results, previews):
        assert len(raw["content"]) > 1000
        assert store.read(store.resolve(preview["artifact_id"], "n05:test"), "n05:test", 1_048_576) == raw["content"]


@pytest.mark.parametrize("mode", ["observe", "enforce"])
def test_loop_injects_same_store_only_when_enforcing(tmp_path, monkeypatch, mode):
    from nanobot.agent.loop import AgentLoop
    from nanobot.agent.tools.context_artifact import ContextArtifactTool
    from nanobot.agent.tools.loader import ToolLoader
    from nanobot.bus.queue import MessageBus
    from nanobot.config.schema import ContextConfig
    from tests.agent.test_context_manifest import OfflineProvider

    # Exercise the real construction seam without instantiating unrelated plugins.
    monkeypatch.setattr(ToolLoader, "discover", lambda self: [ContextArtifactTool])
    monkeypatch.setattr(ToolLoader, "_discover_plugins", lambda self: {})
    loop = AgentLoop(bus=MessageBus(), provider=OfflineProvider(provider_name="offline"),
                     workspace=tmp_path / "workspace", context_config=ContextConfig(mode=mode),
                     runtime_data_dir=tmp_path / "explicit-runtime")
    assert loop.tools.has("context_artifact_read") is (mode == "enforce")
    if mode == "enforce":
        assert loop.context_artifact_store.root == tmp_path / "explicit-runtime" / "context-artifacts"
        assert loop.tools.get("context_artifact_read")._store is loop.context_artifact_store
    else:
        assert loop.context_artifact_store is None
        assert not (tmp_path / "explicit-runtime" / "context-artifacts").exists()


@pytest.mark.parametrize("excess", [0, 1])
async def test_l1_exact_batch_boundary(tmp_path, excess):
    from nanobot.agent.context_governance import ContextGovernor

    raw = _batch(["boundary evidence " * 120] * 2)
    costs = [helpers.estimate_prompt_tokens([m]) - 4 for m in raw if m["role"] == "tool"]
    state, store = _l1_state(tmp_path, raw, single=max(costs), batch=sum(costs) - excess)
    prepared, _ = await ContextGovernor().prepare_request(state, raw, tool_definitions=[])
    assert (prepared == raw) is (excess == 0)
    assert sum(helpers.estimate_prompt_tokens([m]) - 4 for m in prepared if m["role"] == "tool") <= sum(costs) - excess
    assert bool(list(store.root.iterdir())) is bool(excess)


async def test_l1_versioned_blocks_and_repeated_governance(tmp_path):
    from nanobot.agent.context_governance import ContextGovernor

    blocks = [{"type": "text", "text": "first block\r\n" * 1000, "annotations": ["retain"]},
              {"type": "text", "text": "second block\n" * 1000}]
    raw = _batch([blocks])
    state, store = _l1_state(tmp_path, raw)
    first, _ = await ContextGovernor().prepare_request(state, raw, tool_definitions=[])
    files = sorted(store.root.iterdir())
    second, _ = await ContextGovernor().prepare_request(state, raw, tool_definitions=[])
    assert first == second
    assert sorted(store.root.iterdir()) == files
    value = json.loads(first[-1]["content"])
    archived = json.loads(store.read(store.resolve(value["artifact_id"], "n05:test"), "n05:test", 1_048_576))
    assert archived == {"schema_version": 1, "kind": "tool_result_blocks", "blocks": blocks}
    assert value["format"] == "blocks-json-v1"
    assert raw[-1]["content"] == blocks


async def test_l1_global_budget_counts_schema_once(tmp_path):
    from nanobot.agent.context_governance import ContextGovernor

    raw = _batch(["large global evidence\n" * 4000] * 2)
    state, _ = _l1_state(tmp_path, raw, single=2048, batch=4096, window=2400)
    schemas = [{"type": "function", "function": {"name": "custom", "description": "details " * 50,
                "parameters": {"type": "object", "properties": {}}}}]
    prepared, _ = await ContextGovernor().prepare_request(state, raw, tool_definitions=schemas)
    assert helpers.estimate_prompt_tokens(prepared, schemas) <= 2400 - 128 - 1024
    assert sum(s.kind == "mcp_schema" for s in state.manifest.plan.sources) == 1
    assert sum(s.kind == "envelope" for s in state.manifest.plan.sources) == 1
    final = {d.source_id: d.after_tokens for d in state.manifest.plan.decisions}
    assert state.manifest.plan.predicted_total == sum(final.values())
    assert state.manifest.plan.rendered_estimate == helpers.estimate_prompt_tokens(prepared, schemas)


async def test_l1_disabled_keeps_whole_payload_and_no_archive(tmp_path):
    from nanobot.agent.context_governance import ContextGovernor

    raw = _batch(["raw evidence " * 2000])
    state, store = _l1_state(tmp_path, raw, layers=[])
    prepared, _ = await ContextGovernor().prepare_request(state, raw, tool_definitions=[])
    assert prepared == raw
    assert list(store.root.iterdir()) == []
    assert all(d.stage != "L1" for d in state.manifest.plan.decisions)


async def test_l1_preview_keeps_a_meaningful_prefix_and_declares_reference_strategy(tmp_path):
    from nanobot.agent.context_governance import ContextGovernor
    from nanobot.agent.context_plan import ContextPlanError

    raw = _batch(["Evidence sentence with enough detail to make a decision.\n" * 500])
    state, _ = _l1_state(tmp_path, raw)
    prepared, _ = await ContextGovernor().prepare_request(state, raw, tool_definitions=[])
    value = json.loads(prepared[-1]["content"])
    assert len(value["preview"]) >= 256
    assert raw[-1]["content"].startswith(value["preview"])
    source = next(s for s in state.manifest.plan.sources if s.kind == "tool_result")
    assert "reference" in source.allowed_strategies
    # Fit metadata and one character, but not a useful minimum prefix.
    overhead = {**prepared[-1], "content": json.dumps({**value, "preview": "E"}, ensure_ascii=False, separators=(",", ":"))}
    tiny, _ = _l1_state(tmp_path, raw, single=helpers.estimate_prompt_tokens([overhead]) - 4)
    with pytest.raises(ContextPlanError, match="irreducible_floor"):
        await ContextGovernor().prepare_request(tiny, raw, tool_definitions=[])


async def test_l1_preview_to_real_read_tool_roundtrip_after_restart(tmp_path):
    from nanobot.agent.context_artifacts import ToolResultArtifactStore
    from nanobot.agent.context_governance import ContextGovernor
    from nanobot.agent.tools.context import RequestContext, request_context
    from nanobot.agent.tools.context_artifact import ContextArtifactTool

    original = "完整证据🙂\r\nline\n\r" * 500
    raw = _batch([original], name="read_file")
    state, store = _l1_state(tmp_path, raw)
    prepared, _ = await ContextGovernor().prepare_request(state, raw, tool_definitions=[])
    ref = json.loads(prepared[-1]["content"])["artifact_id"]
    tool = ContextArtifactTool(ToolResultArtifactStore(store.root, max_bytes=1_048_576), page_token_budget=400)
    chunks, cursor = [], 0
    with request_context(RequestContext(channel="test", chat_id="a", session_key="n05:test")):
        while True:
            result = await tool.execute(ref=ref, offset_chars=cursor, max_chars=16384)
            assert not result.is_error
            page = json.loads(result)
            chunks.append(page["text"])
            batch = _batch([str(result)], name=tool.name)
            next_state, _ = _l1_state(tmp_path, batch)
            fitted, _ = await ContextGovernor().prepare_request(next_state, batch, tool_definitions=[])
            assert fitted == batch
            if page["next_offset_chars"] is None:
                break
            assert page["next_offset_chars"] == cursor + len(page["text"])
            cursor = page["next_offset_chars"]
    assert "".join(chunks) == original
    assert len(list(store.root.iterdir())) == 1


async def test_l1_read_pages_never_recursively_offload(tmp_path):
    from nanobot.agent.context_governance import ContextGovernor
    from nanobot.agent.context_plan import ContextPlanError

    raw = _batch(["page text " * 1000] * 3, name="context_artifact_read")
    state, store = _l1_state(tmp_path, raw)
    with pytest.raises(ContextPlanError, match="irreducible_floor"):
        await ContextGovernor().prepare_request(state, raw, tool_definitions=[])
    assert list(store.root.iterdir()) == []
    assert raw[-1]["content"] == "page text " * 1000


async def test_l1_no_session_does_not_create_shared_default_scope(tmp_path):
    from nanobot.agent.context_governance import ContextGovernor
    from nanobot.agent.context_plan import ContextPlanError

    raw = _batch(["scoped private evidence " * 2000])
    state, store = _l1_state(tmp_path, raw)
    state.config.session_key = None
    with pytest.raises(ContextPlanError, match="archive_failed"):
        await ContextGovernor().prepare_request(state, raw, tool_definitions=[])
    assert list(store.root.iterdir()) == []


@pytest.mark.parametrize("structured,is_error", [(False, False), (True, False), (False, True)], ids=["text", "blocks", "error"])
async def test_sdk_config_injection_and_persisted_original_survive_restart(tmp_path, monkeypatch, structured, is_error):
    from nanobot.agent.tools.base import Tool, ToolResult
    from nanobot.agent.tools.context_artifact import ContextArtifactTool
    from nanobot.agent.tools.loader import ToolLoader
    from nanobot.nanobot import Nanobot
    from nanobot.providers.base import LLMResponse, ToolCallRequest
    from tests.agent.test_context_manifest import OfflineProvider

    text = "Persistent original evidence🙂\r\n" * 1000
    original = ([{"type": "text", "text": text, "annotations": ["retain"]},
                 {"type": "text", "text": "Second evidence\r\n" * 1200}]
                if structured else text)
    expected = original + "\n\n[Analyze the error above and try a different approach.]" if is_error else original

    class EvidenceTool(Tool):
        name = "evidence"
        description = "Read synthetic evidence"
        parameters = {"type": "object", "properties": {}}
        read_only = True

        @classmethod
        def create(cls, ctx):
            return cls()

        async def execute(self, **kwargs):
            return ToolResult.error(original) if is_error else original

    calls = []

    class Provider(OfflineProvider):
        async def chat(self, messages, **kwargs):
            calls.append(deepcopy(messages))
            return LLMResponse(content="", tool_calls=[ToolCallRequest(
                id="persisted-call", name="evidence", arguments={})]) if len(calls) == 1 else LLMResponse(content="done")

    monkeypatch.setattr(ToolLoader, "discover", lambda self: [EvidenceTool, ContextArtifactTool])
    monkeypatch.setattr(ToolLoader, "_discover_plugins", lambda self: {})
    monkeypatch.setattr("nanobot.providers.factory.make_provider", lambda *a, **k: Provider(provider_name="offline"))
    config_path = tmp_path / "instance" / "config.json"
    config_path.parent.mkdir()
    config_path.write_text(json.dumps({"agents": {"defaults": {"model": "offline", "contextWindowTokens": 100000},
        "context": {"mode": "enforce", "enabledLayers": ["L1"]}}}), encoding="utf-8")
    bot = Nanobot.from_config(config_path, workspace=tmp_path / "ws")
    try:
        outcome = await bot.run("Read the evidence", session_key="sdk:n06")
        assert outcome.content == "done"
        expected_root = config_path.parent / "context-artifacts"
        assert bot._loop.context_artifact_store.root == expected_root
        assert bot._loop.tools.get("context_artifact_read")._store is bot._loop.context_artifact_store
        ref_id = json.loads(next(m["content"] for m in calls[-1] if m["role"] == "tool"))["artifact_id"]
    finally:
        await bot.aclose()
    restarted = Nanobot.from_config(config_path, workspace=tmp_path / "ws")
    try:
        session = restarted._loop.sessions.get_or_create("sdk:n06")
        persisted = next(m for m in session.messages if m["role"] == "tool")
        assert persisted["content"] == expected
        assert persisted["_meta"] == {"tool_result_error": is_error}
        replayed = next(m for m in session.get_history() if m["role"] == "tool")
        assert replayed["_meta"] == {"tool_result_error": is_error}
        store = restarted._loop.context_artifact_store
        archived = store.read(store.resolve(ref_id, "sdk:n06"), "sdk:n06", 1_048_576)
        if structured:
            assert json.loads(archived) == {"schema_version": 1, "kind": "tool_result_blocks", "blocks": original}
        else:
            assert archived == expected
        await restarted.run("Review the saved evidence", session_key="sdk:n06")
        old_preview = json.loads(next(m["content"] for m in calls[-1] if m["role"] == "tool"))
        assert old_preview["is_error"] is is_error
    finally:
        await restarted.aclose()


@pytest.mark.parametrize("mode", ["observe", "enforce"])
@pytest.mark.parametrize("flag", [True, False, 1, "true", None])
def test_persisted_error_metadata_is_boolean_tool_only(mode, flag):
    from nanobot.agent.loop import AgentLoop
    from nanobot.config.schema import ContextConfig
    from nanobot.session.manager import Session

    loop = AgentLoop.__new__(AgentLoop)
    loop.context_config = ContextConfig(mode=mode)
    loop.max_tool_result_chars = 16_000
    session = Session(key="n6:metadata")
    messages = _batch(["small error evidence"])[1:]
    for message in messages:
        message["_meta"] = {"tool_result_error": flag, "private_runtime": "must not replay"}
    loop._save_turn(session, messages, skip=0)
    for message in session.messages + session.get_history():
        if mode == "enforce" and type(flag) is bool and message["role"] == "tool":
            assert message.get("_meta") == {"tool_result_error": flag}
        else:
            assert "_meta" not in message


@pytest.mark.parametrize("flag", [True, False, 1, "true", None])
def test_history_replays_only_boolean_tool_error_metadata(flag):
    from nanobot.session.manager import Session

    messages = _batch(["small evidence"])[1:]
    for message in messages:
        message["_meta"] = {"tool_result_error": flag, "private_runtime": "must not replay"}
    session = Session(key="n6:replay", messages=messages)
    for message in session.get_history():
        if type(flag) is bool and message["role"] == "tool":
            assert message.get("_meta") == {"tool_result_error": flag}
        else:
            assert "_meta" not in message


@pytest.mark.parametrize("structured", [False, True], ids=["text", "blocks"])
def test_observe_save_turn_keeps_legacy_truncation(structured):
    from nanobot.agent.loop import AgentLoop
    from nanobot.config.schema import ContextConfig
    from nanobot.session.manager import Session

    loop = AgentLoop.__new__(AgentLoop)
    loop.context_config = ContextConfig(mode="observe")
    loop.max_tool_result_chars = 16_000
    text = "0123456789" * 1700
    content = [{"type": "text", "text": text, "annotations": ["retain"]}] if structured else text
    messages = _batch([content])[1:]
    original = deepcopy(messages)
    session = Session(key="n6:observe")
    loop._save_turn(session, messages, skip=0)
    truncated = "0123456789" * 1600 + "\n... (truncated)"
    assert session.messages[-1]["content"] == ([{"type": "text", "text": truncated, "annotations": ["retain"]}] if structured else truncated)
    assert messages == original


@pytest.mark.parametrize("mode", ["observe", "enforce"])
def test_tool_image_sanitization_unchanged_by_persistence_mode(mode):
    from nanobot.agent.loop import AgentLoop
    from nanobot.config.schema import ContextConfig
    from nanobot.session.manager import Session

    loop = AgentLoop.__new__(AgentLoop)
    loop.context_config = ContextConfig(mode=mode)
    loop.max_tool_result_chars = 16_000
    blocks = [{"type": "text", "text": "visible text"},
              {"type": "image_url", "image_url": {"url": "data:image/png;base64,private"}, "_meta": {"path": "/media/photo.png"}},
              {"type": "image_url", "image_url": {"url": "https://example.invalid/image.png"}}]
    messages = _batch([blocks])[1:]
    original = deepcopy(messages)
    session = Session(key="n6:images")
    loop._save_turn(session, messages, skip=0)
    assert session.messages[-1]["content"] == [
        {"type": "text", "text": "visible text"},
        {"type": "text", "text": "[image: /media/photo.png]"},
        {"type": "image_url", "image_url": {"url": "https://example.invalid/image.png"}},
    ]
    assert messages == original
