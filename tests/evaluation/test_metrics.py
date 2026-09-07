"""Usage accounting and comparable, non-vacuous evaluation reports."""

import pytest
from pydantic import ValidationError

from nanobot.evaluation.metrics import (
    EvalUsageRecord,
    compare_results,
    evaluate_memory_save_attempt,
    render_comparison_markdown,
    summarize_usage,
)
from nanobot.evaluation.models import EvalResult, EvalSuiteResult


def usage(request_id="r1", **changes):
    return EvalUsageRecord(**{
        "request_id": request_id, "purpose": "worker", "input_tokens": 10,
        "output_tokens": 3, "cached_tokens": 2, **changes,
    })


def result(case_id="one", status="passed", metrics=None, **origin):
    return EvalResult(
        case_id=case_id, mode="baseline", status=status, verdicts=[],
        metrics=metrics or {}, trace_ref="trace.json", provenance={
            "case_hash": case_id, "config_hash": "config", "model": "scripted",
            "temperature": 0, "render_version": "v1", "dependencies": {"pydantic": "2"},
            "mechanism_only": True, **origin,
        },
    )


def compare(left, right):
    return compare_results(EvalSuiteResult(results=left), EvalSuiteResult(results=right))


def test_deduplicates_requests_and_keeps_all_purposes():
    record = usage()
    totals = summarize_usage([
        record, record.model_copy(), usage("r2", purpose="summary"),
        usage("r3", purpose="dream", input_tokens=5),
    ])
    assert totals.request_count == 3
    assert totals.unknown_request_count == 0
    assert totals.known_total == totals.actual_input_tokens == 25
    assert totals.actual_output_tokens == 9
    assert totals.actual_cached_tokens == 6
    assert totals.by_purpose["summary"].request_count == 1
    assert totals.by_purpose["dream"].actual_input_tokens == 5


@pytest.mark.parametrize("changes", [{"input_tokens": 11}, {"purpose": "dream"}])
def test_conflicting_request_ids_raise(changes):
    with pytest.raises(ValueError, match="r1"):
        summarize_usage([usage(), usage(**changes)])


@pytest.mark.parametrize("field", ["input_tokens", "output_tokens", "cached_tokens"])
@pytest.mark.parametrize("value", [-1, True, 1.5, "1"])
def test_token_counts_are_strict_nonnegative_integers(field, value):
    with pytest.raises(ValidationError):
        usage(**{field: value})


@pytest.mark.parametrize("changes", [{"request_id": ""}, {"purpose": "other"}])
def test_rejects_invalid_request_identity(changes):
    with pytest.raises(ValidationError):
        usage(**changes)


def test_unknown_counts_do_not_erase_known_partial_usage():
    totals = summarize_usage([usage(), usage("r2", input_tokens=None, output_tokens=None)])
    assert totals.request_count == 2
    assert totals.unknown_request_count == 1
    assert totals.known_total == 10
    assert totals.actual_input_tokens is None
    assert totals.actual_output_tokens is None
    assert totals.actual_cached_tokens == 4


def test_unknown_cached_usage_does_not_poison_known_input():
    totals = summarize_usage([usage(cached_tokens=None)])
    assert totals.actual_input_tokens == 10
    assert totals.actual_cached_tokens is None
    assert totals.unknown_request_count == 1


def test_no_requests_is_unknown_not_zero():
    totals = summarize_usage([])
    assert totals.request_count == totals.unknown_request_count == totals.known_total == 0
    assert totals.actual_input_tokens is None
    assert totals.actual_output_tokens is None
    assert totals.actual_cached_tokens is None


def test_claim_without_tool_commit_is_not_success():
    row = evaluate_memory_save_attempt(
        expected_to_save=True,
        committed=False,
        claimed_saved=True,
    )

    assert row["eligible_save_requests"] == 1
    assert row["missed_save_requests"] == 1
    assert row["false_memory_confirmation_count"] == 1
    assert row["unauthorized_commit_count"] == 0


def test_unrequested_commit_is_counted_separately():
    row = evaluate_memory_save_attempt(
        expected_to_save=False,
        committed=True,
        claimed_saved=False,
    )

    assert row["eligible_save_requests"] == 0
    assert row["missed_save_requests"] == 0
    assert row["unauthorized_commit_count"] == 1
    assert row["false_memory_confirmation_count"] == 0


