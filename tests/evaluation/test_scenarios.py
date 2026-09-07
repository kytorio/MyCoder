"""Generated fixtures and synthetic model must not bypass task evidence."""

from copy import deepcopy

import pytest

from nanobot.evaluation.models import canonical_hash
from nanobot.evaluation.scenarios import build_context_cases, build_memory_cases


def test_pico_style_matrix_is_fixed_and_reproducible():
    params = {"history_counts": [4, 12, 24], "memory_counts": [2, 10],
              "request_styles": ["short", "long"], "seed": 7}
    cases = build_context_cases(params)
    assert len(cases) == 12
    assert canonical_hash([c.model_dump(mode="json") for c in cases]) == canonical_hash(
        [c.model_dump(mode="json") for c in build_context_cases(params)])
    assert {c.parameters["pressure_ratio"] for c in cases} == {0.5, 0.9, 1.3}
    assert all(len(c.initial_messages) == c.parameters["history_count"] for c in cases)
    assert all(c.setup_kind == "accepted_history" for c in cases)


def test_memory_variants_are_independent_fixtures():
    cases = build_memory_cases({"seed": 7, "instances_per_category": 4})
    assert len(cases) == 36
    assert {c.memory_variant for c in cases} == {"memory_on", "memory_off", "memory_irrelevant"}
    assert {c.parameters["category"] for c in cases} == {"fact_lookup", "edit_dependency", "history_reference"}
    assert all(c.setup_kind == "preseeded" and not c.initial_messages for c in cases)
    first = deepcopy(cases[0].workspace_files)
    cases[1].workspace_files["facts.txt"] = "mutated"
    assert cases[0].workspace_files == first


@pytest.mark.parametrize("parameters", [{"history_counts": [3]}, {"seed": "bad"}, {"extra": 4}])
def test_invalid_matrix_parameters_are_rejected(parameters):
    with pytest.raises(ValueError):
        build_context_cases(parameters)


def test_final_context_request_does_not_leak_ground_truth():
    for case in build_context_cases({}):
        assert "eu-test-2" not in case.user_inputs[-1]
        assert "deployment_region" not in case.user_inputs[-1]
        assert "3" in case.user_inputs[-1]  # authorized latest correction, not the older answer
        assert "eu-test-2" not in str(case.workspace_files)


async def test_large_first_read_and_missing_reference_capability_are_explicit(tmp_path):
    import json
    from pathlib import Path

    from nanobot.evaluation.runner import EvalRunner
    from nanobot.evaluation.scenarios import build_tool_context_cases

    cases = build_tool_context_cases()
    assert {
        case.parameters["probe_kind"]
        for case in cases
        if "probe_kind" in case.parameters
    } == {"oversized_first_read", "reference_roundtrip"}
    assert len(cases) == 3
    assert len(cases[0].workspace_files["large.txt"]) > 100_000
    results = [await EvalRunner().run_case(c, mode="baseline", output_root=tmp_path) for c in cases]
    assert results[0].metrics["tool_steps"] >= 1
    assert results[0].status == "passed"
    trace = json.loads(Path(results[0].trace_ref).read_text(encoding="utf-8"))
    assert trace["requests"][1]["estimated_input_tokens"] - trace["requests"][0]["estimated_input_tokens"] > 2048
    assert results[1].status == "not_implemented"
