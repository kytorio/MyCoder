"""Offline roots for N00; never construct a runtime before these are selected."""

import asyncio
import json
import os
import socket
from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from functools import partial
from pathlib import Path

import pytest

from nanobot.config import loader, paths
from nanobot.providers.base import GenerationSettings, LLMProvider
from nanobot.session import manager as sessions


@dataclass(frozen=True)
class IsolatedRoots:
    workspace: Path
    runtime: Path
    config: Path


@contextmanager
def isolated_environment(tmp_path: Path) -> Iterator[IsolatedRoots]:
    """Restore even a previously unset config selector on failure or teardown."""
    workspace = tmp_path / "workspace"
    runtime = tmp_path / "runtime"
    workspace.mkdir()
    runtime.mkdir()
    roots = IsolatedRoots(workspace, runtime, runtime / "config.json")
    roots.config.write_text(json.dumps({
        "agents": {"defaults": {"workspace": str(workspace)}},
        "tools": {"restrictToWorkspace": True},
    }), encoding="utf-8")

    def deny_network(*args, **kwargs):
        raise AssertionError("N00 offline isolation: network access denied")

    with pytest.MonkeyPatch.context() as patch:
        for name in tuple(os.environ):
            if name.startswith("NANOBOT_"):
                patch.delenv(name)
        patch.setattr(loader, "_current_config_path", roots.config)
        patch.setattr(sessions, "get_runtime_subdir", paths.get_runtime_subdir)
        patch.setattr(sessions, "get_legacy_sessions_dir", lambda: runtime / "legacy-sessions")
        patch.setattr(paths, "get_legacy_sessions_dir", lambda: runtime / "legacy-sessions")
        patch.setattr(socket, "create_connection", deny_network)
        patch.setattr(socket, "getaddrinfo", deny_network)
        patch.setattr(socket.socket, "connect", deny_network)
        patch.setattr(socket.socket, "connect_ex", deny_network)
        patch.setattr(socket.socket, "sendto", deny_network)
        # Proactor I/O can bypass socket.connect; cover the async entrypoints.
        loop = asyncio.get_running_loop()
        patch.setattr(loop, "create_connection", deny_network)
        patch.setattr(loop, "create_datagram_endpoint", deny_network)
        patch.setattr(loop, "sock_connect", deny_network)
        # Prevent external code from loading before a registry allowlist applies.
        patch.setattr("nanobot.agent.tools.loader.entry_points", lambda **kwargs: ())
        yield roots


@pytest.fixture(autouse=True)
async def isolated_roots(tmp_path, _isolate_sessions_root):
    # The repository fixture uses siblings of tmp_path. Rebind its aliases so
    # every N00 session/migration target is inside this test's tmp_path instead.
    # pytest creates Windows' loopback self-pipe before installing the deny
    # guard, then restores the guard before closing that event loop.
    with isolated_environment(tmp_path) as roots:
        yield roots


@pytest.fixture
def isolation_scope():
    return isolated_environment


class ScriptedProvider(LLMProvider):
    """Replace only external responses; retain safe-chat, retry and usage paths."""

    def __init__(self, responses):
        super().__init__(provider_name="n00-scripted")
        self.generation = GenerationSettings(temperature=0, max_tokens=128)
        self.responses = list(responses)
        self.requests = []
        self.calls = []
        self.set_llm_call_observer(self.calls.append)

    def get_default_model(self):
        return "n00-scripted"

    def estimate_prompt_tokens(self, messages, tools=None, model=None):
        # A deterministic conservative estimate, not measured provider usage.
        return len(json.dumps([messages, tools], ensure_ascii=False).encode("utf-8")), "n00-bytes"

    async def chat(self, **kwargs):
        self.requests.append(deepcopy(kwargs))
        if not self.responses:
            pytest.fail("N00 response script exhausted; no live fallback allowed")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


@pytest.fixture
async def baseline_bot(isolated_roots, monkeypatch):
    from nanobot.agent.loop import AgentLoop
    from nanobot.agent.tools import loader as tool_loader
    from nanobot.agent.tools.filesystem import ReadFileTool
    from nanobot.agent.tools.registry import ToolRegistry
    from nanobot.bus.queue import MessageBus
    from nanobot.nanobot import Nanobot

    # Exercise the real loader with an explicit builtin set and no entrypoints;
    # no default tool constructors run and no runtime method is replaced.
    monkeypatch.setattr(tool_loader, "ToolLoader", partial(tool_loader.ToolLoader, test_classes=[]))
    bots = []

    def build(responses, *, read_files=False):
        provider = ScriptedProvider(responses)
        manager = sessions.SessionManager(
            isolated_roots.workspace, sessions_root=isolated_roots.runtime / "sessions"
        )
        registry = ToolRegistry()
        if read_files:
            registry.register(ReadFileTool(
                workspace=isolated_roots.workspace,
                allowed_dir=isolated_roots.workspace,
                restrict_to_workspace=True,
            ))
        loop = AgentLoop(
            bus=MessageBus(), provider=provider, workspace=isolated_roots.workspace,
            model="n00-scripted", session_manager=manager, tool_registry=registry,
            context_window_tokens=100_000, max_iterations=4, timezone="UTC",
            restrict_to_workspace=True, session_ttl_minutes=0,
            idle_compact_check_interval_seconds=0,
        )
        bot = Nanobot(loop)
        bots.append((bot, provider))
        return bot, provider, manager

    yield build
    for bot, provider in bots:
        await bot.aclose()
        assert not provider.responses, "N00 response script was not fully consumed"
