"""Protected-memory checks for Dream restore candidates."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from nanobot.agent.memory import MemoryStore
from nanobot.agent.memory_writes import (
    MemoryFact,
    MemoryMutation,
    MemoryRememberRequested,
    MemoryScope,
    MemoryWriteCoordinator,
)
from nanobot.bus.events import InboundMessage
from nanobot.command.builtin import cmd_dream_restore
from nanobot.command.router import CommandContext


def _allow(_mutation: MemoryMutation) -> None:
    return None


def _versioned_store(tmp_path: Path) -> tuple[MemoryStore, str]:
    workspace = tmp_path / "workspace"
    writer = MemoryWriteCoordinator(
        workspace,
        tmp_path / "runtime",
        authorize=_allow,
    )
    store = MemoryStore(workspace, writer=writer)
    store.write_user("language=before\n")
    store.write_soul("be helpful\n")
    store.write_memory("project=nanobot\n")
    store.set_last_dream_cursor(1)
    assert store.git.init()

    store.write_user("language=after-dream\n")
    store.set_last_dream_cursor(2)
    dream_sha = store.git.auto_commit("dream: update language")
    assert dream_sha is not None
    return store, dream_sha


def _remember_language(store: MemoryStore) -> None:
    scope = MemoryScope(instance_id="test-instance")
    result = store.remember(
        MemoryRememberRequested(
            operation_id="remember-after-dream",
            message_ref="session:test:turn:2",
            original_text_hash="source-hash",
            owner_id="test-owner",
            scope=scope,
            facts=(
                MemoryFact(
                    entry_id="protected-language",
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


def test_restore_candidate_is_read_only_until_validated(tmp_path: Path) -> None:
    store, dream_sha = _versioned_store(tmp_path)
    before_user = store.read_user()
    before_cursor = store.get_last_dream_cursor()

    candidate = store.git.read_revert_candidate(dream_sha, message_prefix="dream:")

    assert candidate is not None
    assert candidate[0]["USER.md"] == "language=after-dream\n"
    assert candidate[1]["USER.md"] == "language=before\n"
    assert store.read_user() == before_user
    assert store.get_last_dream_cursor() == before_cursor


def test_restore_rejects_candidate_that_removes_new_explicit_memory(
    tmp_path: Path,
) -> None:
    store, dream_sha = _versioned_store(tmp_path)
    _remember_language(store)
    before = store.writer.snapshot() if store.writer is not None else None
    before_cursor = store.get_last_dream_cursor()

    result = store.restore_dream_version(dream_sha)

    assert result is not None
    assert result.status == "conflict"
    assert result.reason_code == "protected_entry_conflict"
    assert store.writer is not None and before is not None
    assert store.writer.snapshot().revision == before.revision
    assert store.get_last_dream_cursor() == before_cursor
    assert "以后使用中文回答" in store.read_user()


def test_restore_without_protected_conflict_commits_then_advances_cursor(
    tmp_path: Path,
) -> None:
    store, dream_sha = _versioned_store(tmp_path)

    result = store.restore_dream_version(dream_sha)

    assert result is not None and result.status == "committed"
    assert store.read_user() == "language=before\n"
    assert store.get_last_dream_cursor() == 1
    safety_sha = store.git.auto_commit(f"revert: undo {dream_sha}")
    assert safety_sha is not None


@pytest.mark.asyncio
async def test_restore_command_reports_protected_conflict_without_git_write(
    tmp_path: Path,
) -> None:
    store, dream_sha = _versioned_store(tmp_path)
    _remember_language(store)
    before_head = store.git.log(max_entries=1)[0].sha
    msg = InboundMessage(
        channel="cli",
        sender_id="u1",
        chat_id="direct",
        content=f"/dream-restore {dream_sha}",
    )
    ctx = CommandContext(
        msg=msg,
        session=None,
        key=msg.session_key,
        raw=msg.content,
        args=dream_sha,
        loop=SimpleNamespace(consolidator=SimpleNamespace(store=store)),
    )

    response = await cmd_dream_restore(ctx)

    assert "protected memory validation failed" in response.content
    assert "No memory files or Git history were changed" in response.content
    assert store.git.log(max_entries=1)[0].sha == before_head
    assert "以后使用中文回答" in store.read_user()

