"""Recoverable, single-process commits for canonical agent memory files."""

from __future__ import annotations

import json
import os
import re
import threading
import weakref
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal, cast

from nanobot.security.workspace_policy import resolve_allowed_path
from nanobot.utils.helpers import write_text_atomic

MemoryTarget = Literal["user", "soul", "memory"]
MemoryMutationSource = Literal["explicit", "dream", "sdk", "restore"]
MemoryCommitStatus = Literal["committed", "conflict", "failed"]

_TARGET_PATHS: dict[MemoryTarget, str] = {
    "user": "USER.md",
    "soul": "SOUL.md",
    "memory": "memory/MEMORY.md",
}
_ENTRY_START = "<!-- nanobot-explicit-memory:v1 "
_ENTRY_END = "<!-- /nanobot-explicit-memory:v1 -->"
_ENTRY_RE = re.compile(
    rf"(?ms)^<!-- nanobot-explicit-memory:v1 (?P<meta>\{{.*?\}}) -->\n"
    rf"(?P<text>.*?)\n{re.escape(_ENTRY_END)}(?:\n|$)"
)
_OPERATION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,191}$")
_PRIVATE_FILE_DIGEST_CHARS = 48


class MemoryConflictError(RuntimeError):
    """A protected entry, external edit, or journal state prevents a safe write."""

    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


class MemoryWriteUnavailableError(RuntimeError):
    """No recoverable writer was configured for an explicit memory request."""


@dataclass(frozen=True, slots=True)
class MemoryScope:
    instance_id: str
    project_id: str | None = None


@dataclass(frozen=True, slots=True)
class MemoryFact:
    entry_id: str
    target: MemoryTarget
    key: str
    text: str
    scope: MemoryScope
    replaces_entry_id: str | None = None


@dataclass(frozen=True, slots=True)
class MemoryRememberRequested:
    operation_id: str
    message_ref: str
    original_text_hash: str
    owner_id: str
    scope: MemoryScope
    facts: tuple[MemoryFact, ...]
    requested_at: str | None = None


@dataclass(frozen=True, slots=True)
class MemorySnapshot:
    revision: str
    file_hashes: Mapping[MemoryTarget, str]
    contents: Mapping[MemoryTarget, str]
    explicit_entries: tuple[MemoryFact, ...]


@dataclass(frozen=True, slots=True)
class MemoryMutation:
    operation_id: str
    source: MemoryMutationSource
    base_revision: str
    proposed_files: Mapping[MemoryTarget, str]
    facts: tuple[MemoryFact, ...] = ()
    request_hash: str | None = None


@dataclass(frozen=True, slots=True)
class MemoryCommitResult:
    operation_id: str
    status: MemoryCommitStatus
    revision: str
    entry_ids: tuple[str, ...]
    replayed: bool = False
    reason_code: str | None = None


@dataclass(frozen=True, slots=True)
class _ParsedEntry:
    fact: MemoryFact
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class _TextEdit:
    start: int
    end: int
    replacement: tuple[str, ...]


_ROOT_LOCKS: weakref.WeakValueDictionary[str, threading.RLock] = (
    weakref.WeakValueDictionary()
)
_ROOT_LOCKS_GUARD = threading.Lock()
_SHARED_WRITERS: weakref.WeakValueDictionary[
    tuple[str, str, int, int], MemoryWriteCoordinator
] = weakref.WeakValueDictionary()
_SHARED_WRITERS_GUARD = threading.Lock()


def _hash_text(text: str) -> str:
    return sha256(text.encode("utf-8")).hexdigest()


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _revision(file_hashes: Mapping[MemoryTarget, str]) -> str:
    return sha256(_canonical_json(dict(file_hashes)).encode()).hexdigest()


def memory_instance_id(root: Path) -> str:
    """Return a stable, non-reversible id for one canonical memory root."""
    logical = os.path.normcase(str(Path(os.path.abspath(root))))
    return f"instance-{_hash_text(logical)[:24]}"


def memory_project_id(project: Path) -> str:
    """Return a stable, non-reversible id for one project-scoped memory view."""
    logical = os.path.normcase(str(Path(os.path.abspath(project))))
    return f"project-{_hash_text(logical)[:24]}"


