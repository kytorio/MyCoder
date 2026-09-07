"""Explicit, source-bound durable memory tool for the main model."""

from __future__ import annotations

import json
import re
from hashlib import sha256
from typing import TYPE_CHECKING, Any, Self, cast

from pydantic import Field, model_validator

from nanobot.agent.memory_writes import (
    MemoryConflictError,
    MemoryFact,
    MemoryRememberRequested,
    MemoryScope,
    MemoryTarget,
    MemoryWriteUnavailableError,
    memory_instance_id,
    memory_project_id,
)
from nanobot.agent.tools.base import Tool, ToolResult, tool_parameters
from nanobot.agent.tools.context import current_request_context
from nanobot.agent.tools.schema import StringSchema, tool_parameters_schema
from nanobot.config_base import Base

if TYPE_CHECKING:
    from nanobot.agent.memory import MemoryStore
    from nanobot.agent.tools.context import ToolContext


_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----", re.IGNORECASE),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE),
    re.compile(r"\b(?:sk|pk)-[A-Za-z0-9_-]{12,}\b", re.IGNORECASE),
    re.compile(
        r"\b(?:api[_ -]?key|api[_ -]?token|password|passwd|private[_ -]?key|"
        r"secret|credential)\b\s*[:=]\s*\S{6,}",
        re.IGNORECASE,
    ),
)


class MemoryToolConfig(Base):
    """Configuration for explicit model-triggered memory saves."""

    enabled: bool = False
    transaction_max_bytes: int = Field(default=1_048_576, ge=1, strict=True)
    journal_max_bytes: int = Field(default=67_108_864, ge=1, strict=True)

    @model_validator(mode="after")
    def validate_journal_capacity(self) -> Self:
        if self.journal_max_bytes < self.transaction_max_bytes:
            raise ValueError("journalMaxBytes must be >= transactionMaxBytes")
        return self


def _contains_secret(value: str) -> bool:
    return any(pattern.search(value) is not None for pattern in _SECRET_PATTERNS)


def _digest(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(payload.encode("utf-8")).hexdigest()


def _error(reason_code: str) -> ToolResult:
    return ToolResult.error(
        json.dumps(
            {
                "status": "failed",
                "reason_code": reason_code,
                "message": "记忆未确认保存",
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


@tool_parameters(
    tool_parameters_schema(
        target=StringSchema(
            "Canonical memory area: user preferences, safe soul habits, or project memory.",
            enum=("user", "soul", "memory"),
        ),
        key=StringSchema(
            "Stable semantic key such as reply.language; it is not a file path.",
            min_length=1,
            max_length=128,
        ),
        content=StringSchema(
            "One durable fact or preference to save.",
            min_length=1,
            max_length=2048,
        ),
        source_excerpt=StringSchema(
            "Exact excerpt from the bound user request that explicitly supports this save.",
            min_length=1,
            max_length=2048,
        ),
        replaces_entry_id=StringSchema(
            "Existing explicit entry id being corrected, only when the user explicitly corrects it.",
            min_length=1,
            max_length=192,
            nullable=True,
        ),
        required=["target", "key", "content", "source_excerpt"],
    )
)
class MemorySaveTool(Tool):
    """Commit one explicit user-backed fact through the canonical memory writer."""

    config_key = "memory"

    def __init__(self, memory: MemoryStore) -> None:
        self.memory = memory

    @classmethod
    def config_cls(cls) -> type[MemoryToolConfig]:
        return MemoryToolConfig

    @classmethod
    def enabled(cls, ctx: ToolContext) -> bool:
        config = getattr(ctx.config, "memory", None)
        return bool(getattr(config, "enabled", False)) and ctx.memory is not None

    @classmethod
    def create(cls, ctx: ToolContext) -> Tool:
        if ctx.memory is None:
            raise MemoryWriteUnavailableError("memory_writer_unavailable")
        return cls(ctx.memory)

    @property
    def name(self) -> str:
        return "memory_save"

    @property
    def description(self) -> str:
        return (
            "Save one durable fact or preference only when the user explicitly asks or clearly "
            "states it. Quote exact supporting text from the current user request. Never save "
            "secrets, temporary instructions, quoted third-party claims, or inferred facts. "
            "A successful result is the only confirmation that the memory was stored."
        )

    @property
    def read_only(self) -> bool:
        return False

    @property
    def exclusive(self) -> bool:
        return True

    async def execute(  # pyright: ignore[reportIncompatibleMethodOverride]
        self,
        target: str,
        key: str,
        content: str,
        source_excerpt: str,
        replaces_entry_id: str | None = None,
        **_kwargs: Any,
    ) -> str:
        request_context = current_request_context()
        if (
            request_context is None
            or not request_context.original_user_text
            or not request_context.sender_id
        ):
            return _error("trusted_user_source_unavailable")

        normalized_key = key.strip()
        normalized_content = content.strip()
        normalized_excerpt = source_excerpt.strip()
        original_text = request_context.original_user_text
        if not _KEY_RE.fullmatch(normalized_key):
            return _error("invalid_memory_key")
        if not normalized_content or not normalized_excerpt:
            return _error("invalid_memory_fact")
        if normalized_excerpt not in original_text:
            return _error("source_excerpt_not_found")
        if _contains_secret(normalized_content) or _contains_secret(normalized_excerpt):
            return _error("secret_material_rejected")

        message_ref = (
            request_context.message_id
            or request_context.turn_id
            or request_context.session_key
        )
        if not message_ref:
            return _error("trusted_user_source_unavailable")
        if target not in {"user", "soul", "memory"}:
            return _error("invalid_memory_target")
        memory_target = cast(MemoryTarget, target)
        project = request_context.workspace or self.memory.workspace
        scope = MemoryScope(
            instance_id=memory_instance_id(self.memory.workspace),
            project_id=(memory_project_id(project) if memory_target == "memory" else None),
        )
        identity = {
            "message_ref": message_ref,
            "owner_id": request_context.sender_id,
            "scope": {
                "instance_id": scope.instance_id,
                "project_id": scope.project_id,
            },
            "target": memory_target,
            "key": normalized_key,
            "content": normalized_content,
            "source_excerpt": normalized_excerpt,
            "replaces_entry_id": replaces_entry_id,
        }
        operation_id = f"memory:{_digest(identity)}"
        entry_id = f"entry:{_digest({'operation_id': operation_id, 'fact': identity})}"
        request = MemoryRememberRequested(
            operation_id=operation_id,
            message_ref=message_ref,
            original_text_hash=_digest(original_text),
            owner_id=request_context.sender_id,
            scope=scope,
            facts=(
                MemoryFact(
                    entry_id=entry_id,
                    target=memory_target,
                    key=normalized_key,
                    text=normalized_content,
                    scope=scope,
                    replaces_entry_id=replaces_entry_id,
                ),
            ),
        )
        try:
            result = self.memory.remember(request)
        except (MemoryConflictError, MemoryWriteUnavailableError) as exc:
            return _error(getattr(exc, "reason_code", str(exc)))
        except OSError:
            return _error("memory_io_failed")
        if result.status != "committed":
            return _error(result.reason_code or "memory_commit_failed")
        return json.dumps(
            {
                "status": "committed",
                "operation_id": result.operation_id,
                "revision": result.revision,
                "entry_ids": list(result.entry_ids),
                "replayed": result.replayed,
                "saved_facts": [
                    {
                        "target": memory_target,
                        "key": normalized_key,
                        "content": normalized_content,
                    }
                ],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
