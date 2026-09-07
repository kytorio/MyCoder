"""Memory storage, transcript archiving, and session checkpoint consolidation."""

# Tool schemas are installed by the ``@tool_parameters`` class decorator at
# runtime; static analyzers cannot observe that it clears ``parameters`` from
# ``__abstractmethods__`` before these classes are instantiated.
# pyright: reportAbstractUsage=false, reportPrivateUsage=false

from __future__ import annotations

import asyncio
import json
import os
import re
import threading
import weakref
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING, Any, Callable, Iterator, cast

from loguru import logger

from nanobot.agent.context_plan import (
    ContextSummaryCandidate,
    StructuredContextSummary,
    render_context_summary,
)
from nanobot.agent.tools.base import Tool, ToolResult
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.llm_usage.context import llm_usage_source
from nanobot.providers.base import ProviderCallContext, ProviderConversationState
from nanobot.runtime_context import public_history_messages
from nanobot.session.manager import (
    MIN_COMPACTED_REPLAY_MESSAGES,
    Session,
    SessionManager,
)
from nanobot.session.summary import session_summary_from_metadata
from nanobot.utils.gitstore import GitStore
from nanobot.utils.helpers import (
    content_with_media_breadcrumbs,
    ensure_dir,
    estimate_prompt_tokens_chain,
    strip_think,
    truncate_text,
    truncate_text_to_tokens,
    write_text_atomic,
)
from nanobot.utils.prompt_templates import render_template
from nanobot.utils.workspace_prompts import (
    WORKSPACE_PROMPT_MAX_CHARS,
    has_workspace_prompt_override,
    load_workspace_prompt_override,
    workspace_prompt_file,
)

if TYPE_CHECKING:
    from nanobot.agent.memory_writes import (
        MemoryCommitResult,
        MemoryRememberRequested,
        MemoryTarget,
        MemoryWriteCoordinator,
    )
    from nanobot.utils.llm_runtime import LLMRuntime

# ---------------------------------------------------------------------------
# MemoryStore — pure file I/O layer
# ---------------------------------------------------------------------------