def _root_lock(root: Path) -> threading.RLock:
    key = os.path.normcase(str(root))
    with _ROOT_LOCKS_GUARD:
        lock = _ROOT_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _ROOT_LOCKS[key] = lock
        return lock


def _scope_from(value: object) -> MemoryScope:
    if not isinstance(value, dict):
        raise ValueError("invalid_scope")
    row = cast(dict[str, Any], value)
    instance_id, project_id = row.get("instance_id"), row.get("project_id")
    if not isinstance(instance_id, str) or not instance_id:
        raise ValueError("invalid_scope")
    if project_id is not None and not isinstance(project_id, str):
        raise ValueError("invalid_scope")
    return MemoryScope(instance_id=instance_id, project_id=project_id)


def _parse_entries(target: MemoryTarget, content: str) -> list[_ParsedEntry]:
    parsed: list[_ParsedEntry] = []
    for match in _ENTRY_RE.finditer(content):
        try:
            metadata = json.loads(match.group("meta"))
            if not isinstance(metadata, dict):
                raise ValueError("invalid_metadata")
            row = cast(dict[str, Any], metadata)
            if row.get("target") != target:
                raise ValueError("invalid_target")
            entry_id, key = row.get("entry_id"), row.get("key")
            replaces = row.get("replaces_entry_id")
            provenance = (
                row.get("operation_id"),
                row.get("message_ref"),
                row.get("original_text_hash"),
                row.get("owner_id"),
                row.get("recorded_at"),
            )
            if not isinstance(entry_id, str) or not entry_id:
                raise ValueError("invalid_entry_id")
            if not isinstance(key, str) or not key:
                raise ValueError("invalid_key")
            if replaces is not None and not isinstance(replaces, str):
                raise ValueError("invalid_replacement")
            if any(not isinstance(value, str) or not value for value in provenance):
                raise ValueError("invalid_provenance")
            fact = MemoryFact(
                entry_id=entry_id,
                target=target,
                key=key,
                text=match.group("text"),
                scope=_scope_from(row.get("scope")),
                replaces_entry_id=replaces,
            )
        except (ValueError, TypeError) as exc:
            raise MemoryConflictError("corrupt_managed_entry") from exc
        parsed.append(_ParsedEntry(fact=fact, start=match.start(), end=match.end()))
    unmatched = _ENTRY_RE.sub("", content)
    if _ENTRY_START in unmatched or _ENTRY_END in unmatched:
        raise MemoryConflictError("corrupt_managed_entry")
    return parsed


def _managed_blocks(content: str) -> tuple[str, tuple[str, ...]]:
    """Return unmanaged text plus validated, byte-stable managed blocks."""
    blocks: list[str] = []
    unmanaged: list[str] = []
    cursor = 0
    for match in _ENTRY_RE.finditer(content):
        unmanaged.append(content[cursor : match.start()])
        block = match.group(0)
        try:
            metadata_value: object = json.loads(match.group("meta"))
            metadata = (
                cast(dict[str, object], metadata_value)
                if isinstance(metadata_value, dict)
                else {}
            )
            target_value = metadata.get("target")
        except (TypeError, ValueError) as exc:
            raise MemoryConflictError("corrupt_managed_entry") from exc
        if target_value not in _TARGET_PATHS:
            raise MemoryConflictError("corrupt_managed_entry")
        target = target_value
        if len(_parse_entries(target, block)) != 1:
            raise MemoryConflictError("corrupt_managed_entry")
        blocks.append(block)
        cursor = match.end()
    unmanaged.append(content[cursor:])
    remainder = _ENTRY_RE.sub("", content)
    if _ENTRY_START in remainder or _ENTRY_END in remainder:
        raise MemoryConflictError("corrupt_managed_entry")
    return "".join(unmanaged), tuple(blocks)


def _line_edits(base: tuple[str, ...], changed: tuple[str, ...]) -> list[_TextEdit]:
    return [
        _TextEdit(i1, i2, changed[j1:j2])
        for tag, i1, i2, j1, j2 in SequenceMatcher(
            a=base,
            b=changed,
            autojunk=False,
        ).get_opcodes()
        if tag != "equal"
    ]


def _edits_overlap(left: _TextEdit, right: _TextEdit) -> bool:
    if left == right:
        return False
    left_insert = left.start == left.end
    right_insert = right.start == right.end
    if left_insert and right_insert:
        return left.start == right.start
    if left_insert:
        return right.start <= left.start < right.end
    if right_insert:
        return left.start <= right.start < left.end
    return max(left.start, right.start) < min(left.end, right.end)


