"""Run isolated offline or explicitly bounded live evaluations."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from pathlib import Path

from nanobot.evaluation.metrics import compare_results, render_comparison_markdown
from nanobot.evaluation.models import (
    MODES,
    EvalBudget,
    EvalCase,
    EvalExecutionConfig,
    EvalResult,
    EvalSuite,
    EvalSuiteResult,
)
from nanobot.evaluation.scenarios import (
    ScenarioSuite,
    build_context_cases,
    build_dream_conflict_cases,
    build_existing_safety_cases,
    build_explicit_memory_cases,
    build_memory_cases,
    build_memory_negative_cases,
    build_recovery_cases,
    build_tool_context_cases,
)
from nanobot.providers.factory import ProviderSnapshot


def load_suite(path: Path) -> list[EvalCase]:
    files = sorted(path.glob("*.json")) if path.is_dir() else [path]
    cases: list[EvalCase] = []
    for file in files:
        payload = json.loads(file.read_text(encoding="utf-8"))
        if isinstance(payload, dict) and "generator" in payload:
            suite = ScenarioSuite.model_validate(payload)
            if suite.generator == "tool_context":
                if suite.parameters:
                    raise ValueError("tool context controls have no configurable parameters")
                cases.extend(build_tool_context_cases())
            else:
                builders = {
                    "context_pressure": build_context_cases,
                    "memory_dependency": build_memory_cases,
                    "explicit_memory": build_explicit_memory_cases,
                    "memory_negatives": build_memory_negative_cases,
                    "dream_conflict": build_dream_conflict_cases,
                    "recovery": build_recovery_cases,
                    "existing_safety": build_existing_safety_cases,
                }
                build = builders[suite.generator]
                cases.extend(build(suite.parameters))
        else:
            cases.extend(EvalSuite.model_validate(payload).cases)
    if not cases or len({case.id for case in cases}) != len(cases):
        raise ValueError("suite must contain unique, nonempty cases")
    return cases


async def run_suite(
    cases: list[EvalCase],
    *,
    mode: str,
    output_root: Path,
    execution: EvalExecutionConfig | None = None,
    provider_snapshot: ProviderSnapshot | None = None,
) -> EvalSuiteResult:
    from nanobot.evaluation.runner import EvalRunner

    output_root.mkdir(parents=True, exist_ok=True)
    suite_root = Path(tempfile.mkdtemp(prefix="suite-", dir=output_root.resolve()))
    runner = EvalRunner(execution=execution, provider_snapshot=provider_snapshot)
    results = [await runner.run_case(case, mode=mode, output_root=suite_root) for case in cases]
    payload = EvalSuiteResult(results=results)
    (suite_root / "suite-results.json").write_text(payload.model_dump_json(indent=2), encoding="utf-8")
    return payload


def prepare_live_execution(
    *,
    config_path: Path | None,
    max_requests: int | None,
    max_tokens: int | None,
    max_wall_seconds: float | None,
) -> tuple[EvalExecutionConfig, ProviderSnapshot]:
    """Resolve one explicit test provider and a hard suite-wide budget."""
    if config_path is None:
        raise ValueError("live evaluation requires --config")
    if not config_path.is_file():
        raise ValueError("live evaluation config must be an existing file")
    if (
        max_requests is None
        or max_requests <= 0
        or max_tokens is None
        or max_tokens <= 0
        or max_wall_seconds is None
        or max_wall_seconds <= 0
    ):
        raise ValueError("live evaluation requires positive request/token/time limits")

    from nanobot.config.loader import load_config, resolve_config_env_vars
    from nanobot.providers.factory import build_provider_snapshot

    resolved_path = config_path.resolve()
    config = resolve_config_env_vars(
        load_config(resolved_path),
        config_path=resolved_path,
    )
    if config.agents.defaults.fallback_models:
        raise ValueError("live evaluation does not allow fallback models")
    budget = EvalBudget(
        max_requests=max_requests,
        max_steps=2_147_483_647,
        max_tokens=max_tokens,
        max_wall_seconds=max_wall_seconds,
    )
    return (
        EvalExecutionConfig(
            kind="live",
            config_path=resolved_path,
            suite_budget=budget,
        ),
        build_provider_snapshot(config),
    )


def load_results(path: Path) -> EvalSuiteResult:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict) and "case_id" in data:
        return EvalSuiteResult(results=[EvalResult.model_validate(data)])
    return EvalSuiteResult.model_validate(data)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="run isolated offline or live cases")
    run.add_argument("--suite", type=Path, required=True)
    run.add_argument("--mode", choices=sorted(MODES), default="baseline")
    run.add_argument("--output-root", type=Path, required=True)
    run.add_argument("--live", action="store_true")
    run.add_argument("--config", type=Path)
    run.add_argument("--max-requests", type=int)
    run.add_argument("--max-tokens", type=int)
    run.add_argument("--max-wall-seconds", type=float)
    compare = commands.add_parser("compare", help="compare compatible case results")
    compare.add_argument("--baseline", type=Path, required=True)
    compare.add_argument("--candidate", type=Path, required=True)
    compare.add_argument("--format", choices=["json", "markdown"], default="json")
    args = parser.parse_args(argv)
    if args.command == "compare":
        try:
            report = compare_results(load_results(args.baseline), load_results(args.candidate))
        except (OSError, ValueError) as exc:
            print(f"comparison refused: {type(exc).__name__}", file=sys.stderr)
            return 2
        print(json.dumps(report, ensure_ascii=False) if args.format == "json" else render_comparison_markdown(report))
        return 0
    live_values = (args.config, args.max_requests, args.max_tokens, args.max_wall_seconds)
    if not args.live and any(value is not None for value in live_values):
        print("offline budgets belong in the fixture; live-only options cannot select a provider", file=sys.stderr)
        return 2
    try:
        execution: EvalExecutionConfig | None = None
        provider_snapshot: ProviderSnapshot | None = None
        if args.live:
            execution, provider_snapshot = prepare_live_execution(
                config_path=args.config,
                max_requests=args.max_requests,
                max_tokens=args.max_tokens,
                max_wall_seconds=args.max_wall_seconds,
            )
        cases = load_suite(args.suite)
        payload = asyncio.run(run_suite(
            cases,
            mode=args.mode,
            output_root=args.output_root,
            execution=execution,
            provider_snapshot=provider_snapshot,
        ))
    except (OSError, ValueError, RuntimeError) as exc:
        label = "live evaluation failed" if args.live else "evaluation failed"
        print(f"{label}: {type(exc).__name__}", file=sys.stderr)
        return 2
    print(payload.model_dump_json())
    return 0 if all(result.status == "passed" for result in payload.results) else 1


if __name__ == "__main__":
    # This dedicated CLI exports the allowlisted trace/result only. Do not let
    # the application runtime's normal conversation logs leak fixture bodies.
    from loguru import logger

    logger.disable("nanobot")
    raise SystemExit(main())
