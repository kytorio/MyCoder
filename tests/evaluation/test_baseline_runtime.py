"""N00 characterization through the SDK and real runtime/storage."""

from copy import deepcopy

from nanobot.agent.memory import MemoryStore
from nanobot.providers.base import LLMResponse, ToolCallRequest
from nanobot.session.manager import SessionManager


async def test_normal_reply_reaches_provider_and_persists(baseline_bot, isolated_roots):
    bot, provider, sessions = baseline_bot([LLMResponse(content="baseline hello")])
    result = await bot.run("Say hello for N00.", session_key="n00:reply")
    assert result.content == "baseline hello"
    assert result.error is None
    assert len(provider.requests) == 1
    request = provider.requests[0]
    assert request["model"] == "n00-scripted"
    assert request["temperature"] == 0
    assert request["max_tokens"] == 128
    assert request["messages"][0]["role"] == "system"
    assert "Say hello for N00." in str(request["messages"][-1]["content"])
    persisted = SessionManager(
        isolated_roots.workspace, sessions_root=isolated_roots.runtime / "sessions"
    ).get_or_create("n00:reply")
    assert persisted.messages[-1]["content"] == "baseline hello"
    assert sessions.sessions_dir.is_relative_to(isolated_roots.runtime)


async def test_parallel_tool_results_remain_paired(baseline_bot, isolated_roots):
    (isolated_roots.workspace / "alpha.txt").write_text("alpha evidence", encoding="utf-8")
    (isolated_roots.workspace / "beta.txt").write_text("beta evidence", encoding="utf-8")
    bot, provider, _ = baseline_bot([
        LLMResponse(content=None, tool_calls=[
            ToolCallRequest(id="call-alpha", name="read_file", arguments={"path": "alpha.txt"}),
            ToolCallRequest(id="call-beta", name="read_file", arguments={"path": "beta.txt"}),
        ], finish_reason="tool_calls"),
        LLMResponse(content="both files read"),
    ], read_files=True)
    result = await bot.run("Read alpha.txt and beta.txt together.", session_key="n00:tools")
    assert result.error is None
    assert result.content == "both files read"
    assert len(provider.requests) == 2
    assert [t["function"]["name"] for t in provider.requests[0]["tools"]] == ["read_file"]
    for messages in (provider.requests[1]["messages"], result.messages):
        calls = [c["id"] for m in messages for c in m.get("tool_calls", [])]
        results = [m for m in messages if m["role"] == "tool"]
        assert calls == ["call-alpha", "call-beta"]
        assert [m["tool_call_id"] for m in results] == calls
        assert "alpha evidence" in results[0]["content"]
        assert "beta evidence" in results[1]["content"]


def test_dream_cursor_advances_only_when_explicitly_set(isolated_roots):
    store = MemoryStore(isolated_roots.workspace)
    assert store.get_last_dream_cursor() == 0
    assert store.append_history("first fact") == 1
    assert store.append_history("second fact") == 2
    before = store.history_file.read_bytes()
    prompt, cursor = store.build_dream_prompt(max_entries=1)
    assert "first fact" in prompt
    assert "second fact" not in prompt
    assert cursor == 1
    assert store.get_last_dream_cursor() == 0
    store.write_memory("A synthetic durable fact.")
    assert store.get_last_dream_cursor() == 0
    store.set_last_dream_cursor(cursor)
    reopened = MemoryStore(isolated_roots.workspace)
    assert reopened.get_last_dream_cursor() == 1
    assert reopened.get_latest_cursor() == 2
    prompt, cursor = reopened.build_dream_prompt()
    assert "second fact" in prompt
    assert "first fact" not in prompt
    assert cursor == 2
    assert reopened.history_file.read_bytes() == before


async def test_context_overflow_reports_failure_without_losing_history(baseline_bot):
    # Raw provider exceptions go through the real _safe_chat conversion. Assert
    # durable/error invariants, not the current absence of overflow recovery.
    error = RuntimeError("maximum context length exceeded: N00 synthetic overflow")
    error.status_code = 400
    error.error_code = "context_length_exceeded"
    bot, provider, sessions = baseline_bot([error])
    session = sessions.get_or_create("n00:overflow")
    session.add_message("user", "Preserve the earlier constraint.")
    session.add_message("assistant", "Acknowledged.")
    sessions.save(session)
    before = deepcopy(session.messages)
    result = await bot.run("Keep the current request.", session_key=session.key)
    assert result.stop_reason == "error"
    assert result.error and "context" in result.error.lower()
    assert provider.calls[0].finish_reason == "error"
    assert provider.calls[0].error_status_code == 400
    assert provider.calls[0].usage is None
    assert "Keep the current request." in str(provider.requests[0]["messages"])
    assert session.messages[:len(before)] == before
    assert any("Keep the current request." in str(m.get("content")) for m in session.messages)
