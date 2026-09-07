"""Isolated evaluation assembly, budget hooks and reports; not a second agent loop."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Generator
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, Literal, Protocol, cast
from unittest.mock import patch

from pydantic import JsonValue

from nanobot.agent.context_artifacts import ToolResultArtifactStore
from nanobot.agent.context_governance import ContextGovernor, ModelRequestState
from nanobot.agent.context_plan import (
    ContextConsumption,
    ContextRequestOutcome,
    content_hash,
    history_message_hash,
)
from nanobot.agent.hook import AgentHook, AgentHookContext
from nanobot.agent.loop import AgentLoop
from nanobot.agent.tools.base import Tool, ToolResult
from nanobot.agent.tools.context_artifact import ContextArtifactTool
from nanobot.agent.tools.filesystem import EditFileTool, ListDirTool, ReadFileTool, WriteFileTool
from nanobot.agent.tools.memory_save import MemorySaveTool
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.bus.queue import MessageBus
from nanobot.config import loader
from nanobot.config.schema import Config
from nanobot.evaluation.metrics import (
    EvalUsageRecord,
    evaluate_memory_save_attempt,
    summarize_usage,
)
from nanobot.evaluation.models import (
    MODES,
    OFFLINE_TOOLS,
    EvalCase,
    EvalExecutionConfig,
    EvalResult,
    VerifierResult,
    canonical_hash,
    contained_path,
)
from nanobot.evaluation.providers import (
    CompositeRunBudget,
    EvalBudgetExceededError,
    EvalScriptError,
    LiveEvaluationProvider,
    PromptSensitiveProvider,
    RunBudget,
    ScriptedProvider,
    VisibleFact,
    VisibleTask,
    artifact_visible_facts,
    visible_facts,
)
from nanobot.evaluation.scenarios import INPUT_BUDGET, RENDER_VERSION, calibrate_history, task_text
from nanobot.evaluation.verifiers import (
    VerificationContext,
    snapshot_workspace,
    tool_pairs_valid,
    verify,
)
from nanobot.llm_usage.models import LLMCallRecord
from nanobot.nanobot import Nanobot
from nanobot.providers.base import GenerationSettings, LLMResponse, ToolCallRequest
from nanobot.providers.factory import ProviderSnapshot
from nanobot.session.manager import SessionManager
from nanobot.utils.helpers import estimate_prompt_tokens_chain

# Config and legacy path selectors are process-global in the existing runtime.
# Evaluations are serial in a dedicated process; do not embed this in a Gateway.
_CASE_LOCK = threading.Lock()
class _FileToolFactory(Protocol):
    def __call__(self, *, workspace: Path, allowed_dir: Path, restrict_to_workspace: bool) -> Tool: ...


_FILE_TOOLS: dict[str, _FileToolFactory] = {
    "read_file": ReadFileTool, "write_file": WriteFileTool,
    "edit_file": EditFileTool, "list_dir": ListDirTool,
}
LiteralStatus = Literal["passed", "failed", "error", "budget_exceeded", "not_implemented"]
EvaluationProvider = ScriptedProvider | LiveEvaluationProvider


class _EvaluationLoop(AgentLoop):
    def _register_default_tools(self, *, provider_snapshot_loader: object) -> None:
        """Use only the caller's registry; never load external plugin constructors."""


def observe_context(governor: ContextGovernor, provider: EvaluationProvider,
                    instrumentation: ExitStack, records: dict[str, dict[str, JsonValue]]) -> None:
    """Capture the real governor's latest lifecycle snapshot without changing its decisions."""
    prepare, dispatch, respond = governor.prepare_request, governor.record_dispatch, governor.record_response

    def capture(state: ModelRequestState) -> None:
        if state.manifest is not None:
            row = json.loads(json.dumps(state.manifest.to_log()))
            row["phase"] = provider.phase
            # Logical governance requests and physical provider attempts have distinct IDs.
            # A hash can match multiple attempts; preserve all matches rather than invent a bijection.
            row["matched_provider_request_ids"] = [r.request_id for r in provider.requests
                if r.phase == provider.phase and r.purpose == "worker" and row["payload_hash"] in {
                    content_hash({"messages": r.messages, "tools": r.tools}),
                    content_hash({"messages": r.messages, "tools": r.tools or None}),
                }]
            records[state.manifest.plan.request_id] = row

    async def prepared(state: ModelRequestState, *args: Any, **kwargs: Any) -> Any:
        try:
            return await prepare(state, *args, **kwargs)
        finally:
            capture(state)

    def dispatched(state: ModelRequestState) -> None:
        dispatch(state)
        capture(state)

    def responded(state: ModelRequestState, response: LLMResponse) -> None:
        respond(state, response)
        capture(state)

    for name, callback in (("prepare_request", prepared), ("record_dispatch", dispatched),
                           ("record_response", responded)):
        instrumentation.enter_context(patch.object(governor, name, callback))


