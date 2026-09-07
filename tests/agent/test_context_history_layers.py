"""Verified old groups may be archived; raw history and first reads stay intact."""

import json
from copy import deepcopy
from dataclasses import replace

import pytest

from nanobot.agent.context_governance import ContextGovernor
from nanobot.agent.context_plan import (
    ContextPlanError,
    ContextRequestOutcome,
    content_hash,
    reconcile_plan,
)
from tests.agent.test_context_acceptance import compaction_for
from tests.agent.test_tool_batch_budget import _l1_state


def history_state(tmp_path, history, *, layers, boundary=None, budget=2000):
    raw, compaction = compaction_for(history)
    state, store = _l1_state(tmp_path, raw, layers=layers)
    state.config.context_block_limit = budget
    state.compaction = compaction
    if boundary is not None:
        request = ContextRequestOutcome("seed", "seed", 0, "unknown")
        compaction.begin_request(raw[:boundary], raw_boundary=boundary, request=request)
        compaction.accept_request(raw[:boundary], raw_boundary=boundary,
                                  outcome=replace(request, status="accepted"))
    return raw, state, store


def parallel_turn():
    return [
        {"role": "user", "content": "old inspection"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": key, "type": "function", "function": {"name": "custom", "arguments": "{}"}}
            for key in ("a", "b")]},
        {"role": "tool", "tool_call_id": "a", "name": "custom", "content": "first evidence\r\n" * 1500},
        {"role": "tool", "tool_call_id": "b", "name": "custom", "content": "second evidence\n" * 1500},
        {"role": "assistant", "content": "inspection complete"},
    ]


async def test_l2_archives_complete_old_parallel_group_once_and_preserves_raw(tmp_path, monkeypatch):
    raw, state, store = history_state(tmp_path, parallel_turn(), layers=["L2"], boundary=6)
    original = deepcopy(raw)
    writes = []
    put = store.put

    def track(*args):
        writes.append(args)
        return put(*args)

    monkeypatch.setattr(store, "put", track)
    governor = ContextGovernor()
    first, _ = await governor.prepare_request(state, raw, tool_definitions=[], transcript=raw)
    second, _ = await governor.prepare_request(state, raw, tool_definitions=[], transcript=raw)
    assert first == second
    assert first[0] == raw[0] and first[-1] == raw[-1]
    assert governor.has_valid_tool_pairs(first)
    decisions = [d for d in state.manifest.plan.decisions if d.stage == "L2"]
    assert decisions and all(d.reason_code == "snipped" for d in decisions)
    assert len(writes) == 1
    ref = store.resolve(next(d.artifact_ref for d in decisions if d.artifact_ref), state.config.session_key)
    assert json.loads(store.read(ref, state.config.session_key, 1_048_576)) == original[1:6]
    assert raw == original
    final_costs = {d.source_id: d.after_tokens for d in state.manifest.plan.decisions}
    assert state.manifest.plan.predicted_total == sum(final_costs.values())


async def test_l2_unknown_imported_history_is_protected(tmp_path):
    raw, state, store = history_state(tmp_path, parallel_turn(), layers=["L2"])
    with pytest.raises(ContextPlanError):
        await ContextGovernor().prepare_request(state, raw, tool_definitions=[], transcript=raw)
    assert not list(store.root.glob("*.json"))
    assert state.compaction.raw_accepted_boundary == 0


async def test_l2_archive_failure_does_not_delete_group(tmp_path, monkeypatch):
    raw, state, store = history_state(tmp_path, parallel_turn(), layers=["L2"], boundary=6)
    original = deepcopy(raw)

    def fail(*args):
        raise OSError("synthetic full disk")

    monkeypatch.setattr(store, "put", fail)
    with pytest.raises(ContextPlanError) as error:
        await ContextGovernor().prepare_request(state, raw, tool_definitions=[], transcript=raw)
    assert error.value.manifest.reason_code == "archive_failed"
    assert raw == original


