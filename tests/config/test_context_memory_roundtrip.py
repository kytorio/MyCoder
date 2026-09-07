"""N11 delivery-gate coverage for context and explicit-memory configuration."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from nanobot.agent.loop import AgentLoop
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.config.loader import load_config, save_config
from nanobot.config.schema import Config
from nanobot.webui.settings_services import WebUISettingsConfig


def test_defaults_stay_non_mutating_across_alias_roundtrip() -> None:
    config = Config.model_validate({})

    serialized = config.model_dump(mode="json", by_alias=True)
    restored = Config.model_validate(serialized)

    assert serialized["agents"]["context"]["mode"] == "observe"
    assert serialized["tools"]["memory"]["enabled"] is False
    assert serialized["agents"]["defaults"]["dream"]["intervalH"] == 2
    assert restored.agents.context.mode == "observe"
    assert restored.tools.memory.enabled is False
    assert restored.agents.defaults.dream.interval_h == 2


def test_custom_settings_and_env_template_survive_atomic_save(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "agents": {
                    "context": {
                        "mode": "enforce",
                        "enabledLayers": ["L1", "L3", "L4"],
                        "safetyMarginTokens": 1536,
                    }
                },
                "tools": {
                    "memory": {
                        "enabled": True,
                        "transactionMaxBytes": 4096,
                        "journalMaxBytes": 8192,
                    }
                },
                "providers": {"groq": {"apiKey": "${N11_TEST_API_KEY}"}},
            }
        ),
        encoding="utf-8",
    )

    config = load_config(config_path)
    save_config(config, config_path)
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    restored = load_config(config_path)

    assert saved["agents"]["context"]["enabledLayers"] == ["L1", "L3", "L4"]
    assert saved["tools"]["memory"] == {
        "enabled": True,
        "transactionMaxBytes": 4096,
        "journalMaxBytes": 8192,
    }
    assert saved["providers"]["groq"]["apiKey"] == "${N11_TEST_API_KEY}"
    assert restored.runtime_data_dir == tmp_path.resolve()
    assert restored.agents.context.mode == "enforce"
    assert restored.tools.memory.enabled is True


def test_settings_read_modify_write_preserves_context_and_memory(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    initial = Config.model_validate(
        {
            "agents": {"context": {"mode": "enforce", "enabledLayers": ["L2", "L4"]}},
            "tools": {"memory": {"enabled": True}},
        }
    )
    save_config(initial, config_path)
    settings = WebUISettingsConfig(config_path)

    settings.update(lambda config: setattr(config.agents.defaults, "max_tokens", 2048))
    restored = load_config(config_path)

    assert restored.agents.defaults.max_tokens == 2048
    assert restored.agents.context.mode == "enforce"
    assert restored.agents.context.enabled_layers == ["L2", "L4"]
    assert restored.tools.memory.enabled is True


@pytest.mark.parametrize("field", ["transactionMaxBytes", "journalMaxBytes"])
def test_memory_byte_limits_reject_boolean_values(field: str) -> None:
    with pytest.raises(ValidationError):
        Config.model_validate({"tools": {"memory": {field: True}}})


class _CaptureLoop(AgentLoop):
    def __init__(self, **kwargs: object) -> None:
        self.parameters = kwargs


def _build_loop(config: Config, **extra: object) -> _CaptureLoop:
    return _CaptureLoop.from_config(
        config,
        provider=object(),
        tool_registry=ToolRegistry(),
        session_manager=object(),
        **extra,
    )


@pytest.mark.parametrize(
    "config",
    [
        Config.model_validate({"agents": {"context": {"mode": "enforce"}}}),
        Config.model_validate({"tools": {"memory": {"enabled": True}}}),
    ],
)
def test_stateful_features_require_an_explicit_runtime_root(config: Config) -> None:
    with pytest.raises(ValueError, match="require a runtime data root"):
        _build_loop(config)


def test_bound_or_explicit_runtime_root_is_forwarded(tmp_path: Path) -> None:
    bound = Config.model_validate({"agents": {"context": {"mode": "enforce"}}})
    bound.bind_source_path(tmp_path / "bound" / "config.json")
    bound_loop = _build_loop(bound)

    explicit = Config.model_validate({"tools": {"memory": {"enabled": True}}})
    explicit_root = tmp_path / "explicit"
    explicit_loop = _build_loop(explicit, runtime_data_dir=explicit_root)

    assert bound_loop.parameters["runtime_data_dir"] == (tmp_path / "bound").resolve()
    assert explicit_loop.parameters["runtime_data_dir"] == explicit_root


def test_observe_defaults_remain_usable_without_a_bound_path() -> None:
    loop = _build_loop(Config())

    assert loop.parameters["runtime_data_dir"] is None
    assert loop.parameters["context_config"].mode == "observe"