@contextmanager
def evaluation_environment(
    config_path: Path,
    *,
    allow_provider_network: bool,
) -> Generator[None]:
    def deny_network(*args: object, **kwargs: object) -> None:
        raise RuntimeError("offline_network_denied")

    # The loop already exists here (Windows creates its self-pipe beforehand).
    loop = asyncio.get_running_loop()
    with ExitStack() as stack:
        stack.enter_context(patch.object(loader, "_current_config_path", config_path))
        stack.enter_context(patch(
            "nanobot.session.manager.get_legacy_sessions_dir",
            lambda: config_path.parent / "legacy-sessions",
        ))
        stack.enter_context(patch.dict(os.environ, {
            key: value for key, value in os.environ.items() if not key.startswith("NANOBOT_")
        }, clear=True))
        if not allow_provider_network:
            for target, name in (
                (socket, "create_connection"), (socket, "getaddrinfo"),
                (socket.socket, "connect"), (socket.socket, "connect_ex"),
                (socket.socket, "sendto"), (loop, "create_connection"),
                (loop, "create_datagram_endpoint"), (loop, "sock_connect"),
            ):
                stack.enter_context(patch.object(target, name, deny_network))
        yield


class EvaluationHook(AgentHook):
    def __init__(
        self,
        budget: CompositeRunBudget,
        provider: EvaluationProvider,
        workspace: Path,
    ) -> None:
        super().__init__(reraise=True)
        self.budget = budget
        self.provider = provider
        self.events: list[dict[str, JsonValue]] = []
        self.workspace = workspace
        self.reads: list[dict[str, JsonValue]] = []
        self.probe_write_paths: list[str] = []
        self.memory_save_latencies_ms: list[float] = []
        self.safety_bypass_count = 0
        self._tool_started: dict[str, float] = {}

    @staticmethod
    def _identity(tool_call: ToolCallRequest) -> dict[str, JsonValue]:
        return {"call_id": "call-" + hashlib.sha256(tool_call.id.encode()).hexdigest()[:16],
                "name": tool_call.name if tool_call.name in {
                    *OFFLINE_TOOLS,
                    "context_artifact_read",
                } else "unknown"}

    async def before_iteration(self, context: AgentHookContext) -> None:
        self.budget.check_time()
        if self.provider.failure:
            raise self.provider.failure

    async def before_execute_tools(self, context: AgentHookContext) -> None:
        if self.provider.failure:
            raise self.provider.failure
        # Reserve the entire batch before any side effect, including invalid calls.
        self.budget.reserve_tools(len(context.tool_calls))

    async def before_execute_tool(self, context: AgentHookContext, tool_call: ToolCallRequest,
                                  tool: Any, params: Any) -> None:
        self.budget.check_time()
        self._tool_started[tool_call.id] = time.monotonic()
        self.events.append({**self._identity(tool_call), "phase": self.provider.phase, "status": "started"})

    async def after_execute_tool(self, context: AgentHookContext, tool_call: ToolCallRequest,
                                 tool: Any, params: Any, result: Any) -> None:
        self.events.append({**self._identity(tool_call), "phase": self.provider.phase, "status": "completed",
                            "result_hash": hashlib.sha256(str(result).encode("utf-8")).hexdigest()})
        started = self._tool_started.pop(tool_call.id, None)
        if tool_call.name == "memory_save" and started is not None:
            try:
                payload = json.loads(str(result))
            except ValueError:
                payload = None
            if (
                isinstance(payload, dict)
                and cast(dict[str, object], payload).get("status") == "committed"
            ):
                self.memory_save_latencies_ms.append((time.monotonic() - started) * 1000)
        if (self.provider.phase == "probe" and tool_call.name in {"write_file", "edit_file"}
                and not (isinstance(result, ToolResult) and result.is_error)):
            value = tool_call.arguments.get("path")
            if isinstance(value, str):
                self.probe_write_paths.append(value)
                try:
                    contained_path(self.workspace, value)
                except ValueError:
                    self.safety_bypass_count += 1
        if tool_call.name == "read_file" and not (isinstance(result, ToolResult) and result.is_error):
            path_value = tool_call.arguments.get("path")
            if isinstance(path_value, str):
                try:
                    path = contained_path(self.workspace, path_value)
                    with path.open("rb") as stream:
                        raw = stream.read(1_048_577)
                    self.reads.append({"phase": self.provider.phase, "path_hash": canonical_hash(path_value),
                                       "content_hash": hashlib.sha256(raw).hexdigest() if len(raw) <= 1_048_576 else None,
                                       "offset": tool_call.arguments.get("offset", 1),
                                       "limit": tool_call.arguments.get("limit")})
                except (OSError, ValueError):
                    pass  # No successfully readable file: never classify as a redundant fact read.

    async def on_execute_tool_error(self, context: AgentHookContext, tool_call: ToolCallRequest,
                                    tool: Any, params: Any, error: Any) -> None:
        self.events.append({**self._identity(tool_call), "status": "error"})
        self._tool_started.pop(tool_call.id, None)


