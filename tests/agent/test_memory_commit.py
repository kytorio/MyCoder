"""Commit-contract tests for canonical, explicitly managed memory."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest

from nanobot.agent.memory import MemoryStore
from nanobot.agent.memory_writes import (
    MemoryConflictError,
    MemoryFact,
    MemoryMutation,
    MemoryRememberRequested,
    MemoryScope,
    MemoryWriteCoordinator,
    build_explicit_mutation,
    shared_memory_write_coordinator,
)


def _allow(_mutation: MemoryMutation) -> None:
    return None


def _writer(tmp_path: Path, **limits: int) -> MemoryWriteCoordinator:
    return MemoryWriteCoordinator(
        tmp_path / "workspace",
        tmp_path / "runtime",
        authorize=_allow,
        **limits,
    )


def _request(
    *,
    operation_id: str = "op-1",
    entry_id: str = "entry-1",
    text: str = "以后用中文回答",
    key: str = "reply.language",
    replaces_entry_id: str | None = None,
) -> MemoryRememberRequested:
    scope = MemoryScope(instance_id="test-instance", project_id=None)
    return MemoryRememberRequested(
        operation_id=operation_id,
        message_ref="session:test:turn:1",
        original_text_hash="source-hash",
        owner_id="test-owner",
        scope=scope,
        facts=(
            MemoryFact(
                entry_id=entry_id,
                target="user",
                key=key,
                text=text,
                scope=scope,
                replaces_entry_id=replaces_entry_id,
            ),
        ),
        requested_at="2026-09-06T12:00:00+00:00",
    )


def test_replay_keeps_revision_and_receipt(tmp_path: Path) -> None:
    writer = _writer(tmp_path)
    mutation = build_explicit_mutation(_request(), writer.snapshot())

    first = writer.commit(mutation)
    second = writer.commit(mutation)

    assert first.status == second.status == "committed"
    assert second.replayed is True
    assert second.revision == first.revision
    assert "以后用中文回答" in writer.snapshot().contents["user"]
    assert not any(writer.transactions_dir.glob("*.json"))
    assert writer._receipt_path("op-1").is_file()


def test_memory_store_replays_same_request_without_rebuilding_conflict(
    tmp_path: Path,
) -> None:
    writer = _writer(tmp_path)
    store = MemoryStore(writer.root, writer=writer)
    request = _request()

    first = store.remember(request)
    second = store.remember(request)

    assert first.status == second.status == "committed"
    assert second.replayed is True
    assert second.revision == first.revision


def test_same_operation_id_with_different_request_conflicts(tmp_path: Path) -> None:
    writer = _writer(tmp_path)
    base = writer.snapshot()
    first = build_explicit_mutation(_request(), base)
    different = build_explicit_mutation(
        _request(entry_id="entry-2", text="Please answer in English"),
        base,
    )

    assert writer.commit(first).status == "committed"
    conflict = writer.commit(different)

    assert conflict.status == "conflict"
    assert conflict.reason_code == "operation_id_conflict"
    assert "Please answer in English" not in writer.snapshot().contents["user"]


def test_same_fact_under_new_operation_is_semantic_noop(tmp_path: Path) -> None:
    writer = _writer(tmp_path)
    assert writer.commit(
        build_explicit_mutation(_request(), writer.snapshot())
    ).status == "committed"

    duplicate = _request(operation_id="op-2", entry_id="entry-2")
    result = writer.commit(build_explicit_mutation(duplicate, writer.snapshot()))
    snapshot = writer.snapshot()

    assert result.status == "committed"
    assert result.entry_ids == ("entry-1",)
    assert [fact.entry_id for fact in snapshot.explicit_entries] == ["entry-1"]
    assert snapshot.contents["user"].count("以后用中文回答") == 1


def test_explicit_correction_requires_exact_replaced_entry(tmp_path: Path) -> None:
    writer = _writer(tmp_path)
    writer.commit(build_explicit_mutation(_request(), writer.snapshot()))

    with pytest.raises(MemoryConflictError, match="protected_entry_conflict"):
        build_explicit_mutation(
            _request(operation_id="op-2", entry_id="entry-2", text="用英文回答"),
            writer.snapshot(),
        )

    correction = _request(
        operation_id="op-3",
        entry_id="entry-3",
        text="用英文回答",
        replaces_entry_id="entry-1",
    )
    result = writer.commit(build_explicit_mutation(correction, writer.snapshot()))

    assert result.status == "committed"
    snapshot = writer.snapshot()
    assert [fact.entry_id for fact in snapshot.explicit_entries] == ["entry-3"]
    assert "以后用中文回答" not in snapshot.contents["user"]
    assert "用英文回答" in snapshot.contents["user"]


def test_sdk_write_is_sync_inside_event_loop_and_cannot_remove_explicit_entry(
    tmp_path: Path,
) -> None:
    writer = _writer(tmp_path)
    store = MemoryStore(writer.root, writer=writer)
    assert store.remember(_request()).status == "committed"

    async def overwrite() -> None:
        store.write_user("replacement")

    with pytest.raises(MemoryConflictError, match="protected_entry_conflict"):
        asyncio.run(overwrite())
    assert "以后用中文回答" in store.read_user()


def test_authorization_denial_does_not_modify_canonical_files(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"

    def deny(_mutation: MemoryMutation) -> None:
        raise PermissionError("read-only")

    writer = MemoryWriteCoordinator(
        workspace,
        tmp_path / "runtime",
        authorize=deny,
    )
    mutation = build_explicit_mutation(_request(), writer.snapshot())

    result = writer.commit(mutation)

    assert result.status == "failed"
    assert result.reason_code == "authorization_denied"
    assert not (workspace / "USER.md").exists()
    assert not any(writer.transactions_dir.glob("*.json"))


def test_external_edit_after_snapshot_wins_with_conflict(tmp_path: Path) -> None:
    writer = _writer(tmp_path)
    base = writer.snapshot()
    proposed = dict(base.contents)
    proposed["user"] = "planned"
    mutation = MemoryMutation(
        operation_id="sdk-user-1",
        source="sdk",
        base_revision=base.revision,
        proposed_files=proposed,
    )
    writer.root.mkdir(parents=True, exist_ok=True)
    (writer.root / "USER.md").write_text("external", encoding="utf-8")

    result = writer.commit(mutation)

    assert result.status == "conflict"
    assert result.reason_code == "base_revision_conflict"
    assert (writer.root / "USER.md").read_text(encoding="utf-8") == "external"


def test_transaction_limit_fails_before_journal_or_canonical_write(
    tmp_path: Path,
) -> None:
    writer = _writer(tmp_path, transaction_max_bytes=512, journal_max_bytes=512)
    base = writer.snapshot()
    proposed = dict(base.contents)
    proposed["memory"] = "x" * 1024

    result = writer.commit(
        MemoryMutation(
            operation_id="sdk-large",
            source="sdk",
            base_revision=base.revision,
            proposed_files=proposed,
        )
    )

    assert result.status == "conflict"
    assert result.reason_code == "transaction_too_large"
    assert not (writer.root / "memory" / "MEMORY.md").exists()
    assert not any(writer.transactions_dir.glob("*.json"))


def test_corrupt_managed_marker_blocks_snapshot_and_sdk_write(tmp_path: Path) -> None:
    writer = _writer(tmp_path)
    writer.root.mkdir(parents=True, exist_ok=True)
    (writer.root / "USER.md").write_text(
        "<!-- nanobot-explicit-memory:v1 {bad-json} -->\ntext\n",
        encoding="utf-8",
    )

    with pytest.raises(MemoryConflictError, match="corrupt_managed_entry"):
        writer.snapshot()


def test_shared_factory_returns_one_process_owner(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    runtime = tmp_path / "runtime"

    first = shared_memory_write_coordinator(root, runtime, authorize=_allow)
    second = shared_memory_write_coordinator(root, runtime, authorize=_allow)

    assert first is second


def test_canonical_symlink_is_rejected_when_supported(tmp_path: Path) -> None:
    writer = _writer(tmp_path)
    writer.root.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside.md"
    outside.write_text("outside", encoding="utf-8")
    try:
        (writer.root / "USER.md").symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation is unavailable on this Windows host")

    with pytest.raises(MemoryConflictError, match="unsafe_canonical_path"):
        writer.snapshot()
    assert outside.read_text(encoding="utf-8") == "outside"


def test_explicit_commit_does_not_advance_dream_cursor(tmp_path: Path) -> None:
    writer = _writer(tmp_path)
    store = MemoryStore(writer.root, writer=writer)
    store._dream_cursor_file.write_text("7", encoding="utf-8")

    assert store.remember(_request()).status == "committed"

    assert store._dream_cursor_file.read_text(encoding="utf-8") == "7"


def test_explicit_mutation_cannot_smuggle_unmanaged_edits(tmp_path: Path) -> None:
    writer = _writer(tmp_path)
    mutation = build_explicit_mutation(_request(), writer.snapshot())
    proposed = dict(mutation.proposed_files)
    proposed["memory"] = "unrelated edit"

    result = writer.commit(replace(mutation, proposed_files=proposed))

    assert result.status == "conflict"
    assert result.reason_code == "invalid_explicit_mutation"
    assert not (writer.root / "memory" / "MEMORY.md").exists()
