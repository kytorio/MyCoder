"""Controller integration probes: real SDK turns, transient receipts and disk reload."""

import json
from copy import deepcopy

import pytest

from nanobot.agent.tools.base import Tool
from nanobot.agent.tools.context_artifact import ContextArtifactTool
from nanobot.agent.tools.loader import ToolLoader
from nanobot.nanobot import Nanobot
from nanobot.providers.base import LLMResponse, ToolCallRequest
from nanobot.utils.helpers import estimate_prompt_tokens
from tests.agent.test_context_manifest import OfflineProvider


@pytest.fixture
def sdk_evidence(tmp_path, monkeypatch):
    calls = []

    class EvidenceTool(Tool):
        name = "evidence"
        description = "Read offline integration evidence"
        parameters = {"type": "object", "properties": {}}
        read_only = True

        @classmethod
        def create(cls, ctx):
            return cls()

        async def execute(self, **kwargs):
            return "verified early evidence\r\n" * 3000

    class Provider(OfflineProvider):
        async def chat(self, messages, **kwargs):
            calls.append(deepcopy(messages))
            if len(calls) == 1:
                return LLMResponse(content="", context_acceptance="accepted", tool_calls=[
                    ToolCallRequest(id="early", name="evidence", arguments={})])
            return LLMResponse(content="done", context_acceptance="accepted")

    monkeypatch.setattr(ToolLoader, "discover", lambda self: [EvidenceTool, ContextArtifactTool])
    monkeypatch.setattr(ToolLoader, "_discover_plugins", lambda self: {})
    monkeypatch.setattr("nanobot.providers.factory.make_provider",
                        lambda *a, **k: Provider(provider_name="offline"))
    config = tmp_path / "instance" / "config.json"
    config.parent.mkdir()
    config.write_text(json.dumps({"agents": {
        "defaults": {"model": "offline", "contextWindowTokens": 100000},
        "context": {"mode": "enforce", "enabledLayers": ["L2"]},
    }}), encoding="utf-8")
    return config, tmp_path / "ws", calls


async def test_sdk_l2_uses_verified_prior_turn_and_preserves_persisted_raw(sdk_evidence):
    config, workspace, calls = sdk_evidence
    bot = Nanobot.from_config(config, workspace=workspace)
    try:
        await bot.run("Read the early evidence", session_key="sdk:n07")
        await bot.run("Acknowledge the previous result", session_key="sdk:n07")
        session = bot._loop.sessions.get_or_create("sdk:n07")
        assert session.context_consumption is not None
        raw_before = deepcopy(session.messages)
        bot._loop.context_block_limit = estimate_prompt_tokens(calls[0]) + 1500
        result = await bot.run("Now finish using the saved evidence", session_key="sdk:n07")
        assert result.content == "done"
        markers = [m for m in calls[-1] if '"kind":"history_artifact"' in str(m.get("content"))]
        assert markers
        assert session.messages[:len(raw_before)] == raw_before
        assert any(len(m.get("content", "")) > 50000 for m in session.messages if m["role"] == "tool")
        archive_count = len(list(bot._loop.context_artifact_store.root.glob("*.json")))
        await bot.run("Confirm completion", session_key="sdk:n07")
        assert any('"kind":"history_artifact"' in str(m.get("content")) for m in calls[-1])
        assert len(list(bot._loop.context_artifact_store.root.glob("*.json"))) == archive_count
        assert all(len(m.get("content", "")) < 50000 for m in calls[-1])
    finally:
        await bot.aclose()


async def test_sdk_clear_and_cold_reload_do_not_restore_consumption_proof(sdk_evidence):
    config, workspace, _calls = sdk_evidence
    bot = Nanobot.from_config(config, workspace=workspace)
    try:
        await bot.run("Read the early evidence", session_key="sdk:n07")
        session = bot._loop.sessions.get_or_create("sdk:n07")
        assert session.context_consumption is not None
        session.clear()
        assert session.context_consumption is None
        # clear is not saved here: cold loading the previous on-disk session must
        # also lack receipt evidence even though its actual history still exists.
    finally:
        await bot.aclose()
    restarted = Nanobot.from_config(config, workspace=workspace)
    try:
        session = restarted._loop.sessions.get_or_create("sdk:n07")
        assert session.messages
        assert session.context_consumption is None
    finally:
        await restarted.aclose()


@pytest.mark.parametrize("layers", [["L1", "L2"], ["L1", "L3"], ["L1", "L2", "L3"]])
async def test_noop_history_layers_do_not_erase_failed_tool_limit(tmp_path, layers):
    from nanobot.agent.context_governance import ContextGovernor
    from nanobot.agent.context_plan import ContextPlanError
    from tests.agent.test_tool_batch_budget import _batch, _l1_state

    raw = _batch(["first read evidence " * 1000])
    state, _store = _l1_state(tmp_path, raw, single=50, batch=100, layers=layers)
    with pytest.raises(ContextPlanError, match="irreducible_floor"):
        await ContextGovernor().prepare_request(state, raw, tool_definitions=[], transcript=raw)
