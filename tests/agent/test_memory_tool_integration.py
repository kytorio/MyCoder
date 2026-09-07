"""Full runner integration for memory_save and derived context refresh."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from nanobot.agent.context import ContextBuilder
from nanobot.agent.loop import AgentLoop
from nanobot.agent.tools.memory_save import MemoryToolConfig
from nanobot.bus.events import InboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.config.schema import ToolsConfig
from nanobot.providers.base import LLMProvider, LLMResponse, ToolCallRequest


class _ScriptedMemoryProvider(LLMProvider):
    def __init__(self, responses: list[LLMResponse]) -> None:
        super().__init__(provider_name="memory-script")
        self.responses = list(responses)
        self.requests: list[list[dict[str, Any]]] = []
        self.tool_definitions: list[list[dict[str, Any]] | None] = []

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        **_kwargs: Any,
    ) -> LLMResponse:
        self.requests.append(deepcopy(messages))
        self.tool_definitions.append(deepcopy(tools))
        return self.responses.pop(0)

    def get_default_model(self) -> str:
        return "memory-script-model"


def _loop(
    tmp_path: Path,
    provider: LLMProvider,
    *,
    enabled: bool = True,
) -> AgentLoop:
    return AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path / "workspace",
        model="memory-script-model",
        tools_config=ToolsConfig(
            memory=MemoryToolConfig(enabled=enabled),
        ),
        runtime_data_dir=tmp_path / "runtime",
    )


@pytest.mark.asyncio
async def test_runner_save_result_and_next_request_share_committed_revision(
    tmp_path: Path,
) -> None:
    provider = _ScriptedMemoryProvider(
        [
            LLMResponse(
                content="",
                finish_reason="tool_calls",
                context_acceptance="accepted",
                tool_calls=[
                    ToolCallRequest(
                        id="save-1",
                        name="memory_save",
                        arguments={
                            "target": "user",
                            "key": "reply.language",
                            "content": "以后用中文回答",
                            "source_excerpt": "以后用中文回答",
                        },
                    )
                ],
            ),
            LLMResponse(
                content="已保存，我会使用中文。",
                context_acceptance="accepted",
            ),
        ]
    )
    loop = _loop(tmp_path, provider)

    result = await loop._process_message(
        InboundMessage(
            channel="cli",
            sender_id="owner",
            chat_id="direct",
            content="记住，以后用中文回答",
            metadata={"message_id": "message-1"},
        )
    )

    assert result is not None and result.content == "已保存，我会使用中文。"
    assert len(provider.requests) == 2
    first_schema_names = {
        item["function"]["name"] for item in provider.tool_definitions[0] or []
    }
    assert "memory_save" in first_schema_names
    assert "以后用中文回答" not in str(provider.requests[0][0])
    assert "以后用中文回答" in str(provider.requests[1][0])
    tool_message = next(
        message for message in provider.requests[1] if message.get("role") == "tool"
    )
    assert '"status": "committed"' in str(tool_message["content"])
    assert '"revision":' in str(tool_message["content"])
    assert "以后用中文回答" in loop.context.memory.read_user()


@pytest.mark.asyncio
async def test_model_false_confirmation_does_not_trigger_hidden_save(
    tmp_path: Path,
) -> None:
    provider = _ScriptedMemoryProvider(
        [LLMResponse(content="好的，已经记住。", context_acceptance="accepted")]
    )
    loop = _loop(tmp_path, provider)

    result = await loop._process_message(
        InboundMessage(
            channel="cli",
            sender_id="owner",
            chat_id="direct",
            content="记住，以后用中文回答",
            metadata={"message_id": "message-1"},
        )
    )

    assert result is not None and "已经记住" in result.content
    assert loop.context.memory.read_user() == ""
    assert not any((tmp_path / "runtime" / "memory" / "receipts").glob("*.json"))


@pytest.mark.asyncio
async def test_project_memory_is_visible_only_in_matching_project_context(
    tmp_path: Path,
) -> None:
    project_a = tmp_path / "project-a"
    project_b = tmp_path / "project-b"
    provider = _ScriptedMemoryProvider([])
    loop = _loop(tmp_path, provider)
    tool = loop.tools.get("memory_save")
    assert tool is not None

    from nanobot.agent.tools.context import RequestContext, request_context

    with request_context(
        RequestContext(
            channel="cli",
            chat_id="direct",
            message_id="message-1",
            session_key="cli:direct",
            sender_id="owner",
            original_user_text="记住项目端口是 4317",
            workspace=project_a,
        )
    ):
        saved = await tool.execute(
            target="memory",
            key="project.telemetry.port",
            content="项目端口是 4317",
            source_excerpt="项目端口是 4317",
        )
    assert '"status": "committed"' in str(saved)

    builder = ContextBuilder(loop.workspace, memory_writer=loop.context.memory.writer)
    prompt_a = builder.build_system_prompt(workspace=project_a)
    prompt_b = builder.build_system_prompt(workspace=project_b)

    assert "项目端口是 4317" in prompt_a
    assert "项目端口是 4317" not in prompt_b