def test_failures_and_missing_capabilities_have_distinct_denominators():
    rows = [result(str(i), status) for i, status in enumerate([
        "passed", "failed", "error", "budget_exceeded", "not_implemented",
    ])]
    report = compare(rows, rows)
    stats = report["baseline"]
    assert stats["task_success_rate"] == 0.25
    assert stats["eligible_count"] == 4
    assert stats["not_implemented_count"] == 1
    assert stats["coverage"] == 0.8
    assert stats["status_counts"]["error"] == 1
    assert report["mechanism_only"] is True


@pytest.mark.parametrize("rows", [[], [result(status="not_implemented")]])
def test_no_executable_samples_never_reports_success(rows):
    report = compare(rows, rows)
    assert report["baseline"]["task_success_rate"] is None
    assert report["baseline"]["eligible_count"] == 0
    assert report["baseline"]["metrics"]["wall_seconds"]["mean"] is None
    assert report["baseline"]["correct_without_fact_reread_rate"] is None
    if not rows:
        assert report["mechanism_only"] is False
        assert report["baseline"]["coverage"] is None


def test_numeric_deltas_unknowns_and_normalized_statistics():
    left = [result(metrics={"actual_input_tokens": 10, "wall_seconds": 2,
                            "flag": True, "description": "before"}), result("two")]
    right = [result(metrics={"actual_input_tokens": 6, "wall_seconds": 1,
                             "flag": False, "description": "after"}), result("two")]
    report = compare(left, right)
    deltas = report["cases"][0]["metric_deltas"]
    assert deltas["actual_input_tokens"] == -4
    assert deltas["wall_seconds"] == -1
    assert "flag" not in deltas and "description" not in deltas
    assert report["cases"][1]["metric_deltas"]["actual_input_tokens"] is None
    stats = report["baseline"]["metrics"]["actual_input_tokens"]
    assert stats["sample_count"] == stats["unknown_count"] == 1
    assert stats["known_total"] == stats["mean"] == 10
    assert stats["total"] is None


def test_memory_rate_requires_correctness_and_known_zero_rereads():
    rows = [result("a", metrics={"repeated_fact_read_count": 0}),
            result("b", "failed", {"repeated_fact_read_count": 0}),
            result("c", metrics={"repeated_fact_read_count": 2}), result("basic")]
    stats = compare(rows, rows)["baseline"]
    assert stats["memory_eligible_count"] == 3
    assert stats["correct_without_fact_reread_rate"] == pytest.approx(1 / 3)
    rows.append(result("unknown", metrics={"repeated_fact_read_count": None}))
    stats = compare(rows, rows)["baseline"]
    assert stats["memory_unknown_count"] == 1
    assert stats["correct_without_fact_reread_rate"] is None


@pytest.mark.parametrize("field,value", [
    ("case_hash", "different"), ("config_hash", "different"), ("model", "other"),
    ("temperature", 0.7), ("render_version", "v2"), ("dependencies", {"pydantic": "3"}),
])
def test_incompatible_provenance_is_rejected(field, value):
    with pytest.raises(ValueError, match=field):
        compare([result()], [result(**{field: value})])


def test_missing_compatibility_evidence_is_rejected():
    row = result()
    del row.provenance["case_hash"]
    with pytest.raises(ValueError, match="case_hash"):
        compare([row], [row])


def test_case_ids_must_be_unique_and_match():
    for left, right in [([result(), result()], [result()]),
                        ([result()], [result(), result()]), ([result()], [result("other")])]:
        with pytest.raises(ValueError, match="case"):
            compare(left, right)


def test_modes_and_code_revisions_can_differ_and_order_is_stable():
    candidate = result(git_commit="new", dirty_diff_hash="new", mechanism_only=False)
    candidate.mode = "full"
    report = compare([result("two"), result(git_commit="old", dirty_diff_hash="old")],
                     [candidate, result("two")])
    assert [row["case_id"] for row in report["cases"]] == ["one", "two"]
    assert report["cases"][0]["candidate_status"] == "passed"
    assert report["mechanism_only"] is False


def test_markdown_reports_statuses_unknowns_and_limits():
    report = compare([result("a|b", "error")], [result("a|b", "passed")])
    markdown = render_comparison_markdown(report)
    assert "a\\|b" in markdown
    assert "error" in markdown and "passed" in markdown
    assert "unknown" in markdown.lower()
    assert "mechanism" in markdown.lower()
    assert "coverage" in markdown.lower()