def _merge_unmanaged_lines(base: str, latest: str, proposed: str) -> str:
    if proposed == base:
        return latest
    if latest == base or latest == proposed:
        return proposed
    base_lines = tuple(base.splitlines(keepends=True))
    latest_edits = _line_edits(base_lines, tuple(latest.splitlines(keepends=True)))
    proposed_edits = _line_edits(base_lines, tuple(proposed.splitlines(keepends=True)))
    if any(_edits_overlap(left, right) for left in latest_edits for right in proposed_edits):
        raise MemoryConflictError("overlapping_memory_change")
    edits = list(dict.fromkeys([*latest_edits, *proposed_edits]))
    merged = list(base_lines)
    for edit in sorted(edits, key=lambda item: (item.start, item.end), reverse=True):
        merged[edit.start : edit.end] = edit.replacement
    return "".join(merged)


def merge_unmanaged_text(base: str, latest: str, proposed: str) -> str:
    """Three-way merge unmanaged text while preserving latest explicit entries.

    Dream must return every managed block it observed unchanged. Newer explicit
    entries come only from ``latest`` and are reattached verbatim after a
    deterministic, non-overlapping line merge of the remaining text.
    """
    base_text, base_blocks = _managed_blocks(base)
    latest_text, latest_blocks = _managed_blocks(latest)
    proposed_text, proposed_blocks = _managed_blocks(proposed)
    if proposed_blocks != base_blocks:
        raise MemoryConflictError("protected_entry_conflict")
    merged = _merge_unmanaged_lines(base_text, latest_text, proposed_text)
    if not latest_blocks:
        return merged
    separator = "" if not merged or merged.endswith("\n") else "\n"
    return merged + separator + "".join(latest_blocks)


def validate_protected_entries_preserved(current: str, proposed: str) -> None:
    """Reject whole-file candidates that alter any current explicit entry."""
    _, current_blocks = _managed_blocks(current)
    _, proposed_blocks = _managed_blocks(proposed)
    if any(block not in proposed_blocks for block in current_blocks):
        raise MemoryConflictError("protected_entry_conflict")


def _entry_block(fact: MemoryFact, request: MemoryRememberRequested) -> str:
    metadata = {
        "entry_id": fact.entry_id,
        "target": fact.target,
        "key": fact.key,
        "scope": asdict(fact.scope),
        "replaces_entry_id": fact.replaces_entry_id,
        "operation_id": request.operation_id,
        "message_ref": request.message_ref,
        "original_text_hash": request.original_text_hash,
        "owner_id": request.owner_id,
        "recorded_at": request.requested_at
        or datetime.now(timezone.utc).isoformat(),
    }
    return f"{_ENTRY_START}{_canonical_json(metadata)} -->\n{fact.text}\n{_ENTRY_END}\n"


def visible_memory_content(
    content: str,
    *,
    instance_id: str,
    project_id: str,
) -> str:
    """Hide managed project facts belonging to another project or instance."""
    visible = content
    for entry in reversed(_parse_entries("memory", content)):
        scope = entry.fact.scope
        if scope.instance_id != instance_id or scope.project_id not in {
            None,
            project_id,
        }:
            visible = visible[: entry.start] + visible[entry.end :]
    return visible


def _validate_fact(fact: MemoryFact, scope: MemoryScope) -> None:
    if (
        fact.scope != scope
        or not _OPERATION_RE.fullmatch(fact.entry_id)
        or not fact.key
        or fact.key.strip() != fact.key
        or not fact.text.strip()
    ):
        raise MemoryConflictError("invalid_fact")
    if len(fact.key) > 128 or len(fact.text) > 2048:
        raise MemoryConflictError("invalid_fact")
    if _ENTRY_START in fact.text or _ENTRY_END in fact.text:
        raise MemoryConflictError("invalid_fact")


