"""Conflict and orchestration coverage for Dream's canonical memory writes."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from nanobot.agent.memory import MemoryStore
from nanobot.agent.memory_writes import (
    MemoryConflictError,
    MemoryFact,
    MemoryMutation,
    MemoryRememberRequested,
    MemoryScope,
    MemoryWriteCoordinator,
    merge_unmanaged_text,
)
from nanobot.agent.tools.base import ToolResult
from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.cli.gateway_runtime import _run_dream_cron_job
from nanobot.command.builtin import cmd_dream
from nanobot.command.router import CommandContext
from nanobot.session.manager import SessionManager


def _allow(_mutation: MemoryMutation) -> None:
    return None


def _store(tmp_path: Path) -> MemoryStore:
    workspace = tmp_path / "workspace"
    writer = MemoryWriteCoordinator(
        workspace,
        tmp_path / "runtime",
        authorize=_allow,
    )
    store = MemoryStore(workspace, writer=writer)
    store.write_user("language=old\n")
    store.write_soul("be helpful\n")
    store.write_memory("project=nanobot\n")
    return store


def _remember_language(store: MemoryStore) -> None:
    scope = MemoryScope(instance_id="test-instance")
    result = store.remember(
        MemoryRememberRequested(
            operation_id="remember-language",
            message_ref="session:test:turn:1",
            original_text_hash="source-hash",
            owner_id="test-owner",
            scope=scope,
            facts=(
                MemoryFact(
                    entry_id="language-entry",
                    target="user",
                    key="reply.language",
                    text="以后使用中文回答",
                    scope=scope,
                ),
            ),
            requested_at="2026-09-06T12:00:00+00:00",
        )
    )
    assert result.status == "committed"


def test_three_way_merge_combines_non_overlapping_unmanaged_edits() -> None:
    merged = merge_unmanaged_text(
        "language=old\ntone=plain\n",
        "language=Chinese\ntone=plain\n",
        "language=old\ntone=concise\n",
    )

    assert merged == "language=Chinese\ntone=concise\n"


def test_three_way_merge_rejects_overlapping_unmanaged_edits() -> None:
    with pytest.raises(MemoryConflictError, match="overlapping_memory_change"):
        merge_unmanaged_text(
            "language=old\n",
            "language=Chinese\n",
            "language=English\n",
        )


@pytest.mark.asyncio
async def test_old_dream_write_preserves_new_explicit_memory(tmp_path: Path) -> None:
    store = _store(tmp_path)
    tools = store.build_dream_tools()
    _remember_language(store)

    result = await tools.execute(
        "write_file",
        {"path": "USER.md", "content": "language=organized\n"},
    )

    assert not (isinstance(result, ToolResult) and result.is_error)
    content = store.read_user()
    assert "language=organized" in content
    assert "以后使用中文回答" in content
    assert "nanobot-explicit-memory:v1" in content
    assert MemoryStore.dream_tools_completed(tools)


@pytest.mark.asyncio
async def test_conflict_requires_read_then_retry_and_blocks_completion(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    tools = store.build_dream_tools()
    store.write_user("language=Chinese\n")

    conflict = await tools.execute(
        "edit_file",
        {
            "path": "USER.md",
            "old_text": "language=old",
            "new_text": "language=English",
        },
    )

    assert isinstance(conflict, ToolResult) and conflict.is_error
    assert "overlapping_memory_change" in conflict
    assert not MemoryStore.dream_tools_completed(tools)
    assert store.read_user() == "language=Chinese\n"

    await tools.execute("read_file", {"path": "USER.md", "force": True})
    retry = await tools.execute(
        "edit_file",
        {
            "path": "USER.md",
            "old_text": "language=Chinese",
            "new_text": "language=English",
        },
    )

    assert not (isinstance(retry, ToolResult) and retry.is_error)
    assert MemoryStore.dream_tools_completed(tools)
    assert store.read_user() == "language=English\n"


@pytest.mark.asyncio
async def test_mixed_skill_and_memory_patch_must_be_split(tmp_path: Path) -> None:
    store = _store(tmp_path)
    tools = store.build_dream_tools()
    skill = store.workspace / "skills" / "demo" / "SKILL.md"
    mixed_edits = [
        {
            "path": "USER.md",
            "action": "replace",
            "old_text": "language=old",
            "new_text": "language=Chinese",
        },
        {
            "path": "skills/demo/SKILL.md",
            "action": "add",
            "new_text": "---\nname: demo\n---\n",
        },
    ]

    mixed = await tools.execute("apply_patch", {"edits": mixed_edits})

    assert isinstance(mixed, ToolResult) and mixed.is_error
    assert "cannot mix canonical memory files with Skill files" in mixed
    assert store.read_user() == "language=old\n"
    assert not skill.exists()
    assert not MemoryStore.dream_tools_completed(tools)

    memory_result = await tools.execute("apply_patch", {"edits": [mixed_edits[0]]})
    skill_result = await tools.execute("apply_patch", {"edits": [mixed_edits[1]]})

    assert "Patch applied" in memory_result
    assert "Patch applied" in skill_result
    assert MemoryStore.dream_tools_completed(tools)
    assert store.read_user() == "language=Chinese\n"
    assert skill.is_file()


@pytest.mark.asyncio
async def test_manual_dream_entry_preserves_concurrent_explicit_memory(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    store.append_history("The user asked for a lasting preference.")
    bus_done = asyncio.Event()
    outbound: list[OutboundMessage] = []

    class _Bus:
        async def publish_outbound(self, message: OutboundMessage) -> None:
            outbound.append(message)
            bus_done.set()

    async def process_direct(*_args: Any, **kwargs: Any) -> OutboundMessage:
        _remember_language(store)
        result = await kwargs["tools"].execute(
            "write_file",
            {"path": "USER.md", "content": "language=organized\n"},
        )
        assert not (isinstance(result, ToolResult) and result.is_error)
        return OutboundMessage(
            channel="cli",
            chat_id="direct",
            content="done",
            metadata={"_stop_reason": "completed"},
        )

    msg = InboundMessage(
        channel="cli",
        sender_id="u1",
        chat_id="direct",
        content="/dream",
    )
    loop = SimpleNamespace(
        bus=_Bus(),
        context=SimpleNamespace(memory=store, timezone="UTC"),
        sessions=SessionManager(
            store.workspace,
            sessions_root=tmp_path / "sessions",
        ),
        process_direct=process_direct,
        dream_runtime=lambda: object(),
    )
    ctx = CommandContext(
        msg=msg,
        session=None,
        key=msg.session_key,
        raw="/dream",
        args="",
        loop=loop,
    )

    await cmd_dream(ctx)
    await asyncio.wait_for(bus_done.wait(), timeout=2)

    assert store.get_last_dream_cursor() == 1
    assert "以后使用中文回答" in store.read_user()
    assert outbound and "Dream completed" in outbound[-1].content


@pytest.mark.asyncio
async def test_cron_dream_entry_does_not_advance_cursor_on_unresolved_conflict(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    store.append_history("History for periodic Dream.")

    async def process_direct(*_args: Any, **kwargs: Any) -> SimpleNamespace:
        store.write_user("language=Chinese\n")
        result = await kwargs["tools"].execute(
            "edit_file",
            {
                "path": "USER.md",
                "old_text": "language=old",
                "new_text": "language=English",
            },
        )
        assert isinstance(result, ToolResult) and result.is_error
        return SimpleNamespace(metadata={"_stop_reason": "completed"})

    class _MCP:
        async def connect(self) -> None:
            return None

    agent = SimpleNamespace(
        context=SimpleNamespace(memory=store),
        sessions=SessionManager(
            store.workspace,
            sessions_root=tmp_path / "sessions",
        ),
        process_direct=process_direct,
        dream_runtime=lambda: object(),
    )

    await _run_dream_cron_job(agent, _MCP())

    assert store.get_last_dream_cursor() == 0
    assert store.read_user() == "language=Chinese\n"

