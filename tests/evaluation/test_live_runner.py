"""The live runner reuses real scenarios while delegating to a non-network fake."""

import json
from pathlib import Path

import pytest

from nanobot.evaluation.models import EvalBudget, EvalCase, EvalExecutionConfig
from nanobot.evaluation.providers import PromptSensitiveProvider, RunBudget
from nanobot.evaluation.runner import EvalRunner
from nanobot.evaluation.scenarios import build_context_cases
from nanobot.providers.base import GenerationSettings, LLMProvider, LLMResponse, LLMUsage
from nanobot.providers.factory import ProviderSnapshot


class ScenarioLiveProvider(LLMProvider):
    def __init__(self) -> None:
        super().__init__(api_key="PRIVATE-LIVE-RUNNER-KEY", provider_name="fake-scenario")
        self.generation = GenerationSettings(temperature=0, max_tokens=512)
        self.driver = PromptSensitiveProvider()
        self.driver.budget = RunBudget(EvalBudget(
            max_requests=100,
            max_steps=1000,
            max_wall_seconds=60,
            max_tokens=2_000_000,
        ))
        self.calls = 0

    def get_default_model(self) -> str:
        return "fake/scenario"

    async def chat(self, **kwargs) -> LLMResponse:
        self.calls += 1
        response = await self.driver.chat(**kwargs)
        response.usage = LLMUsage.reported(
            input_tokens=17,
            output_tokens=3,
            cache_read_tokens=0,
        )
        return response


def live_runner(tmp_path: Path) -> tuple[EvalRunner, ScenarioLiveProvider]:
    config_path = tmp_path / "explicit-live-config.json"
    config_path.write_text('{"secret":"PRIVATE-LIVE-RUNNER-CONFIG"}', encoding="utf-8")
    provider = ScenarioLiveProvider()
    snapshot = ProviderSnapshot(
        provider=provider,
        model=provider.get_default_model(),
        context_window_tokens=131_072,
        signature=("fake-scenario",),
        generation=provider.generation,
    )
    execution = EvalExecutionConfig(
        kind="live",
        config_path=config_path,
        suite_budget=EvalBudget(
            max_requests=100,
            max_steps=1000,
            max_wall_seconds=60,
            max_tokens=2_000_000,
        ),
    )
    return EvalRunner(execution=execution, provider_snapshot=snapshot), provider


async def test_live_runner_records_real_usage_and_sanitized_provenance(tmp_path: Path):
    runner, provider = live_runner(tmp_path)
    case = build_context_cases({
        "history_counts": [24],
        "memory_counts": [2],
        "request_styles": ["short"],
    })[0]

    result = await runner.run_case(case, mode="full", output_root=tmp_path.parent / "live-r")

    assert result.status == "passed"
    assert provider.calls == result.metrics["request_count"]
    assert result.metrics["actual_input_tokens"] == provider.calls * 17
    assert result.metrics["bootstrap_request_count"] == 0
    assert result.metrics["probe_request_count"] == provider.calls
    assert result.metrics["summary_request_count"] >= 1
    assert result.provenance["execution_kind"] == "live"
    assert result.provenance["mechanism_only"] is False
    assert result.provenance["provider"] == "fake-scenario"
    assert result.provenance["model"] == "fake/scenario"
    assert len(str(result.provenance["source_config_hash"])) == 64
    exported = Path(result.trace_ref).read_text(encoding="utf-8")
    exported += Path(result.trace_ref).with_name("result.json").read_text(encoding="utf-8")
    assert "PRIVATE-LIVE-RUNNER" not in exported
    trace = json.loads(Path(result.trace_ref).read_text(encoding="utf-8"))
    assert len(trace["requests"]) == provider.calls
    assert trace["scenario"]["bootstrap_strategy"] == "ingested_calibrated_history"
    assert all(row["phase"] == "probe" for row in trace["requests"])
    assert all(row["input_tokens"] == 17 for row in trace["requests"])


async def test_live_runner_reaches_low_pressure_probe_without_live_bootstrap(tmp_path: Path):
    runner, provider = live_runner(tmp_path)
    case = build_context_cases({
        "history_counts": [2],
        "memory_counts": [2],
        "request_styles": ["short"],
    })[0]

    result = await runner.run_case(
        case,
        mode="baseline",
        output_root=tmp_path.parent / "live-low-pressure",
    )

    assert result.status == "passed"
    assert result.metrics["scenario_valid"] is True
    assert result.metrics["bootstrap_request_count"] == 0
    assert result.metrics["probe_request_count"] == provider.calls
    assert result.metrics["summary_request_count"] == 0


async def test_live_runner_refuses_scripted_basic_cases_before_dispatch(tmp_path: Path):
    runner, provider = live_runner(tmp_path)
    case = EvalCase.model_validate({
        "schema_version": 1,
        "id": "scripted-only",
        "description": "scripted response contract",
        "user_inputs": ["reply pong"],
        "scripted_responses": [{"content": "pong"}],
        "allowed_tools": [],
        "verifiers": [{"name": "answer_contains", "expected": "pong"}],
    })

    with pytest.raises(ValueError, match="not eligible"):
        await runner.run_case(case, mode="baseline", output_root=tmp_path / "results")

    assert provider.calls == 0