def build_explicit_mutation(
    request: MemoryRememberRequested,
    base: MemorySnapshot,
) -> MemoryMutation:
    """Merge explicit facts into managed blocks without replacing protected entries."""
    if (
        not _OPERATION_RE.fullmatch(request.operation_id)
        or not request.message_ref
        or not request.original_text_hash
        or not request.owner_id
        or not request.scope.instance_id
        or not request.facts
    ):
        raise MemoryConflictError("invalid_request")
    proposed = dict(base.contents)
    effective: list[MemoryFact] = []
    for fact in request.facts:
        _validate_fact(fact, request.scope)
        current = proposed[fact.target]
        entries = _parse_entries(fact.target, current)
        same_id = next((entry for entry in entries if entry.fact.entry_id == fact.entry_id), None)
        if same_id is not None:
            if same_id.fact != fact:
                raise MemoryConflictError("entry_id_conflict")
            effective.append(same_id.fact)
            continue
        matching_keys = [
            entry
            for entry in entries
            if entry.fact.scope == fact.scope and entry.fact.key == fact.key
        ]
        if len(matching_keys) > 1:
            raise MemoryConflictError("duplicate_managed_entry")
        same_key = matching_keys[0] if matching_keys else None
        if same_key is not None and same_key.fact.text == fact.text:
            effective.append(same_key.fact)
            continue
        if same_key is not None:
            if fact.replaces_entry_id != same_key.fact.entry_id:
                raise MemoryConflictError("protected_entry_conflict")
            block = _entry_block(fact, request)
            proposed[fact.target] = current[: same_key.start] + block + current[same_key.end :]
        else:
            if fact.replaces_entry_id is not None:
                raise MemoryConflictError("replacement_not_found")
            separator = "" if not current or current.endswith("\n") else "\n"
            proposed[fact.target] = current + separator + _entry_block(fact, request)
        effective.append(fact)
    return MemoryMutation(
        operation_id=request.operation_id,
        source="explicit",
        base_revision=base.revision,
        proposed_files=proposed,
        facts=tuple(effective),
        request_hash=_hash_text(
            _canonical_json(
                {
                    "operation_id": request.operation_id,
                    "message_ref": request.message_ref,
                    "original_text_hash": request.original_text_hash,
                    "owner_id": request.owner_id,
                    "scope": asdict(request.scope),
                    "facts": [asdict(fact) for fact in request.facts],
                }
            )
        ),
    )


def workspace_memory_authorizer(root: Path) -> Callable[[MemoryMutation], None]:
    """Adapt the existing exact-path workspace guard to canonical memory writes."""
    logical_root = Path(os.path.abspath(root))
    allowed = tuple(logical_root / relative for relative in _TARGET_PATHS.values())

    def authorize(mutation: MemoryMutation) -> None:
        for target in mutation.proposed_files:
            path = logical_root / _TARGET_PATHS[target]
            resolve_allowed_path(path, workspace=logical_root, extra_allowed_files=allowed)

    return authorize


