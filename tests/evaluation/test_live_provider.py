"""Live evaluation uses a bounded recording adapter without making network calls."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from nanobot.evaluation.__main__ import prepare_live_execution
from nanobot.evaluation.models import EvalBudget, EvalExecutionConfig
from nanobot.evaluation.providers import (
    CompositeRunBudget,
    EvalBudgetExceededError,
    LiveEvaluationProvider,
    RunBudget,
)
from nanobot.providers.base import GenerationSettings, LLMProvider, LLMResponse, LLMUsage
from nanobot.providers.factory import ProviderSnapshot


class FakeLiveProvider(LLMProvider):
    def __init__(self) -> None:
        super().__init__(api_key="PRIVATE-LIVE-KEY", provider_name="fake-live")
        self.generation = GenerationSettings(temperature=0.2, max_tokens=64)
        self.calls = 0

    def get_default_model(self) -> str:
        return "fake/model"

    def estimate_prompt_tokens(self, messages, tools=None, model=None):
        return 11, "fake-estimate"

    async def chat(self, **kwargs) -> LLMResponse:
        self.calls += 1
        return LLMResponse(
            content="ok",
            usage=LLMUsage.reported(
                input_tokens=13,
                output_tokens=2,
                cache_read_tokens=3,
            ),
        )


def budget(*, requests: int = 5, tokens: int = 1000) -> RunBudget:
    return RunBudget(EvalBudget(
        max_requests=requests,
        max_steps=10,
        max_wall_seconds=30,
        max_tokens=tokens,
    ))


def test_live_execution_requires_explicit_config_and_budget(tmp_path: Path):
    with pytest.raises(ValidationError):
        EvalExecutionConfig(kind="live")
    with pytest.raises(ValidationError):
        EvalExecutionConfig(config_path=tmp_path / "config.json")


def test_composite_budget_admission_is_atomic():
    case = budget(requests=2)
    suite = budget(requests=1)
    combined = CompositeRunBudget(case, suite)
    combined.reserve_request(10, 20)

    with pytest.raises(EvalBudgetExceededError, match="request_budget"):
        combined.reserve_request(10, 20)

    assert case.requests == 1
    assert suite.requests == 1
    assert case.reserved_tokens == suite.reserved_tokens == 30


async def test_live_provider_records_reported_usage_and_purpose():
    inner = FakeLiveProvider()
    provider = LiveEvaluationProvider(
        inner,
        model="fake/model",
        budget=CompositeRunBudget(budget()),
        generation=GenerationSettings(temperature=0, max_tokens=32),
    )

    async def summarize() -> LLMResponse:
        return await provider.chat(
            messages=[{"role": "user", "content": "PRIVATE-PROMPT"}],
            max_tokens=16,
            temperature=0,
        )

    response = await provider.label_purpose(summarize, "summary")()

    assert response.content == "ok"
    assert inner.calls == 1
    assert len(provider.requests) == 1
    record = provider.requests[0]
    assert record.purpose == "summary"
    assert record.estimated_input_tokens == 11
    assert (record.input_tokens, record.output_tokens, record.cached_tokens) == (13, 2, 3)
    assert "PRIVATE-PROMPT" not in str(record.public_record())


async def test_live_provider_rejects_over_budget_before_dispatch():
    inner = FakeLiveProvider()
    provider = LiveEvaluationProvider(
        inner,
        model="fake/model",
        budget=CompositeRunBudget(budget(tokens=10)),
        generation=GenerationSettings(temperature=0, max_tokens=32),
    )

    with pytest.raises(EvalBudgetExceededError, match="token_budget"):
        await provider.chat(
            messages=[{"role": "user", "content": "bounded"}],
            max_tokens=16,
            temperature=0,
        )

    assert inner.calls == 0
    assert provider.requests == []


def test_prepare_live_execution_uses_only_explicit_config(tmp_path: Path, monkeypatch):
    from nanobot.providers import factory

    config_path = tmp_path / "live-config.json"
    config_path.write_text("{}", encoding="utf-8")
    expected = ProviderSnapshot(
        provider=FakeLiveProvider(),
        model="fake/model",
        context_window_tokens=32_000,
        signature=("fake",),
        generation=GenerationSettings(temperature=0.2, max_tokens=64),
    )
    monkeypatch.setattr(factory, "build_provider_snapshot", lambda config: expected)

    execution, snapshot = prepare_live_execution(
        config_path=config_path,
        max_requests=3,
        max_tokens=2000,
        max_wall_seconds=30,
    )

    assert execution.kind == "live"
    assert execution.config_path == config_path.resolve()
    assert execution.suite_budget is not None
    assert execution.suite_budget.max_requests == 3
    assert snapshot is expected


@pytest.mark.parametrize(
    "values",
    [
        {"max_requests": None, "max_tokens": 100, "max_wall_seconds": 10},
        {"max_requests": 1, "max_tokens": 0, "max_wall_seconds": 10},
        {"max_requests": 1, "max_tokens": 100, "max_wall_seconds": -1},
    ],
)
def test_prepare_live_execution_rejects_missing_or_nonpositive_limits(
    tmp_path: Path,
    values,
):
    config_path = tmp_path / "live-config.json"
    config_path.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="positive"):
        prepare_live_execution(config_path=config_path, **values)


def test_prepare_live_execution_rejects_fallback_models(tmp_path: Path, monkeypatch):
    from nanobot.config import loader
    from nanobot.config.schema import Config

    config_path = tmp_path / "live-config.json"
    config_path.write_text("{}", encoding="utf-8")
    config = Config()
    config.agents.defaults.fallback_models = ["backup"]
    monkeypatch.setattr(loader, "load_config", lambda path: config)
    monkeypatch.setattr(loader, "resolve_config_env_vars", lambda value, **kwargs: value)

    with pytest.raises(ValueError, match="fallback"):
        prepare_live_execution(
            config_path=config_path,
            max_requests=1,
            max_tokens=100,
            max_wall_seconds=10,
        )
