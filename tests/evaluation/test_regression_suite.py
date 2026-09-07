"""N10 full-mode and fixed regression-gate coverage."""

from __future__ import annotations

from pathlib import Path

import pytest

from nanobot.evaluation.__main__ import load_suite
from nanobot.evaluation.models import MODES
from nanobot.evaluation.runner import EvalRunner

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.asyncio
async def test_all_modes_execute_capability_free_smoke(tmp_path: Path) -> None:
    case = load_suite(FIXTURES / "basic.json")[0]

    results = [
        await EvalRunner().run_case(case, mode=mode, output_root=tmp_path / mode)
        for mode in sorted(MODES)
    ]

    assert all(result.status == "passed" for result in results)
    assert len({Path(result.trace_ref).parent for result in results}) == len(MODES)


@pytest.mark.asyncio
async def test_explicit_memory_is_unavailable_in_baseline_and_real_in_full(
    tmp_path: Path,
) -> None:
    case = load_suite(FIXTURES / "explicit_memory.json")[0]

    baseline = await EvalRunner().run_case(
        case,
        mode="baseline",
        output_root=tmp_path / "baseline",
    )
    full = await EvalRunner().run_case(
        case,
        mode="full",
        output_root=tmp_path / "full",
    )

    assert baseline.status == "not_implemented"
    assert full.status == "passed"
    assert full.metrics["eligible_save_requests"] == 1
    assert full.metrics["missed_save_requests"] == 0
    assert full.metrics["unauthorized_commit_count"] == 0
    assert full.metrics["false_memory_confirmation_count"] == 0
    assert isinstance(full.metrics["memory_save_latency_ms"], float)


@pytest.mark.asyncio
async def test_memory_negatives_have_no_commits_or_false_confirmations(
    tmp_path: Path,
) -> None:
    cases = load_suite(FIXTURES / "memory_negatives.json")

    results = [
        await EvalRunner().run_case(case, mode="full", output_root=tmp_path)
        for case in cases
    ]

    assert len(results) == 3
    assert all(result.status == "passed" for result in results)
    assert all(result.metrics["eligible_save_requests"] == 0 for result in results)
    assert all(result.metrics["unauthorized_commit_count"] == 0 for result in results)
    assert all(result.metrics["false_memory_confirmation_count"] == 0 for result in results)


@pytest.mark.asyncio
@pytest.mark.parametrize("fixture", ["dream_conflict.json", "recovery.json"])
async def test_memory_coordination_regressions_pass_in_full(
    tmp_path: Path,
    fixture: str,
) -> None:
    case = load_suite(FIXTURES / fixture)[0]

    result = await EvalRunner().run_case(case, mode="full", output_root=tmp_path)

    assert result.status == "passed", result.metrics
    assert result.metrics["duplicate_memory_entry_count"] == 0
    if fixture == "dream_conflict.json":
        assert result.metrics["stale_dream_overwrite_count"] == 0
    else:
        assert result.metrics["restart_memory_visible"] is True


@pytest.mark.asyncio
async def test_existing_workspace_safety_gate_is_non_vacuous(tmp_path: Path) -> None:
    case = load_suite(FIXTURES / "existing_safety.json")[0]

    result = await EvalRunner().run_case(case, mode="full", output_root=tmp_path)

    assert result.status == "passed"
    assert result.metrics["safety_bypass_count"] == 0
    assert result.metrics["tool_steps"] == 1


@pytest.mark.asyncio
async def test_artifact_roundtrip_is_executable_in_context_mode(tmp_path: Path) -> None:
    cases = load_suite(FIXTURES / "tool_context.json")
    case = next(case for case in cases if case.id.endswith("reference-roundtrip"))

    result = await EvalRunner().run_case(
        case,
        mode="context-only",
        output_root=tmp_path,
    )

    assert result.status == "passed", result.metrics
    assert result.metrics["tool_steps"] > 1
    assert result.metrics["layer_triggers"]["L1"] > 0
    assert result.metrics["recovery_replays"] > 0


@pytest.mark.asyncio
async def test_l3_duplicate_case_detects_the_l3_ablation(tmp_path: Path) -> None:
    cases = load_suite(FIXTURES / "tool_context.json")
    case = next(case for case in cases if case.id == "context-l3-duplicate-read")

    full = await EvalRunner().run_case(case, mode="full", output_root=tmp_path / "full")
    ablated = await EvalRunner().run_case(
        case,
        mode="no-l3",
        output_root=tmp_path / "no-l3",
    )

    assert full.status == "passed", full.metrics
    assert full.metrics["layer_triggers"]["L3"] > 0
    assert ablated.status == "failed"
