"""Frozen N05 configuration defaults and invalid boundaries."""

import pytest
from pydantic import ValidationError

from nanobot.config.schema import AgentsConfig


def test_context_defaults_and_alias_roundtrip():
    context = AgentsConfig().context
    assert context.model_dump(by_alias=True) == {
        "mode": "observe", "enabledLayers": ["L1", "L2", "L3", "L4"],
        "highWatermark": 0.85, "targetRatio": 0.65, "safetyMarginTokens": None,
        "toolResultTokenBudget": 2048, "toolBatchTokenBudget": 8192,
        "artifactMaxBytes": 134217728, "schemaDiscovery": False,
    }
    assert AgentsConfig.model_validate({"context": context.model_dump(by_alias=True)}).context == context


@pytest.mark.parametrize("override", [
    {"mode": "silent"}, {"enabledLayers": ["L1", "L1"]}, {"enabledLayers": ["L5"]},
    {"targetRatio": 0.85}, {"highWatermark": 1}, {"targetRatio": 0},
    {"safetyMarginTokens": 0}, {"safetyMarginTokens": True},
    {"toolResultTokenBudget": 8193}, {"toolBatchTokenBudget": -1},
    {"artifactMaxBytes": 0},
])
def test_invalid_context_configuration_fails(override):
    with pytest.raises(ValidationError):
        AgentsConfig.model_validate({"context": override})


def test_from_config_injects_explicit_context_root_without_workspace_fallback(tmp_path):
    from nanobot.agent.loop import AgentLoop
    from nanobot.agent.tools.registry import ToolRegistry
    from nanobot.config.schema import Config
    from tests.agent.test_context_manifest import OfflineProvider

    class CaptureLoop(AgentLoop):
        def __init__(self, **kwargs):
            self.parameters = kwargs

    config = Config()
    config.agents.defaults.workspace = str(tmp_path / "workspace")
    provider = OfflineProvider(provider_name="n05-offline")
    loop = CaptureLoop.from_config(config, provider=provider, tool_registry=ToolRegistry(),
                                   session_manager=object())
    assert loop.parameters["runtime_data_dir"] is None
    config.bind_source_path(tmp_path / "runtime" / "config.json")
    config.agents.context.mode = "enforce"
    loop = CaptureLoop.from_config(config, provider=provider, tool_registry=ToolRegistry(),
                                   session_manager=object())
    assert loop.parameters["runtime_data_dir"] == (tmp_path / "runtime").resolve()
    assert loop.parameters["context_config"].mode == "enforce"