def read_exchange(call_id, content, path="src/main.py"):
    return [{"role": "assistant", "content": "", "tool_calls": [{
        "id": call_id, "type": "function", "function": {
            "name": "read_file", "arguments": json.dumps({"path": path})}}]},
        {"role": "tool", "name": "read_file", "tool_call_id": call_id, "content": content}]


@pytest.mark.parametrize("changed_path,changed_version", [(False, False), (True, False), (False, True)])
async def test_l3_requires_accepted_same_path_and_version_and_keeps_first_reads(tmp_path, changed_path, changed_version):
    content = "important source evidence\r\n" * 500
    duplicate = "new version\r\n" * 500 if changed_version else content
    history = [{"role": "user", "content": "inspect sources"},
               *read_exchange("one", content),
               *read_exchange("two", duplicate, path="other.py" if changed_path else "src/main.py"),
               *read_exchange("fresh", content)]
    raw, state, store = history_state(tmp_path, history, layers=["L3"], boundary=6, budget=20000)
    original = deepcopy(raw)
    prepared, _ = await ContextGovernor().prepare_request(state, raw, tool_definitions=[], transcript=raw)
    results = {m["tool_call_id"]: m for m in prepared if m["role"] == "tool"}
    assert results["one"]["content"] == content
    assert results["fresh"]["content"] == content
    assert ContextGovernor.has_valid_tool_pairs(prepared)
    if changed_path or changed_version:
        assert results["two"]["content"] == duplicate
        assert not list(store.root.glob("*.json"))
    else:
        preview = json.loads(results["two"]["content"])
        assert preview["preview"] and len(preview["preview"]) < len(content)
        ref = store.resolve(preview["artifact_id"], state.config.session_key)
        assert store.read(ref, state.config.session_key, 1_048_576) == content
        assert any(d.reason_code == "micro_compacted" for d in state.manifest.plan.decisions)
    assert raw == original


async def test_l3_unknown_duplicate_history_keeps_both_results(tmp_path):
    content = "same content " * 500
    raw, state, store = history_state(tmp_path, [
        {"role": "user", "content": "inspect"}, *read_exchange("one", content),
        *read_exchange("two", content)], layers=["L3"], budget=20000)
    prepared, _ = await ContextGovernor().prepare_request(state, raw, tool_definitions=[], transcript=raw)
    assert prepared == raw
    assert not list(store.root.glob("*.json"))


async def test_l1_then_l2_keeps_last_decision_cost_and_current_input(tmp_path):
    raw, state, store = history_state(tmp_path, parallel_turn(), layers=["L1", "L2"], boundary=6, budget=400)
    prepared, _ = await ContextGovernor().prepare_request(state, raw, tool_definitions=[], transcript=raw)
    assert prepared[-1] == raw[-1]
    assert any(d.stage == "L1" for d in state.manifest.plan.decisions)
    assert any(d.stage == "L2" and d.action == "reference" for d in state.manifest.plan.decisions)
    costs = {d.source_id: d.after_tokens for d in state.manifest.plan.decisions}
    assert state.manifest.plan.predicted_total == sum(costs.values())
    assert state.manifest.plan.rendered_estimate <= 400


async def test_current_turn_is_not_snipped_even_after_accepted_request(tmp_path):
    raw, state, store = history_state(tmp_path, [], layers=["L2"], boundary=None, budget=100)
    raw[-1]["content"] = "current requirements " * 500
    request = ContextRequestOutcome("seed", "seed", 0, "unknown")
    state.compaction.begin_request(raw, raw_boundary=2, request=request)
    state.compaction.accept_request(raw, raw_boundary=2, outcome=replace(request, status="accepted"))
    with pytest.raises(ContextPlanError):
        await ContextGovernor().prepare_request(state, raw, tool_definitions=[], transcript=raw)
    assert not list(store.root.glob("*.json"))


async def test_changed_raw_prefix_invalidates_old_history_proof(tmp_path):
    raw, state, store = history_state(tmp_path, parallel_turn(), layers=["L2"], boundary=6)
    raw[3]["content"] = "unseen replacement evidence " * 5000
    with pytest.raises(ContextPlanError):
        await ContextGovernor().prepare_request(state, raw, tool_definitions=[], transcript=raw)
    assert not list(store.root.glob("*.json"))


