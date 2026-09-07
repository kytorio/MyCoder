"""N08 run-local tool-schema discovery contracts."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobot.agent.tools.base import Tool
from nanobot.agent.tools.context import ToolContext
from nanobot.agent.tools.loader import ToolLoader
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.config.schema import ToolsConfig
from nanobot.providers.base import LLMProvider, LLMResponse, ToolCallRequest


class _NamedTool(Tool):
    def __init__(self, name: str, description: str = "available") -> None:
        self._name = name
        self._description = description

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return self._description

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {"value": {"type": "string", "enum": ["one", "two"]}},
            "required": ["value"],
            "additionalProperties": False,
        }

    async def execute(self, **kwargs: object) -> str:
        return str(kwargs["value"])


def _tool_context(tmp_path: Path, *, discovery: bool) -> ToolContext:
    return ToolContext(
        config=ToolsConfig(),
        workspace=str(tmp_path),
        schema_discovery=discovery,
    )


def test_tool_loader_registration_follows_schema_discovery(tmp_path: Path) -> None:
    from nanobot.agent.tools.tool_discovery import ToolDiscoveryTool

    loader = ToolLoader(test_classes=[ToolDiscoveryTool])
    disabled = ToolRegistry()
    enabled = ToolRegistry()

    assert loader.load(_tool_context(tmp_path, discovery=False), disabled) == []
    assert loader.load(_tool_context(tmp_path, discovery=True), enabled) == ["tool_discovery"]


@pytest.mark.asyncio
async def test_tool_discovery_loads_exact_names_monotonically_from_bound_registry() -> None:
    from nanobot.agent.tools.tool_discovery import (
        ToolDiscoveryState,
        ToolDiscoveryTool,
        bind_tool_discovery_state,
        reset_tool_discovery_state,
    )

    registry = ToolRegistry()
    registry.register(_NamedTool("allowed", "A" * 500))
    registry.register(_NamedTool("second"))
    discovery = ToolDiscoveryTool()
    registry.register(discovery)
    original_names = set(registry.tool_names)
    state = ToolDiscoveryState(registry)
    token = bind_tool_discovery_state(state)
    try:
        listed = json.loads(str(await discovery.execute(action="search", query="allow")))
        assert listed == {"tools": [{"name": "allowed", "description": "A" * 240}]}

        loaded = json.loads(str(await discovery.execute(action="load", names=["allowed"])))
        assert loaded == {"loaded": ["allowed"]}
        assert state.loaded_names == {"allowed"}

        error = await discovery.execute(action="load", names=["missing"])
        assert error.is_error
        assert state.loaded_names == {"allowed"}

        await discovery.execute(action="load", names=["second", "allowed"])
        assert state.loaded_names == {"allowed", "second"}
        assert set(registry.tool_names) == original_names
    finally:
        reset_tool_discovery_state(token)


def _schema_names(definitions: list[dict] | None) -> list[str]:
    if definitions is None:
        return []
    return [definition["function"]["name"] for definition in definitions]


def _discovery_registry(*business_names: str) -> ToolRegistry:
    from nanobot.agent.tools.tool_discovery import ToolDiscoveryTool

    registry = ToolRegistry()
    for name in business_names:
        registry.register(_NamedTool(name))
    registry.register(_NamedTool("context_artifact_read"))
    registry.register(ToolDiscoveryTool())
    return registry


@pytest.mark.asyncio
async def test_runner_discovers_then_sends_complete_schema_and_executes_tool() -> None:
    from nanobot.agent.context_governance import ContextGovernor
    from nanobot.agent.runner import AgentRunner
    from nanobot.config.schema import ContextConfig
    from tests.agent.runner_helpers import make_run_spec

    registry = _discovery_registry("allowed", "deferred")
    initial_count = len(registry)
    requests: list[list[dict] | None] = []
    provider = MagicMock(spec=LLMProvider)
    provider.can_resume_conversation_state.return_value = False

    async def chat_with_retry(**kwargs):
        requests.append(kwargs["tools"])
        if len(requests) == 1:
            return LLMResponse(
                content="load allowed",
                tool_calls=[
                    ToolCallRequest(
                        id="discover",
                        name="tool_discovery",
                        arguments={"action": "load", "names": ["allowed"]},
                    )
                ],
            )
        if len(requests) == 2:
            return LLMResponse(
                content="use allowed",
                tool_calls=[
                    ToolCallRequest(
                        id="allowed-call",
                        name="allowed",
                        arguments={"value": "two"},
                    )
                ],
            )
        return LLMResponse(content="done")

    provider.chat_with_retry = AsyncMock(side_effect=chat_with_retry)
    class CapturingGovernor(ContextGovernor):
        def __init__(self) -> None:
            self.dispatched = []

        def record_dispatch(self, state) -> None:
            assert state.manifest is not None
            self.dispatched.append(state.manifest)
            super().record_dispatch(state)

    runner = AgentRunner()
    governor = CapturingGovernor()
    runner.context_governor = governor
    result = await runner.run(
        make_run_spec(
            provider,
            model="gpt-4o",
            initial_messages=[{"role": "user", "content": "use the allowed tool"}],
            tools=registry,
            max_iterations=3,
            max_tool_result_chars=16_000,
            context_config=ContextConfig(schema_discovery=True),
        )
    )

    assert _schema_names(requests[0]) == ["context_artifact_read", "tool_discovery"]
    assert _schema_names(requests[1]) == [
        "allowed",
        "context_artifact_read",
        "tool_discovery",
    ]
    assert _schema_names(requests[2]) == _schema_names(requests[1])
    assert result.final_content == "done"
    assert result.tools_used == ["tool_discovery", "allowed"]
    assert len(registry) == initial_count
    allowed_schema = requests[1][0]["function"]["parameters"]
    assert allowed_schema == registry.get("allowed").parameters
    first_deferrals = [
        decision
        for decision in governor.dispatched[0].plan.decisions
        if decision.reason_code == "schema_deferred"
    ]
    second_deferrals = [
        decision
        for decision in governor.dispatched[1].plan.decisions
        if decision.reason_code == "schema_deferred"
    ]
    assert {decision.source_id for decision in first_deferrals} == {
        "schema:deferred:allowed",
        "schema:deferred:deferred",
    }
    assert {decision.source_id for decision in second_deferrals} == {
        "schema:deferred:deferred",
    }
    assert governor.dispatched[0].reason_code == "schema_deferred"


@pytest.mark.asyncio
async def test_runner_discovery_state_isolated_between_concurrent_runs() -> None:
    from nanobot.agent.runner import AgentRunner
    from nanobot.config.schema import ContextConfig
    from tests.agent.runner_helpers import make_run_spec

    entered = 0
    both_entered = asyncio.Event()

    async def one_run(name: str) -> list[list[str]]:
        nonlocal entered
        registry = _discovery_registry(name)
        schemas: list[list[str]] = []
        provider = MagicMock(spec=LLMProvider)
        provider.can_resume_conversation_state.return_value = False

        async def chat_with_retry(**kwargs):
            nonlocal entered
            schemas.append(_schema_names(kwargs["tools"]))
            if len(schemas) == 1:
                entered += 1
                if entered == 2:
                    both_entered.set()
                await both_entered.wait()
                return LLMResponse(
                    content=None,
                    tool_calls=[
                        ToolCallRequest(
                            id=f"load-{name}",
                            name="tool_discovery",
                            arguments={"action": "load", "names": [name]},
                        )
                    ]
                )
            return LLMResponse(content="done")

        provider.chat_with_retry = AsyncMock(side_effect=chat_with_retry)
        await AgentRunner().run(
            make_run_spec(
                provider,
                model="gpt-4o",
                initial_messages=[{"role": "user", "content": name}],
                tools=registry,
                max_iterations=2,
                max_tool_result_chars=16_000,
                context_config=ContextConfig(schema_discovery=True),
            )
        )
        return schemas

    first, second = await asyncio.gather(one_run("first"), one_run("second"))

    assert first[1] == ["first", "context_artifact_read", "tool_discovery"]
    assert second[1] == ["second", "context_artifact_read", "tool_discovery"]


@pytest.mark.asyncio
async def test_no_tools_finalization_remains_no_tools_with_discovery() -> None:
    from nanobot.agent.runner import AgentRunner
    from nanobot.config.schema import ContextConfig
    from tests.agent.runner_helpers import make_run_spec

    registry = _discovery_registry("allowed")
    sent_tools: list[list[dict] | None] = []
    provider = MagicMock(spec=LLMProvider)
    provider.can_resume_conversation_state.return_value = False
    responses = iter(
        [
            LLMResponse(
                content=None,
                tool_calls=[
                    ToolCallRequest(
                        id="load",
                        name="tool_discovery",
                        arguments={"action": "load", "names": ["allowed"]},
                    )
                ]
            ),
            LLMResponse(content="final without tools"),
        ]
    )

    async def captured_chat(**kwargs):
        sent_tools.append(kwargs["tools"])
        return next(responses)

    provider.chat_with_retry = AsyncMock(side_effect=captured_chat)
    result = await AgentRunner().run(
        make_run_spec(
            provider,
            model="gpt-4o",
            initial_messages=[{"role": "user", "content": "load"}],
            tools=registry,
            max_iterations=1,
            max_tool_result_chars=16_000,
            context_config=ContextConfig(schema_discovery=True),
        )
    )

    assert _schema_names(sent_tools[0]) == ["context_artifact_read", "tool_discovery"]
    assert sent_tools[1] is None
    assert result.final_content == "final without tools"


def test_loop_registers_discovery_only_when_configured(tmp_path: Path) -> None:
    from nanobot.agent.loop import AgentLoop
    from nanobot.bus.queue import MessageBus
    from nanobot.config.schema import ContextConfig

    provider = MagicMock(spec=LLMProvider)
    provider.get_default_model.return_value = "gpt-4o"

    disabled = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path / "disabled",
        context_config=ContextConfig(schema_discovery=False),
    )
    enabled = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path / "enabled",
        context_config=ContextConfig(schema_discovery=True),
    )

    assert "tool_discovery" not in disabled.tools
    assert "tool_discovery" in enabled.tools
