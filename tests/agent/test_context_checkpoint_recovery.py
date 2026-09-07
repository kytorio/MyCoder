from __future__ import annotations

from pathlib import Path

import pytest

from nanobot.agent.context_plan import StructuredContextSummary, render_context_summary
from nanobot.bus.queue import MessageBus
from nanobot.session.history_visibility import HIDDEN_HISTORY_META
from nanobot.session.manager import Session, SessionManager
from nanobot.session.recovery import (
    RECOVERY_METADATA_KEY,
    RUNTIME_CHECKPOINT_KEY,
    RecoveryCoordinator,
    restore_runtime_checkpoint,
)
from nanobot.session.summary import SUMMARY_CONTINUATION_TEXT


def _summary() -> StructuredContextSummary:
    return StructuredContextSummary(
        schema_version=1,
        tasks=["Recover the task"],
        constraints=[],
        explicit_preferences=[],
        decisions=[],
        file_changes=[],
        errors=[],
        evidence_refs=[],
        remaining_work=["Continue after restart"],
    )


def _staged_summary(*, insert_at: int = 1) -> dict:
    structured = _summary()
    return {
        "version": 1,
        "text": render_context_summary(structured),
        "structured": structured.model_dump(mode="json"),
        "transcript_boundary": 2,
        "session_insert_at": insert_at,
    }


def test_recovery_materializes_summary_only_checkpoint_once() -> None:
    session = Session(
        key="test:summary-recovery",
        messages=[{"role": "user", "content": "accepted"}],
        metadata={
            RUNTIME_CHECKPOINT_KEY: {
                "summary_checkpoint": _staged_summary(),
                "summary_checkpoint_delta": False,
            }
        },
    )

    assert restore_runtime_checkpoint(session)
    assert session.last_archived == 1
    assert session.messages[1]["content"] == SUMMARY_CONTINUATION_TEXT
    assert session.messages[1][HIDDEN_HISTORY_META] is True
    assert session.metadata["_last_summary"]["structured"]["schema_version"] == 1
    assert RUNTIME_CHECKPOINT_KEY not in session.metadata
    assert not restore_runtime_checkpoint(session)
    assert sum(
        message.get("content") == SUMMARY_CONTINUATION_TEXT
        for message in session.messages
    ) == 1


def test_recovery_restores_only_post_summary_tool_delta_without_duplicates() -> None:
    assistant = {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": "call-1",
                "type": "function",
                "function": {"name": "read_file", "arguments": "{}"},
            }
        ],
    }
    tool_result = {
        "role": "tool",
        "tool_call_id": "call-1",
        "name": "read_file",
        "content": "result",
    }
    session = Session(
        key="test:summary-delta",
        messages=[{"role": "user", "content": "accepted"}],
        metadata={
            RUNTIME_CHECKPOINT_KEY: {
                "phase": "tools_completed",
                "assistant_message": assistant,
                "completed_tool_results": [tool_result],
                "pending_tool_calls": [],
                "summary_checkpoint": _staged_summary(),
                "summary_checkpoint_delta": True,
            }
        },
    )

    assert restore_runtime_checkpoint(session)
    assert [message.get("tool_call_id") for message in session.messages].count("call-1") == 1
    assert [message.get("role") for message in session.messages[-2:]] == ["assistant", "tool"]
    assert not restore_runtime_checkpoint(session)
    assert [message.get("tool_call_id") for message in session.messages].count("call-1") == 1


def test_unknown_summary_version_is_not_trusted_but_regular_checkpoint_recovers() -> None:
    session = Session(
        key="test:unknown-summary",
        messages=[{"role": "user", "content": "question"}],
        metadata={
            RUNTIME_CHECKPOINT_KEY: {
                "phase": "final_response",
                "assistant_message": {"role": "assistant", "content": "answer"},
                "completed_tool_results": [],
                "pending_tool_calls": [],
                "summary_checkpoint": {**_staged_summary(), "version": 2},
                "summary_checkpoint_delta": True,
            }
        },
    )

    assert restore_runtime_checkpoint(session)
    assert "_last_summary" not in session.metadata
    assert session.messages[-1]["content"] == "answer"


@pytest.mark.asyncio
async def test_recovery_coordinator_accepts_summary_only_sidecar(tmp_path: Path) -> None:
    sessions_root = tmp_path / "sessions"
    workspace = tmp_path / "workspace"
    sessions = SessionManager(workspace, sessions_root=sessions_root)
    session = sessions.get_or_create("websocket:chat")
    session.messages.append({"role": "user", "content": "accepted"})
    session.metadata["webui"] = True
    sessions.save(session)
    session.metadata[RUNTIME_CHECKPOINT_KEY] = {
        "summary_checkpoint": _staged_summary(),
        "summary_checkpoint_delta": False,
    }
    sessions.save_runtime_checkpoint(session)

    restarted = SessionManager(workspace, sessions_root=sessions_root)
    coordinator = RecoveryCoordinator(restarted, MessageBus())
    await coordinator.scan()

    restored = restarted.get_or_create("websocket:chat")
    assert restored.metadata["_last_summary"]["structured"]["schema_version"] == 1
    assert restored.metadata[RECOVERY_METADATA_KEY]["reason"] == "restart_requires_confirmation"
