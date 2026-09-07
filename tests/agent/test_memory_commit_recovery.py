"""Crash-window and interleaving tests for canonical memory commits."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import nanobot.agent.memory_writes as memory_writes
from nanobot.agent.memory_writes import (
    MemoryConflictError,
    MemoryMutation,
    MemoryWriteCoordinator,
)


def _allow(_mutation: MemoryMutation) -> None:
    return None


def _writer(tmp_path: Path) -> MemoryWriteCoordinator:
    return MemoryWriteCoordinator(
        tmp_path / "workspace",
        tmp_path / "runtime",
        authorize=_allow,
    )


def _mutation(
    writer: MemoryWriteCoordinator,
    operation_id: str,
    **changes: str,
) -> MemoryMutation:
    base = writer.snapshot()
    proposed = dict(base.contents)
    for target, content in changes.items():
        proposed[target] = content  # type: ignore[index]
    return MemoryMutation(
        operation_id=operation_id,
        source="sdk",
        base_revision=base.revision,
        proposed_files=proposed,
    )


def test_recovery_finishes_transaction_that_failed_before_canonical_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writer = _writer(tmp_path)
    mutation = _mutation(writer, "recover-before", user="new user")
    original = memory_writes.write_text_atomic

    def fail_user(path: Path, content: str, *, newline: str | None = None) -> None:
        if path == writer.root / "USER.md":
            raise OSError("injected canonical failure")
        original(path, content, newline=newline)

    monkeypatch.setattr(memory_writes, "write_text_atomic", fail_user)
    result = writer.commit(mutation)
    monkeypatch.setattr(memory_writes, "write_text_atomic", original)

    assert result.status == "failed"
    assert writer._journal_path("recover-before").is_file()
    recovered = _writer(tmp_path).snapshot()
    assert recovered.contents["user"] == "new user"
    assert not writer._journal_path("recover-before").exists()
    assert writer._receipt_path("recover-before").is_file()


def test_recovery_finishes_mixed_old_and_new_hashes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writer = _writer(tmp_path)
    mutation = _mutation(
        writer,
        "recover-middle",
        user="new user",
        memory="new memory",
    )
    original = memory_writes.write_text_atomic

    def fail_memory(path: Path, content: str, *, newline: str | None = None) -> None:
        if path == writer.root / "memory" / "MEMORY.md":
            raise OSError("injected second canonical failure")
        original(path, content, newline=newline)

    monkeypatch.setattr(memory_writes, "write_text_atomic", fail_memory)
    result = writer.commit(mutation)
    monkeypatch.setattr(memory_writes, "write_text_atomic", original)

    assert result.status == "failed"
    assert (writer.root / "USER.md").read_text(encoding="utf-8") == "new user"
    assert not (writer.root / "memory" / "MEMORY.md").exists()

    recovered = _writer(tmp_path).snapshot()
    assert recovered.contents["user"] == "new user"
    assert recovered.contents["memory"] == "new memory"


def test_recovery_writes_receipt_after_all_canonical_files_are_new(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writer = _writer(tmp_path)
    mutation = _mutation(writer, "recover-receipt", user="new user")
    original = memory_writes.write_text_atomic

    def fail_receipt(path: Path, content: str, *, newline: str | None = None) -> None:
        if path.parent == writer.receipts_dir:
            raise OSError("injected receipt failure")
        original(path, content, newline=newline)

    monkeypatch.setattr(memory_writes, "write_text_atomic", fail_receipt)
    result = writer.commit(mutation)
    monkeypatch.setattr(memory_writes, "write_text_atomic", original)

    assert result.status == "failed"
    assert (writer.root / "USER.md").read_text(encoding="utf-8") == "new user"
    assert writer._journal_path("recover-receipt").is_file()
    assert not writer._receipt_path("recover-receipt").exists()

    recovered = _writer(tmp_path).snapshot()
    assert recovered.contents["user"] == "new user"
    assert writer._receipt_path("recover-receipt").is_file()


def test_recovery_cleans_journal_left_after_durable_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writer = _writer(tmp_path)
    mutation = _mutation(writer, "recover-cleanup", user="new user")
    journal = writer._journal_path("recover-cleanup")
    original_unlink = Path.unlink

    def fail_journal_unlink(path: Path, missing_ok: bool = False) -> None:
        if path == journal:
            raise OSError("injected cleanup failure")
        original_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", fail_journal_unlink)
    result = writer.commit(mutation)
    monkeypatch.setattr(Path, "unlink", original_unlink)

    assert result.status == "failed"
    assert journal.is_file()
    assert writer._receipt_path("recover-cleanup").is_file()

    recovered = _writer(tmp_path).snapshot()
    assert recovered.contents["user"] == "new user"
    assert not journal.exists()


def test_third_hash_stops_recovery_and_preserves_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writer = _writer(tmp_path)
    mutation = _mutation(
        writer,
        "recover-third-hash",
        user="transaction user",
        memory="transaction memory",
    )
    original = memory_writes.write_text_atomic

    def fail_memory(path: Path, content: str, *, newline: str | None = None) -> None:
        if path == writer.root / "memory" / "MEMORY.md":
            raise OSError("injected second canonical failure")
        original(path, content, newline=newline)

    monkeypatch.setattr(memory_writes, "write_text_atomic", fail_memory)
    assert writer.commit(mutation).status == "failed"
    monkeypatch.setattr(memory_writes, "write_text_atomic", original)
    (writer.root / "USER.md").write_text("external third value", encoding="utf-8")
    journal = writer._journal_path("recover-third-hash")

    with pytest.raises(MemoryConflictError, match="external_memory_change"):
        _writer(tmp_path).snapshot()

    assert journal.is_file()
    assert (writer.root / "USER.md").read_text(encoding="utf-8") == "external third value"
    assert not (writer.root / "memory" / "MEMORY.md").exists()


def test_corrupt_journal_blocks_reads_and_is_preserved(tmp_path: Path) -> None:
    writer = _writer(tmp_path)
    writer.snapshot()
    journal = writer.transactions_dir / "broken.json"
    journal.write_text("{not-json", encoding="utf-8")

    with pytest.raises(MemoryConflictError, match="corrupt_memory_journal"):
        _writer(tmp_path).snapshot()

    assert journal.read_text(encoding="utf-8") == "{not-json"


def test_two_coordinators_serialize_and_one_stale_mutation_conflicts(
    tmp_path: Path,
) -> None:
    first = _writer(tmp_path)
    second = _writer(tmp_path)
    base = first.snapshot()
    one_files = dict(base.contents)
    one_files["user"] = "one"
    two_files = dict(base.contents)
    two_files["user"] = "two"
    mutations = (
        MemoryMutation("thread-one", "sdk", base.revision, one_files),
        MemoryMutation("thread-two", "sdk", base.revision, two_files),
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(
            pool.map(
                lambda pair: pair[0].commit(pair[1]),
                ((first, mutations[0]), (second, mutations[1])),
            )
        )

    assert sorted(result.status for result in results) == ["committed", "conflict"]
    assert first.snapshot().contents["user"] in {"one", "two"}
    assert not any(first.transactions_dir.glob("*.json"))
