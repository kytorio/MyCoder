"""Offline evaluation must judge actual runtime side effects and bounded failures."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from nanobot.evaluation.models import EvalCase
from nanobot.evaluation.runner import EvalRunner


def smoke_case(**overrides):
    payload = {
        "schema_version": 1, "id": "smoke", "description": "runtime smoke",
        "user_inputs": ["Reply pong"],
        "scripted_responses": [{"content": "pong", "finish_reason": "stop"}],
        "allowed_tools": [],
        "verifiers": [{"name": "answer_contains", "expected": "pong"}],
    }
    payload.update(overrides)
    return EvalCase.model_validate(payload)


async def test_scripted_runtime_smoke(tmp_path):
    result = await EvalRunner().run_case(smoke_case(), mode="baseline", output_root=tmp_path)
    assert result.status == "passed"
    assert result.provenance["case_hash"]
    assert result.provenance["mechanism_only"] is True
    assert result.metrics["request_count"] == 1
    assert result.metrics["actual_input_tokens"] is None
    assert Path(result.trace_ref).is_file()


async def test_verifier_reads_actual_file_not_model_claim(tmp_path):
    case = smoke_case(
        user_inputs=["Create result.json"], allowed_tools=["write_file"],
        scripted_responses=[
            {"content": None, "finish_reason": "tool_calls", "tool_calls": [
                {"id": "write-1", "name": "write_file", "arguments": {
                    "path": "result.json", "content": '{"replicas":3}',
                }},
            ]},
            {"content": "done"},
        ],
        verifiers=[{"name": "json_equals", "path": "result.json", "expected": {"replicas": 3}}],
    )
    result = await EvalRunner().run_case(case, mode="baseline", output_root=tmp_path)
    assert result.status == "passed"
    assert result.metrics["tool_steps"] == 1
    case.scripted_responses = [{"content": "I created result.json with replicas 3"}]
    result = await EvalRunner().run_case(case, mode="baseline", output_root=tmp_path)
    assert result.status == "failed"


async def test_each_run_uses_fresh_roots(tmp_path):
    runner = EvalRunner()
    first = await runner.run_case(smoke_case(), mode="baseline", output_root=tmp_path)
    second = await runner.run_case(smoke_case(), mode="baseline", output_root=tmp_path)
    assert first.trace_ref != second.trace_ref
    assert first.provenance["case_hash"] == second.provenance["case_hash"]
    assert first.provenance["config_hash"] == second.provenance["config_hash"]


@pytest.mark.parametrize("responses", [[], [{"content": "pong", "expect_contains": ["absent-marker"]}]])
async def test_missing_or_mismatched_script_never_passes(tmp_path, responses):
    case = smoke_case(scripted_responses=responses)
    result = await EvalRunner().run_case(case, mode="baseline", output_root=tmp_path)
    assert result.status == "error"
    assert "script" in str(result.metrics["failure_reason"]).lower()


async def test_token_budget_stops_before_provider_call(tmp_path):
    result = await EvalRunner().run_case(
        smoke_case(budget={"max_tokens": 1}), mode="baseline", output_root=tmp_path,
    )
    assert result.status == "budget_exceeded"
    assert result.metrics["request_count"] == 0
    assert json.loads(Path(result.trace_ref).read_text(encoding="utf-8"))["calls"] == []


async def test_request_budget_stops_second_request(tmp_path):
    case = smoke_case(
        user_inputs=["first", "second"], budget={"max_requests": 1},
        scripted_responses=[{"content": "pong"}, {"content": "pong"}],
    )
    result = await EvalRunner().run_case(case, mode="baseline", output_root=tmp_path)
    assert result.status == "budget_exceeded"
    assert result.metrics["request_count"] == 1


async def test_tool_batch_budget_prevents_partial_side_effects(tmp_path):
    case = smoke_case(
        allowed_tools=["write_file"], budget={"max_steps": 1},
        scripted_responses=[{"content": None, "tool_calls": [
            {"id": f"w{i}", "name": "write_file", "arguments": {"path": f"{i}.txt", "content": "x"}}
            for i in (1, 2)
        ]}],
    )
    result = await EvalRunner().run_case(case, mode="baseline", output_root=tmp_path)
    assert result.status == "budget_exceeded"
    assert result.metrics["tool_steps"] == 0
    assert not list(Path(result.trace_ref).parent.glob("workspace/[12].txt"))


@pytest.mark.parametrize("path", ["../escape", "/absolute", "C:/escape", r"..\escape", "safe/../../escape", "file:stream", "CON"])
def test_unsafe_fixture_path_is_rejected(path):
    with pytest.raises(ValidationError):
        smoke_case(workspace_files={path: "no"})


@pytest.mark.parametrize("tool", ["exec", "spawn", "web_fetch", "mcp_external", "unknown"])
def test_fixture_cannot_enable_external_tools(tool):
    with pytest.raises(ValidationError):
        smoke_case(allowed_tools=[tool])


def test_arbitrary_verifier_is_rejected():
    with pytest.raises(ValidationError):
        smoke_case(verifiers=[{"name": "shell", "expected": "echo unsafe"}])


async def test_unimplemented_mode_is_not_a_pass(tmp_path):
    case = smoke_case(
        allowed_tools=["memory_save"],
        requires_capabilities=["memory_save"],
    )
    result = await EvalRunner().run_case(case, mode="baseline", output_root=tmp_path)
    assert result.status == "not_implemented"
    assert result.metrics["request_count"] == 0


async def test_trace_is_json_and_does_not_export_prompt_text(tmp_path):
    case = smoke_case(user_inputs=["PRIVATE-SYNTHETIC-TEXT: reply pong"])
    result = await EvalRunner().run_case(case, mode="baseline", output_root=tmp_path)
    text = Path(result.trace_ref).read_text(encoding="utf-8")
    trace = json.loads(text)
    assert trace["schema_version"] == 1
    assert trace["requests"]
    assert "PRIVATE-SYNTHETIC-TEXT" not in text


async def test_wall_budget_includes_setup_before_first_request(tmp_path):
    result = await EvalRunner().run_case(
        smoke_case(budget={"max_wall_seconds": 0.000001}), mode="baseline", output_root=tmp_path,
    )
    assert result.status == "budget_exceeded"
    assert result.metrics["request_count"] == 0


async def test_failure_closes_bot_and_restores_selected_config(tmp_path, monkeypatch, isolated_roots):
    from nanobot.config.loader import get_config_path
    from nanobot.nanobot import Nanobot

    closed = []
    original = Nanobot.aclose

    async def close(bot):
        await original(bot)
        closed.append(bot.runtime.workspace)

    monkeypatch.setattr(Nanobot, "aclose", close)
    result = await EvalRunner().run_case(
        smoke_case(scripted_responses=[]), mode="baseline", output_root=tmp_path,
    )
    assert result.status == "error"
    assert len(closed) == 1
    assert closed[0].is_relative_to(tmp_path)
    assert get_config_path() == isolated_roots.config


def test_fixture_and_verifier_reject_symlink_escape(tmp_path):
    from nanobot.evaluation.models import VerifierSpec, contained_path
    from nanobot.evaluation.verifiers import VerificationContext, verify

    safe, outside = tmp_path / "safe", tmp_path / "outside"
    safe.mkdir()
    outside.mkdir()
    (outside / "secret.txt").write_text("do not read", encoding="utf-8")
    try:
        (safe / "linked").symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks unavailable: {exc.winerror}")
    with pytest.raises(ValueError):
        contained_path(safe, "linked/secret.txt")
    verdict = verify(
        VerifierSpec(name="file_equals", path="linked/secret.txt", expected="do not read"),
        VerificationContext(safe, [], [], {}),
    )
    assert verdict.status == "failed"


async def test_reported_usage_is_not_confused_with_estimates(tmp_path):
    case = smoke_case(scripted_responses=[{"content": "pong", "usage": {
        "input_tokens": 17, "output_tokens": 2, "cached_tokens": 5,
    }}])
    result = await EvalRunner().run_case(case, mode="baseline", output_root=tmp_path)
    assert result.metrics["actual_input_tokens"] == 17
    assert result.metrics["estimated_input_tokens"] != 17


def test_invalid_script_shape_is_rejected_without_running(tmp_path):
    from nanobot.evaluation.providers import ScriptedProvider

    with pytest.raises(ValidationError):
        ScriptedProvider([{"content": "pong", "arbitrary_python": "raise RuntimeError"}])


async def test_provenance_time_counts_toward_wall_budget(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from nanobot.evaluation import providers, runner

    ticks = [0.0]
    clock = SimpleNamespace(monotonic=lambda: ticks[0])
    original_provenance = runner.provenance
    monkeypatch.setattr(runner, "time", clock)
    monkeypatch.setattr(providers, "time", clock)
    monkeypatch.setattr(runner, "RunBudget", lambda limits: providers.RunBudget(limits, started=ticks[0]))

    def slow_provenance(*args):
        value = original_provenance(*args)
        ticks[0] += 2.0
        return value

    monkeypatch.setattr(runner, "provenance", slow_provenance)
    result = await EvalRunner().run_case(
        smoke_case(budget={"max_wall_seconds": 1}), mode="baseline", output_root=tmp_path,
    )
    assert result.status == "budget_exceeded"
    assert result.metrics["request_count"] == 0
    assert result.metrics["wall_seconds"] >= 2


async def test_existing_summary_requests_keep_their_actual_purpose(tmp_path):
    case = smoke_case(
        initial_messages=[
            {"role": "user", "content": "Retain the earlier decision."},
            {"role": "assistant", "content": "old evidence " * 60_000},
        ],
        user_inputs=["new evidence " * 6_000 + "Reply pong"],
        budget={"max_tokens": 1_000_000},
        scripted_responses=[
            {"content": "Earlier decision retained.", "expect_purpose": "summary"},
            {"content": "pong", "expect_purpose": "worker"},
        ],
    )
    result = await EvalRunner().run_case(case, mode="baseline", output_root=tmp_path)
    assert result.status == "passed"
    trace = json.loads(Path(result.trace_ref).read_text(encoding="utf-8"))
    assert [row["purpose"] for row in trace["requests"]] == ["summary", "worker"]


async def test_script_metadata_is_not_exported_verbatim(tmp_path):
    marker = "PRIVATE-METADATA"
    case = smoke_case(allowed_tools=["read_file"], workspace_files={"source.txt": "ok"},
        scripted_responses=[{"tool_calls": [{"id": marker, "name": "read_file",
                              "arguments": {"path": "source.txt"}}]},
                            {"content": "pong", "finish_reason": marker}])
    result = await EvalRunner().run_case(case, mode="baseline", output_root=tmp_path)
    assert marker not in Path(result.trace_ref).read_text(encoding="utf-8")


async def test_observe_records_real_context_manifest(tmp_path):
    result = await EvalRunner().run_case(smoke_case(), mode="observe", output_root=tmp_path)
    assert result.status == "passed"
    assert result.provenance["observe_placeholder"] is False
    trace = json.loads(Path(result.trace_ref).read_text(encoding="utf-8"))
    manifests = trace["context_manifests"]
    assert len(manifests) == result.metrics["context_manifest_count"] == 1
    assert manifests[0]["status"] == "responded"
    assert manifests[0]["actual_input_tokens"] is None
    assert manifests[0]["input_budget"] == 131_072 - 512 - 1024
    assert manifests[0]["phase"] == "probe"
    assert manifests[0]["matched_provider_request_ids"] == ["request-1"]


async def test_observe_manifest_records_failure_without_body_leak(tmp_path):
    case = smoke_case(user_inputs=["PRIVATE-OBSERVE-CONTENT"], scripted_responses=[])
    result = await EvalRunner().run_case(case, mode="observe", output_root=tmp_path)
    assert result.status == "error"
    trace_text = Path(result.trace_ref).read_text(encoding="utf-8")
    assert "PRIVATE-OBSERVE-CONTENT" not in trace_text
    manifests = json.loads(trace_text)["context_manifests"]
    assert manifests[0]["status"] == "responded"
    assert manifests[0]["reason_code"] == "provider_error"
    assert manifests[0]["actual_input_tokens"] is None


async def test_observe_and_baseline_send_identical_payloads(tmp_path, monkeypatch):
    from nanobot.evaluation.providers import ScriptedProvider

    captured = []
    original = ScriptedProvider.next_response

    def record(provider, messages):
        captured.append(provider)
        return original(provider, messages)

    monkeypatch.setattr(ScriptedProvider, "next_response", record)
    case = smoke_case(workspace_files={
        "USER.md": "PRIVATE-PREFERENCE", "SOUL.md": "PRIVATE-PERSONA",
        "memory/MEMORY.md": "PRIVATE-MEMORY", "facts.txt": "PRIVATE-FACT",
    }, allowed_tools=["read_file"], scripted_responses=[
        {"tool_calls": [{"id": "read-1", "name": "read_file", "arguments": {"path": "facts.txt"}}]},
        {"content": "pong", "usage": {"input_tokens": 17, "output_tokens": 2}},
    ])
    payloads = []
    results = []
    for mode in ("baseline", "observe"):
        result = await EvalRunner().run_case(case, mode=mode, output_root=tmp_path)
        assert result.status == "passed"
        provider = captured[-1]
        # Only independently allocated fixture roots differ; no source or field is removed.
        root = str(Path(result.trace_ref).parent)
        payloads.append(json.dumps([(r.messages, r.tools) for r in provider.requests], sort_keys=True)
                        .replace(json.dumps(root)[1:-1], "<case-root>"))
        results.append(result)
    assert payloads[0] == payloads[1]
    trace = json.loads(Path(results[1].trace_ref).read_text(encoding="utf-8"))
    manifests = trace["context_manifests"]
    assert len(manifests) == 2
    assert manifests[-1]["actual_input_tokens"] == 17
    assert {s["source_id"] for s in manifests[0]["sources"] if s["kind"] == "memory"} == {
        "memory:USER.md", "memory:SOUL.md", "memory:MEMORY.md",
    }
    assert sum(s["kind"] == "mcp_schema" for s in manifests[0]["sources"]) == 1
    assert "PRIVATE-" not in json.dumps(manifests)


@pytest.mark.parametrize("responses,reason", [([{"content": "pong"}], "cleanup_error"), ([], "script_exhausted")])
async def test_cleanup_failure_still_writes_result(tmp_path, monkeypatch, responses, reason):
    from nanobot.nanobot import Nanobot

    original = Nanobot.aclose

    async def close(bot):
        await original(bot)
        raise RuntimeError("PRIVATE-CLEANUP")

    monkeypatch.setattr(Nanobot, "aclose", close)
    result = await EvalRunner().run_case(smoke_case(scripted_responses=responses), mode="baseline", output_root=tmp_path)
    assert result.status == "error"
    assert result.metrics["failure_reason"] == reason
    assert result.metrics["cleanup_error"] == "RuntimeError"
    saved = Path(result.trace_ref).with_name("result.json").read_text(encoding="utf-8")
    assert "PRIVATE-CLEANUP" not in saved


async def test_verifier_time_counts_toward_wall_budget(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from nanobot.evaluation import providers, runner

    ticks = [0.0]
    clock = SimpleNamespace(monotonic=lambda: ticks[0])
    original_verify = runner.verify
    monkeypatch.setattr(runner, "time", clock)
    monkeypatch.setattr(providers, "time", clock)
    monkeypatch.setattr(runner, "RunBudget", lambda limits: providers.RunBudget(limits, started=ticks[0]))

    def slow_verify(*args):
        verdict = original_verify(*args)
        ticks[0] += 2.0
        return verdict

    monkeypatch.setattr(runner, "verify", slow_verify)
    result = await EvalRunner().run_case(smoke_case(budget={"max_wall_seconds": 1}), mode="baseline", output_root=tmp_path)
    assert result.status == "budget_exceeded"


@pytest.mark.parametrize("name", ["file_equals", "files_unchanged"])
def test_bounded_verifier_preserves_text_newline_semantics(tmp_path, name):
    from nanobot.evaluation.models import VerifierSpec
    from nanobot.evaluation.verifiers import VerificationContext, verify

    (tmp_path / "source.txt").write_bytes(b"first\r\nsecond\rthird\n")
    expected = "first\nsecond\nthird\n"
    spec = VerifierSpec(name=name, path="source.txt", expected=expected if name == "file_equals" else ["source.txt"])
    result = verify(spec, VerificationContext(tmp_path, [], [], {"source.txt": expected}))
    assert result.status == "passed"