class MemoryStore:
    """Pure file I/O for memory files: MEMORY.md, history.jsonl, SOUL.md, USER.md."""

    _DEFAULT_MAX_HISTORY = 1000
    # Durable files whose real working-tree delta grounds Dream commit messages.
    # Deliberately excludes memory/.dream_cursor so progress bookkeeping never
    # appears as a durable-memory edit in the audit record.
    _DREAM_CONTENT_PATHS = ("SOUL.md", "USER.md", "memory/MEMORY.md")
    _LEGACY_ENTRY_START_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2}[^\]]*)\]\s*")
    _LEGACY_TIMESTAMP_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2})\]\s*")
    _LEGACY_RAW_MESSAGE_RE = re.compile(
        r"^\[\d{4}-\d{2}-\d{2}[^\]]*\]\s+[A-Z][A-Z0-9_]*(?:\s+\[tools:\s*[^\]]+\])?:"
    )

    def __init__(
        self,
        workspace: Path,
        max_history_entries: int = _DEFAULT_MAX_HISTORY,
        *,
        writer: MemoryWriteCoordinator | None = None,
    ):
        self.workspace = workspace
        self.max_history_entries = max_history_entries
        self.memory_dir = ensure_dir(workspace / "memory")
        self.memory_file = self.memory_dir / "MEMORY.md"
        self.history_file = self.memory_dir / "history.jsonl"
        self.legacy_history_file = self.memory_dir / "HISTORY.md"
        self.soul_file = workspace / "SOUL.md"
        self.user_file = workspace / "USER.md"
        self._cursor_file = self.memory_dir / ".cursor"
        self._dream_cursor_file = self.memory_dir / ".dream_cursor"
        self._corruption_logged = False  # rate-limit invalid cursor warning
        self._malformed_entry_logged = False  # rate-limit bad history shape warning
        self._oversize_logged = False  # rate-limit oversized-entry warning
        self._dream_prompt_oversize_logged = False
        self._append_lock = threading.Lock()  # serialize cursor allocation + append
        self.writer = writer
        self._git = GitStore(workspace, tracked_files=[
            "SOUL.md", "USER.md", "memory/MEMORY.md", "memory/.dream_cursor",
        ])
        self._maybe_migrate_legacy_history()

    @property
    def git(self) -> GitStore:
        return self._git

    # -- generic helpers -----------------------------------------------------

    @staticmethod
    def read_file(path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ""

    def _maybe_migrate_legacy_history(self) -> None:
        """One-time upgrade from legacy HISTORY.md to history.jsonl.

        The migration is best-effort and prioritizes preserving as much content
        as possible over perfect parsing.
        """
        if not self.legacy_history_file.exists():
            return
        if self.history_file.exists() and self.history_file.stat().st_size > 0:
            return

        try:
            legacy_text = self.legacy_history_file.read_text(
                encoding="utf-8",
                errors="replace",
            )
        except OSError:
            logger.exception("Failed to read legacy HISTORY.md for migration")
            return

        entries = self._parse_legacy_history(legacy_text)
        try:
            if entries:
                self._write_entries(entries)
                last_cursor = entries[-1]["cursor"]
                self._cursor_file.write_text(str(last_cursor), encoding="utf-8")
                # Default to "already processed" so upgrades do not replay the
                # user's entire historical archive into Dream on first start.
                self._dream_cursor_file.write_text(str(last_cursor), encoding="utf-8")

            backup_path = self._next_legacy_backup_path()
            self.legacy_history_file.replace(backup_path)
            logger.info(
                "Migrated legacy HISTORY.md to history.jsonl ({} entries)",
                len(entries),
            )
        except Exception:
            logger.exception("Failed to migrate legacy HISTORY.md")

    def _parse_legacy_history(self, text: str) -> list[dict[str, Any]]:
        normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()
        if not normalized:
            return []

        fallback_timestamp = self._legacy_fallback_timestamp()
        entries: list[dict[str, Any]] = []
        chunks = self._split_legacy_history_chunks(normalized)

        for cursor, chunk in enumerate(chunks, start=1):
            timestamp = fallback_timestamp
            content = chunk
            match = self._LEGACY_TIMESTAMP_RE.match(chunk)
            if match:
                timestamp = match.group(1)
                remainder = chunk[match.end():].lstrip()
                if remainder:
                    content = remainder

            entries.append({
                "cursor": cursor,
                "timestamp": timestamp,
                "content": content,
            })
        return entries

    def _split_legacy_history_chunks(self, text: str) -> list[str]:
        lines = text.split("\n")
        chunks: list[str] = []
        current: list[str] = []
        saw_blank_separator = False

        for line in lines:
            if saw_blank_separator and line.strip() and current:
                chunks.append("\n".join(current).strip())
                current = [line]
                saw_blank_separator = False
                continue
            if self._should_start_new_legacy_chunk(line, current):
                chunks.append("\n".join(current).strip())
                current = [line]
                saw_blank_separator = False
                continue
            current.append(line)
            saw_blank_separator = not line.strip()

        if current:
            chunks.append("\n".join(current).strip())
        return [chunk for chunk in chunks if chunk]

    def _should_start_new_legacy_chunk(self, line: str, current: list[str]) -> bool:
        if not current:
            return False
        if not self._LEGACY_ENTRY_START_RE.match(line):
            return False
        if self._is_raw_legacy_chunk(current) and self._LEGACY_RAW_MESSAGE_RE.match(line):
            return False
        return True

    def _is_raw_legacy_chunk(self, lines: list[str]) -> bool:
        first_nonempty = next((line for line in lines if line.strip()), "")
        match = self._LEGACY_TIMESTAMP_RE.match(first_nonempty)
        if not match:
            return False
        return first_nonempty[match.end():].lstrip().startswith("[RAW]")

    def _legacy_fallback_timestamp(self) -> str:
        try:
            return datetime.fromtimestamp(
                self.legacy_history_file.stat().st_mtime,
            ).strftime("%Y-%m-%d %H:%M")
        except OSError:
            return datetime.now().strftime("%Y-%m-%d %H:%M")

    def _next_legacy_backup_path(self) -> Path:
        candidate = self.memory_dir / "HISTORY.md.bak"
        suffix = 2
        while candidate.exists():
            candidate = self.memory_dir / f"HISTORY.md.bak.{suffix}"
            suffix += 1
        return candidate

    # -- MEMORY.md (long-term facts) -----------------------------------------

    def read_memory(self, *, project: Path | None = None) -> str:
        content = self.read_file(self.memory_file)
        if project is None or not content:
            return content
        from nanobot.agent.memory_writes import (
            memory_instance_id,
            memory_project_id,
            visible_memory_content,
        )

        return visible_memory_content(
            content,
            instance_id=memory_instance_id(self.workspace),
            project_id=memory_project_id(project),
        )

    def write_memory(self, content: str) -> None:
        self._write_canonical("memory", content)

    # -- SOUL.md -------------------------------------------------------------

    def read_soul(self) -> str:
        return self.read_file(self.soul_file)

    def write_soul(self, content: str) -> None:
        self._write_canonical("soul", content)

    # -- USER.md -------------------------------------------------------------

    def read_user(self) -> str:
        return self.read_file(self.user_file)

    def write_user(self, content: str) -> None:
        self._write_canonical("user", content)

    def _write_canonical(self, target: MemoryTarget, content: str) -> None:
        """Preserve the synchronous API while enforcing managed-entry protection."""
        if self.writer is None:
            path = {
                "memory": self.memory_file,
                "soul": self.soul_file,
                "user": self.user_file,
            }[target]
            write_text_atomic(path, content)
            return
        from nanobot.agent.memory_writes import MemoryConflictError

        result = self.writer.replace_file(target, content, source="sdk")
        if result.status == "conflict":
            raise MemoryConflictError(result.reason_code or "memory_conflict")
        if result.status != "committed":
            raise OSError(result.reason_code or "memory_write_failed")

    def remember(self, request: MemoryRememberRequested) -> MemoryCommitResult:
        """Commit an explicit request through the configured recoverable writer."""
        if self.writer is None:
            from nanobot.agent.memory_writes import MemoryWriteUnavailableError

            raise MemoryWriteUnavailableError("memory_writer_unavailable")
        from nanobot.agent.memory_writes import build_explicit_mutation

        base = self.writer.snapshot()
        return self.writer.commit(build_explicit_mutation(request, base))

    # -- context injection (used by context.py) ------------------------------

    def get_memory_context(self, *, project: Path | None = None) -> str:
        long_term = self.read_memory(project=project)
        return f"## Long-term Memory\n{long_term}" if long_term else ""

    # -- history.jsonl — append-only, JSONL format ---------------------------

    def _normalize_history_entry(
        self,
        entry: str,
        *,
        max_chars: int | None = None,
    ) -> str:
        """Return the exact bounded, model-safe text accepted by the journal."""
        limit = max_chars if max_chars is not None else _HISTORY_ENTRY_HARD_CAP
        raw = entry.rstrip()
        content = strip_think(raw)
        if len(content) > limit:
            if not self._oversize_logged:
                self._oversize_logged = True
                logger.warning(
                    "history entry exceeds {} chars ({}); truncating. "
                    "Usually means a caller forgot its own cap; "
                    "further occurrences suppressed.",
                    limit,
                    len(content),
                )
            content = truncate_text(content, limit)
        return content

    def append_history(
        self,
        entry: str,
        *,
        max_chars: int | None = None,
        session_key: str | None = None,
    ) -> int:
        """Append *entry* to history.jsonl and return its auto-incrementing cursor.

        Entries are passed through `strip_think` to drop template-level leaks
        (e.g. unclosed `<think` prefixes, `<channel|>` markers) before being
        persisted. If the cleaned content is empty but the raw entry wasn't,
        the record is persisted with an empty string rather than falling back
        to the raw leak — otherwise `strip_think`'s guarantees would be
        undone when Dream consumes the journal entry.

        A defensive cap (*max_chars*, default ``_HISTORY_ENTRY_HARD_CAP``) is
        applied as a final safety net: individual callers should cap their own
        content more tightly; this default only exists to catch unintentional
        large writes (e.g. an LLM echoing its input back as a "summary").
        """
        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        raw = entry.rstrip()
        content = self._normalize_history_entry(entry, max_chars=max_chars)
        # Cursor allocation and the append must be atomic: concurrent writers
        # could otherwise read the same current cursor and emit duplicates.
        with self._append_lock:
            cursor = self._next_cursor()
            if raw and not content:
                logger.debug(
                    "history entry {} stripped to empty (likely template leak); "
                    "persisting empty content to avoid re-polluting Dream input",
                    cursor,
                )
            record = {"cursor": cursor, "timestamp": ts, "content": content}
            if session_key:
                record["session_key"] = session_key
            with open(self.history_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
            self._cursor_file.write_text(str(cursor), encoding="utf-8")
        return cursor

    @staticmethod
    def _valid_cursor(value: Any) -> int | None:
        """Non-negative int cursors only; reject bool (``isinstance(True, int)`` is True)."""
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None
        return value

    def _iter_valid_entries(self) -> Iterator[tuple[dict[str, Any], int]]:
        """Yield ``(entry, cursor)`` for well-formed entries; warn once on corruption."""
        poisoned: Any = None
        malformed_cursor: int | None = None
        for entry in self._read_entries():
            raw = entry.get("cursor")
            if raw is None:
                continue
            cursor = self._valid_cursor(raw)
            if cursor is None:
                poisoned = raw
                continue
            if not self._valid_history_payload(entry):
                malformed_cursor = cursor
                continue
            yield entry, cursor
        if poisoned is not None and not self._corruption_logged:
            self._corruption_logged = True
            logger.warning(
                "history.jsonl contains an invalid cursor ({!r}); dropping it. "
                "Usually caused by an external writer; further occurrences suppressed.",
                poisoned,
            )
        if malformed_cursor is not None and not self._malformed_entry_logged:
            self._malformed_entry_logged = True
            logger.warning(
                "history.jsonl contains a malformed entry at cursor {}; dropping it. "
                "Usually caused by an external writer; further occurrences suppressed.",
                malformed_cursor,
            )

    @staticmethod
    def _valid_history_payload(entry: dict[str, Any]) -> bool:
        if not isinstance(entry.get("timestamp"), str):
            return False
        if not isinstance(entry.get("content"), str):
            return False
        session_key = entry.get("session_key")
        return session_key is None or isinstance(session_key, str)

    def _read_cursor_counter(self) -> int | None:
        """Return the persisted cursor counter when it is usable."""
        if not self._cursor_file.exists():
            return None
        with suppress(ValueError, OSError):
            cursor = int(self._cursor_file.read_text(encoding="utf-8").strip())
            if cursor >= 0:
                return cursor
        return None

    def _next_cursor(self) -> int:
        """Read the current cursor counter and return the next value."""
        cursor_counter = self._read_cursor_counter()
        last = self._read_last_entry() or {}
        last_cursor = self._valid_cursor(last.get("cursor"))
        if cursor_counter is not None:
            if last_cursor is not None:
                return max(cursor_counter, last_cursor) + 1
            max_history_cursor = max((c for _, c in self._iter_valid_entries()), default=0)
            return max(cursor_counter, max_history_cursor) + 1

        # Fast path: trust the tail when intact.  Otherwise scan the whole
        # file and take ``max`` — that stays correct even if the monotonic
        # invariant was broken by external writes.
        if last_cursor is not None:
            return last_cursor + 1
        return max((c for _, c in self._iter_valid_entries()), default=0) + 1

    def read_unprocessed_history(self, since_cursor: int) -> list[dict[str, Any]]:
        """Return history entries with a valid cursor > *since_cursor*."""
        return [e for e, c in self._iter_valid_entries() if c > since_cursor]

    def compact_history(self) -> None:
        """Drop oldest processed entries without discarding pending Dream input."""
        if self.max_history_entries <= 0:
            return
        entries = self._read_entries()
        if len(entries) <= self.max_history_entries:
            return
        last_dream_cursor = self.get_last_dream_cursor()
        first_unprocessed = next(
            (
                index
                for index, entry in enumerate(entries)
                if (
                    (cursor := self._valid_cursor(entry.get("cursor"))) is not None
                    and cursor > last_dream_cursor
                )
            ),
            len(entries),
        )
        keep_from = min(len(entries) - self.max_history_entries, first_unprocessed)
        kept = entries[keep_from:]
        if len(kept) > self.max_history_entries:
            logger.warning(
                "History compaction retained {} unprocessed entries beyond the configured "
                "limit of {}",
                len(kept),
                self.max_history_entries,
            )
        self._write_entries(kept)

    # -- JSONL helpers -------------------------------------------------------

    def _read_entries(self) -> list[dict[str, Any]]:
        """Read all entries from history.jsonl."""
        entries: list[dict[str, Any]] = []
        with suppress(FileNotFoundError):
            with open(self.history_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            parsed: object = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if isinstance(parsed, dict):
                            entries.append(cast(dict[str, Any], parsed))

        return entries

    def _read_last_entry(self) -> dict[str, Any] | None:
        """Read the last entry from the JSONL file efficiently."""
        try:
            with open(self.history_file, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                if size == 0:
                    return None
                read_size = min(size, 4096)
                f.seek(size - read_size)
                data = f.read().decode("utf-8")
                lines = [line for line in data.split("\n") if line.strip()]
                if not lines:
                    return None
                parsed: object = json.loads(lines[-1])
                return cast(dict[str, Any], parsed) if isinstance(parsed, dict) else None
        except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError):
            return None

    def _write_entries(self, entries: list[dict[str, Any]]) -> None:
        """Overwrite history.jsonl with the given entries (atomic write)."""
        tmp_path = self.history_file.with_suffix(self.history_file.suffix + ".tmp")
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                for entry in entries:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self.history_file)

            # fsync the directory so the rename is durable.
            # On Windows, opening a directory with O_RDONLY raises
            # PermissionError — skip the dir sync there (NTFS
            # journals metadata synchronously).
            with suppress(PermissionError):
                fd = os.open(str(self.history_file.parent), os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise

    # -- dream cursor --------------------------------------------------------

    def get_last_dream_cursor(self) -> int:
        if self._dream_cursor_file.exists():
            with suppress(ValueError, OSError):
                return int(self._dream_cursor_file.read_text(encoding="utf-8").strip())
        return 0

    def set_last_dream_cursor(self, cursor: int) -> None:
        if cursor < 0:
            raise ValueError("dream cursor must be non-negative")
        write_text_atomic(self._dream_cursor_file, str(cursor), newline="")

    def get_latest_cursor(self) -> int:
        return max(self._next_cursor() - 1, 0)

    @property
    def dream_prompt_file(self) -> Path:
        return workspace_prompt_file(self.workspace, "dream")

    def has_dream_prompt_override(self) -> bool:
        return has_workspace_prompt_override(self.dream_prompt_file)

    @staticmethod
    def default_dream_prompt() -> str:
        from nanobot.agent.skills import BUILTIN_SKILLS_DIR

        return render_template(
            "agent/dream.md",
            strip=True,
            skill_creator_path=str(BUILTIN_SKILLS_DIR / "skill-creator" / "SKILL.md"),
        )

    def _dream_template(self) -> str:
        text, original_chars = load_workspace_prompt_override(self.dream_prompt_file)
        if text is not None:
            if (
                original_chars > WORKSPACE_PROMPT_MAX_CHARS
                and not self._dream_prompt_oversize_logged
            ):
                self._dream_prompt_oversize_logged = True
                logger.warning(
                    "workspace Dream prompt exceeds {} chars ({}); truncating. "
                    "Further occurrences suppressed.",
                    WORKSPACE_PROMPT_MAX_CHARS, original_chars,
                )
            return text
        return self.default_dream_prompt()

    def build_dream_prompt(self, *, max_entries: int = 20) -> tuple[str, int] | None:
        """Build the Dream prompt with unprocessed history context.

        Returns ``(prompt, last_cursor)`` or ``None`` if nothing to process.

        The current contents of the durable memory files (SOUL.md, USER.md,
        memory/MEMORY.md) reach Dream through the normal agent system context.
        """
        last_cursor = self.get_last_dream_cursor()
        entries = self.read_unprocessed_history(since_cursor=last_cursor)
        if not entries:
            return None

        batch = entries[:max_entries]
        history_text = "\n".join(
            f"[{e['timestamp']}] {truncate_text(e['content'], 1000)}"
            for e in batch
        )
        template = self._dream_template()
        prompt = f"{template}\n\n## Conversation History\n{history_text}"
        return (prompt, batch[-1]["cursor"])

    def dream_content_diff(self) -> str:
        """Structured summary of uncommitted changes to the durable memory files.

        Returns "" when git is unavailable or no content file changed. This is
        the ground-truth input for diff-grounded Dream commit messages.
        """
        if not self._git.is_initialized():
            return ""
        return self._git.summarize_working_tree(list(self._DREAM_CONTENT_PATHS))

    @staticmethod
    def _canonical_relative_path(target: MemoryTarget) -> str:
        return {
            "user": "USER.md",
            "soul": "SOUL.md",
            "memory": "memory/MEMORY.md",
        }[target]

    def _canonical_target_for_path(self, path: Path) -> MemoryTarget | None:
        candidate = Path(os.path.abspath(path))
        for target in ("user", "soul", "memory"):
            canonical = Path(
                os.path.abspath(self.workspace / self._canonical_relative_path(target))
            )
            if candidate == canonical:
                return target
        return None

    def _dream_file_bases(self) -> dict[MemoryTarget, _DreamFileBase]:
        contents: Mapping[MemoryTarget, str]
        if self.writer is not None:
            contents = self.writer.snapshot().contents
        else:
            contents = {
                "user": self.read_user(),
                "soul": self.read_soul(),
                "memory": self.read_memory(),
            }
        return {
            target: _DreamFileBase(
                content=contents[target],
                exists=(self.workspace / self._canonical_relative_path(target)).exists(),
            )
            for target in ("user", "soul", "memory")
        }

    def _commit_dream_files(
        self,
        bases: dict[MemoryTarget, str],
        proposed: dict[MemoryTarget, str],
    ) -> tuple[bool, str | None]:
        """Merge and commit one Dream tool call, retrying one version race."""
        from nanobot.agent.memory_writes import (
            MemoryConflictError,
            MemoryMutation,
            merge_unmanaged_text,
        )

        attempts = 2 if self.writer is not None else 1
        for attempt in range(attempts):
            try:
                if self.writer is not None:
                    latest = self.writer.snapshot()
                    current: dict[MemoryTarget, str] = dict(latest.contents)
                    base_revision = latest.revision
                else:
                    current = {
                        "user": self.read_user(),
                        "soul": self.read_soul(),
                        "memory": self.read_memory(),
                    }
                    base_revision = "legacy"
                merged: dict[MemoryTarget, str] = dict(current)
                for target, candidate in proposed.items():
                    merged[target] = merge_unmanaged_text(
                        bases[target],
                        current[target],
                        candidate,
                    )
            except MemoryConflictError as exc:
                return False, exc.reason_code

            if self.writer is None:
                for target in proposed:
                    path = self.workspace / self._canonical_relative_path(target)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    write_text_atomic(path, merged[target], newline="")
                return True, None
            if merged == current:
                return True, None
            identity = json.dumps(
                {"base_revision": base_revision, "proposed": merged},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            result = self.writer.commit(
                MemoryMutation(
                    operation_id=f"dream:{sha256(identity.encode()).hexdigest()[:32]}",
                    source="dream",
                    base_revision=base_revision,
                    proposed_files=merged,
                )
            )
            if result.status == "committed":
                return True, None
            if result.reason_code == "base_revision_conflict" and attempt == 0:
                continue
            return False, result.reason_code or "dream_memory_commit_failed"
        return False, "base_revision_conflict"

    def restore_dream_version(self, commit: str) -> MemoryCommitResult | None:
        """Apply a Dream parent-tree candidate only after protected-entry checks."""
        from nanobot.agent.memory_writes import (
            MemoryCommitResult,
            MemoryConflictError,
            MemoryMutation,
            validate_protected_entries_preserved,
        )

        candidate = self.git.read_revert_candidate(commit, message_prefix="dream:")
        if candidate is None:
            return None
        _committed_files, parent_files = candidate
        cursor_text = parent_files.get("memory/.dream_cursor", "0").strip() or "0"
        try:
            cursor = int(cursor_text)
            if cursor < 0:
                raise ValueError
        except ValueError:
            return MemoryCommitResult(
                operation_id=f"restore:{commit}",
                status="conflict",
                revision="unknown",
                entry_ids=(),
                reason_code="invalid_dream_cursor",
            )

        for attempt in range(2 if self.writer is not None else 1):
            if self.writer is not None:
                latest = self.writer.snapshot()
                current: dict[MemoryTarget, str] = dict(latest.contents)
                revision = latest.revision
            else:
                current = {
                    "user": self.read_user(),
                    "soul": self.read_soul(),
                    "memory": self.read_memory(),
                }
                revision = "legacy"
            proposed: dict[MemoryTarget, str] = dict(current)
            try:
                for target in ("user", "soul", "memory"):
                    restored = parent_files.get(
                        self._canonical_relative_path(target),
                        "",
                    )
                    validate_protected_entries_preserved(current[target], restored)
                    proposed[target] = restored
            except MemoryConflictError as exc:
                return MemoryCommitResult(
                    operation_id=f"restore:{commit}",
                    status="conflict",
                    revision=revision,
                    entry_ids=(),
                    reason_code=exc.reason_code,
                )

            operation_id = f"restore:{commit}:{sha256(json.dumps(proposed, sort_keys=True).encode()).hexdigest()[:24]}"
            if self.writer is None:
                for target in ("user", "soul", "memory"):
                    self._write_canonical(target, proposed[target])
                result = MemoryCommitResult(
                    operation_id=operation_id,
                    status="committed",
                    revision=revision,
                    entry_ids=(),
                )
            else:
                result = self.writer.commit(
                    MemoryMutation(
                        operation_id=operation_id,
                        source="restore",
                        base_revision=revision,
                        proposed_files=proposed,
                    )
                )
                if result.reason_code == "base_revision_conflict" and attempt == 0:
                    continue
            if result.status == "committed":
                self.set_last_dream_cursor(cursor)
            return result
        return MemoryCommitResult(
            operation_id=f"restore:{commit}",
            status="conflict",
            revision="unknown",
            entry_ids=(),
            reason_code="base_revision_conflict",
        )

    def build_dream_tools(self) -> ToolRegistry:
        """Build the restricted tool registry used by Dream runs."""
        from nanobot.agent.skills import BUILTIN_SKILLS_DIR
        from nanobot.agent.tools.apply_patch import ApplyPatchTool
        from nanobot.agent.tools.file_state import FileStates
        from nanobot.agent.tools.filesystem import EditFileTool, ReadFileTool, WriteFileTool
        file_states = FileStates()
        workspace = self.workspace
        skills_dir = workspace / "skills"
        skills_dir.mkdir(parents=True, exist_ok=True)

        extra_read = [BUILTIN_SKILLS_DIR] if BUILTIN_SKILLS_DIR.exists() else None
        editable_files = [self.memory_file, self.soul_file, self.user_file]
        state = _DreamWriteState(self._dream_file_bases())
        tools = _DreamToolRegistry(state)

        tools.register(_DreamToolAdapter(self, state, ReadFileTool(
            workspace=workspace,
            allowed_dir=workspace,
            extra_read_allowed_dirs=extra_read,
            file_states=file_states,
        )))
        tools.register(_DreamToolAdapter(self, state, EditFileTool(
            workspace=workspace,
            allowed_dir=skills_dir,
            extra_write_allowed_files=editable_files,
            file_states=file_states,
        )))
        tools.register(_DreamToolAdapter(self, state, ApplyPatchTool(
            workspace=workspace,
            allowed_dir=skills_dir,
            extra_write_allowed_files=editable_files,
            file_states=file_states,
        )))
        tools.register(_DreamToolAdapter(self, state, WriteFileTool(
            workspace=workspace,
            allowed_dir=skills_dir,
            extra_write_allowed_files=editable_files,
            file_states=file_states,
        )))
        return tools

    @staticmethod
    def dream_tools_completed(tools: ToolRegistry | None) -> bool:
        return not isinstance(tools, _DreamToolRegistry) or tools.writes_resolved

    @staticmethod
    def dream_run_completed(
        resp: object | None,
    ) -> bool:
        """Return True when the Dream agent reached a normal terminal response."""
        metadata = getattr(resp, "metadata", None)
        if not isinstance(metadata, dict):
            return False
        return cast(dict[str, Any], metadata).get("_stop_reason") == "completed"

    @staticmethod
    def dream_incompletion_reason(
        resp: object | None,
        tools: ToolRegistry | None = None,
    ) -> str:
        """Human-readable explanation of why a Dream run cannot advance."""
        if isinstance(tools, _DreamToolRegistry) and not tools.writes_resolved:
            return "unresolved memory write: " + ", ".join(tools.unresolved_writes)
        metadata = getattr(resp, "metadata", None)
        if isinstance(metadata, dict):
            stop_reason = cast(dict[str, Any], metadata).get("_stop_reason", "unknown")
        else:
            stop_reason = "missing response metadata"
        return f"stop_reason: {stop_reason}"

    # -- message formatting utility ------------------------------------------

    @staticmethod
    def _format_messages(messages: list[dict[str, Any]]) -> str:
        lines: list[str] = []
        for message in messages:
            content = content_with_media_breadcrumbs(
                message.get("role"),
                message.get("content", ""),
                message.get("media"),
            )
            if not content:
                continue
            tools_used = message.get("tools_used")
            tools = (
                f" [tools: {', '.join(cast(list[str], tools_used))}]"
                if tools_used
                else ""
            )
            raw_timestamp = message.get("timestamp")
            timestamp = str(raw_timestamp) if raw_timestamp is not None else "?"
            role = str(message.get("role") or "unknown")
            lines.append(f"[{timestamp[:16]}] {role.upper()}{tools}: {content}")
        return "\n".join(lines)

    def raw_archive(
        self,
        messages: list[dict[str, Any]],
        *,
        max_chars: int | None = None,
        session_key: str | None = None,
    ) -> str:
        """Persist and return a bounded raw checkpoint when summarization degrades."""
        checkpoint = self._build_raw_checkpoint(messages, max_chars=max_chars)
        self.append_history(checkpoint, session_key=session_key)
        logger.warning(
            "Memory consolidation degraded: raw-archived {} messages", len(messages)
        )
        return checkpoint

    def _build_raw_checkpoint(
        self,
        messages: list[dict[str, Any]],
        *,
        max_chars: int | None = None,
    ) -> str:
        """Build the same bounded checkpoint as :meth:`raw_archive` without writing it."""
        limit = max_chars if max_chars is not None else _RAW_ARCHIVE_MAX_CHARS
        checkpoint = (
            f"[RAW] {len(messages)} messages\n"
            f"{self._format_messages(public_history_messages(messages))}"
        )
        return self._normalize_history_entry(checkpoint, max_chars=limit)

    # ------------------------------------------------------------------
    # Dream helpers
    # ------------------------------------------------------------------

    @staticmethod
    def dream_session_key() -> str:
        """Return a unique session key for a Dream run, e.g. ``dream:20260528-100000``."""
        return f"dream:{datetime.now():%Y%m%d-%H%M%S}"

    @staticmethod
    def build_dream_commit_message(prefix: str, diff_body: str) -> str:
        """Build a Dream commit message grounded in the real working-tree diff.

        *diff_body* is a structured, machine-derived summary of the actual file
        changes (see :meth:`dream_content_diff` /
        :meth:`GitStore.summarize_working_tree`). The LLM narrative is
        deliberately excluded so the audit record (``/dream-log``) reflects the
        filesystem's truth, not the model's self-report.

        An empty *diff_body* yields the bare *prefix*, which ``auto_commit``
        turns into a no-op when there is nothing to stage.
        """
        diff_body = (diff_body or "").strip()
        if not diff_body:
            return prefix
        return f"{prefix}\n\n{diff_body}"

    @staticmethod
    def prune_dream_sessions(sessions: SessionManager, *, keep: int = 10) -> None:
        """Remove the oldest Dream session files, keeping only the N most recent.

        Only current base64url-encoded Dream session keys are considered.
        Non-dream session files are never touched.
        """
        with sessions.locked_session_files() as sessions_dir:
            dream_files: list[tuple[Path, str]] = []
            for path in sessions_dir.glob("*.jsonl"):
                decoded_key = SessionManager.decode_storage_key(path.stem)
                if decoded_key is not None and decoded_key.startswith("dream:"):
                    dream_files.append((path, decoded_key))
            dream_files.sort(key=lambda item: item[0].stat().st_mtime)

            for path, key in dream_files[: max(0, len(dream_files) - keep)]:
                if sessions.delete_session(key):
                    logger.debug("Pruned old dream session: {}", path.stem)
                else:
                    logger.warning("Failed to prune dream session {}", path)


@dataclass(frozen=True, slots=True)
class _DreamFileBase:
    """Canonical file contents observed by one Dream tool session."""

    content: str
    exists: bool


class _DreamWriteState:
    """Track trusted bases and write conflicts that Dream has not resolved."""

    def __init__(self, bases: dict[MemoryTarget, _DreamFileBase]) -> None:
        self.bases = dict(bases)
        self._unresolved: set[str] = set()

    def refresh(self, bases: dict[MemoryTarget, _DreamFileBase]) -> None:
        self.bases = dict(bases)

    def mark_unresolved(self, *paths: str) -> None:
        self._unresolved.update(paths)

    def clear_unresolved(self, *paths: str) -> None:
        self._unresolved.difference_update(paths)

    @property
    def writes_resolved(self) -> bool:
        return not self._unresolved

    @property
    def unresolved_writes(self) -> tuple[str, ...]:
        return tuple(sorted(self._unresolved))


class _DreamToolRegistry(ToolRegistry):
    """Ordinary registry with Dream write-conflict status attached."""

    def __init__(self, state: _DreamWriteState) -> None:
        super().__init__()
        self._state = state

    @property
    def writes_resolved(self) -> bool:
        return self._state.writes_resolved

    @property
    def unresolved_writes(self) -> tuple[str, ...]:
        return self._state.unresolved_writes


class _DreamToolAdapter(Tool):
    """Route canonical Dream edits through the shared memory coordinator."""

    def __init__(
        self,
        store: MemoryStore,
        state: _DreamWriteState,
        delegate: Tool,
    ) -> None:
        self._store = store
        self._state = state
        self._delegate = delegate

    @property
    def name(self) -> str:
        return self._delegate.name

    @property
    def description(self) -> str:
        return self._delegate.description

    @property
    def parameters(self) -> dict[str, Any]:
        return self._delegate.parameters

    @property
    def read_only(self) -> bool:
        return self._delegate.read_only

    @property
    def exclusive(self) -> bool:
        return self._delegate.exclusive

    def runtime_context_provider(self):
        return self._delegate.runtime_context_provider()

    @staticmethod
    def _is_error(result: object) -> bool:
        return isinstance(result, ToolResult) and result.is_error

    def _resolve_path(self, path: str, *, read: bool = False) -> Path:
        method_name = "_resolve_read" if read else "_resolve_write"
        resolver = cast(Callable[[str], Path], getattr(self._delegate, method_name))
        return resolver(path)

    def _target_key(self, target: MemoryTarget) -> str:
        return self._store._canonical_relative_path(target)

    def _path_key(self, path: Path, target: MemoryTarget | None) -> str:
        return self._target_key(target) if target is not None else str(path)

    def _refresh_bases(self) -> None:
        self._state.refresh(self._store._dream_file_bases())

    def _commit(
        self,
        targets: set[MemoryTarget],
        proposed: dict[MemoryTarget, str],
        keys: set[str],
    ) -> ToolResult | None:
        bases: dict[MemoryTarget, str] = {
            target: self._state.bases[target].content for target in targets
        }
        committed, reason = self._store._commit_dream_files(bases, proposed)
        if not committed:
            self._state.mark_unresolved(*keys)
            return ToolResult.error(
                "Memory write conflict: "
                f"{reason or 'dream_memory_commit_failed'}. "
                "Re-read the affected canonical file and retry the edit once."
            )
        self._state.clear_unresolved(*keys)
        self._refresh_bases()
        return None

    @staticmethod
    def _materialize_base(
        shadow: Path,
        relative_path: str,
        base: _DreamFileBase,
    ) -> Path:
        path = shadow / relative_path
        if base.exists:
            path.parent.mkdir(parents=True, exist_ok=True)
            write_text_atomic(path, base.content, newline="")
        return path

    async def _execute_read(self, **kwargs: Any) -> Any:
        result = await self._delegate.execute(**kwargs)
        raw_path = kwargs.get("path")
        if self._is_error(result) or not isinstance(raw_path, str):
            return result
        try:
            path = self._resolve_path(raw_path, read=True)
            target = self._store._canonical_target_for_path(path)
        except (OSError, RuntimeError, TypeError, ValueError):
            return result
        if target is not None:
            latest = self._store._dream_file_bases()
            self._state.bases[target] = latest[target]
        return result

    async def _execute_write(self, **kwargs: Any) -> Any:
        raw_path = kwargs.get("path")
        content = kwargs.get("content")
        if not isinstance(raw_path, str) or not isinstance(content, str):
            return await self._delegate.execute(**kwargs)
        try:
            path = self._resolve_path(raw_path)
        except (OSError, RuntimeError, TypeError, ValueError):
            return await self._delegate.execute(**kwargs)
        target = self._store._canonical_target_for_path(path)
        if target is None:
            result = await self._delegate.execute(**kwargs)
            if not self._is_error(result):
                self._state.clear_unresolved(self._path_key(path, None))
            return result
        key = self._target_key(target)
        error = self._commit({target}, {target: content}, {key})
        if error is not None:
            return error
        return f"Successfully wrote {len(content)} characters to {key}"

    async def _execute_edit(self, **kwargs: Any) -> Any:
        from nanobot.agent.tools.file_state import FileStates
        from nanobot.agent.tools.filesystem import EditFileTool

        raw_path = kwargs.get("path")
        if not isinstance(raw_path, str):
            return await self._delegate.execute(**kwargs)
        try:
            path = self._resolve_path(raw_path)
        except (OSError, RuntimeError, TypeError, ValueError):
            return await self._delegate.execute(**kwargs)
        target = self._store._canonical_target_for_path(path)
        if target is None:
            result = await self._delegate.execute(**kwargs)
            if not self._is_error(result):
                self._state.clear_unresolved(self._path_key(path, None))
            return result

        key = self._target_key(target)
        with TemporaryDirectory(prefix="nanobot-dream-") as raw_shadow:
            shadow = Path(raw_shadow)
            shadow_path = self._materialize_base(
                shadow,
                key,
                self._state.bases[target],
            )
            tool = EditFileTool(
                workspace=shadow,
                allowed_dir=shadow,
                file_states=FileStates(),
            )
            shadow_kwargs = dict(kwargs)
            shadow_kwargs["path"] = key
            result = await tool.execute(**shadow_kwargs)
            if self._is_error(result):
                return result
            proposed = shadow_path.read_text(encoding="utf-8") if shadow_path.exists() else ""

        error = self._commit({target}, {target: proposed}, {key})
        if error is not None:
            return error
        return f"Successfully edited {key} through the protected memory writer"

    async def _execute_patch(self, **kwargs: Any) -> Any:
        from nanobot.agent.tools.apply_patch import ApplyPatchTool
        from nanobot.agent.tools.file_state import FileStates

        edits_value = kwargs.get("edits")
        if not isinstance(edits_value, list):
            return await self._delegate.execute(**kwargs)
        edits = cast(list[object], edits_value)
        resolved: list[tuple[dict[str, Any], Path, MemoryTarget | None]] = []
        try:
            for value in edits:
                if not isinstance(value, dict):
                    return await self._delegate.execute(**kwargs)
                edit = cast(dict[str, Any], value)
                raw_path = edit.get("path")
                if not isinstance(raw_path, str):
                    return await self._delegate.execute(**kwargs)
                path = self._resolve_path(raw_path)
                resolved.append((edit, path, self._store._canonical_target_for_path(path)))
        except (OSError, RuntimeError, TypeError, ValueError):
            return await self._delegate.execute(**kwargs)

        has_canonical = any(target is not None for _, _, target in resolved)
        has_other = any(target is None for _, _, target in resolved)
        keys = {
            self._path_key(path, target)
            for _, path, target in resolved
        }
        if has_canonical and has_other:
            self._state.mark_unresolved(*keys)
            return ToolResult.error(
                "A patch cannot mix canonical memory files with Skill files. "
                "Split it into separate apply_patch calls and retry."
            )
        if not has_canonical:
            result = await self._delegate.execute(**kwargs)
            if not self._is_error(result):
                self._state.clear_unresolved(*keys)
            return result

        targets: set[MemoryTarget] = {
            target
            for _, _, target in resolved
            if target is not None
        }
        with TemporaryDirectory(prefix="nanobot-dream-") as raw_shadow:
            shadow = Path(raw_shadow)
            shadow_paths: dict[MemoryTarget, Path] = {}
            for target in targets:
                relative_path = self._target_key(target)
                shadow_paths[target] = self._materialize_base(
                    shadow,
                    relative_path,
                    self._state.bases[target],
                )
            shadow_edits: list[dict[str, Any]] = []
            for edit, _, target in resolved:
                assert target is not None
                shadow_edit = dict(edit)
                shadow_edit["path"] = self._target_key(target)
                shadow_edits.append(shadow_edit)
            tool = ApplyPatchTool(
                workspace=shadow,
                allowed_dir=shadow,
                file_states=FileStates(),
            )
            result = await tool.execute(
                edits=cast(list[object], shadow_edits),
                dry_run=bool(kwargs.get("dry_run", False)),
            )
            if self._is_error(result) or bool(kwargs.get("dry_run", False)):
                return result
            proposed: dict[MemoryTarget, str] = {
                target: (
                    shadow_paths[target].read_text(encoding="utf-8")
                    if shadow_paths[target].exists()
                    else ""
                )
                for target in targets
            }

        error = self._commit(targets, proposed, keys)
        if error is not None:
            return error
        return str(result)

    async def execute(self, **kwargs: Any) -> Any:
        if self.name == "read_file":
            return await self._execute_read(**kwargs)
        if self.name == "write_file":
            return await self._execute_write(**kwargs)
        if self.name == "edit_file":
            return await self._execute_edit(**kwargs)
        if self.name == "apply_patch":
            return await self._execute_patch(**kwargs)
        return await self._delegate.execute(**kwargs)


# ---------------------------------------------------------------------------
# Memory ingestion and context-pressure coordination
# ---------------------------------------------------------------------------

# Raw fallbacks use a tighter cap. Completed model summaries may scale with the
# configured generation budget, while append_history() still enforces the
# emergency hard cap against pathological provider output.
_RAW_ARCHIVE_MAX_CHARS = 16_000   # fallback dump (LLM failed)
_HISTORY_ENTRY_HARD_CAP = 64_000  # emergency cap in append_history


class MemoryArchiver:
    """Write durable transcript batches to the Memory ingestion journal.

    The archiver deliberately has no SessionManager dependency: it may read a
    captured transcript batch and append to history.jsonl, but it cannot mutate
    provider continuation state or advance a session watermark.
    """

    def __init__(
        self,
        store: MemoryStore,
        build_messages: Callable[..., list[dict[str, Any]]],
        get_tool_definitions: Callable[[], list[dict[str, Any]]],
        resolve_prompt_context: Callable[[Session], tuple[str | None, Path | None]] | None = None,
    ) -> None:
        self.store = store
        self._build_messages = build_messages
        self._get_tool_definitions = get_tool_definitions
        self._resolve_prompt_context = resolve_prompt_context

    def _raw_checkpoint(
        self,
        messages: list[dict[str, Any]],
        *,
        session_key: str,
        previous_summary: str | None,
        max_tokens: int,
    ) -> str:
        """Persist the failed chunk and return a bounded replacement checkpoint."""
        raw = self.store.raw_archive(messages, session_key=session_key)
        return self._combine_raw_checkpoint(
            raw,
            previous_summary=previous_summary,
            max_tokens=max_tokens,
        )

    @staticmethod
    def _combine_raw_checkpoint(
        raw: str,
        *,
        previous_summary: str | None,
        max_tokens: int,
    ) -> str:
        """Return a bounded checkpoint that preserves prior and newly archived context."""
        token_limit = max(1, max_tokens)
        if not previous_summary:
            return truncate_text_to_tokens(raw, token_limit)

        combined = (
            "[Previous archived context]\n"
            f"{previous_summary}\n\n"
            "[Newly archived raw context]\n"
            f"{raw}"
        )
        bounded = truncate_text_to_tokens(combined, token_limit)
        if bounded == combined:
            return combined

        # Keep evidence from both sides when their full concatenation cannot fit.
        section_limit = max(1, (token_limit - 32) // 2)
        return truncate_text_to_tokens(
            "[Previous archived context]\n"
            f"{truncate_text_to_tokens(previous_summary, section_limit)}\n\n"
            "[Newly archived raw context]\n"
            f"{truncate_text_to_tokens(raw, section_limit)}",
            token_limit,
        )

    async def archive(
        self,
        source_messages: list[dict[str, Any]],
        *,
        runtime: LLMRuntime,
        session_key: str,
        history: list[dict[str, Any]],
        request_tools: list[dict[str, Any]],
        previous_summary: str | None = None,
        input_token_budget: int | None = None,
        fallback_max_tokens: int | None = None,
        provider_state: ProviderConversationState | None = None,
        structured_context: bool = False,
    ) -> str | None:
        """Append the archive prompt to H and persist its summary."""
        if not source_messages:
            return None

        def raw_fallback() -> str | None:
            if structured_context:
                return None
            return self._raw_checkpoint(
                source_messages,
                session_key=session_key,
                previous_summary=previous_summary,
                max_tokens=(
                    fallback_max_tokens
                    if fallback_max_tokens is not None
                    else runtime.generation.max_tokens
                ),
            )

        prompt = render_template(
            "agent/context_summary.md" if structured_context else "agent/consolidator_archive.md",
            strip=True,
            archive_count=len(source_messages),
        )
        prompt_message = {"role": "user", "content": prompt}
        provider_context = None
        call_tools = request_tools
        if provider_state is not None:
            if not runtime.provider.can_resume_conversation_state(
                provider_state,
                runtime.model,
            ):
                return raw_fallback()
            instruction_messages: list[dict[str, Any]] = []
            for message in history:
                if message.get("role") not in {"system", "developer"}:
                    break
                instruction_messages.append(dict(message))
            request_messages = [*instruction_messages, prompt_message]
            provider_context = ProviderCallContext(
                conversation_state=provider_state.with_pending_messages([
                    *provider_state.pending_messages,
                    prompt_message,
                ]),
                context_window_tokens=runtime.context_window_tokens,
                session_id=session_key,
            )
            call_tools = []
        else:
            request_messages = [
                *[dict(message) for message in history],
                prompt_message,
            ]
        if input_token_budget is not None and provider_context is None:
            estimated, source = estimate_prompt_tokens_chain(
                runtime.provider,
                runtime.model,
                request_messages,
                call_tools,
            )
            if input_token_budget <= 0 or estimated > input_token_budget:
                logger.debug(
                    "Memory archive input does not fit for {}: {}/{} via {}; raw-dumping",
                    session_key,
                    estimated,
                    input_token_budget,
                    source,
                )
                return raw_fallback()

        try:
            with llm_usage_source("dream"):
                response = await runtime.provider.chat_with_retry(
                    model=runtime.model,
                    messages=request_messages,
                    tools=call_tools,
                    temperature=runtime.generation.temperature,
                    max_tokens=runtime.generation.max_tokens,
                    reasoning_effort=runtime.generation.reasoning_effort,
                    provider_context=provider_context,
                )
        except Exception:
            logger.warning("Memory archive provider call failed, raw-dumping to history")
            return raw_fallback()
        if response.finish_reason in {"error", "length"}:
            logger.warning(
                "Memory archive provider did not complete ({}), raw-dumping to history",
                response.finish_reason,
            )
            return raw_fallback()
        if response.has_tool_calls is True:
            logger.warning("Memory archive provider returned tool calls, raw-dumping to history")
            return raw_fallback()
        summary = response.content
        if not summary or not summary.strip():
            logger.warning("Memory archive provider returned no summary, raw-dumping to history")
            return raw_fallback()
        if structured_context:
            return summary.strip()
        summary = self.store._normalize_history_entry(summary)
        if not summary:
            logger.warning("Memory archive provider summary was not safe to replay, raw-dumping")
            return raw_fallback()
        if summary == "(nothing)":
            return "(nothing)"
        self.store.append_history(summary, session_key=session_key)
        return summary

    async def archive_session(
        self,
        session: Session,
        *,
        archive_end: int,
        runtime: LLMRuntime,
        input_token_budget: int,
    ) -> str | None:
        """Archive a captured session prefix without mutating the session."""
        messages = list(session.messages[session.last_archived:archive_end])
        if not messages:
            return None
        session_summary = session_summary_from_metadata(
            session.metadata,
            fallback_last_active=session.updated_at,
        )
        previous_summary = session_summary["text"] if session_summary else None

        if input_token_budget <= 0:
            logger.debug(
                "Memory archive has no safe input budget for {}; raw-dumping",
                session.key,
            )
            return self._raw_checkpoint(
                messages,
                session_key=session.key,
                previous_summary=previous_summary,
                max_tokens=runtime.generation.max_tokens,
            )
        prefix = Session(
            key=session.key,
            messages=list(session.messages[:archive_end]),
            last_consolidated=session.last_archived,
        )
        history = prefix.get_history(max_tokens=input_token_budget)
        archive_history = Session(
            key=session.key,
            messages=messages,
        ).get_history()
        if not archive_history or history[-len(archive_history):] != archive_history:
            logger.debug(
                "Memory archive cannot replay the full chunk for {}; raw-dumping",
                session.key,
            )
            return self._raw_checkpoint(
                messages,
                session_key=session.key,
                previous_summary=previous_summary,
                max_tokens=runtime.generation.max_tokens,
            )
        channel = session.key.split(":", 1)[0] if ":" in session.key else None
        workspace: Path | None = None
        if self._resolve_prompt_context is not None:
            channel, workspace = self._resolve_prompt_context(session)
        history_messages = self._build_messages(
            history=history,
            current_message=None,
            channel=channel,
            session_summary=session_summary,
            workspace=workspace,
        )
        tools = self._get_tool_definitions()
        return await self.archive(
            messages,
            runtime=runtime,
            session_key=session.key,
            history=history_messages,
            request_tools=tools,
            previous_summary=previous_summary,
            input_token_budget=input_token_budget,
        )


class Consolidator:
    """Coordinate session Memory checkpoints through ``MemoryArchiver``."""

    _SAFETY_BUFFER = 1024  # extra headroom for tokenizer estimation drift

    def __init__(
        self,
        store: MemoryStore,
        sessions: SessionManager,
        build_messages: Callable[..., list[dict[str, Any]]],
        get_tool_definitions: Callable[[], list[dict[str, Any]]],
        resolve_prompt_context: Callable[[Session], tuple[str | None, Path | None]] | None = None,
    ):
        self.store = store
        self.sessions = sessions
        self._build_messages = build_messages
        self._get_tool_definitions = get_tool_definitions
        self.archiver = MemoryArchiver(
            store=store,
            build_messages=build_messages,
            get_tool_definitions=get_tool_definitions,
            resolve_prompt_context=resolve_prompt_context,
        )
        self._locks: weakref.WeakValueDictionary[str, asyncio.Lock] = (
            weakref.WeakValueDictionary()
        )

    def get_lock(self, session_key: str) -> asyncio.Lock:
        """Return the shared consolidation lock for one session."""
        return self._locks.setdefault(session_key, asyncio.Lock())

    async def summarize_transcript(
        self,
        accepted_messages: list[dict[str, Any]],
        previous_summary: str | None,
        *,
        runtime: LLMRuntime,
        session_key: str,
        tools: list[dict[str, Any]],
        provider_state: ProviderConversationState | None = None,
        structured_context: bool = False,
    ) -> ContextSummaryCandidate | str | None:
        """Summarize the exact transcript prefix already accepted by the model."""
        source_messages = [
            dict(message)
            for message in accepted_messages
            if message.get("role") != "system"
        ]
        if not source_messages:
            return None

        max_output_tokens = max(0, runtime.generation.max_tokens)
        input_token_budget = runtime.context_window_tokens - max_output_tokens
        checkpoint_tokens = min(
            max_output_tokens,
            max(1, (input_token_budget - self._SAFETY_BUFFER) // 2),
        )

        summary = await self.archiver.archive(
            source_messages,
            runtime=runtime,
            session_key=session_key,
            history=accepted_messages,
            request_tools=tools,
            previous_summary=previous_summary,
            input_token_budget=input_token_budget,
            fallback_max_tokens=max(1, checkpoint_tokens),
            provider_state=provider_state,
            structured_context=structured_context,
        )
        if summary is None:
            return None
        if not structured_context:
            return summary
        try:
            structured = StructuredContextSummary.model_validate_json(summary)
        except ValueError:
            logger.warning("Context summary provider returned invalid structured output")
            return None
        return ContextSummaryCandidate(
            text=render_context_summary(structured),
            structured=structured,
        )

    async def summarize_provider_compaction(
        self,
        state: ProviderConversationState,
        fallback_messages: list[dict[str, Any]],
        previous_summary: str | None,
        *,
        runtime: LLMRuntime,
        session_key: str,
        tools: list[dict[str, Any]],
        structured_context: bool = False,
    ) -> ContextSummaryCandidate | str | None:
        """Prompt a native compacted state without replaying its raw history."""
        return await self.summarize_transcript(
            fallback_messages,
            previous_summary,
            runtime=runtime,
            session_key=session_key,
            tools=tools,
            provider_state=state,
            structured_context=structured_context,
        )

    @staticmethod
    def _full_replay_history(
        session: Session,
    ) -> list[dict[str, Any]]:
        """Return all messages that can reach the next model prompt."""
        if not session.messages:
            return []
        return session.get_history()

    @staticmethod
    def _set_last_summary(
        session: Session,
        summary: str,
        *,
        last_active: datetime | None = None,
    ) -> None:
        if summary != "(nothing)":
            session.metadata["_last_summary"] = {
                "text": summary,
                "last_active": (last_active or session.updated_at).isoformat(),
            }

    def estimate_session_prompt_tokens(
        self,
        session: Session,
        *,
        runtime: LLMRuntime,
    ) -> tuple[int, str]:
        """Estimate prompt size from the full replayable session history."""
        history = self._full_replay_history(session)
        channel = session.key.split(":", 1)[0] if ":" in session.key else None
        summary = session_summary_from_metadata(
            session.metadata,
            fallback_last_active=session.updated_at,
        )
        probe_messages = self._build_messages(
            history=history,
            current_message="[token-probe]",
            channel=channel,
            session_summary=summary,
        )
        return estimate_prompt_tokens_chain(
            runtime.provider,
            runtime.model,
            probe_messages,
            self._get_tool_definitions(),
        )

    def _input_token_budget(self, runtime: LLMRuntime) -> int:
        """Available input token budget for consolidation LLM."""
        return (
            runtime.context_window_tokens
            - runtime.generation.max_tokens
            - self._SAFETY_BUFFER
        )

    async def archive_session(
        self,
        session: Session,
        *,
        archive_end: int,
        runtime: LLMRuntime,
    ) -> str | None:
        """Archive one captured session range through the shared Memory path."""
        return await self.archiver.archive_session(
            session,
            archive_end=archive_end,
            runtime=runtime,
            input_token_budget=self._input_token_budget(runtime),
        )

    async def compact_idle_session(
        self,
        session_key: str,
        *,
        runtime: LLMRuntime,
        max_suffix: int = MIN_COMPACTED_REPLAY_MESSAGES,
    ) -> str | None:
        """Archive the full idle tail while keeping recent messages replayable.

        ``max_suffix`` remains accepted for SDK compatibility. Replay retention
        is now derived independently from archive progress using the project-wide
        compacted-session window.
        """
        if max_suffix != MIN_COMPACTED_REPLAY_MESSAGES:
            logger.debug(
                "Idle-session compact for {} uses the fixed replay window ({}, requested {})",
                session_key,
                MIN_COMPACTED_REPLAY_MESSAGES,
                max_suffix,
            )
        lock = self.get_lock(session_key)
        async with lock:
            self.sessions.invalidate(session_key)
            session = self.sessions.get_or_create(session_key)

            archive_start = session.last_archived
            messages_to_archive = list(session.messages[archive_start:])
            if not messages_to_archive:
                return ""

            last_active = session.updated_at
            archive_end = archive_start + len(messages_to_archive)
            summary = await self.archive_session(
                session,
                archive_end=archive_end,
                runtime=runtime,
            )
            if summary is None:
                return None

            self._set_last_summary(session, summary, last_active=last_active)

            # A turn can append while the provider call is in flight. Advance only
            # through the captured batch so new messages remain eligible next time.
            session.last_archived = archive_end
            self.sessions.save(session)

            visible = session.get_history(
                max_messages=MIN_COMPACTED_REPLAY_MESSAGES,
                extend_to_user=True,
            )

            logger.info(
                "Idle-session compact for {}: archived={}, visible={}, retained={}, summary={}",
                session_key,
                len(messages_to_archive),
                len(visible),
                len(session.messages),
                bool(summary),
            )

            return summary
