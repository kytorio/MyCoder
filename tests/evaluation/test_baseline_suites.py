"""Task scores come from the current runtime, even when baseline behavior fails."""

import json
from pathlib import Path

import pytest

from nanobot.evaluation.runner import EvalRunner
from nanobot.evaluation.scenarios import build_context_cases, build_memory_cases


@pytest.mark.parametrize("history_count,ratio", [(4, 0.5), (12, 0.9), (24, 1.3)])
async def test_context_probe_reaches_measured_pressure(tmp_path, history_count, ratio):
    case = build_context_cases({"history_counts": [history_count], "memory_counts": [2],
                                "request_styles": ["short"]})[0]
    result = await EvalRunner().run_case(case, mode="baseline", output_root=tmp_path)
    assert result.status in {"passed", "failed", "error"}
    assert result.metrics["bootstrap_completed"] is True
    assert result.metrics["candidate_input_tokens"] / 6656 == pytest.approx(ratio, abs=0.08)
    assert result.metrics["input_budget"] == 6656
    assert result.metrics["probe_request_count"] > 0 or result.status == "error"
    assert result.provenance["setup_kind"] == "accepted_history"
    if ratio == 0.5:
        assert result.status == "passed"
    if ratio == 1.3:
        assert result.metrics["compression_observed"] or result.status == "error"


async def test_enforced_context_mode_runs_structured_l4_on_real_runtime(tmp_path):
    case = build_context_cases({
        "history_counts": [12],
        "memory_counts": [2],
        "request_styles": ["short"],
    })[0]

    result = await EvalRunner().run_case(case, mode="no-l2", output_root=tmp_path)

    assert result.status == "passed"
    assert result.metrics["compression_observed"] is True
    assert result.metrics["summary_request_count"] >= 1
    assert result.metrics["context_manifest_count"] >= 1


async def test_enforced_context_mode_recovers_l2_history_artifacts(tmp_path):
    case = build_context_cases({
        "history_counts": [24],
        "memory_counts": [2],
        "request_styles": ["short"],
    })[0]

    result = await EvalRunner().run_case(case, mode="context-only", output_root=tmp_path)

    assert result.status == "passed"
    assert result.metrics["compression_observed"] is True
    assert result.metrics["constraint_retention"] == 1
    assert result.metrics["tool_steps"] > 2


async def test_memory_new_session_controls_reread_and_correctness(tmp_path):
    cases = build_memory_cases({"instances_per_category": 1})[:3]
    results = [await EvalRunner().run_case(c, mode="baseline", output_root=tmp_path) for c in cases]
    assert all(r.status == "passed" for r in results)
    assert [r.metrics["repeated_fact_read_count"] for r in results] == [0, 1, 1]
    assert len({str(Path(r.trace_ref).parent) for r in results}) == 3
    for result in results:
        trace = json.loads(Path(result.trace_ref).read_text(encoding="utf-8"))
        assert trace["scenario"]["bootstrap_session"] != trace["scenario"]["probe_session"]
        assert trace["scenario"]["probe_initial_history_count"] == 0
        assert result.provenance["setup_kind"] == "preseeded"
        assert result.metrics["actual_input_tokens"] is None


async def test_missing_fact_cannot_be_recovered_from_hidden_verifier(tmp_path):
    case = build_context_cases({"history_counts": [4], "memory_counts": [2],
                                "request_styles": ["short"], "drop_key_fact": True})[0]
    result = await EvalRunner().run_case(case, mode="baseline", output_root=tmp_path)
    assert result.status == "failed"


async def test_unavailable_source_makes_memory_off_fail_not_guess(tmp_path):
    cases = build_memory_cases({"instances_per_category": 1, "source_available": False})[:3]
    results = [await EvalRunner().run_case(c, mode="baseline", output_root=tmp_path) for c in cases]
    assert [r.status for r in results] == ["passed", "failed", "failed"]
    assert all(r.metrics["repeated_fact_read_count"] == 0 for r in results)


async def test_memory_save_capability_is_not_preseeded_success(tmp_path):
    case = build_memory_cases({"instances_per_category": 1})[0]
    case.requires_capabilities = ["memory_save"]
    case.setup_kind = "tool_write"
    result = await EvalRunner().run_case(case, mode="baseline", output_root=tmp_path)
    assert result.status == "not_implemented"
    assert result.metrics["request_count"] == 0


async def test_memory_save_capability_uses_real_tool_before_fresh_session(tmp_path):
    case = build_memory_cases({"instances_per_category": 1})[0]
    case.requires_capabilities = ["memory_save"]
    case.setup_kind = "tool_write"
    case.allowed_tools.append("memory_save")

    result = await EvalRunner().run_case(
        case,
        mode="memory-only",
        output_root=tmp_path,
    )

    assert result.status == "passed"
    assert result.metrics["repeated_fact_read_count"] == 0
    trace = json.loads(Path(result.trace_ref).read_text(encoding="utf-8"))
    assert trace["scenario"]["bootstrap_session"] != trace["scenario"]["probe_session"]
    assert any(
        event["name"] == "memory_save" and event["status"] == "completed"
        for event in trace["tool_events"]
    )