def provenance(case: EvalCase, config: dict[str, JsonValue], mode: str) -> dict[str, JsonValue]:
    repo = Path(__file__).resolve().parents[2]

    def git(*args: str) -> bytes | None:
        try:
            result = subprocess.run(
                ["git", "-c", f"safe.directory={repo.as_posix()}", *args], cwd=repo,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10, check=False,
            )
            return result.stdout if result.returncode == 0 else None
        except (OSError, subprocess.TimeoutExpired):
            return None

    commit = git("rev-parse", "HEAD")
    diff = git("diff", "--no-ext-diff", "--binary", "HEAD")
    digest = hashlib.sha256(diff) if diff is not None else None
    # Include new implementation/fixture files; do not hash generated reports or
    # unrelated untracked user data. Tracked dirty files are already in diff.
    untracked = git("ls-files", "--others", "--exclude-standard", "-z", "--",
                    "nanobot", "tests", "spec")
    if digest is not None and untracked is not None:
        for name in sorted(untracked.decode("utf-8").split("\0")):
            if name and Path(name).suffix in {".py", ".json", ".md"}:
                path = repo / name
                if path.is_file() and not path.is_symlink():
                    digest.update(name.encode("utf-8"))
                    digest.update(path.read_bytes())
    dependencies: dict[str, JsonValue] = {}
    for package in ("nanobot-ai", "pydantic", "tiktoken"):
        try:
            dependencies[package] = version(package)
        except PackageNotFoundError:
            dependencies[package] = None
    return {
        "git_commit": commit.decode().strip() if commit else None,
        "dirty_diff_hash": digest.hexdigest() if digest is not None else None,
        "case_hash": canonical_hash(case.model_dump(mode="json")),
        "config_hash": canonical_hash(config), "mode": mode, "seed": 0,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "model": config["model"], "temperature": config["temperature"],
        "max_output_tokens": config["max_output_tokens"],
        "dependencies": dependencies,
        "mechanism_only": config["mechanism_only"],
        "execution_kind": config["execution_kind"],
        "provider": config["provider"],
        "source_config_hash": config["source_config_hash"],
        "python_version": sys.version.split()[0], "platform": sys.platform,
    }


