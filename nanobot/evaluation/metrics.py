"""Pure usage accounting and comparisons; no runtime or filesystem access."""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Iterable
from typing import Annotated, Literal

from pydantic import Field, JsonValue

from nanobot.evaluation.models import EvalModel, EvalResult, EvalSuiteResult

_Count = Annotated[int, Field(ge=0, strict=True)]
_Purpose = Literal["worker", "summary", "dream"]
_PURPOSES: tuple[_Purpose, ...] = ("worker", "summary", "dream")
_METRICS = (
    "actual_input_tokens", "actual_output_tokens", "actual_cached_tokens",
    "estimated_input_tokens", "request_count", "tool_steps", "wall_seconds",
    "repeated_fact_read_count",
    "eligible_save_requests", "missed_save_requests",
    "unauthorized_commit_count", "false_memory_confirmation_count",
    "duplicate_memory_entry_count", "stale_dream_overwrite_count",
    "safety_bypass_count", "memory_save_latency_ms",
)
_COMPATIBILITY = (
    "case_hash", "config_hash", "model", "temperature", "render_version", "dependencies",
)
_LIMITS = (
    "Unknown values remain null; known totals and means describe observed samples only. "
    "Coverage excludes not_implemented tasks; errors and budget failures count as failures. "
    "Deltas are candidate minus baseline; lower cost alone is not improved correctness. "
    "Fixed cases do not establish general quality; mechanism-only scripts do not establish "
    "real-model semantic benefits. Preseeded memory does not establish automatic saving."
)


class EvalUsageRecord(EvalModel):
    request_id: str = Field(min_length=1)
    purpose: _Purpose
    input_tokens: _Count | None = None
    output_tokens: _Count | None = None
    cached_tokens: _Count | None = None


class UsageTotals(EvalModel):
    """known_total is partial input usage; unknown_request_count covers any missing field."""

    request_count: _Count = 0
    unknown_request_count: _Count = 0
    known_total: _Count = 0
    actual_input_tokens: _Count | None = None
    actual_output_tokens: _Count | None = None
    actual_cached_tokens: _Count | None = None
    by_purpose: dict[_Purpose, UsageTotals] = Field(default_factory=dict)


def evaluate_memory_save_attempt(
    *,
    expected_to_save: bool,
    committed: bool,
    claimed_saved: bool,
) -> dict[str, int]:
    """Score one labelled save decision without treating a claim as a commit."""
    return {
        "eligible_save_requests": int(expected_to_save),
        "missed_save_requests": int(expected_to_save and not committed),
        "unauthorized_commit_count": int(not expected_to_save and committed),
        "false_memory_confirmation_count": int(claimed_saved and not committed),
    }


def _usage_totals(records: list[EvalUsageRecord]) -> UsageTotals:
    def total(field: str) -> int | None:
        values: list[int | None] = [getattr(record, field) for record in records]
        if not values or any(value is None for value in values):
            return None
        return sum(value for value in values if value is not None)

    return UsageTotals(
        request_count=len(records),
        unknown_request_count=sum(
            any(value is None for value in (r.input_tokens, r.output_tokens, r.cached_tokens))
            for r in records
        ),
        known_total=sum(r.input_tokens for r in records if r.input_tokens is not None),
        actual_input_tokens=total("input_tokens"), actual_output_tokens=total("output_tokens"),
        actual_cached_tokens=total("cached_tokens"),
    )


def summarize_usage(records: Iterable[EvalUsageRecord]) -> UsageTotals:
    """Deduplicate observations of each underlying request before counting any purpose."""
    unique: dict[str, EvalUsageRecord] = {}
    for record in records:
        if record.request_id in unique and unique[record.request_id] != record:
            raise ValueError(f"conflicting usage for request_id {record.request_id}")
        unique[record.request_id] = record
    rows = list(unique.values())
    totals = _usage_totals(rows)
    totals.by_purpose = {
        purpose: _usage_totals([r for r in rows if r.purpose == purpose]) for purpose in _PURPOSES
    }
    return totals


def _number(value: JsonValue) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if isinstance(value, int) or math.isfinite(value) else None


def _delta(left: JsonValue, right: JsonValue) -> int | float | None:
    before, after = _number(left), _number(right)
    return after - before if before is not None and after is not None else None


def _statistics(values: list[JsonValue]) -> dict[str, JsonValue]:
    known = sorted(number for value in values if (number := _number(value)) is not None)
    count = len(known)
    # Nearest-rank percentiles, always accompanied by observed sample counts.
    return {
        "sample_count": count, "unknown_count": len(values) - count,
        "known_total": sum(known),
        "total": sum(known) if count and count == len(values) else None,
        "mean": sum(known) / count if count else None,
        "p50": known[math.ceil(count * 0.5) - 1] if count else None,
        "p95": known[math.ceil(count * 0.95) - 1] if count else None,
    }