@pytest.mark.parametrize("category", ["edit_dependency", "history_reference"])
async def test_memory_tasks_verify_edits_and_historical_evidence(tmp_path, category):
    cases = [c for c in build_memory_cases({"instances_per_category": 1}) if c.parameters["category"] == category]
    results = [await EvalRunner().run_case(c, mode="baseline", output_root=tmp_path) for c in cases]
    assert all(r.status == "passed" for r in results)
    assert [r.metrics["repeated_fact_read_count"] for r in results] == [0, 1, 1]


async def test_changed_source_read_is_not_redundant(tmp_path):
    case = build_memory_cases({"instances_per_category": 1, "source_changed": True})[1]
    result = await EvalRunner().run_case(case, mode="baseline", output_root=tmp_path)
    assert result.status == "failed"
    assert result.metrics["repeated_fact_read_count"] == 0
    assert result.metrics["correct_without_fact_reread"] is False


@pytest.mark.parametrize("control", ["stale", "contradictory"])
async def test_wrong_memory_is_failure_even_without_reread(tmp_path, control):
    case = build_memory_cases({"instances_per_category": 1})[0]
    original = case.workspace_files["memory/MEMORY.md"]
    wrong = original.replace("eu-test-2", "wrong-zone")
    case.workspace_files["memory/MEMORY.md"] = wrong if control == "stale" else original + wrong
    result = await EvalRunner().run_case(case, mode="baseline", output_root=tmp_path)
    assert result.status == "failed"
    assert result.metrics["repeated_fact_read_count"] == 0
    assert result.metrics["correct_without_fact_reread"] is False


async def test_irreducible_current_request_cannot_be_marked_compression_success(tmp_path):
    case = build_context_cases({"history_counts": [4], "memory_counts": [2],
                                "request_styles": ["short"], "irreducible": True})[0]
    result = await EvalRunner().run_case(case, mode="baseline", output_root=tmp_path)
    assert result.metrics["bootstrap_completed"] is True
    assert result.status in {"error", "budget_exceeded"}


async def test_archive_failure_is_observed_on_actual_summary_call(tmp_path):
    case = build_context_cases({"history_counts": [24], "memory_counts": [2],
                                "request_styles": ["long"], "archive_error": True})[0]
    case.parameters["pressure_ratio"] = 1.02
    result = await EvalRunner().run_case(case, mode="baseline", output_root=tmp_path)
    trace = json.loads(Path(result.trace_ref).read_text(encoding="utf-8"))
    summaries = {r["request_id"] for r in trace["requests"] if r["purpose"] == "summary"}
    assert summaries
    assert any(c["request_id"] in summaries and c["finish_reason"] == "error" for c in trace["calls"])
    assert result.metrics["actual_input_tokens"] is None


async def test_cli_case_rejects_missed_pressure_target(tmp_path, monkeypatch):
    from nanobot.evaluation import runner

    case = build_context_cases({"history_counts": [24], "memory_counts": [2], "request_styles": ["short"]})[0]
    monkeypatch.setattr(runner, "calibrate_history", lambda case, *args: (
        [m["content"] for m in case.initial_messages if m["role"] == "user"], {}))
    result = await EvalRunner().run_case(case, mode="baseline", output_root=tmp_path)
    assert result.status == "error"
    assert result.metrics["scenario_valid"] is False
    assert result.metrics["failure_reason"] == "invalid_pressure_sample"


@pytest.mark.parametrize("path", ["rogue.txt", "memory/MEMORY.md", "facts.txt"])
async def test_probe_rejects_unrelated_workspace_writes(tmp_path, monkeypatch, path):
    from nanobot.evaluation.providers import PromptSensitiveProvider, ScriptResponse, ScriptToolCall

    original = PromptSensitiveProvider.next_response
    attacked = set()

    def malicious(provider, messages):
        response = original(provider, messages)
        if provider.phase == "probe" and id(provider) not in attacked:
            attacked.add(id(provider))
            return ScriptResponse(tool_calls=[
                ScriptToolCall(id="read-attack-target", name="read_file", arguments={"path": path}),
                ScriptToolCall(id="attack", name="write_file", arguments={"path": path, "content": "tampered"}),
            ])
        return response

    monkeypatch.setattr(PromptSensitiveProvider, "next_response", malicious)
    case = build_memory_cases({"instances_per_category": 1})[0]
    result = await EvalRunner().run_case(case, mode="baseline", output_root=tmp_path)
    assert (Path(result.trace_ref).parent / "workspace" / path).read_text(encoding="utf-8") == "tampered"
    assert result.status == "failed"
    assert any(v.name == "workspace_changes" and v.status == "failed" for v in result.verdicts)
