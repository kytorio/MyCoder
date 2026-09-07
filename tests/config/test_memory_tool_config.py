"""Configuration and discovery tests for memory_save."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from nanobot.agent.memory import MemoryStore
from nanobot.agent.memory_writes import MemoryMutation, MemoryWriteCoordinator
from nanobot.agent.tools.context import ToolContext
from nanobot.agent.tools.loader import ToolLoader
from nanobot.agent.tools.memory_save import MemorySaveTool
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.config.schema import ToolsConfig


def _allow(_mutation: MemoryMutation) -> None:
    return None


def _context(tmp_path: Path, config: ToolsConfig) -> ToolContext:
    root = tmp_path / "workspace"
    writer = MemoryWriteCoordinator(
        root,
        tmp_path / "runtime",
        authorize=_allow,
        transaction_max_bytes=config.memory.transaction_max_bytes,
        journal_max_bytes=config.memory.journal_max_bytes,
    )
    return ToolContext(
        config=config,
        workspace=str(root),
        memory=MemoryStore(root, writer=writer),
    )


def test_memory_tool_defaults_disabled_with_bounded_journal() -> None:
    config = ToolsConfig().memory

    assert config.enabled is False
    assert config.transaction_max_bytes == 1_048_576
    assert config.journal_max_bytes == 67_108_864


def test_memory_tool_accepts_camel_case_config() -> None:
    config = ToolsConfig.model_validate(
        {
            "memory": {
                "enabled": True,
                "transactionMaxBytes": 4096,
                "journalMaxBytes": 8192,
            }
        }
    )

    assert config.memory.enabled is True
    assert config.memory.transaction_max_bytes == 4096
    assert config.memory.journal_max_bytes == 8192


def test_memory_tool_rejects_journal_smaller_than_transaction() -> None:
    with pytest.raises(ValidationError, match="journalMaxBytes"):
        ToolsConfig.model_validate(
            {
                "memory": {
                    "transactionMaxBytes": 4096,
                    "journalMaxBytes": 1024,
                }
            }
        )


def test_disabled_memory_tool_is_not_registered(tmp_path: Path) -> None:
    registry = ToolRegistry()

    registered = ToolLoader(test_classes=[MemorySaveTool]).load(
        _context(tmp_path, ToolsConfig()),
        registry,
    )

    assert "memory_save" not in registered
    assert registry.get("memory_save") is None


def test_enabled_memory_tool_is_discovered_with_write_metadata(tmp_path: Path) -> None:
    registry = ToolRegistry()
    config = ToolsConfig.model_validate({"memory": {"enabled": True}})

    registered = ToolLoader(test_classes=[MemorySaveTool]).load(
        _context(tmp_path, config),
        registry,
    )
    tool = registry.get("memory_save")

    assert "memory_save" in registered
    assert tool is not None
    assert tool.read_only is False
    assert tool.exclusive is True
    assert tool.config_key == "memory"

