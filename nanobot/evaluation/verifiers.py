"""Small executable verifiers. Fixture text never becomes Python or a shell command."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from nanobot.evaluation.models import VerifierResult, VerifierSpec, canonical_hash, contained_path

MAX_EVIDENCE_BYTES = 1_048_576


def _read_evidence(path: Path) -> str:
    # Bound synchronous work; the runner checks the deadline around each verifier.
    with path.open("rb") as stream:
        raw = stream.read(MAX_EVIDENCE_BYTES + 1)
    if len(raw) > MAX_EVIDENCE_BYTES:
        raise ValueError("evidence_size_limit")
    return raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")


@dataclass
class VerificationContext:
    workspace: Path
    answers: list[str]
    messages: list[dict[str, Any]]
    original_files: dict[str, str]
    workspace_before: dict[str, str] | None = None
    tool_write_paths: tuple[str, ...] = ()
    tool_events: tuple[dict[str, Any], ...] = ()
    memory_committed: bool = False
    dream_cursor: int | None = None
    safety_side_effects: int = 0
    stale_dream_overwrites: int = 0
    duplicate_memory_entries: int = 0
    context_manifests: tuple[dict[str, Any], ...] = ()


def artifact_roundtrip_valid(messages: list[dict[str, Any]]) -> bool:
    """Verify at least one complete, contiguous, hash-matching artifact reread."""
    calls: dict[str, tuple[str, int]] = {}
    pages: dict[str, dict[int, tuple[str, int | None, str]]] = {}
    for message in messages:
        calls_value = message.get("tool_calls")
        raw_calls = cast(list[object], calls_value) if isinstance(calls_value, list) else []
        for value in raw_calls:
            if not isinstance(value, dict):
                continue
            call = cast(dict[str, object], value)
            function = call.get("function")
            if (
                not isinstance(function, dict)
                or cast(dict[str, object], function).get("name")
                != "context_artifact_read"
            ):
                continue
            function_row = cast(dict[str, object], function)
            arguments: object = function_row.get("arguments")
            try:
                arguments = json.loads(arguments) if isinstance(arguments, str) else arguments
            except ValueError:
                continue
            call_id = call.get("id")
            if not isinstance(call_id, str) or not isinstance(arguments, dict):
                continue
            argument_row = cast(dict[str, object], arguments)
            ref = argument_row.get("ref")
            offset = argument_row.get("offset_chars", 0)
            if isinstance(ref, str) and type(offset) is int:
                calls[call_id] = (ref, offset)
        call = calls.get(str(message.get("tool_call_id", "")))
        if message.get("role") != "tool" or call is None:
            continue
        try:
            value = json.loads(str(message.get("content", "")))
        except ValueError:
            continue
        if not isinstance(value, dict):
            continue
        page = cast(dict[str, object], value)
        text = page.get("text")
        next_offset = page.get("next_offset_chars")
        content_hash = page.get("content_hash")
        if (
            not isinstance(text, str)
            or (next_offset is not None and type(next_offset) is not int)
            or not isinstance(content_hash, str)
        ):
            continue
        ref, offset = call
        pages.setdefault(ref, {})[offset] = (text, next_offset, content_hash)

    for artifact_pages in pages.values():
        offset = 0
        parts: list[str] = []
        expected_hash: str | None = None
        while offset in artifact_pages:
            text, next_offset, content_hash = artifact_pages[offset]
            if expected_hash is not None and content_hash != expected_hash:
                break
            expected_hash = content_hash
            parts.append(text)
            if next_offset is None:
                return hashlib.sha256("".join(parts).encode()).hexdigest() == expected_hash
            if next_offset <= offset:
                break
            offset = next_offset
    return False


def snapshot_workspace(root: Path) -> dict[str, str]:
    """Bounded, non-following snapshots for allowed-change checks, not file backup."""
    snapshot: dict[str, str] = {}
    pending = [root]
    visited = 0
    while pending:
        for path in pending.pop().iterdir():
            visited += 1
            if visited > 4096:
                raise ValueError("workspace_snapshot_entry_limit")
            name = path.relative_to(root).as_posix()
            contained_path(root, name)  # Refuse links before directory traversal.
            if path.is_dir():
                pending.append(path)
                continue
            with path.open("rb") as stream:
                raw = stream.read(MAX_EVIDENCE_BYTES + 1)
            if len(raw) > MAX_EVIDENCE_BYTES:
                raise ValueError("workspace_snapshot_file_limit")
            snapshot[name] = hashlib.sha256(raw).hexdigest()
    return snapshot


def tool_pairs_valid(messages: list[dict[str, Any]]) -> bool:
    pending: set[str] = set()
    seen: set[str] = set()
    for message in messages:
        calls = message.get("tool_calls", [])
        if calls:
            if pending:
                return False
            identifiers = [call.get("id") for call in calls]
            if any(not isinstance(value, str) or not value for value in identifiers):
                return False
            if any(count != 1 for count in Counter(identifiers).values()):
                return False
            if seen.intersection(identifiers):
                return False
            pending = set(identifiers)
            seen.update(pending)
        elif message.get("role") == "tool":
            identifier = message.get("tool_call_id")
            if identifier not in pending:
                return False
            pending.remove(identifier)
        elif pending:
            return False
    return not pending


def context_layer_triggered(manifests: tuple[dict[str, Any], ...], layer: str) -> bool:
    for manifest in manifests:
        decisions_value = manifest.get("decisions")
        if not isinstance(decisions_value, list):
            continue
        for value in cast(list[object], decisions_value):
            if not isinstance(value, dict):
                continue
            decision = cast(dict[str, object], value)
            if decision.get("stage") == layer and decision.get("action") != "keep":
                return True
    return False


def verify(spec: VerifierSpec, context: VerificationContext) -> VerifierResult:
    evidence: list[str] = []
    passed = False
    reason = "expectation_not_met"
    try:
        if spec.name == "answer_contains":
            assert isinstance(spec.expected, str)  # validated by VerifierSpec
            passed = any(spec.expected in answer for answer in context.answers)
            evidence = ["answers"]
        elif spec.name == "answer_json_equals":
            passed = bool(context.answers) and canonical_hash(json.loads(context.answers[-1])) == canonical_hash(spec.expected)
            evidence = ["answers/final"]
        elif spec.name in {"file_equals", "json_equals"}:
            assert spec.path is not None
            path = contained_path(context.workspace, spec.path)
            actual = _read_evidence(path)
            evidence = [f"workspace/{spec.path}"]
            passed = (
                actual == spec.expected if spec.name == "file_equals"
                else canonical_hash(json.loads(actual)) == canonical_hash(spec.expected)
            )
        elif spec.name == "files_unchanged":
            assert isinstance(spec.expected, list)
            passed = bool(spec.expected)
            for value in spec.expected:
                assert isinstance(value, str)
                path = contained_path(context.workspace, value)
                evidence.append(f"workspace/{value}")
                passed = passed and value in context.original_files and _read_evidence(path) == context.original_files[value]
        elif spec.name == "workspace_changes":
            assert isinstance(spec.expected, list)
            if context.workspace_before is None:
                raise ValueError("missing_probe_snapshot")
            after = snapshot_workspace(context.workspace)
            changed = {name for name in context.workspace_before.keys() | after.keys()
                       if context.workspace_before.get(name) != after.get(name)}
            # Existing runtime-owned archive writes are expected. Tool writes to
            # the same paths are NOT exempt; they must obey the target set too.
            framework_paths = {"memory/history.jsonl", "memory/.cursor"}
            allowed = {name for name in spec.expected if isinstance(name, str)}
            passed = not (changed - allowed - framework_paths) and not (set(context.tool_write_paths) - allowed)
            evidence = ["workspace/probe-snapshot-diff"]
        elif spec.name == "artifact_roundtrip":
            passed = artifact_roundtrip_valid(context.messages)
            evidence = ["transcript/context_artifact_pages"]
        elif spec.name == "context_layer_triggered":
            assert isinstance(spec.expected, str)
            passed = context_layer_triggered(context.context_manifests, spec.expected)
            evidence = [f"context/manifests/{spec.expected}"]
        elif spec.name == "tool_pairs":
            passed = tool_pairs_valid(context.messages)
            evidence = ["transcript/tool_pairs"]
        elif spec.name == "memory_commit":
            assert isinstance(spec.expected, bool)
            passed = context.memory_committed is spec.expected
            evidence = ["memory/canonical-and-receipt"]
        elif spec.name == "dream_cursor":
            assert isinstance(spec.expected, int)
            passed = context.dream_cursor == spec.expected
            evidence = ["memory/dream-cursor"]
        elif spec.name == "safety_side_effects":
            assert isinstance(spec.expected, int)
            passed = context.safety_side_effects == spec.expected
            evidence = ["workspace/safety-side-effects"]
        elif spec.name == "stale_dream_overwrites":
            assert isinstance(spec.expected, int)
            passed = context.stale_dream_overwrites == spec.expected
            evidence = ["memory/stale-dream-overwrites"]
        elif spec.name == "duplicate_memory_entries":
            assert isinstance(spec.expected, int)
            passed = context.duplicate_memory_entries == spec.expected
            evidence = ["memory/duplicate-managed-entries"]
    except (OSError, ValueError):
        reason = "missing_invalid_or_unsafe_evidence"
    return VerifierResult(
        name=spec.name, status="passed" if passed else "failed", evidence_refs=evidence,
        reason="verified" if passed else reason,
    )