def _aggregate(rows: list[EvalResult]) -> dict[str, JsonValue]:
    eligible = [row for row in rows if row.status != "not_implemented"]
    memory = [row for row in eligible if "repeated_fact_read_count" in row.metrics]
    unknown = sum(_number(row.metrics["repeated_fact_read_count"]) is None for row in memory)
    correct = sum(row.status == "passed" and _number(row.metrics["repeated_fact_read_count"]) == 0
                  for row in memory)
    counts = Counter(row.status for row in rows)
    metrics: dict[str, JsonValue] = {}
    for key in _METRICS:
        samples = memory if key == "repeated_fact_read_count" else eligible
        metrics[key] = _statistics([row.metrics.get(key) for row in samples])
    return {
        "case_count": len(rows), "eligible_count": len(eligible),
        "not_implemented_count": counts["not_implemented"],
        "invalid_scenario_count": sum(row.metrics.get("scenario_valid") is False for row in rows),
        "status_counts": {status: counts[status] for status in (
            "passed", "failed", "error", "budget_exceeded", "not_implemented",
        )},
        "coverage": len(eligible) / len(rows) if rows else None,
        "task_success_rate": counts["passed"] / len(eligible) if eligible else None,
        "memory_eligible_count": len(memory), "memory_unknown_count": unknown,
        "correct_without_fact_reread_rate": correct / len(memory) if memory and not unknown else None,
        "metrics": metrics,
    }


def _indexed(suite: EvalSuiteResult) -> dict[str, EvalResult]:
    rows = {row.case_id: row for row in suite.results}
    if len(rows) != len(suite.results):
        raise ValueError("duplicate case IDs in comparison suite")
    return rows


def compare_results(baseline: EvalSuiteResult, candidate: EvalSuiteResult) -> dict[str, JsonValue]:
    """Require paired fixture/configuration evidence, allowing code and mode changes."""
    before, after = _indexed(baseline), _indexed(candidate)
    if before.keys() != after.keys():
        raise ValueError("comparison requires the same case IDs")
    cases: list[JsonValue] = []
    for case_id in sorted(before):
        left, right = before[case_id], after[case_id]
        for key in _COMPATIBILITY:
            if left.provenance.get(key) is None or right.provenance.get(key) is None:
                raise ValueError(f"case {case_id}: missing {key} compatibility evidence")
            if left.provenance[key] != right.provenance[key]:
                raise ValueError(f"case {case_id}: incompatible {key}")
        for key in ("seed", "max_output_tokens", "python_version", "platform"):
            if left.provenance.get(key) != right.provenance.get(key):
                raise ValueError(f"case {case_id}: incompatible {key}")
        keys = set(_METRICS) | {
            key for row in (left, right) for key, value in row.metrics.items()
            if _number(value) is not None
        }
        cases.append({
            "case_id": case_id, "baseline_status": left.status, "candidate_status": right.status,
            "baseline_mode": left.mode, "candidate_mode": right.mode,
            "metric_deltas": {key: _delta(left.metrics.get(key), right.metrics.get(key))
                              for key in sorted(keys)},
        })
    baseline_stats, candidate_stats = _aggregate(baseline.results), _aggregate(candidate.results)
    return {
        "schema_version": 1, "cases": cases,
        "baseline": baseline_stats, "candidate": candidate_stats,
        "mechanism_only": bool(cases) and all(
            row.provenance.get("mechanism_only") is True
            for row in [*baseline.results, *candidate.results]
        ),
        "limits": _LIMITS,
    }


def render_comparison_markdown(report: dict[str, JsonValue]) -> str:
    """Render a comparison in memory; unknown values are explicitly labelled."""
    def cell(value: JsonValue) -> str:
        text = "unknown" if value is None else str(value)
        return text.replace("\\", "\\\\").replace("|", "\\|").replace("\r", " ").replace("\n", " ")

    lines = ["| Metric | Baseline | Candidate |", "| --- | --- | --- |"]
    left, right = report.get("baseline"), report.get("candidate")
    if isinstance(left, dict) and isinstance(right, dict):
        for key in ("case_count", "eligible_count", "not_implemented_count", "coverage",
                    "task_success_rate", "correct_without_fact_reread_rate"):
            lines.append(f"| {key} | {cell(left.get(key))} | {cell(right.get(key))} |")
    lines += ["", "| Case | Baseline status | Candidate status | Metric deltas |",
              "| --- | --- | --- | --- |"]
    cases = report.get("cases")
    if isinstance(cases, list):
        for row in cases:
            if not isinstance(row, dict):
                continue
            deltas = row.get("metric_deltas")
            text = "; ".join(f"{key}: {cell(value)}" for key, value in deltas.items()) if isinstance(deltas, dict) else "unknown"
            lines.append(f"| {cell(row.get('case_id'))} | {cell(row.get('baseline_status'))} | {cell(row.get('candidate_status'))} | {text} |")
    lines += ["", f"Mechanism only: {cell(report.get('mechanism_only'))}.", "",
              str(report.get("limits", _LIMITS))]
    return "\n".join(lines) + "\n"
