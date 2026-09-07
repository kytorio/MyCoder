"""Behavioral tests for the explicit memory_save tool."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nanobot.agent.memory import MemoryStore
from nanobot.agent.memory_writes import MemoryMutation, MemoryWriteCoordinator
from nanobot.agent.tools.base import ToolResult
from nanobot.agent.tools.context import RequestContext, request_context
from nanobot.agent.tools.memory_save import MemorySaveTool


def _memory(tmp_path: Path, *, denied: bool = False) -> MemoryStore:
    def authorize(_mutation: MemoryMutation) -> None:
        if denied:
            raise PermissionError("read-only")

    root = tmp_path / "workspace"
    writer = MemoryWriteCoordinator(
        root,
        tmp_path / "runtime",
        authorize=authorize,
    )
    return MemoryStore(root, writer=writer)


def _context(
    text: str = "记住，以后用中文回答",
    *,
    message_id: str = "message-1",
    workspace: Path | None = None,
) -> RequestContext:
    return RequestContext(
        channel="cli",
        chat_id="test",
        session_key="cli:test",
        message_id=message_id,
        sender_id="owner",
        original_user_text=text,
        workspace=workspace,
    )


@pytest.mark.asyncio
async def test_tool_commits_before_return_and_replays(tmp_path: Path) -> None:
    memory = _memory(tmp_path)
    tool = MemorySaveTool(memory)
    params = {
        "target": "user",
        "key": "reply.language",
        "content": "以后用中文回答",
        "source_excerpt": "以后用中文回答",
    }

    with request_context(_context()):
        first = await tool.execute(**params)
        second = await tool.execute(**params)

    first_data = json.loads(first)
    second_data = json.loads(second)
    assert first_data["status"] == second_data["status"] == "committed"
    assert second_data["replayed"] is True
    assert second_data["revision"] == first_data["revision"]
    assert "以后用中文回答" in memory.read_user()


@pytest.mark.asyncio
async def test_tool_binds_owner_scope_and_source_without_model_parameters(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project-a"
    memory = _memory(tmp_path)
    tool = MemorySaveTool(memory)

    with request_context(_context("请记住项目端口是 4317", workspace=project)):
        result = await tool.execute(
            target="memory",
            key="project.telemetry.port",
            content="遥测端口为 4317",
            source_excerpt="项目端口是 4317",
        )

    data = json.loads(result)
    snapshot = memory.writer.snapshot()
    assert data["status"] == "committed"
    assert snapshot.explicit_entries[0].scope.instance_id.startswith("instance-")
    assert snapshot.explicit_entries[0].scope.project_id.startswith("project-")
    assert "owner" not in data["saved_facts"][0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("context", "params", "reason"),
    [
        (
            _context("请记住别的内容"),
            {
                "target": "user",
                "key": "reply.language",
                "content": "以后用中文回答",
                "source_excerpt": "以后用中文回答",
            },
            "source_excerpt_not_found",
        ),
        (
            RequestContext(
                channel="cli",
                chat_id="test",
                original_user_text="记住这个偏好",
            ),
            {
                "target": "user",
                "key": "reply.language",
                "content": "以后用中文回答",
                "source_excerpt": "记住这个偏好",
            },
            "trusted_user_source_unavailable",
        ),
        (
            _context("记住 api_key=sk-abcdefghijklmnop"),
            {
                "target": "memory",
                "key": "service.api_key",
                "content": "api_key=sk-abcdefghijklmnop",
                "source_excerpt": "api_key=sk-abcdefghijklmnop",
            },
            "secret_material_rejected",
        ),
    ],
)
async def test_tool_rejects_untrusted_or_sensitive_requests(
    tmp_path: Path,
    context: RequestContext,
    params: dict[str, str],
    reason: str,
) -> None:
    memory = _memory(tmp_path)
    with request_context(context):
        result = await MemorySaveTool(memory).execute(**params)

    assert isinstance(result, ToolResult)
    assert result.is_error is True
    assert json.loads(result)["reason_code"] == reason
    assert memory.read_user() == ""
    assert memory.read_memory() == ""


@pytest.mark.asyncio
async def test_tool_reports_read_only_failure_without_success_receipt(
    tmp_path: Path,
) -> None:
    memory = _memory(tmp_path, denied=True)
    with request_context(_context()):
        result = await MemorySaveTool(memory).execute(
            target="user",
            key="reply.language",
            content="以后用中文回答",
            source_excerpt="以后用中文回答",
        )

    assert isinstance(result, ToolResult)
    assert result.is_error is True
    assert json.loads(result) == {
        "message": "记忆未确认保存",
        "reason_code": "authorization_denied",
        "status": "failed",
    }
    assert memory.read_user() == ""


@pytest.mark.asyncio
async def test_tool_correction_requires_entry_id(tmp_path: Path) -> None:
    memory = _memory(tmp_path)
    tool = MemorySaveTool(memory)
    with request_context(_context()):
        first = json.loads(
            await tool.execute(
                target="user",
                key="reply.language",
                content="以后用中文回答",
                source_excerpt="以后用中文回答",
            )
        )

    correction_context = _context(
        "更正：以后用英文回答",
        message_id="message-2",
    )
    with request_context(correction_context):
        failed = await tool.execute(
            target="user",
            key="reply.language",
            content="以后用英文回答",
            source_excerpt="以后用英文回答",
        )
        corrected = await tool.execute(
            target="user",
            key="reply.language",
            content="以后用英文回答",
            source_excerpt="以后用英文回答",
            replaces_entry_id=first["entry_ids"][0],
        )

    assert isinstance(failed, ToolResult) and failed.is_error
    assert json.loads(failed)["reason_code"] == "protected_entry_conflict"
    assert json.loads(corrected)["status"] == "committed"
    assert "以后用中文回答" not in memory.read_user()
    assert "以后用英文回答" in memory.read_user()


@pytest.mark.asyncio
async def test_registry_validation_rejects_oversized_content(tmp_path: Path) -> None:
    from nanobot.agent.tools.registry import ToolRegistry

    registry = ToolRegistry()
    registry.register(MemorySaveTool(_memory(tmp_path)))
    with request_context(_context()):
        result = await registry.execute(
            "memory_save",
            {
                "target": "user",
                "key": "reply.language",
                "content": "x" * 2049,
                "source_excerpt": "以后用中文回答",
            },
        )

    assert isinstance(result, ToolResult) and result.is_error
    assert "at most 2048 chars" in result