class MemoryWriteCoordinator:
    """Synchronously recover and commit canonical memory under one root lock."""

    _JOURNAL_VERSION = 1
    _RECEIPT_VERSION = 1

    def __init__(
        self,
        root: Path,
        runtime_root: Path,
        *,
        authorize: Callable[[MemoryMutation], None],
        transaction_max_bytes: int = 1_048_576,
        journal_max_bytes: int = 67_108_864,
    ) -> None:
        if transaction_max_bytes <= 0 or journal_max_bytes < transaction_max_bytes:
            raise ValueError("invalid_memory_journal_limits")
        self.root = Path(os.path.abspath(root))
        self.runtime_root = Path(os.path.abspath(runtime_root))
        self.authorize = authorize
        self.transaction_max_bytes = transaction_max_bytes
        self.journal_max_bytes = journal_max_bytes
        self.private_root = self.runtime_root / "memory"
        self.transactions_dir = self.private_root / "transactions"
        self.receipts_dir = self.private_root / "receipts"
        self._lock = _root_lock(self.root)

    def _ensure_private_dirs(self) -> None:
        for path in (
            self.runtime_root,
            self.private_root,
            self.transactions_dir,
            self.receipts_dir,
        ):
            if path.exists() and path.is_symlink():
                raise MemoryConflictError("unsafe_runtime_path")
            path.mkdir(parents=True, exist_ok=True)

    def _path(self, target: MemoryTarget) -> Path:
        return self.root / _TARGET_PATHS[target]

    def _read_contents(self) -> dict[MemoryTarget, str]:
        contents: dict[MemoryTarget, str] = {}
        for target in _TARGET_PATHS:
            path = self._path(target)
            if any(
                item.exists() and item.is_symlink()
                for item in (self.root, path.parent, path)
            ):
                raise MemoryConflictError("unsafe_canonical_path")
            try:
                contents[target] = path.read_text(encoding="utf-8")
            except FileNotFoundError:
                contents[target] = ""
        return contents

    @staticmethod
    def _snapshot_from(contents: Mapping[MemoryTarget, str]) -> MemorySnapshot:
        hashes: dict[MemoryTarget, str] = {
            target: _hash_text(contents[target]) for target in _TARGET_PATHS
        }
        entries = tuple(
            row.fact
            for target in _TARGET_PATHS
            for row in _parse_entries(target, contents[target])
        )
        identities = [(fact.target, fact.entry_id) for fact in entries]
        if len(identities) != len(set(identities)):
            raise MemoryConflictError("duplicate_managed_entry")
        return MemorySnapshot(
            revision=_revision(hashes),
            file_hashes=hashes,
            contents=dict(contents),
            explicit_entries=entries,
        )

    def snapshot(self) -> MemorySnapshot:
        with self._lock:
            self._ensure_private_dirs()
            self._recover_locked()
            return self._snapshot_from(self._read_contents())

    @staticmethod
    def _mutation_payload(mutation: MemoryMutation) -> dict[str, object]:
        return {
            "operation_id": mutation.operation_id,
            "source": mutation.source,
            "base_revision": mutation.base_revision,
            "proposed_files": dict(mutation.proposed_files),
            "facts": [asdict(fact) for fact in mutation.facts],
            "request_hash": mutation.request_hash,
        }

    @staticmethod
    def _idempotency_hash(
        mutation: MemoryMutation,
        payload_json: str,
    ) -> str:
        if mutation.request_hash is None:
            return _hash_text(payload_json)
        return _hash_text(
            _canonical_json(
                {
                    "operation_id": mutation.operation_id,
                    "source": mutation.source,
                    "request_hash": mutation.request_hash,
                }
            )
        )

    def _receipt_path(self, operation_id: str) -> Path:
        # Keep the private name non-reversible while leaving enough headroom for
        # the atomic writer's temporary suffix on legacy Windows path limits.
        digest = _hash_text(operation_id)[:_PRIVATE_FILE_DIGEST_CHARS]
        return self.receipts_dir / f"{digest}.json"

    def _journal_path(self, operation_id: str) -> Path:
        digest = _hash_text(operation_id)[:_PRIVATE_FILE_DIGEST_CHARS]
        return self.transactions_dir / f"{digest}.json"

    def _load_json(
        self,
        path: Path,
        *,
        reason_code: str = "corrupt_memory_journal",
        max_bytes: int | None = None,
    ) -> dict[str, object]:
        try:
            if max_bytes is not None and path.stat().st_size > max_bytes:
                raise MemoryConflictError(reason_code)
            value = json.loads(path.read_text(encoding="utf-8"))
        except MemoryConflictError:
            raise
        except (OSError, ValueError) as exc:
            raise MemoryConflictError(reason_code) from exc
        if not isinstance(value, dict):
            raise MemoryConflictError(reason_code)
        return cast(dict[str, object], value)

    @staticmethod
    def _validate_protected_entries(
        current: MemorySnapshot,
        proposed: MemorySnapshot,
        mutation: MemoryMutation,
    ) -> None:
        before = {(fact.target, fact.entry_id): fact for fact in current.explicit_entries}
        after = {(fact.target, fact.entry_id): fact for fact in proposed.explicit_entries}
        if mutation.source != "explicit":
            if before != after:
                raise MemoryConflictError("protected_entry_conflict")
            return

        if not mutation.facts:
            raise MemoryConflictError("invalid_explicit_mutation")
        for target in _TARGET_PATHS:
            current_unmanaged = _ENTRY_RE.sub("", current.contents[target]).rstrip("\n")
            proposed_unmanaged = _ENTRY_RE.sub("", proposed.contents[target]).rstrip("\n")
            if current_unmanaged != proposed_unmanaged:
                raise MemoryConflictError("invalid_explicit_mutation")
        declared = {(fact.target, fact.entry_id): fact for fact in mutation.facts}
        if len(declared) != len(mutation.facts):
            raise MemoryConflictError("duplicate_managed_entry")
        if any(after.get(identity) != fact for identity, fact in declared.items()):
            raise MemoryConflictError("invalid_explicit_mutation")

        for identity, old_fact in before.items():
            new_at_identity = after.get(identity)
            if new_at_identity == old_fact:
                continue
            replacements = [
                fact
                for fact in mutation.facts
                if fact.replaces_entry_id == old_fact.entry_id
                and fact.target == old_fact.target
                and fact.key == old_fact.key
                and fact.scope == old_fact.scope
            ]
            if len(replacements) != 1:
                raise MemoryConflictError("protected_entry_conflict")

        added = set(after) - set(before)
        if any(identity not in declared for identity in added):
            raise MemoryConflictError("invalid_explicit_mutation")

    @staticmethod
    def _result_from_receipt(
        receipt: Mapping[str, object],
        *,
        replayed: bool,
    ) -> MemoryCommitResult:
        operation_id, revision = receipt.get("operation_id"), receipt.get("revision")
        entries = receipt.get("entry_ids")
        if (
            not isinstance(operation_id, str)
            or not isinstance(revision, str)
            or not isinstance(entries, list)
        ):
            raise MemoryConflictError("corrupt_memory_receipt")
        entry_rows = cast(list[object], entries)
        if any(not isinstance(entry, str) for entry in entry_rows):
            raise MemoryConflictError("corrupt_memory_receipt")
        return MemoryCommitResult(
            operation_id=operation_id,
            status="committed",
            revision=revision,
            entry_ids=tuple(cast(list[str], entry_rows)),
            replayed=replayed,
        )

    def _write_receipt(
        self,
        operation_id: str,
        payload_hash: str,
        revision: str,
        entry_ids: tuple[str, ...],
    ) -> dict[str, object]:
        receipt: dict[str, object] = {
            "version": self._RECEIPT_VERSION,
            "operation_id": operation_id,
            "payload_hash": payload_hash,
            "revision": revision,
            "entry_ids": list(entry_ids),
            "committed_at": datetime.now(timezone.utc).isoformat(),
        }
        write_text_atomic(self._receipt_path(operation_id), _canonical_json(receipt), newline="")
        return receipt

    def _recover_one_locked(self, path: Path) -> None:
        journal = self._load_json(path, max_bytes=self.transaction_max_bytes)
        operation_id = journal.get("operation_id")
        if (
            journal.get("version") != self._JOURNAL_VERSION
            or not isinstance(operation_id, str)
            or path != self._journal_path(operation_id)
        ):
            raise MemoryConflictError("corrupt_memory_journal")
        payload_hash = journal.get("payload_hash")
        old_hashes, new_hashes, proposed = (
            journal.get("old_hashes"),
            journal.get("new_hashes"),
            journal.get("proposed_files"),
        )
        entry_ids = journal.get("entry_ids")
        source, base_revision, facts, request_hash, mutation_hash = (
            journal.get("source"),
            journal.get("base_revision"),
            journal.get("facts"),
            journal.get("request_hash"),
            journal.get("mutation_hash"),
        )
        if not all(
            isinstance(value, dict) for value in (old_hashes, new_hashes, proposed)
        ):
            raise MemoryConflictError("corrupt_memory_journal")
        if (
            not isinstance(payload_hash, str)
            or not isinstance(entry_ids, list)
            or not isinstance(source, str)
            or source not in {"explicit", "dream", "sdk", "restore"}
            or not isinstance(base_revision, str)
            or not isinstance(facts, list)
            or (request_hash is not None and not isinstance(request_hash, str))
            or not isinstance(mutation_hash, str)
        ):
            raise MemoryConflictError("corrupt_memory_journal")
        entry_rows = cast(list[object], entry_ids)
        fact_rows = cast(list[object], facts)
        if any(not isinstance(entry, str) for entry in entry_rows) or any(
            not isinstance(fact, dict) for fact in fact_rows
        ):
            raise MemoryConflictError("corrupt_memory_journal")
        old_hash_map = cast(dict[str, object], old_hashes)
        new_hash_map = cast(dict[str, object], new_hashes)
        proposed_map = cast(dict[str, object], proposed)
        recovered_payload: dict[str, object] = {
            "operation_id": operation_id,
            "source": source,
            "base_revision": base_revision,
            "proposed_files": proposed_map,
            "facts": fact_rows,
            "request_hash": request_hash,
        }
        if _hash_text(_canonical_json(recovered_payload)) != mutation_hash:
            raise MemoryConflictError("corrupt_memory_journal")
        current = self._read_contents()
        for target in _TARGET_PATHS:
            old_hash = old_hash_map.get(target)
            new_hash = new_hash_map.get(target)
            content = proposed_map.get(target)
            if not all(isinstance(value, str) for value in (old_hash, new_hash, content)):
                raise MemoryConflictError("corrupt_memory_journal")
            actual = _hash_text(current[target])
            if actual not in {old_hash, new_hash}:
                raise MemoryConflictError("external_memory_change")
            if actual == old_hash and old_hash != new_hash:
                target_path = self._path(target)
                target_path.parent.mkdir(parents=True, exist_ok=True)
                write_text_atomic(target_path, cast(str, content), newline="")
        verified = self._snapshot_from(self._read_contents())
        if any(
            verified.file_hashes[target] != new_hash_map[target]
            for target in _TARGET_PATHS
        ):
            raise MemoryConflictError("memory_verify_failed")
        receipt_path = self._receipt_path(operation_id)
        if receipt_path.exists():
            receipt = self._load_json(
                receipt_path,
                reason_code="corrupt_memory_receipt",
                max_bytes=65_536,
            )
            self._result_from_receipt(receipt, replayed=True)
            if (
                receipt.get("version") != self._RECEIPT_VERSION
                or receipt.get("operation_id") != operation_id
                or receipt.get("payload_hash") != payload_hash
                or receipt.get("revision") != verified.revision
            ):
                raise MemoryConflictError("operation_id_conflict")
        else:
            self._write_receipt(
                operation_id,
                payload_hash,
                verified.revision,
                tuple(cast(list[str], entry_rows)),
            )
        path.unlink(missing_ok=True)

    def _recover_locked(self) -> None:
        for path in sorted(self.transactions_dir.glob("*.json")):
            if path.is_symlink() or not path.is_file():
                raise MemoryConflictError("unsafe_memory_journal")
            self._recover_one_locked(path)

    def commit(self, mutation: MemoryMutation) -> MemoryCommitResult:
        if not _OPERATION_RE.fullmatch(mutation.operation_id):
            return MemoryCommitResult(
                mutation.operation_id,
                "conflict",
                mutation.base_revision,
                (),
                reason_code="invalid_operation_id",
            )
        try:
            with self._lock:
                self._ensure_private_dirs()
                self._recover_locked()
                if set(mutation.proposed_files) != set(_TARGET_PATHS):
                    raise MemoryConflictError("invalid_targets")
                self.authorize(mutation)
                payload = self._mutation_payload(mutation)
                payload_json = _canonical_json(payload)
                payload_hash = self._idempotency_hash(mutation, payload_json)
                receipt_path = self._receipt_path(mutation.operation_id)
                if receipt_path.exists():
                    receipt = self._load_json(
                        receipt_path,
                        reason_code="corrupt_memory_receipt",
                        max_bytes=65_536,
                    )
                    if (
                        receipt.get("version") != self._RECEIPT_VERSION
                        or receipt.get("operation_id") != mutation.operation_id
                    ):
                        raise MemoryConflictError("corrupt_memory_receipt")
                    if receipt.get("payload_hash") != payload_hash:
                        raise MemoryConflictError("operation_id_conflict")
                    return self._result_from_receipt(receipt, replayed=True)

                current = self._snapshot_from(self._read_contents())
                if current.revision != mutation.base_revision:
                    raise MemoryConflictError("base_revision_conflict")
                proposed: dict[MemoryTarget, str] = {
                    target: mutation.proposed_files[target] for target in _TARGET_PATHS
                }
                proposed_snapshot = self._snapshot_from(proposed)
                self._validate_protected_entries(current, proposed_snapshot, mutation)
                new_hashes = {target: _hash_text(proposed[target]) for target in _TARGET_PATHS}
                entry_ids = tuple(fact.entry_id for fact in mutation.facts)
                journal = {
                    "version": self._JOURNAL_VERSION,
                    "operation_id": mutation.operation_id,
                    "payload_hash": payload_hash,
                    "mutation_hash": _hash_text(payload_json),
                    "source": mutation.source,
                    "base_revision": mutation.base_revision,
                    "facts": payload["facts"],
                    "request_hash": mutation.request_hash,
                    "old_hashes": dict(current.file_hashes),
                    "new_hashes": new_hashes,
                    "proposed_files": proposed,
                    "entry_ids": list(entry_ids),
                }
                journal_text = _canonical_json(journal)
                journal_bytes = len(journal_text.encode("utf-8"))
                if journal_bytes > self.transaction_max_bytes:
                    raise MemoryConflictError("transaction_too_large")
                existing_bytes = sum(
                    item.stat().st_size
                    for item in self.transactions_dir.glob("*.json")
                    if item.is_file() and not item.is_symlink()
                )
                if existing_bytes + journal_bytes > self.journal_max_bytes:
                    raise MemoryConflictError("journal_capacity_exceeded")
                journal_path = self._journal_path(mutation.operation_id)
                write_text_atomic(journal_path, journal_text, newline="")
                self._recover_one_locked(journal_path)
                receipt = self._load_json(
                    receipt_path,
                    reason_code="corrupt_memory_receipt",
                    max_bytes=65_536,
                )
                return self._result_from_receipt(receipt, replayed=False)
        except MemoryConflictError as exc:
            return MemoryCommitResult(
                mutation.operation_id,
                "conflict",
                mutation.base_revision,
                tuple(fact.entry_id for fact in mutation.facts),
                reason_code=exc.reason_code,
            )
        except PermissionError:
            return MemoryCommitResult(
                mutation.operation_id,
                "failed",
                mutation.base_revision,
                tuple(fact.entry_id for fact in mutation.facts),
                reason_code="authorization_denied",
            )
        except OSError:
            return MemoryCommitResult(
                mutation.operation_id,
                "failed",
                mutation.base_revision,
                tuple(fact.entry_id for fact in mutation.facts),
                reason_code="memory_io_failed",
            )

    def replace_file(
        self,
        target: MemoryTarget,
        content: str,
        *,
        source: MemoryMutationSource = "sdk",
    ) -> MemoryCommitResult:
        base = self.snapshot()
        if content == base.contents[target]:
            return MemoryCommitResult(
                operation_id=(
                    f"{source}:{target}:{base.revision[:16]}:{_hash_text(content)[:16]}"
                ),
                status="committed",
                revision=base.revision,
                entry_ids=tuple(fact.entry_id for fact in base.explicit_entries),
                replayed=True,
            )
        proposed = dict(base.contents)
        proposed[target] = content
        before = {(fact.target, fact.entry_id): fact for fact in base.explicit_entries}
        after = {
            (fact.target, fact.entry_id): fact
            for fact in self._snapshot_from(proposed).explicit_entries
        }
        if any(after.get(key) != fact for key, fact in before.items()):
            return MemoryCommitResult(
                operation_id=f"{source}:{target}:{_hash_text(content)[:24]}",
                status="conflict",
                revision=base.revision,
                entry_ids=tuple(fact.entry_id for fact in before.values()),
                reason_code="protected_entry_conflict",
            )
        operation_id = (
            f"{source}:{target}:{base.revision[:16]}:{_hash_text(content)[:16]}"
        )
        return self.commit(
            MemoryMutation(
                operation_id=operation_id,
                source=source,
                base_revision=base.revision,
                proposed_files=proposed,
            )
        )


def shared_memory_write_coordinator(
    root: Path,
    runtime_root: Path,
    *,
    authorize: Callable[[MemoryMutation], None],
    transaction_max_bytes: int = 1_048_576,
    journal_max_bytes: int = 67_108_864,
) -> MemoryWriteCoordinator:
    """Return the process owner for one canonical/runtime root pair."""
    key = (
        os.path.normcase(str(Path(os.path.abspath(root)))),
        os.path.normcase(str(Path(os.path.abspath(runtime_root)))),
        transaction_max_bytes,
        journal_max_bytes,
    )
    with _SHARED_WRITERS_GUARD:
        writer = _SHARED_WRITERS.get(key)
        if writer is None:
            writer = MemoryWriteCoordinator(
                root,
                runtime_root,
                authorize=authorize,
                transaction_max_bytes=transaction_max_bytes,
                journal_max_bytes=journal_max_bytes,
            )
            _SHARED_WRITERS[key] = writer
        return writer