async def test_l3_archive_failure_preserves_duplicate(tmp_path, monkeypatch):
    content = "same evidence " * 500
    raw, state, store = history_state(tmp_path, [{"role": "user", "content": "inspect"},
        *read_exchange("one", content), *read_exchange("two", content)],
        layers=["L3"], boundary=6, budget=20000)
    original = deepcopy(raw)

    def fail(*args):
        raise OSError("synthetic full disk")

    monkeypatch.setattr(store, "put", fail)
    with pytest.raises(ContextPlanError) as error:
        await ContextGovernor().prepare_request(state, raw, tool_definitions=[], transcript=raw)
    assert error.value.manifest.reason_code == "archive_failed"
    assert raw == original


async def test_accepted_l1_preview_does_not_prove_full_raw_result_seen(tmp_path):
    content = "full raw evidence never sent " * 1200
    raw, state, store = history_state(tmp_path, [{"role": "user", "content": "inspect"},
        *read_exchange("one", content), *read_exchange("two", content)],
        layers=["L1"], budget=20000)
    governor = ContextGovernor()
    prepared, context = await governor.prepare_request(state, raw, tool_definitions=[], transcript=raw)
    assert prepared[3]["content"] != content
    from nanobot.providers.base import LLMResponse

    governor.record_response(state, LLMResponse(content="done", context_outcome=replace(
        context.context_request, status="accepted")))
    assert 3 not in governor._verified_positions(state.compaction, raw)
    assert 5 not in governor._verified_positions(state.compaction, raw)
    assert state.compaction.request_messages(raw) == prepared

    from nanobot.agent.context import TranscriptInput
    from nanobot.agent.context_governance import ContextCompactionState

    next_input = TranscriptInput(history=raw[1:], current_message="next turn")
    next_raw, restored = ContextCompactionState.from_transcript(
        next_input, state.compaction.transcript_builder, state.compaction.consolidate_history, None,
        consumption=state.compaction.consumption())
    next_view = restored.request_messages(next_raw)
    assert next_view[:-1] == prepared
    assert all(m.get("content") != content for m in next_view)
    assert 3 not in governor._verified_positions(restored, next_raw)


async def test_accepted_view_retains_only_host_tool_error_metadata(tmp_path):
    from nanobot.providers.base import LLMResponse

    content = "read failed " * 500
    history = [{"role": "user", "content": "inspect"},
               *read_exchange("one", content), *read_exchange("two", content)]
    for message in history:
        if message["role"] == "tool":
            message["_meta"] = {"tool_result_error": True, "untrusted": "do not replay"}
    raw, state, _ = history_state(tmp_path, history, layers=["L3"], budget=20000)
    governor = ContextGovernor()
    prepared, context = await governor.prepare_request(state, raw, tool_definitions=[], transcript=raw)
    assert all("_meta" not in m for m in prepared)
    governor.record_response(state, LLMResponse(content="done", context_outcome=replace(
        context.context_request, status="accepted")))
    next_view = state.compaction.request_messages(raw)
    assert next_view[3]["_meta"] == {"tool_result_error": True}
    second, _ = await governor.prepare_request(state, next_view, tool_definitions=[], transcript=raw)
    assert second[3]["content"] == content
    assert second[5]["content"] == content


async def test_l3_error_exemption_uses_authenticated_raw_flag(tmp_path):
    content = "read failed " * 500
    history = [{"role": "user", "content": "inspect"},
               *read_exchange("one", content), *read_exchange("two", content)]
    for message in history:
        if message["role"] == "tool":
            message["_meta"] = {"tool_result_error": True}
    raw, state, store = history_state(tmp_path, history, layers=["L3"], boundary=6, budget=20000)
    public = [{k: v for k, v in m.items() if k != "_meta"} for m in raw]
    prepared, _ = await ContextGovernor().prepare_request(state, public, tool_definitions=[], transcript=raw)
    assert prepared[5]["content"] == content
    assert not list(store.root.glob("*.json"))