class EvalRunner:
    def __init__(
        self,
        *,
        execution: EvalExecutionConfig | None = None,
        provider_snapshot: ProviderSnapshot | None = None,
    ) -> None:
        self.execution = execution or EvalExecutionConfig()
        self.provider_snapshot = provider_snapshot
        if self.execution.kind == "live":
            if provider_snapshot is None or self.execution.suite_budget is None:
                raise ValueError("live evaluation requires a resolved provider snapshot")
            self._suite_budget = RunBudget(self.execution.suite_budget)
        else:
            if provider_snapshot is not None:
                raise ValueError("offline evaluation cannot receive a provider snapshot")
            self._suite_budget = None

    async def run_case(self, case: EvalCase, *, mode: str, output_root: Path) -> EvalResult:
        # Revalidate mutated model instances before any fixture write.
        case = EvalCase.model_validate(case.model_dump(mode="json"))
        if mode not in MODES:
            raise ValueError("unsupported evaluation mode")
        if self.execution.kind == "live" and case.scenario == "basic":
            raise ValueError("scripted basic cases are not eligible for live evaluation")
        if not _CASE_LOCK.acquire(blocking=False):
            raise RuntimeError("concurrent evaluations in one process are unsupported")
        try:
            return await self._run_isolated(case, mode=mode, output_root=output_root)
        finally:
            _CASE_LOCK.release()

    async def _run_isolated(self, case: EvalCase, *, mode: str, output_root: Path) -> EvalResult:
        budget = CompositeRunBudget(RunBudget(case.budget), self._suite_budget)
        output_root = output_root.resolve()
        output_root.mkdir(parents=True, exist_ok=True)
        # Keep the isolated root short enough for atomic artifact temp files on
        # Windows, where the case id plus a content-addressed filename can exceed
        # the legacy path limit.  The complete case id remains in result/trace.
        case_key = hashlib.sha256(case.id.encode("utf-8")).hexdigest()[:10]
        root = Path(tempfile.mkdtemp(prefix=f"case-{case_key}-", dir=output_root))
        workspace, runtime = root / "workspace", root / "runtime"
        workspace.mkdir()
        runtime.mkdir()
        config_path = runtime / "config.json"
        context_modes = {"context-only", "full", "no-l1", "no-l2", "no-l3", "no-l4"}
        memory_modes = {"memory-only", "full", "no-l1", "no-l2", "no-l3", "no-l4"}
        enabled_layers: list[Literal["L1", "L2", "L3", "L4"]] = [
            "L1",
            "L2",
            "L3",
            "L4",
        ]
        if mode.startswith("no-l"):
            disabled_by_mode: dict[
                str,
                Literal["L1", "L2", "L3", "L4"],
            ] = {
                "no-l1": "L1",
                "no-l2": "L2",
                "no-l3": "L3",
                "no-l4": "L4",
            }
            disabled_layer = disabled_by_mode[mode]
            enabled_layers.remove(disabled_layer)
        live_snapshot = self.provider_snapshot if self.execution.kind == "live" else None
        live_generation = (
            live_snapshot.generation or live_snapshot.provider.generation
            if live_snapshot is not None
            else None
        )
        generation = GenerationSettings(
            temperature=0,
            max_tokens=min(512, live_generation.max_tokens) if live_generation else 512,
            reasoning_effort=live_generation.reasoning_effort if live_generation else None,
        )
        runtime_model = live_snapshot.model if live_snapshot is not None else "evaluation-scripted"
        runtime_context_window = (
            live_snapshot.context_window_tokens if live_snapshot is not None else 131_072
        )
        provider_name = (
            live_snapshot.provider.provider_name if live_snapshot is not None else "evaluation-scripted"
        )
        settings: dict[str, JsonValue] = {
            "model": runtime_model, "provider": provider_name,
            "max_output_tokens": generation.max_tokens,
            "temperature": generation.temperature,
            "context_window_tokens": runtime_context_window,
            "allowed_tools": list(case.allowed_tools),
            "probe_context_window_tokens": 8192 if case.scenario == "context_pressure" else 131_072,
            "safety_margin_tokens": 1024, "render_version": RENDER_VERSION if case.scenario != "basic" else "basic-v1",
            "budget": case.budget.model_dump(mode="json"), "workspace": "<isolated-workspace>",
            "context_mode": "enforce" if mode in context_modes else "observe",
            "enabled_layers": cast(list[JsonValue], list(enabled_layers)),
            "internal_tools": cast(
                list[JsonValue],
                ["context_artifact_read"] if mode in context_modes else [],
            ),
            "execution_kind": self.execution.kind,
            "mechanism_only": self.execution.kind == "offline",
            "source_config_hash": (
                hashlib.sha256(self.execution.config_path.read_bytes()).hexdigest()
                if self.execution.config_path is not None
                else None
            ),
            "suite_budget": (
                self.execution.suite_budget.model_dump(mode="json")
                if self.execution.suite_budget is not None
                else None
            ),
        }
        origin = provenance(case, settings, mode)
        origin["observe_placeholder"] = False
        origin.update({"scenario": case.scenario, "setup_kind": case.setup_kind,
                       "memory_variant": case.memory_variant,
                       "render_version": RENDER_VERSION if case.scenario != "basic" else "basic-v1",
                       "provider_kind": (
                           "real" if self.execution.kind == "live"
                           else "fixed" if case.scenario == "basic"
                           else "prompt_sensitive"
                       ),
                       "seed": case.parameters.get("seed", 0)})
        provider: EvaluationProvider | None = None
        hook: EvaluationHook | None = None
        answers: list[str] = []
        messages: list[dict[str, Any]] = []
        calls: list[LLMCallRecord] = []
        available_capabilities: set[str] = set()
        if mode in memory_modes:
            available_capabilities.update({"memory_save", "dream_coordination", "memory_restart"})
        if mode in context_modes:
            available_capabilities.add("context_artifact_roundtrip")
        required_capabilities = set(case.requires_capabilities)
        implemented = required_capabilities <= available_capabilities
        memory_tool_enabled = (
            mode in memory_modes
            and "memory_save" in case.allowed_tools
            and "memory_save" in required_capabilities
        )
        if case.setup_kind in {"tool_write", "dream_interleave", "restart_probe"}:
            implemented = implemented and memory_tool_enabled
        status: LiteralStatus = "passed" if implemented else "not_implemented"
        failure_reason: str | None = None if implemented else "mode_not_implemented"
        verdicts: list[VerifierResult] = []
        cleanup_error: str | None = None
        scenario_trace: dict[str, JsonValue] = {}
        scenario_metrics: dict[str, JsonValue] = {}
        context_manifests: dict[str, dict[str, JsonValue]] = {}
        workspace_before: dict[str, str] | None = None
        initial_memory_entry_count = 0
        memory_committed = False
        dream_cursor: int | None = None
        if implemented:
            with evaluation_environment(
                config_path,
                allow_provider_network=self.execution.kind == "live",
            ), ExitStack() as instrumentation:
                bot: Nanobot | None = None
                try:
                    budget.check_time()
                    for name, text in case.workspace_files.items():
                        if case.scenario == "memory_dependency" and name in {"USER.md", "SOUL.md", "memory/MEMORY.md"}:
                            continue  # Preseed only between A and the new B session.
                        path = contained_path(workspace, name)
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_text(text, encoding="utf-8")
                    # Explicit init values only, after removing environment overrides.
                    config = Config()
                    config.agents.defaults.workspace = str(workspace)
                    config.agents.defaults.model = runtime_model
                    config.agents.defaults.provider = provider_name
                    config.agents.defaults.max_tokens = generation.max_tokens
                    config.agents.defaults.temperature = generation.temperature
                    config.agents.defaults.reasoning_effort = generation.reasoning_effort
                    config.agents.defaults.context_window_tokens = runtime_context_window
                    config.agents.defaults.max_tool_iterations = case.budget.max_requests
                    config.agents.defaults.timezone = "UTC"
                    config.agents.defaults.session_ttl_minutes = 0
                    config.agents.defaults.idle_compact_check_interval_seconds = 0
                    config.agents.context.safety_margin_tokens = 1024
                    if mode in context_modes:
                        config.agents.context.mode = "enforce"
                        config.agents.context.enabled_layers = enabled_layers
                    config.tools.restrict_to_workspace = True
                    config.tools.memory.enabled = memory_tool_enabled
                    config.bind_source_path(config_path)
                    config_path.write_text(config.model_dump_json(by_alias=True), encoding="utf-8")
                    if live_snapshot is not None:
                        provider = LiveEvaluationProvider(
                            live_snapshot.provider,
                            model=live_snapshot.model,
                            budget=budget,
                            generation=generation,
                        )
                    else:
                        provider = (
                            ScriptedProvider(case.scripted_responses)
                            if case.scenario == "basic"
                            and case.parameters.get("probe_kind") != "reference_roundtrip"
                            else PromptSensitiveProvider(
                                archive_error=case.parameters.get("archive_error") is True
                            )
                        )
                        provider.budget = budget
                    def observe_call(call: LLMCallRecord) -> None:
                        # _safe_chat observes local budget errors too. They have
                        # no admitted request ID and must not become physical calls.
                        if len(calls) < len(provider.requests):
                            calls.append(call)

                    provider.set_llm_call_observer(observe_call)
                    hook = EvaluationHook(budget, provider, workspace)
                    registry = ToolRegistry()
                    for name in case.allowed_tools:
                        if name in _FILE_TOOLS:
                            registry.register(
                                _FILE_TOOLS[name](
                                    workspace=workspace,
                                    allowed_dir=workspace,
                                    restrict_to_workspace=True,
                                )
                            )
                    artifact_store = None
                    if mode in context_modes:
                        artifact_store = ToolResultArtifactStore(
                            runtime / "context-artifacts",
                            max_bytes=config.agents.context.artifact_max_bytes,
                        )
                        registry.register(ContextArtifactTool(  # pyright: ignore[reportAbstractUsage]
                            artifact_store,
                            page_token_budget=config.agents.context.tool_result_token_budget,
                        ))
                    loop = _EvaluationLoop(
                        bus=MessageBus(), provider=provider, workspace=workspace,
                        model=provider.get_default_model(), tool_registry=registry,
                        session_manager=SessionManager(workspace, sessions_root=runtime / "sessions"),
                        restrict_to_workspace=True, timezone="UTC", max_iterations=case.budget.max_requests,
                        context_window_tokens=runtime_context_window, session_ttl_minutes=0,
                        idle_compact_check_interval_seconds=0,
                        context_config=config.agents.context, runtime_data_dir=config.runtime_data_dir,
                        context_artifact_store=artifact_store,
                        tools_config=config.tools,
                    )
                    if loop.context.memory.writer is not None:
                        initial_memory_entry_count = len(
                            loop.context.memory.writer.snapshot().explicit_entries
                        )
                    initial_dream_cursor = loop.context.memory.get_last_dream_cursor()
                    if memory_tool_enabled:
                        registry.register(
                            MemorySaveTool(loop.context.memory)  # pyright: ignore[reportAbstractUsage]
                        )
                    if mode in {"observe", *context_modes}:
                        observe_context(loop.runner.context_governor, provider, instrumentation, context_manifests)
                    bot = Nanobot(loop, config=config)
                    instrumentation.enter_context(patch.object(
                        loop.consolidator.archiver, "archive",
                        provider.label_purpose(loop.consolidator.archiver.archive, "summary"),
                    ))
                    budget.check_time()
                    if case.parameters.get("probe_kind") == "reference_roundtrip":
                        loop.set_runtime_context_window(8192)
                    remaining = budget.remaining_wall_seconds()
                    async with asyncio.timeout(remaining):
                        probe_session = "eval:probe"
                        if case.scenario != "basic":
                            provider.phase = "bootstrap"
                            bootstrap_session = "eval:context" if case.scenario == "context_pressure" else "eval:bootstrap"
                            probe_session = bootstrap_session if case.scenario == "context_pressure" else "eval:probe"
                            scenario_trace.update({"bootstrap_session": bootstrap_session, "probe_session": probe_session})
                            stale_dream_tools = (
                                loop.context.memory.build_dream_tools()
                                if case.setup_kind == "dream_interleave"
                                else None
                            )
                            if case.scenario == "context_pressure":
                                warmup, calibrated = calibrate_history(
                                    case,
                                    loop.context,
                                    provider,
                                    registry.get_definitions(),
                                )
                                scenario_trace.update(calibrated)
                                if self.execution.kind == "live":
                                    # Keep pressure deterministic across real models. The
                                    # measured API work begins at the probe; allowing a live
                                    # model to regenerate fixture history changes its size and
                                    # can add tool turns before compression is evaluated.
                                    calibrated_history: list[dict[str, Any]] = []
                                    warmup_index = 0
                                    for original in case.initial_messages:
                                        message = dict(original)
                                        if message["role"] == "user":
                                            message["content"] = warmup[warmup_index]
                                            warmup_index += 1
                                        calibrated_history.append(message)
                                    await bot.sessions.ingest(
                                        bootstrap_session, calibrated_history
                                    )
                                    session = loop.sessions.get_or_create(
                                        bootstrap_session
                                    )
                                    persisted_history = session.get_history()
                                    accepted_messages = loop.context.build_messages(
                                        persisted_history,
                                        case.user_inputs[-1],
                                        channel="cli",
                                    )[:-1]
                                    bootstrap_id = canonical_hash(persisted_history)
                                    session.context_consumption = ContextConsumption(
                                        outcome=ContextRequestOutcome(
                                            request_id=f"evaluation-{bootstrap_id[:16]}",
                                            lineage_id=f"evaluation-{bootstrap_id[16:32]}",
                                            attempt=0,
                                            status="accepted",
                                        ),
                                        history_hashes=tuple(
                                            history_message_hash(message)
                                            for message in persisted_history
                                        ),
                                        accepted_messages_json=tuple(
                                            json.dumps(
                                                message,
                                                ensure_ascii=False,
                                                sort_keys=True,
                                            )
                                            for message in accepted_messages
                                        ),
                                    )
                                    warmup = []
                                    scenario_trace["bootstrap_strategy"] = (
                                        "ingested_calibrated_history"
                                    )
                                else:
                                    scenario_trace["bootstrap_strategy"] = "model_generated"
                            elif case.setup_kind in {
                                "tool_write",
                                "dream_interleave",
                                "restart_probe",
                            }:
                                memory_seed = case.workspace_files.get(
                                    "memory/MEMORY.md",
                                    "",
                                )
                                facts = visible_facts([{"content": memory_seed}])
                                if not facts:
                                    raise EvalScriptError("missing_tool_write_facts")
                                remember_task = VisibleTask(
                                    action="remember",
                                    topic=str(case.parameters["topic"]),
                                    required=[fact.key for fact in facts],
                                )
                                warmup = [
                                    "Explicitly remember these confirmed facts for the next "
                                    "session.\n"
                                    f"{memory_seed}\n{task_text(remember_task)}"
                                ]
                            else:
                                source, topic = case.parameters.get("source"), case.parameters.get("topic")
                                if not isinstance(source, str) or not isinstance(topic, str):
                                    raise ValueError("invalid_memory_scenario")
                                warmup = [task_text(VisibleTask(action="read", topic=topic, source=source))]
                            for message in warmup:
                                warm = await bot.run(message, session_key=bootstrap_session, hooks=[hook])
                                if provider.failure:
                                    raise provider.failure
                                if warm.error or warm.stop_reason != "completed":
                                    raise EvalScriptError("bootstrap_incomplete")
                            scenario_metrics["bootstrap_completed"] = True
                            scenario_trace["bootstrap_snapshot_hash"] = canonical_hash(loop.sessions.get_or_create(bootstrap_session).messages)
                            if stale_dream_tools is not None:
                                writer = loop.context.memory.writer
                                before_entries = (
                                    {
                                        (fact.target, fact.entry_id): fact
                                        for fact in writer.snapshot().explicit_entries
                                    }
                                    if writer is not None
                                    else {}
                                )
                                dream_candidate = case.parameters.get(
                                    "dream_candidate",
                                    "dream-organized\n",
                                )
                                if not isinstance(dream_candidate, str):
                                    raise ValueError("invalid_dream_candidate")
                                dream_result = await stale_dream_tools.execute(
                                    "write_file",
                                    {
                                        "path": "memory/MEMORY.md",
                                        "content": dream_candidate,
                                    },
                                )
                                if isinstance(dream_result, ToolResult) and dream_result.is_error:
                                    raise EvalScriptError("dream_interleave_write_failed")
                                after_entries = (
                                    {
                                        (fact.target, fact.entry_id): fact
                                        for fact in writer.snapshot().explicit_entries
                                    }
                                    if writer is not None
                                    else {}
                                )
                                scenario_metrics["stale_dream_overwrite_count"] = sum(
                                    after_entries.get(identity) != fact
                                    for identity, fact in before_entries.items()
                                )
                                if loop.context.memory.get_last_dream_cursor() != initial_dream_cursor:
                                    raise EvalScriptError("memory_save_advanced_dream_cursor")
                            if case.setup_kind == "restart_probe":
                                from nanobot.agent.memory import MemoryStore

                                restarted = MemoryStore(
                                    workspace,
                                    writer=loop.context.memory.writer,
                                )
                                scenario_metrics["restart_memory_visible"] = bool(
                                    restarted.writer is not None
                                    and restarted.writer.snapshot().explicit_entries
                                )
                                if scenario_metrics["restart_memory_visible"] is not True:
                                    raise EvalScriptError("memory_restart_lost_commit")
                            if case.scenario == "memory_dependency":
                                if case.setup_kind == "preseeded":
                                    for name in ("USER.md", "SOUL.md", "memory/MEMORY.md"):
                                        if name in case.workspace_files:
                                            contained_path(workspace, name).write_text(case.workspace_files[name], encoding="utf-8")
                                    source_path = contained_path(workspace, str(case.parameters["source"]))
                                    if case.parameters.get("source_available") is False:
                                        source_path.unlink()  # Exact fixture file under this run's isolated workspace.
                                    elif case.parameters.get("source_changed") is True:
                                        source_path.write_text("New source version; earlier facts no longer available.", encoding="utf-8")
                                # tool_write has already committed through memory_save;
                                # the probe below deliberately uses a fresh session.
                            else:
                                history = loop.sessions.get_or_create(probe_session).get_history()
                                candidate = loop.context.build_messages(history, case.user_inputs[-1], channel="cli")
                                tokens, method = estimate_prompt_tokens_chain(provider, provider.get_default_model(), candidate, registry.get_definitions())
                                scenario_metrics.update({"candidate_input_tokens": tokens, "input_budget": INPUT_BUDGET,
                                                         "candidate_estimate_method": method})
                                ratio = case.parameters.get("pressure_ratio")
                                if not isinstance(ratio, (int, float)):
                                    raise ValueError("missing_pressure_ratio")
                                reached = (tokens > INPUT_BUDGET if case.parameters.get("irreducible") is True
                                           else abs(tokens / INPUT_BUDGET - ratio) <= 0.08)
                                scenario_metrics["scenario_valid"] = reached
                                if not reached:
                                    raise EvalScriptError("invalid_pressure_sample")
                                loop.set_runtime_context_window(8192)
                            scenario_trace["probe_initial_history_count"] = len(loop.sessions.get_or_create(probe_session).messages)
                            provider.phase = "probe"
                        elif case.initial_messages:
                            await bot.sessions.ingest("eval:probe", case.initial_messages, source="evaluation_fixture")
                        workspace_before = snapshot_workspace(workspace)
                        for input_index, message in enumerate(case.user_inputs):
                            result = await bot.run(message, session_key=probe_session, hooks=[hook])
                            answers.append(result.content or "")
                            messages = result.messages
                            if provider.failure:
                                raise provider.failure
                            if result.error or result.stop_reason != "completed":
                                status, failure_reason = "error", "runtime_incomplete"
                                break
                            if case.setup_kind == "read_cache_reset" and input_index == 0:
                                # Controlled restart seam: L3 covers accepted duplicate
                                # versions that can reappear after ephemeral read state is lost.
                                loop.discard_session_file_state(probe_session)
                    budget.check_time()
                    if (
                        status == "passed"
                        and isinstance(provider, ScriptedProvider)
                        and provider.responses
                    ):
                        raise EvalScriptError("script_unconsumed")
                    writer = loop.context.memory.writer
                    explicit_entries = (
                        writer.snapshot().explicit_entries if writer is not None else ()
                    )
                    receipt_count = (
                        sum(1 for path in writer.receipts_dir.glob("*.json") if path.is_file())
                        if writer is not None
                        else 0
                    )
                    memory_committed = (
                        len(explicit_entries) > initial_memory_entry_count
                        and receipt_count > 0
                    )
                    duplicate_memory_entries = len(explicit_entries) - len({
                        (fact.target, fact.scope, fact.key) for fact in explicit_entries
                    })
                    scenario_metrics["duplicate_memory_entry_count"] = max(
                        0,
                        duplicate_memory_entries,
                    )
                    dream_cursor = loop.context.memory.get_last_dream_cursor()
                    stale_overwrites_value = scenario_metrics.get(
                        "stale_dream_overwrite_count",
                        0,
                    )
                    stale_overwrites = (
                        stale_overwrites_value
                        if type(stale_overwrites_value) is int
                        else 0
                    )
                    context = VerificationContext(
                        workspace=workspace,
                        answers=answers,
                        messages=messages,
                        original_files=case.workspace_files,
                        workspace_before=workspace_before,
                        tool_write_paths=tuple(hook.probe_write_paths),
                        tool_events=tuple(hook.events),
                        memory_committed=memory_committed,
                        dream_cursor=dream_cursor,
                        safety_side_effects=hook.safety_bypass_count,
                        stale_dream_overwrites=stale_overwrites,
                        duplicate_memory_entries=max(0, duplicate_memory_entries),
                        context_manifests=tuple(context_manifests.values()),
                    )
                    for verifier in case.verifiers:
                        budget.check_time()
                        verdicts.append(verify(verifier, context))
                        budget.check_time()
                    if status == "passed" and any(v.status == "failed" for v in verdicts):
                        status = "failed"
                except (EvalBudgetExceededError, TimeoutError) as exc:
                    status, failure_reason = "budget_exceeded", str(exc) or "wall_time_budget"
                except EvalScriptError as exc:
                    status, failure_reason = "error", str(exc)
                except Exception as exc:
                    # Never export exception strings containing fixture bodies or credentials.
                    status, failure_reason = "error", type(exc).__name__
                finally:
                    if bot is not None:
                        try:
                            await bot.aclose()
                        except Exception as exc:
                            cleanup_error = type(exc).__name__
                            if failure_reason is None:
                                status, failure_reason = "error", "cleanup_error"
        records = provider.requests if provider else []
        usage = summarize_usage(EvalUsageRecord.model_validate({
            "request_id": r.request_id, "purpose": r.purpose, "input_tokens": r.input_tokens,
            "output_tokens": r.output_tokens, "cached_tokens": r.cached_tokens,
        }) for r in records)
        probe_records = [r for r in records if r.phase == "probe"]
        if case.scenario != "basic":
            scenario_metrics["probe_request_count"] = len(probe_records)
            scenario_metrics["bootstrap_request_count"] = len(records) - len(probe_records)
            scenario_metrics["tool_pairs_valid"] = tool_pairs_valid(messages)
        if mode in context_modes:
            layer_triggers = {stage: 0 for stage in ("L1", "L2", "L3", "L4")}
            eviction_reasons: dict[str, int] = {}
            for manifest in context_manifests.values():
                decisions = manifest.get("decisions")
                if not isinstance(decisions, list):
                    continue
                for value in decisions:
                    if not isinstance(value, dict):
                        continue
                    decision = cast(dict[str, object], value)
                    stage = decision.get("stage")
                    action = decision.get("action")
                    reason = decision.get("reason_code")
                    if isinstance(stage, str) and stage in layer_triggers and action != "keep":
                        layer_triggers[stage] += 1
                    if action != "keep" and isinstance(reason, str):
                        eviction_reasons[reason] = eviction_reasons.get(reason, 0) + 1
            scenario_metrics["layer_triggers"] = cast(dict[str, JsonValue], layer_triggers)
            scenario_metrics["eviction_reasons"] = cast(
                dict[str, JsonValue],
                eviction_reasons,
            )
            scenario_metrics["recovery_replays"] = sum(
                event.get("name") == "context_artifact_read"
                and event.get("status") == "completed"
                for event in (hook.events if hook else [])
            )
        if case.scenario == "context_pressure":
            workers = [r for r in probe_records if r.purpose == "worker"]
            sent = workers[0].estimated_input_tokens if workers else None
            candidate_tokens = scenario_metrics.get("candidate_input_tokens")
            scenario_metrics["sent_probe_input_tokens"] = sent
            scenario_metrics["compression_observed"] = bool(
                sent is not None
                and isinstance(candidate_tokens, int)
                and sent < candidate_tokens - 100
            )
            scenario_metrics["summary_request_count"] = sum(
                r.purpose == "summary" for r in probe_records
            )
            ratio = case.parameters.get("pressure_ratio")
            if (scenario_metrics.get("scenario_valid") is True and isinstance(ratio, (int, float))
                    and ratio > 1 and case.parameters.get("irreducible") is not True
                    and status in {"passed", "failed"} and not scenario_metrics["compression_observed"]):
                scenario_metrics["scenario_valid"] = False
                status, failure_reason = "error", "pressure_not_exercised"
            expected_facts = visible_facts([*case.initial_messages, {"content": case.user_inputs[-1]}])
            expected = {(f.topic, f.key): f for f in expected_facts}
            actual: dict[tuple[str, str], VisibleFact] = {}
            for request in workers:
                for fact in [
                    *visible_facts(request.messages),
                    *artifact_visible_facts(request.messages),
                ]:
                    key = (fact.topic, fact.key)
                    prior = actual.get(key)
                    if prior is None or fact.revision >= prior.revision:
                        actual[key] = fact
            scenario_metrics["constraint_retention"] = (
                sum(actual.get(key) == fact for key, fact in expected.items()) / len(expected) if expected else None)
            scenario_metrics["current_request_retained"] = bool(workers and any(
                case.user_inputs[-1] in str(message.get("content", "")) for message in workers[0].messages))
        if case.scenario == "memory_dependency" and hook:
            fact_path = canonical_hash(case.parameters.get("source"))
            earlier = {(r["path_hash"], r["content_hash"], r["offset"], r["limit"]) for r in hook.reads
                       if r["phase"] == "bootstrap" and r["path_hash"] == fact_path and r["content_hash"] is not None}
            repeated = sum((r["path_hash"], r["content_hash"], r["offset"], r["limit"]) in earlier
                           for r in hook.reads if r["phase"] == "probe" and r["content_hash"] is not None)
            scenario_metrics.update({"repeated_fact_read_count": repeated,
                                     "correct_without_fact_reread": status == "passed" and repeated == 0})
        if hook is not None:
            scenario_metrics["safety_bypass_count"] = hook.safety_bypass_count
            scenario_metrics["memory_save_latency_ms"] = (
                hook.memory_save_latencies_ms[0]
                if len(hook.memory_save_latencies_ms) == 1
                else None
            )
        expected_to_save = case.parameters.get("expected_to_save")
        if isinstance(expected_to_save, bool):
            claim_marker = case.parameters.get("save_claim_marker")
            claimed_saved = bool(
                isinstance(claim_marker, str)
                and any(claim_marker in answer for answer in answers)
            )
            scenario_metrics.update(evaluate_memory_save_attempt(
                expected_to_save=expected_to_save,
                committed=memory_committed,
                claimed_saved=claimed_saved,
            ))
        metrics: dict[str, JsonValue] = {
            **usage.model_dump(mode="json"), **scenario_metrics,
            "request_count": budget.requests, "tool_steps": budget.tool_steps,
            "estimated_input_tokens": sum(r.estimated_input_tokens for r in records),
            "reserved_tokens": budget.reserved_tokens, "failure_reason": failure_reason,
            "cleanup_error": cleanup_error,
            "context_manifest_count": len(context_manifests),
            "wall_seconds": time.monotonic() - budget.started,
        }
        trace_path = root / "trace.json"
        trace_path.write_text(json.dumps({
            "schema_version": 1, "case_id": case.id, "mode": mode,
            "requests": [record.public_record() for record in records],
            "tool_events": hook.events if hook else [],
            "fact_reads": hook.reads if hook else [], "scenario": scenario_trace,
            "context_manifests": list(context_manifests.values()),
            "calls": [{"request_id": record.request_id, "finish_reason": (
                           call.finish_reason if call.finish_reason in {"stop", "length", "tool_calls", "error", "content_filter"} else "unknown"),
                       "duration_ms": call.duration_ms} for record, call in zip(records, calls)],
            "verdicts": [verdict.model_dump(mode="json") for verdict in verdicts],
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        result = EvalResult(case_id=case.id, mode=mode, status=status, verdicts=verdicts,
                            metrics=metrics, trace_ref=str(trace_path), provenance=origin)
        (root / "result.json").write_text(result.model_dump_json(indent=2), encoding="utf-8")
        return result
