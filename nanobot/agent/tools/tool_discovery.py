"""Run-local discovery of schemas from the effective tool registry."""

from __future__ import annotations

import json
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Any, cast

from nanobot.agent.tools.base import Tool, ToolResult, tool_parameters
from nanobot.agent.tools.context import ToolContext
from nanobot.agent.tools.registry import ToolRegistry

RESIDENT_TOOL_NAMES = frozenset({"tool_discovery", "context_artifact_read"})
_MAX_RESULTS = 20
_MAX_DESCRIPTION_CHARS = 240


@dataclass(slots=True)
class ToolDiscoveryState:
    """Schemas loaded during one AgentRunner.run invocation."""

    registry: ToolRegistry
    loaded_names: set[str] = field(default_factory=set)

    def available_names(self) -> tuple[str, ...]:
        return tuple(sorted(name for name in self.registry.tool_names if name not in RESIDENT_TOOL_NAMES))

    def search(self, query: str) -> list[dict[str, str]]:
        normalized = query.casefold().strip()
        matches: list[dict[str, str]] = []
        for name in self.available_names():
            tool = self.registry.get(name)
            if tool is None:
                continue
            description = tool.description
            if normalized and normalized not in name.casefold() and normalized not in description.casefold():
                continue
            matches.append(
                {
                    "name": name,
                    "description": description[:_MAX_DESCRIPTION_CHARS],
                }
            )
            if len(matches) == _MAX_RESULTS:
                break
        return matches

    def load(self, names: list[str]) -> tuple[str, ...]:
        requested = tuple(dict.fromkeys(names))
        available = set(self.available_names())
        unknown = [name for name in requested if name not in available]
        if unknown:
            raise ValueError("Unknown or unavailable tool name(s): " + ", ".join(unknown))
        self.loaded_names.update(requested)
        return requested


_CURRENT_DISCOVERY_STATE: ContextVar[ToolDiscoveryState | None] = ContextVar(
    "nanobot_tool_discovery_state",
    default=None,
)


def bind_tool_discovery_state(
    state: ToolDiscoveryState | None,
) -> Token[ToolDiscoveryState | None]:
    return _CURRENT_DISCOVERY_STATE.set(state)


def reset_tool_discovery_state(token: Token[ToolDiscoveryState | None]) -> None:
    _CURRENT_DISCOVERY_STATE.reset(token)


def current_tool_discovery_state() -> ToolDiscoveryState | None:
    return _CURRENT_DISCOVERY_STATE.get()


@tool_parameters(
    {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["search", "load"],
                "description": "Search the authorized catalog or load exact schema names.",
            },
            "query": {
                "type": "string",
                "maxLength": 200,
                "description": "Case-insensitive name/description text for search.",
            },
            "names": {
                "type": "array",
                "items": {"type": "string", "minLength": 1, "maxLength": 64},
                "minItems": 1,
                "maxItems": 32,
                "description": "Exact authorized tool names to load for the next request.",
            },
        },
        "required": ["action"],
        "additionalProperties": False,
    }
)
class ToolDiscoveryTool(Tool):
    """Discover and load complete schemas without changing execution permissions."""

    @property
    def name(self) -> str:
        return "tool_discovery"

    @property
    def description(self) -> str:
        return (
            "Search tools already authorized for this run, then load complete schemas by exact "
            "name for the next model request. This does not grant tool permissions."
        )

    @classmethod
    def enabled(cls, ctx: ToolContext) -> bool:
        return ctx.schema_discovery

    @classmethod
    def create(cls, ctx: ToolContext) -> ToolDiscoveryTool:
        return cls()

    async def execute(self, **kwargs: Any) -> ToolResult:
        state = current_tool_discovery_state()
        if state is None:
            return ToolResult.error("Tool discovery is unavailable outside an active agent run.")
        if self.validate_params(kwargs):
            return ToolResult.error("Tool discovery request has invalid parameters.")
        action = kwargs.get("action")
        if action == "search":
            query = kwargs.get("query", "")
            if not isinstance(query, str):
                return ToolResult.error("Tool discovery search query must be text.")
            return ToolResult(
                json.dumps({"tools": state.search(query)}, ensure_ascii=False, separators=(",", ":"))
            )
        raw_names = kwargs.get("names")
        if action != "load" or not isinstance(raw_names, list):
            return ToolResult.error("Tool discovery load requires exact tool names.")
        names = cast(list[object], raw_names)
        if not all(isinstance(name, str) for name in names):
            return ToolResult.error("Tool discovery load requires exact tool names.")
        try:
            loaded = state.load([cast(str, name) for name in names])
        except ValueError as exc:
            return ToolResult.error(str(exc))
        return ToolResult(
            json.dumps({"loaded": list(loaded)}, ensure_ascii=False, separators=(",", ":"))
        )