@pytest.mark.parametrize("route", ["finalization", "budget_finalization", "malformed_retry"])
async def test_synthetic_user_suffix_cannot_make_current_turn_snippable(tmp_path, route):
    from nanobot.agent.runner import AgentRunner
    from nanobot.agent.tools.registry import ToolRegistry
    from nanobot.providers.base import LLMResponse
    from tests.agent.runner_helpers import make_run_spec
    from tests.agent.test_context_acceptance import EvidenceProvider

    raw, state, store = history_state(tmp_path, [], layers=["L2"], budget=400)
    raw[-1]["content"] = "current acceptance requirements " * 1500
    request = ContextRequestOutcome("seed", "seed", 0, "unknown")
    state.compaction.begin_request(raw, raw_boundary=len(raw), request=request)
    state.compaction.accept_request(raw, raw_boundary=len(raw), outcome=replace(request, status="accepted"))
    original = deepcopy(raw)
    provider = EvidenceProvider([LLMResponse(content="must not dispatch")])
    spec = make_run_spec(provider, model="offline", initial_messages=raw, tools=ToolRegistry(),
                         max_iterations=1, max_tool_result_chars=1000)
    runner = AgentRunner()
    with pytest.raises(ContextPlanError, match="irreducible_floor"):
        if route == "finalization":
            await runner._request_finalization_retry(spec, raw, request_state=state, transcript=raw)
        else:
            retry = (runner._budget_exhausted_finalization_messages(raw) if route == "budget_finalization"
                     else runner._malformed_tool_call_retry_messages(raw, "malformed response"))
            await runner._request_no_tools(spec, retry, request_state=state,
                                           transcript=None if route == "malformed_retry" else raw)
    assert raw == original
    assert not list(store.root.glob("*.json"))


async def test_finalization_can_snip_old_turn_but_keeps_current_and_pending_delta(tmp_path):
    from nanobot.agent.runner import AgentRunner

    raw, state, store = history_state(tmp_path, parallel_turn(), layers=["L2"], boundary=7)
    raw.append({"role": "user", "content": "pending new correction"})
    original = deepcopy(raw)
    retry = AgentRunner._finalization_retry_messages(state.compaction.request_messages(raw))
    prepared, _ = await ContextGovernor().prepare_request(state, retry, tool_definitions=None, transcript=raw)
    assert raw[-2] in prepared
    assert raw[-1] in prepared
    assert prepared[-1] == retry[-1]
    assert len(list(store.root.glob("*.json"))) == 1
    assert raw == original


async def test_l2_duplicate_groups_charge_each_source_occurrence_once(tmp_path):
    turn = [{"role": "user", "content": "repeat inspection"},
            {"role": "assistant", "content": "repeated old evidence " * 1500}]
    raw, state, store = history_state(tmp_path, [*deepcopy(turn), *deepcopy(turn)],
                                      layers=["L2"], boundary=5, budget=500)
    original = deepcopy(raw)
    initial = reconcile_plan(None, raw, [], input_budget=500, model=state.config.model)
    prepared, _ = await ContextGovernor().prepare_request(state, raw, tool_definitions=[], transcript=raw)
    plan = state.manifest.plan
    removed_ids = [s.source_id for s in initial.sources
                   if s.content_hash in {content_hash(m) for m in turn}]
    replacements = [d for d in plan.decisions if d.stage == "L2" and d.action == "reference"]
    assert len(removed_ids) == 4
    assert [d.source_id for d in replacements] == removed_ids
    assert [s.source_id for s in plan.sources] == [s.source_id for s in initial.sources]
    final_costs = {d.source_id: d.after_tokens for d in plan.decisions}
    rendered = reconcile_plan(None, prepared, [], input_budget=500, model=state.config.model)
    assert plan.predicted_total == sum(final_costs.values()) == rendered.predicted_total
    assert len(list(store.root.glob("*.json"))) == 2
    assert prepared[-1] == raw[-1]
    assert raw == original
