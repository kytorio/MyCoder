"""Shared execution loop for tool-using agents."""

from __future__ import annotations

import asyncio
import inspect
import os
import time
from collections.abc import Awaitable, Callable, Iterable
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from loguru import logger

from nanobot.agent.context import ContextBuilder, TranscriptInput
from nanobot.agent.context_artifacts import ToolResultArtifactStore
from nanobot.agent.context_governance import (
    ContextCompactionState,
    ContextGovernanceConfig,
    ContextGovernor,
    HistoryConsolidator,
    ModelRequestState,
    ProviderCompactionConsolidator,
    SummaryCheckpointCommitter,
    TranscriptBuilder,
)
from nanobot.agent.context_plan import (
    ContextConsumption,
    ContextPlan,
    ContextPlanError,
    ContextPlanner,
    ContextSource,
)
from nanobot.agent.context_sources import SchemaSelection, schema_name, select_tool_schemas
from nanobot.agent.hook import AgentHook, AgentHookContext, AgentRunHookContext
from nanobot.agent.tools.execution import execute_tool_calls
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.agent.tools.tool_discovery import (
    RESIDENT_TOOL_NAMES,
    ToolDiscoveryState,
    bind_tool_discovery_state,
    current_tool_discovery_state,
    reset_tool_discovery_state,
)
from nanobot.config.schema import ContextConfig
from nanobot.llm_usage.context import (
    LLMUsageSource,
    bind_llm_usage_source,
    reset_llm_usage_source,
    source_from_session_key,
)
from nanobot.providers.base import (
    LLMProvider,
    LLMResponse,
    LLMUsage,
    ProviderConversationState,
)
from nanobot.providers.conversation_state import ProviderConversationStateController
from nanobot.session.summary import SessionSummaryCheckpoint
from nanobot.utils.helpers import (
    build_assistant_message,
    estimate_message_tokens,
    estimate_prompt_tokens_chain,
    extract_reasoning,
    strip_reasoning_tags,
)
from nanobot.utils.llm_runtime import LLMRuntime
from nanobot.utils.prompt_templates import render_template
from nanobot.utils.runtime import (
    EMPTY_FINAL_RESPONSE_MESSAGE,
    build_budget_exhausted_finalization_message,
    build_finalization_retry_message,
    build_length_recovery_message,
    is_blank_text,
)

ContinuationCallback = Callable[[], str | None]
RetryWaitCallback = Callable[[str], Awaitable[None]]
CheckpointCallback = Callable[[dict[str, Any]], Awaitable[None]]
InjectionCallback = Callable[..., Awaitable[Iterable[Any] | None]]

_DEFAULT_ERROR_MESSAGE = "Sorry, I encountered an error calling the AI model."
_ARREARAGE_ERROR_MESSAGE = (
    "The AI provider rejected the request because the API key is out of quota or the "
    "account is in arrears. Please top up / check the billing status of your API key and try again."
)
_PERSISTED_MODEL_ERROR_PLACEHOLDER = "[Assistant reply unavailable due to model error.]"
_MAX_EMPTY_RETRIES = 2
_MAX_LENGTH_RECOVERIES = 3
_MAX_INJECTIONS_PER_TURN = 3
_MAX_INJECTION_CYCLES = 5
_CONTEXT_OVERFLOW_ERROR_TOKENS = frozenset({
    "context_length_exceeded",
    "context_window_exceeded",
    "input_too_long",
    "max_context_length_exceeded",
    "prompt_too_long",
    "request_too_large",
})


# 长度截断恢复（length recovery）拼接多个片段时，清洗步骤会丢弃首尾空白；
# 这里根据原始文本把首尾空白补回，避免拼接处出现多余或缺失的换行/空格。
def _restore_outer_whitespace(content: str, original: str | None) -> str:
    """Restore boundary whitespace stripped while cleaning one recovered segment."""
    if not original:
        return content
    leading_size = len(original) - len(original.lstrip())
    trailing_size = len(original) - len(original.rstrip())
    leading = original[:leading_size]
    trailing = original[-trailing_size:] if trailing_size else ""
    return f"{leading}{content}{trailing}"


# AgentRunSpec 是驱动一次 runner.run() 的全部只读输入：消息来源、模型运行时、
# 工具集、各类回调（检查点/中途注入/续写/重试等待），由调用方（AgentLoop）组装。
@dataclass(slots=True)
class AgentRunSpec:
    """Configuration for a single agent execution."""

    initial_messages: list[dict[str, Any]] | None
    tools: ToolRegistry
    runtime: LLMRuntime
    max_iterations: int
    max_tool_result_chars: int
    transcript_input: TranscriptInput | None = None
    transcript_builder: TranscriptBuilder | None = None
    context_source_collector: Callable[[TranscriptInput], list[ContextSource]] | None = None
    context_config: ContextConfig = field(default_factory=ContextConfig)
    runtime_data_dir: Path | None = None
    artifact_store: ToolResultArtifactStore | None = field(default=None, repr=False)
    initial_context_plan: ContextPlan | None = field(default=None, init=False, repr=False)
    context_consumption: ContextConsumption | None = field(default=None, repr=False)
    hook: AgentHook | None = None
    error_message: str | None = _DEFAULT_ERROR_MESSAGE
    max_iterations_message: str | None = None
    concurrent_tools: bool = False
    workspace: Path | None = None
    session_key: str | None = None
    context_block_limit: int | None = None
    provider_retry_mode: str = "standard"
    retry_wait_callback: RetryWaitCallback | None = None
    checkpoint_callback: CheckpointCallback | None = None
    summary_checkpoint_callback: SummaryCheckpointCommitter | None = None
    consolidate_history: HistoryConsolidator | None = None
    consolidate_provider_compaction: ProviderCompactionConsolidator | None = None
    injection_callback: InjectionCallback | None = None
    terminal_injection_callback: InjectionCallback | None = None
    llm_timeout_s: float | None = None
    continuation_callback: ContinuationCallback | None = None
    finalize_on_max_iterations: bool = True
    provider_state: ProviderConversationState | None = None
    llm_usage_source: LLMUsageSource | None = None


# AgentRunResult 是 run() 的完整产出，供 AgentLoop 落盘会话历史、
# 统计用量、判断是否需要流式补发终止内容等。
@dataclass(slots=True)
class AgentRunResult:
    """Outcome of a shared agent execution."""

    final_content: str | None
    messages: list[dict[str, Any]]
    tools_used: list[str] = field(default_factory=list)
    usage: LLMUsage | None = None
    # One entry per runner-visible model round. Recovery dispatches needed to
    # produce that round's response are folded into the same usage value.
    round_usages: list[LLMUsage] = field(default_factory=list)
    stop_reason: str = "completed"
    error: str | None = None
    tool_events: list[dict[str, str]] = field(default_factory=list)
    had_injections: bool = False
    # Terminal tail to emit when the preceding final-content prefix was already streamed.
    pending_stream_content: str | None = None
    provider_state: ProviderConversationState | None = field(default=None, repr=False)
    summary_checkpoint: SessionSummaryCheckpoint | None = field(default=None, repr=False)
    context_consumption: ContextConsumption | None = field(default=None, repr=False)
    provider_compaction_applied: bool = field(default=False, repr=False)


# AgentRunner 是与"产品层"（渠道、会话存储等）无关的纯执行内核：
# 反复调用模型 -> 执行工具 -> 把结果喂回模型，直到得到最终回复或触发终止条件。
# context_governor 负责单次请求前的上下文裁剪/压缩，与本类的多轮编排职责分离。
class AgentRunner:
    """Run a tool-capable LLM loop without product-layer concerns."""

    # 构造一个上下文治理器实例，在多次 run() 调用之间复用（本身无状态）。
    def __init__(self) -> None:
        self.context_governor = ContextGovernor()

    @staticmethod
    def _is_context_overflow_response(response: LLMResponse) -> bool:
        """Use provider metadata only; error prose is not trusted as a retry signal."""
        if response.finish_reason != "error":
            return False
        tokens = {
            str(value).strip().lower().replace("-", "_")
            for value in (response.error_kind, response.error_type, response.error_code)
            if value is not None
        }
        return bool(tokens & _CONTEXT_OVERFLOW_ERROR_TOKENS)

    # 把中途注入的消息原样追加到 messages 末尾，不改写已有的原始记录。
    @staticmethod
    def _append_injected_messages(
        messages: list[dict[str, Any]],
        injections: list[dict[str, Any]],
    ) -> None:
        """Append injected messages without rewriting the raw transcript."""
        messages.extend(injections)

    # 尝试排空一次“中途注入”（新用户消息/续写/终止等待），如有则把当前
    # assistant_message 落入 messages 并打检查点，让调用方继续迭代循环。
    async def _try_drain_injections(
        self,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
        assistant_message: dict[str, Any] | None,
        injection_cycles: int,
        *,
        conversation_state: ProviderConversationStateController | None = None,
        phase: str = "after error",
        iteration: int | None = None,
        allow_continuation: bool = False,
        wait_at_terminal: bool = False,
    ) -> tuple[bool, int]:
        """Drain pending injections. Returns (should_continue, updated_cycles).

        If injections are found and we haven't exceeded _MAX_INJECTION_CYCLES,
        append them to *messages* (and emit a checkpoint if *assistant_message*
        and *iteration* are both provided) and return (True, cycles+1) so the
        caller continues the iteration loop.  Otherwise return (False, cycles).
        """
        # 三种触发中途继续对话的来源，按优先级尝试：
        # 1) 调用方通过 injection_callback 塞入的新用户消息（如追问）；
        # 2) 没有新消息时，continuation_callback 允许调用方主动要求模型续写
        #    （例如"持续目标"场景）；
        # 3) 到达终止点时，terminal_injection_callback 限时等待可能的后续消息
        #    （见 _run_core 中 wait_at_terminal 的用法）。
        injections: list[dict[str, Any]] = []
        real_injection = False
        if injection_cycles < _MAX_INJECTION_CYCLES:
            injections = await self._drain_injections(spec)
            real_injection = bool(injections)
        if not injections and allow_continuation and assistant_message is not None:
            continuation = self._build_continuation_message(spec)
            if continuation is not None:
                injections = [continuation]
        if (
            not injections
            and wait_at_terminal
            and injection_cycles < _MAX_INJECTION_CYCLES
        ):
            injections = await self._drain_injections(spec, terminal=True)
            real_injection = bool(injections)
        if not injections:
            return False, injection_cycles
        if real_injection:
            injection_cycles += 1
        if assistant_message is not None:
            messages.append(assistant_message)
            if iteration is not None:
                checkpoint: dict[str, Any] = {
                    "phase": "final_response",
                    "iteration": iteration,
                    "model": spec.runtime.model,
                    "assistant_message": assistant_message,
                    "completed_tool_results": [],
                    "pending_tool_calls": [],
                }
                if conversation_state is not None:
                    checkpoint["provider_state"] = conversation_state.checkpoint(
                        messages
                    )
                await self._emit_checkpoint(
                    spec,
                    checkpoint,
                )
        self._append_injected_messages(messages, injections)
        if real_injection:
            logger.info(
                "Injected {} follow-up message(s) {} ({}/{})",
                len(injections), phase, injection_cycles, _MAX_INJECTION_CYCLES,
            )
        else:
            logger.info("Injected caller-requested continuation {}", phase)
        return True, injection_cycles

    # 调用调用方提供的 continuation_callback 生成一条“续写”用户消息（例如持续目标提示）；
    # 回调异常或返回空内容都视为“无需续写”。
    @staticmethod
    def _build_continuation_message(spec: AgentRunSpec) -> dict[str, str] | None:
        callback = spec.continuation_callback
        if callback is None:
            return None
        try:
            content = callback()
        except Exception:
            logger.exception("continuation_callback failed")
            return None
        if content is None or not content.strip():
            return None
        return {"role": "user", "content": content}

    # 调用注入回调（普通或终止态）取出待处理的用户消息，规范化为 {"role": "user", ...}
    # 字典列表，并按 _MAX_INJECTIONS_PER_TURN 截断，超出的部分记录日志而非静默丢弃。
    async def _drain_injections(
        self,
        spec: AgentRunSpec,
        *,
        terminal: bool = False,
    ) -> list[dict[str, Any]]:
        """Drain pending user messages via the injection callback.

        Returns normalized user messages (capped by
        ``_MAX_INJECTIONS_PER_TURN``), or an empty list when there is
        nothing to inject. Messages beyond the cap are logged so they
        are not silently lost.
        """
        callback = (
            spec.terminal_injection_callback
            if terminal
            else spec.injection_callback
        )
        if callback is None:
            return []
        try:
            signature = inspect.signature(callback)
            accepts_limit = (
                "limit" in signature.parameters
                or any(
                    parameter.kind is inspect.Parameter.VAR_KEYWORD
                    for parameter in signature.parameters.values()
                )
            )
            if accepts_limit:
                items = await callback(limit=_MAX_INJECTIONS_PER_TURN)
            else:
                items = await callback()
        except Exception:
            logger.exception("injection_callback failed")
            return []
        if not items:
            return []
        injected_messages: list[dict[str, Any]] = []
        for item in items:
            if item is None:
                continue
            if isinstance(item, dict):
                message_item = cast(dict[str, Any], item)
                if message_item.get("role") == "user" and "content" in message_item:
                    if self._has_injection_content(message_item.get("content")):
                        injected_messages.append(message_item)
                continue
            content = getattr(item, "content") if hasattr(item, "content") else str(item)
            if self._has_injection_content(content):
                injected_messages.append({"role": "user", "content": content})
        if len(injected_messages) > _MAX_INJECTIONS_PER_TURN:
            dropped = len(injected_messages) - _MAX_INJECTIONS_PER_TURN
            logger.warning(
                "Injection callback returned {} messages, capping to {} ({} dropped)",
                len(injected_messages), _MAX_INJECTIONS_PER_TURN, dropped,
            )
            injected_messages = injected_messages[:_MAX_INJECTIONS_PER_TURN]
        return injected_messages

    # 判断一条待注入消息的 content 是否包含实质内容（非空字符串或非空列表）。
    @staticmethod
    def _has_injection_content(content: Any) -> bool:
        if content is None:
            return False
        if isinstance(content, str):
            return bool(content.strip())
        if isinstance(content, list):
            return bool(cast(list[Any], content))
        return True

    # run() 是对外入口：负责 hook 生命周期（before_run/on_error/after_run/on_finally）
    # 与异常兜底，真正的多轮迭代逻辑委托给 _run_core。
    # 无论正常返回、被取消还是抛异常，on_finally 都必须执行一次（见 finally 块）。
    async def run(self, spec: AgentRunSpec) -> AgentRunResult:
        hook = spec.hook or AgentHook()
        messages, compaction = self._initial_transcript_and_compaction(spec)
        context = AgentRunHookContext(messages=deepcopy(messages))
        llm_usage_source_token = bind_llm_usage_source(
            spec.llm_usage_source or source_from_session_key(spec.session_key)
        )
        discovery_state = ToolDiscoveryState(spec.tools) if spec.context_config.schema_discovery else None
        discovery_state_token = bind_tool_discovery_state(discovery_state)

        try:
            await hook.before_run(context)
            result = await self._run_core(spec, hook, messages, compaction)
        except asyncio.CancelledError as exc:
            context.messages = deepcopy(messages)
            context.stop_reason = "cancelled"
            context.error = None
            context.exception = exc
            raise
        except Exception as exc:
            context.messages = deepcopy(messages)
            context.stop_reason = "error"
            context.error = f"Error: {type(exc).__name__}: {exc}"
            context.exception = exc
            await hook.on_error(context)
            raise
        else:
            context.messages = deepcopy(result.messages)
            context.final_content = result.final_content
            context.tools_used = list(result.tools_used)
            context.usage = result.usage
            context.stop_reason = result.stop_reason
            context.error = result.error
            context.tool_events = deepcopy(result.tool_events)
            context.had_injections = result.had_injections
            context.exception = None
            if context.error is not None:
                await hook.on_error(context)
            await hook.after_run(context)
            return result
        finally:
            try:
                context.messages = deepcopy(messages)
                if context.exception is None:
                    await hook.on_finally(context)
                else:
                    try:
                        await hook.on_finally(context)
                    except Exception:
                        logger.exception(
                            "AgentHook.on_finally error after {}",
                            context.stop_reason or "run exception",
                        )
            finally:
                try:
                    reset_tool_discovery_state(discovery_state_token)
                finally:
                    reset_llm_usage_source(llm_usage_source_token)

    # 根据 spec 二选一构建初始消息列表：要么用 transcript_input+transcript_builder
    # 组装（并返回压缩状态），要么直接使用调用方传入的 initial_messages（不支持压缩）。
    @staticmethod
    def _initial_transcript_and_compaction(
        spec: AgentRunSpec,
    ) -> tuple[list[dict[str, Any]], ContextCompactionState | None]:
        """Build the initial transcript and its optional compaction state."""
        transcript_input = spec.transcript_input
        if transcript_input is not None:
            if spec.initial_messages is not None:
                raise ValueError("provide either transcript_input or initial_messages, not both")
            transcript_builder = spec.transcript_builder
            if transcript_builder is None:
                raise ValueError("transcript_builder is required with transcript_input")
            legacy_builder = transcript_builder
            if spec.context_source_collector is not None:
                sources = spec.context_source_collector(transcript_input)
                spec.initial_context_plan = ContextPlanner().plan(sources, input_budget=None)
                if spec.context_config.mode == "enforce":
                    rendered = ContextBuilder.render_plan(spec.initial_context_plan)

                    def selected_builder(_: TranscriptInput) -> list[dict[str, Any]]:
                        return rendered.messages

                    transcript_builder = selected_builder
            messages, compaction = ContextCompactionState.from_transcript(
                transcript_input,
                transcript_builder,
                spec.consolidate_history,
                spec.consolidate_provider_compaction,
                consumption=spec.context_consumption,
                track_consumption=spec.context_config.mode == "enforce",
            )
            if compaction is not None:
                compaction.transcript_builder = legacy_builder
            return messages, compaction
        if spec.initial_messages is None:
            raise ValueError("initial_messages is required without transcript_input")
        if spec.consolidate_history is not None:
            raise ValueError("consolidate_history requires transcript_input")
        return list(spec.initial_messages), None

    # 核心多轮迭代循环：每轮请求模型 -> 若有工具调用则执行并把结果喂回，
    # 否则处理"空响应重试/长度截断续写/中途注入/错误"等收尾分支，直到给出最终答案
    # 或达到 max_iterations（for...else 的 else 分支处理超预算收尾）。
    async def _run_core(
        self,
        spec: AgentRunSpec,
        hook: AgentHook,
        messages: list[dict[str, Any]],
        compaction: ContextCompactionState | None,
    ) -> AgentRunResult:
        final_content: str | None = None
        tools_used: list[str] = []
        usage: LLMUsage | None = None
        round_usages: list[LLMUsage] = []
        error: str | None = None
        stop_reason = "completed"
        tool_events: list[dict[str, str]] = []
        external_lookup_counts: dict[str, int] = {}
        # Per-turn throttle for repeated attempts against the same outside target.
        workspace_violation_counts: dict[str, int] = {}
        empty_content_retries = 0
        # Segments from one uninterrupted length-recovery chain. Tool work or
        # injected user input starts a new logical answer and clears the chain.
        length_recovery_parts: list[str] = []
        had_injections = False
        injection_cycles = 0
        pending_stream_content: str | None = None
        conversation_state = ProviderConversationStateController(
            provider=spec.runtime.provider,
            model=spec.runtime.model,
            messages=messages,
            state=spec.provider_state,
            session_id=spec.session_key,
        )
        governance_config = ContextGovernanceConfig(
            provider=spec.runtime.provider,
            model=spec.runtime.model,
            tools=spec.tools,
            workspace=spec.workspace,
            session_key=spec.session_key,
            max_tool_result_chars=spec.max_tool_result_chars,
            context_window_tokens=spec.runtime.context_window_tokens,
            context_block_limit=spec.context_block_limit,
            max_tokens=spec.runtime.generation.max_tokens,
            context=spec.context_config,
            runtime_data_dir=spec.runtime_data_dir,
            artifact_store=spec.artifact_store,
            initial_plan=spec.initial_context_plan,
            context_source_collector=spec.context_source_collector,
            summary_checkpoint_commit=spec.summary_checkpoint_callback,
        )
        request_state = ModelRequestState(
            config=governance_config,
            conversation=conversation_state,
            compaction=compaction,
        )

        for iteration in range(spec.max_iterations):
            context = AgentHookContext(
                iteration=iteration,
                messages=messages,
                session_key=spec.session_key,
            )
            await hook.before_iteration(context)
            request_message_count = len(messages)
            request_messages = (
                request_state.compaction.request_messages(messages, observe=spec.context_config.mode == "observe")
                if request_state.compaction is not None
                else messages
            )
            response, raw_usage = await self._request_model(
                spec,
                request_messages,
                hook,
                context,
                request_state=request_state,
                transcript=messages,
            )
            assert request_state.messages is not None
            messages_for_model = request_state.messages
            conversation_state.observe_response(response, messages)
            if request_state.compaction is not None and spec.context_config.mode == "observe":
                request_state.compaction.legacy_messages = deepcopy(messages_for_model)
                request_state.compaction.legacy_raw_boundary = request_message_count
            context.response = response
            context.tool_calls = list(response.tool_calls)

            original_content = response.content
            reasoning_text, cleaned_content = extract_reasoning(
                response.reasoning_content,
                response.thinking_blocks,
                response.content,
            )
            response.content = cleaned_content
            round_usages.append(raw_usage)
            context.usage = raw_usage
            usage = self._merge_usage(usage, raw_usage)
            if reasoning_text and not context.streamed_reasoning:
                await hook.emit_reasoning(reasoning_text)
                await hook.emit_reasoning_end()
                context.streamed_reasoning = True

            if response.should_execute_tools:
                context.tool_calls = list(response.tool_calls)
                if hook.wants_streaming():
                    await hook.on_stream_end(context, resuming=True)

                assistant_message = build_assistant_message(
                    response.content or "",
                    tool_calls=[tc.to_openai_tool_call() for tc in response.tool_calls],
                    reasoning_content=response.reasoning_content,
                    thinking_blocks=response.thinking_blocks,
                )
                assistant_message = conversation_state.project_response_message(
                    assistant_message,
                    response,
                )
                messages.append(assistant_message)
                await self._emit_checkpoint(
                    spec,
                    {
                        "phase": "awaiting_tools",
                        "iteration": iteration,
                        "model": spec.runtime.model,
                        "assistant_message": assistant_message,
                        "completed_tool_results": [],
                        "pending_tool_calls": [tc.to_openai_tool_call() for tc in response.tool_calls],
                    },
                )

                await hook.before_execute_tools(context)

                results, new_events = await execute_tool_calls(
                    spec.tools,
                    response.tool_calls,
                    concurrent=spec.concurrent_tools,
                    external_lookup_counts=external_lookup_counts,
                    workspace_violation_counts=workspace_violation_counts,
                    hook=hook,
                    context=context,
                )
                tool_events.extend(new_events)
                tools_used.extend(
                    tool_call.name
                    for tool_call, event in zip(response.tool_calls, new_events)
                    if event.get("status") == "ok"
                )
                context.tool_results = list(results)
                context.tool_events = list(new_events)
                completed_tool_results: list[dict[str, Any]] = []
                for tool_call, result, event in zip(response.tool_calls, results, new_events):
                    tool_message = {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "name": tool_call.name,
                        "content": self.context_governor.normalize_tool_result(
                            governance_config,
                            tool_call.id,
                            tool_call.name,
                            result,
                        ),
                    }
                    if spec.context_config.mode == "enforce":
                        tool_message["_meta"] = {"tool_result_error": event.get("status") == "error"}
                    messages.append(tool_message)
                    completed_tool_results.append(tool_message)
                checkpoint_model_messages = (
                    self.context_governor.prepare_messages_for_model(
                        governance_config,
                        messages,
                    )
                    if response.provider_state is not None
                    else None
                )
                await self._emit_checkpoint(
                    spec,
                    {
                        "phase": "tools_completed",
                        "iteration": iteration,
                        "model": spec.runtime.model,
                        "assistant_message": assistant_message,
                        "completed_tool_results": completed_tool_results,
                        "pending_tool_calls": [],
                        "provider_state": conversation_state.checkpoint(
                            messages,
                            model_messages=checkpoint_model_messages,
                        ),
                    },
                )
                empty_content_retries = 0
                length_recovery_parts.clear()
                # Checkpoint 1: drain injections after tools, before next LLM call
                _drained, injection_cycles = await self._try_drain_injections(
                    spec, messages, None, injection_cycles,
                    phase="after tool execution",
                )
                if _drained:
                    had_injections = True
                await hook.after_iteration(context)
                continue

            if response.has_tool_calls:
                logger.warning(
                    "Ignoring tool calls under finish_reason='{}' for {}",
                    response.finish_reason,
                    spec.session_key or "default",
                )

            clean = hook.finalize_content(context, response.content)
            if (
                response.finish_reason
                not in {"error", "length", "refusal", "content_filter"}
                and is_blank_text(clean)
            ):
                empty_content_retries += 1
                if empty_content_retries < _MAX_EMPTY_RETRIES:
                    logger.warning(
                        "Empty response on turn {} for {} ({}/{}); retrying",
                        iteration,
                        spec.session_key or "default",
                        empty_content_retries,
                        _MAX_EMPTY_RETRIES,
                    )
                    if hook.wants_streaming():
                        await hook.on_stream_end(context, resuming=False)
                    await hook.after_iteration(context)
                    continue
                logger.warning(
                    "Empty response on turn {} for {} after {} retries; attempting finalization",
                    iteration,
                    spec.session_key or "default",
                    empty_content_retries,
                )
                if hook.wants_streaming():
                    await hook.on_stream_end(context, resuming=False)
                response = await self._request_finalization_retry(
                    spec,
                    messages_for_model,
                    request_state=request_state,
                    transcript=messages,
                )
                retry_usage = self._record_request_usage(spec, request_state, response)
                round_usages.append(retry_usage)
                usage = self._merge_usage(usage, retry_usage)
                raw_usage = self._merge_usage(raw_usage, retry_usage)
                context.response = response
                context.usage = raw_usage
                context.tool_calls = list(response.tool_calls)
                original_content = response.content
                clean = hook.finalize_content(context, response.content)

            if response.finish_reason == "length":
                if len(length_recovery_parts) < _MAX_LENGTH_RECOVERIES:
                    length_recovery_parts.append(
                        _restore_outer_whitespace(clean or "", original_content)
                    )
                    logger.info(
                        "Output truncated on turn {} for {} ({}/{}); continuing",
                        iteration,
                        spec.session_key or "default",
                        len(length_recovery_parts),
                        _MAX_LENGTH_RECOVERIES,
                    )
                    if hook.wants_streaming():
                        context.stream_continues_current_message = True
                        await hook.on_stream_end(context, resuming=True)
                    messages.append(conversation_state.project_response_message(
                        build_assistant_message(
                            clean,
                            reasoning_content=response.reasoning_content,
                            thinking_blocks=response.thinking_blocks,
                        ),
                        response,
                    ))
                    messages.append(build_length_recovery_message(clean or ""))
                    await hook.after_iteration(context)
                    continue

            # Some streaming providers recover with a complete response but no
            # content deltas. When an earlier length segment is already visible,
            # emit this terminal segment into the same stream; otherwise the
            # regular full response would duplicate the visible prefix.
            if (
                length_recovery_parts
                and hook.wants_streaming()
                and not context.streamed_content
                and response.finish_reason != "error"
                and not is_blank_text(clean)
            ):
                await hook.on_stream(
                    context,
                    _restore_outer_whitespace(clean or "", original_content),
                )
                context.streamed_content = True

            assistant_message: dict[str, Any] | None = None
            if response.finish_reason != "error" and not is_blank_text(clean):
                assistant_message = build_assistant_message(
                    clean,
                    reasoning_content=response.reasoning_content,
                    thinking_blocks=response.thinking_blocks,
                )
                assistant_message = conversation_state.project_response_message(
                    assistant_message,
                    response,
                )

            # Check for mid-turn injections BEFORE signaling stream end.
            # If injections are found we keep the stream alive (resuming=True)
            # so streaming channels don't prematurely finalize the card.
            should_continue, injection_cycles = await self._try_drain_injections(
                spec, messages, assistant_message, injection_cycles,
                conversation_state=conversation_state,
                phase="after final response",
                iteration=iteration,
                allow_continuation=(
                    response.finish_reason not in {"refusal", "content_filter"}
                ),
                wait_at_terminal=(
                    assistant_message is not None
                    and response.finish_reason
                    not in {"error", "length", "refusal", "content_filter"}
                ),
            )
            if should_continue:
                had_injections = True

            if hook.wants_streaming():
                await hook.on_stream_end(context, resuming=should_continue)

            if should_continue:
                length_recovery_parts.clear()
                await hook.after_iteration(context)
                continue

            if response.finish_reason == "error":
                if LLMProvider.is_arrearage_response(response):
                    final_content = _ARREARAGE_ERROR_MESSAGE
                else:
                    final_content = clean or spec.error_message or _DEFAULT_ERROR_MESSAGE
                stop_reason = "error"
                error = final_content
                self._append_model_error_placeholder(messages)
                context.final_content = final_content
                context.error = error
                context.stop_reason = stop_reason
                await hook.after_iteration(context)
                should_continue, injection_cycles = await self._try_drain_injections(
                    spec, messages, None, injection_cycles,
                    phase="after LLM error",
                )
                if should_continue:
                    had_injections = True
                    length_recovery_parts.clear()
                    continue
                break
            if is_blank_text(clean):
                final_content = EMPTY_FINAL_RESPONSE_MESSAGE
                stop_reason = "empty_final_response"
                error = final_content
                self._append_final_message(messages, final_content)
                context.final_content = final_content
                context.error = error
                context.stop_reason = stop_reason
                await hook.after_iteration(context)
                should_continue, injection_cycles = await self._try_drain_injections(
                    spec, messages, None, injection_cycles,
                    phase="after empty response",
                )
                if should_continue:
                    had_injections = True
                    length_recovery_parts.clear()
                    continue
                break

            messages.append(
                assistant_message
                or conversation_state.project_response_message(
                    build_assistant_message(
                        clean,
                        reasoning_content=response.reasoning_content,
                        thinking_blocks=response.thinking_blocks,
                    ),
                    response,
                )
            )
            await self._emit_checkpoint(
                spec,
                {
                    "phase": "final_response",
                    "iteration": iteration,
                    "model": spec.runtime.model,
                    "assistant_message": messages[-1],
                    "completed_tool_results": [],
                    "pending_tool_calls": [],
                    "provider_state": conversation_state.checkpoint(messages),
                },
            )
            if length_recovery_parts:
                final_content = (
                    "".join(length_recovery_parts)
                    + _restore_outer_whitespace(clean or "", original_content)
                ).strip()
            else:
                final_content = clean
            context.final_content = final_content
            context.stop_reason = stop_reason
            await hook.after_iteration(context)
            break
        else:
            stop_reason = "max_iterations"
            # Drain any remaining injections so they are appended to the
            # conversation history instead of being re-published as
            # independent inbound messages by _dispatch's finally block.
            # We include them before the no-tools finalization pass so the
            # final response can account for every known follow-up.
            drained_after_max_iterations, injection_cycles = await self._try_drain_injections(
                spec, messages, None, injection_cycles,
                phase="after max_iterations",
            )
            if drained_after_max_iterations:
                had_injections = True
            terminal_content = None
            if spec.finalize_on_max_iterations:
                terminal_content, usage = await self._try_finalize_after_max_iterations(
                    spec,
                    hook,
                    messages,
                    usage,
                    request_state=request_state,
                    round_usages=round_usages,
                )
            if terminal_content is None:
                terminal_content = self._max_iterations_fallback(spec)
            if length_recovery_parts:
                terminal_tail = f"\n\n{terminal_content.lstrip()}"
                final_content = (
                    "".join(length_recovery_parts).rstrip() + terminal_tail
                ).strip()
                pending_stream_content = terminal_tail
            else:
                final_content = terminal_content
            self._append_final_message(messages, terminal_content)

        return AgentRunResult(
            final_content=final_content,
            messages=messages,
            tools_used=tools_used,
            usage=usage,
            round_usages=round_usages,
            stop_reason=stop_reason,
            error=error,
            tool_events=tool_events,
            had_injections=had_injections,
            pending_stream_content=pending_stream_content,
            provider_state=conversation_state.finish(messages),
            summary_checkpoint=(
                request_state.compaction.summary_checkpoint
                if request_state.compaction is not None
                else None
            ),
            provider_compaction_applied=request_state.provider_compaction_applied,
            context_consumption=(compaction.consumption() if compaction is not None else None),
        )

    # 把 runtime 的生成参数（温度/max_tokens/reasoning_effort 等）与消息/工具
    # 打包成 provider.chat*_with_retry 所需的关键字参数。
    def _build_request_kwargs(
        self,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "messages": messages,
            "tools": tools,
            "model": spec.runtime.model,
            "retry_mode": spec.provider_retry_mode,
            "on_retry_wait": spec.retry_wait_callback,
        }
        generation = spec.runtime.generation
        kwargs["temperature"] = generation.temperature
        kwargs["max_tokens"] = generation.max_tokens
        kwargs["reasoning_effort"] = generation.reasoning_effort
        return kwargs

    # 发起一次模型请求（流式或非流式），处理超时/取消、原生推理流的开合、
    # provider 侧压缩汇总，并在检测到"畸形工具调用"时递归重试或降级为无工具请求。
    async def _request_model(
        self,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
        hook: AgentHook,
        context: AgentHookContext,
        *,
        request_state: ModelRequestState,
        malformed_retry: bool = False,
        transcript: list[dict[str, Any]] | None,
    ) -> tuple[LLMResponse, LLMUsage]:
        timeout_s = self._resolve_llm_timeout_s(spec)
        schema_selection = self._tool_definitions_for_request(spec)
        tool_definitions = schema_selection.definitions
        messages, provider_context = await self.context_governor.prepare_request(
            request_state,
            messages,
            tool_definitions=tool_definitions,
            transcript=transcript,
            deferred_tool_names=schema_selection.deferred_names,
        )

        kwargs = self._build_request_kwargs(
            spec,
            messages,
            tools=tool_definitions,
        )
        self.context_governor.record_dispatch(request_state)
        wants_streaming = hook.wants_streaming()

        active_hosted_tools: dict[str, dict[str, Any]] = {}
        native_reasoning_open = False
        native_reasoning_close_task: asyncio.Task[None] | None = None
        request_started_at = 0.0
        first_output_at: float | None = None
        generation_started_at: float | None = None
        generation_elapsed_s = 0.0

        # 记录首个输出字符的时间（用于计算 TTFT）并标记生成计时的起点。
        def _generation_delta(delta: str) -> None:
            nonlocal first_output_at, generation_started_at
            if not delta:
                return
            now = time.perf_counter()
            if first_output_at is None:
                first_output_at = now
            if generation_started_at is None:
                generation_started_at = now

        # 流中断/恢复时暂停生成计时，把已经过的时长累加到 generation_elapsed_s。
        def _pause_generation() -> None:
            nonlocal generation_elapsed_s, generation_started_at
            if generation_started_at is None:
                return
            generation_elapsed_s += max(0.0, time.perf_counter() - generation_started_at)
            generation_started_at = None

        # 关闭当前打开的"原生推理"流段（emit_reasoning_end），并安全地等待/传播其取消。
        async def _close_native_reasoning() -> None:
            nonlocal native_reasoning_open, native_reasoning_close_task
            if native_reasoning_close_task is None:
                if not native_reasoning_open:
                    return
                native_reasoning_open = False
                native_reasoning_close_task = asyncio.create_task(
                    hook.emit_reasoning_end()
                )

            close_task = native_reasoning_close_task
            cancellation: asyncio.CancelledError | None = None
            while not close_task.done():
                try:
                    await asyncio.shield(close_task)
                except asyncio.CancelledError as exc:
                    cancellation = cancellation or exc
            try:
                close_task.result()
            finally:
                if native_reasoning_close_task is close_task:
                    native_reasoning_close_task = None
            if cancellation is not None:
                raise cancellation

        # 处理 provider 侧托管工具（hosted tool）事件：先关闭原生推理流，转发给 hook，
        # 并维护 active_hosted_tools 以便请求异常时能补发“error”事件收尾。
        async def _provider_tool_event(event: dict[str, Any]) -> None:
            if event.get("kind") != "hosted_tool":
                return
            await _close_native_reasoning()
            await hook.on_provider_tool_event(context, event)
            call_id = event.get("call_id")
            if not call_id:
                return
            call_id = str(call_id)
            if event.get("phase") == "start":
                active_hosted_tools[call_id] = dict(event)
            elif event.get("phase") in {"end", "error"}:
                active_hosted_tools.pop(call_id, None)

        if wants_streaming:
            thinking_buf = ""

            # 流式正文回调：记录生成计时，首次出现正文内容时关闭原生推理流，再转发给 hook。
            async def _stream(delta: str) -> None:
                _generation_delta(delta)
                if delta:
                    context.streamed_content = True
                    await _close_native_reasoning()
                await hook.on_stream(context, delta)

            # 流式推理（thinking）回调：对累积的原始推理文本做增量清洗后再转发，
            # 避免把标签中间截断的片段直接展示给用户。
            async def _thinking(delta: str) -> None:
                nonlocal native_reasoning_open, thinking_buf
                if not delta:
                    return
                _generation_delta(delta)
                prev_clean = strip_reasoning_tags(thinking_buf)
                thinking_buf += delta
                new_clean = strip_reasoning_tags(thinking_buf)
                incremental = new_clean[len(prev_clean):]
                if incremental:
                    context.streamed_reasoning = True
                    native_reasoning_open = True
                    await hook.emit_reasoning(incremental)

            # 底层连接中断后 provider 触发内部重连恢复：暂停计时、收尾原生推理流，
            # 并通知 hook 当前流段结束（resuming=True 表示后面还有内容续上）。
            async def _stream_recover() -> None:
                _pause_generation()
                await _close_native_reasoning()
                await hook.on_stream_end(context, resuming=True)

            coro = spec.runtime.provider.chat_stream_with_retry(
                **kwargs,
                provider_context=provider_context,
                on_content_delta=_stream,
                on_thinking_delta=_thinking,
                on_tool_call_delta=_provider_tool_event,
                on_stream_recover=_stream_recover,
            )
        else:
            coro = spec.runtime.provider.chat_with_retry(
                **kwargs,
                provider_context=provider_context,
            )

        # Streaming requests also have provider-level idle timeouts
        # (NANOBOT_STREAM_IDLE_TIMEOUT_S), but a stream that keeps producing
        # very slow deltas can still run forever. Use a more generous wall-clock
        # timeout for streaming while preserving NANOBOT_LLM_TIMEOUT_S=0 as an
        # opt-out for all LLM wall-clock timeouts.
        outer_timeout_s = (
            max(300.0, timeout_s * 2)
            if wants_streaming and timeout_s is not None
            else timeout_s
        )
        request_started_at = time.perf_counter()
        try:
            response = (
                await coro if outer_timeout_s is None
                else await asyncio.wait_for(coro, timeout=outer_timeout_s)
            )
        except asyncio.CancelledError:
            _pause_generation()
            await _close_native_reasoning()
            raise
        except asyncio.TimeoutError:
            if outer_timeout_s is None:
                response = LLMResponse(
                    content="Error calling LLM: stream stalled",
                    finish_reason="error",
                    error_kind="timeout",
                )
            else:
                response = LLMResponse(
                    content=f"Error calling LLM: timed out after {outer_timeout_s:g}s",
                    finish_reason="error",
                    error_kind="timeout",
                )
        _pause_generation()
        await _close_native_reasoning()
        if first_output_at is not None:
            response.ttft_ms = max(0, round((first_output_at - request_started_at) * 1000))
        if generation_elapsed_s > 0:
            response.generation_ms = max(1, round(generation_elapsed_s * 1000))
        await self.context_governor.summarize_provider_compaction(
            request_state,
            response,
            current_request_boundary=(len(transcript) if transcript is not None else None),
        )
        self.context_governor.record_response(request_state, response)
        request_state.provider_compaction_applied |= response.provider_compaction_applied
        round_usage = self._record_request_usage(spec, request_state, response)
        if (
            spec.context_config.mode == "enforce"
            and "L4" in spec.context_config.enabled_layers
            and not request_state.overflow_l4_attempted
            and self._is_context_overflow_response(response)
        ):
            request_state.overflow_l4_attempted = True
            request_state.force_l4_reason = "overflow"
            compaction = request_state.compaction
            retry_messages = (
                compaction.request_messages(transcript)
                if compaction is not None and transcript is not None
                else messages
            )
            try:
                retry_response, retry_usage = await self._request_model(
                    spec,
                    retry_messages,
                    hook,
                    context,
                    request_state=request_state,
                    malformed_retry=malformed_retry,
                    transcript=transcript,
                )
            except ContextPlanError:
                request_state.force_l4_reason = None
                logger.warning(
                    "Context-overflow L4 retry could not establish a valid checkpoint for {}",
                    spec.session_key or "default",
                )
            else:
                return retry_response, round_usage + retry_usage
        # chat_stream_with_retry may recover internally, so only fail unfinished
        # hosted calls after the provider returns its final error response.
        if response.finish_reason == "error":
            for event in list(active_hosted_tools.values()):
                await _provider_tool_event({
                    **event,
                    "phase": "error",
                    "result": None,
                    "error": response.content
                    or "Model request failed before the provider-hosted tool completed.",
                })
        dropped, all_dropped, original_finish_reason = (
            self._drop_malformed_tool_calls(response)
        )
        if (
            all_dropped
            and original_finish_reason in ("tool_calls", "function_call")
            and not malformed_retry
        ):
            logger.warning(
                "Retrying LLM request after all {} malformed tool call(s) were dropped",
                dropped,
            )
            retry_messages = self._malformed_tool_call_retry_messages(
                messages, response.content,
            )
            retry_response, retry_usage = await self._request_model(
                spec, retry_messages, hook, context,
                request_state=request_state,
                malformed_retry=True,
                transcript=None,
            )
            return retry_response, round_usage + retry_usage
        if (
            all_dropped
            and original_finish_reason in ("tool_calls", "function_call")
            and malformed_retry
        ):
            logger.warning(
                "Malformed tool calls persisted after retry; falling back to no-tools request",
            )
            fallback_messages = self._malformed_tool_call_retry_messages(
                messages, response.content,
            )
            fallback_response = await self._request_no_tools(
                spec,
                fallback_messages,
                request_state=request_state,
            )
            fallback_usage = self._record_request_usage(
                spec,
                request_state,
                fallback_response,
            )
            return fallback_response, round_usage + fallback_usage
        return response, round_usage

    # 剔除响应中名称缺失/非字符串的“畸形”工具调用（否则会被持久化后在后续每一轮
    # 重放，导致上游校验永久报错、会话卡死）。返回被丢弃数量、是否全部丢弃、原始 finish_reason。
    @staticmethod
    def _drop_malformed_tool_calls(
        response: LLMResponse,
    ) -> tuple[int, bool, str | None]:
        """Strip tool calls whose name is missing/non-string from the response.

        Returns (dropped_count, all_dropped, original_finish_reason).

        A degenerate call (name=None or "") cannot be executed, and if it were
        persisted into the assistant message it would be replayed on every
        subsequent turn, causing upstream validation errors
        (``tool_use.name: Input should be a valid string``) that permanently
        wedge the session. Dropping it here keeps it out of execution, the
        assistant message, and the saved history in one place.
        """
        calls = getattr(response, "tool_calls", None)
        if not calls:
            return (0, False, getattr(response, "finish_reason", None))
        valid = [tc for tc in calls if tc.has_valid_name()]
        if len(valid) == len(calls):
            return (0, False, getattr(response, "finish_reason", None))
        dropped = len(calls) - len(valid)
        original_finish_reason = getattr(response, "finish_reason", None)
        logger.warning(
            "Dropped {} malformed tool call(s) with missing/non-string name "
            "from LLM response (finish_reason={!r})",
            dropped,
            original_finish_reason,
        )
        response.tool_calls = valid
        # The opaque candidate still contains every raw function_call item.
        # Advancing it after dropping even one call would replay an unmatched
        # call without a corresponding tool output on the next request.
        response.provider_state = None
        if not valid:
            response.finish_reason = "stop"
        return (dropped, not valid, original_finish_reason)

    # 在消息末尾追加一条说明，告知模型上一轮工具调用因名称畸形被拒绝，
    # 要求其重新给出有效工具调用或直接给出最终答案。
    @staticmethod
    def _malformed_tool_call_retry_messages(
        messages: list[dict[str, Any]],
        assistant_text: str | None,
    ) -> list[dict[str, Any]]:
        retry_messages = list(messages)
        note = (
            "The previous model response attempted to call tools, but every tool call "
            "was malformed: the tool_use blocks had missing or non-string tool names. "
            "Do not answer with a promise to use tools. Either call the required tools again "
            "using valid tool names from the provided tool list and JSON object inputs, or give "
            "a final answer only if no tool is required."
        )
        if assistant_text:
            note += (
                f"\n\nPrevious assistant text before the malformed calls:\n"
                f"{assistant_text}"
            )
        retry_messages.append({"role": "user", "content": note})
        return retry_messages

    # 连续多次拿到空响应后，追加“请给出最终答案”的提示并发起一次无工具请求，
    # 强制模型收尾；不把该次请求计入常规 conversation_state 的候选推进。
    async def _request_finalization_retry(
        self,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
        *,
        request_state: ModelRequestState,
        transcript: list[dict[str, Any]],
    ) -> LLMResponse:
        retry_messages = self._finalization_retry_messages(messages)
        response = await self._request_no_tools(
            spec,
            retry_messages,
            request_state=request_state,
            transcript=transcript,
        )
        request_state.conversation.observe_response(
            response,
            transcript,
            adopt_candidate_state=False,
        )
        return response

    @staticmethod
    def _tool_definitions_for_request(spec: AgentRunSpec) -> SchemaSelection:
        """Select complete schemas from the effective registry for this request."""
        definitions = spec.tools.get_definitions()
        if not spec.context_config.schema_discovery:
            return SchemaSelection(definitions=definitions, deferred_names=())

        state = current_tool_discovery_state()
        if state is None or state.registry is not spec.tools:
            raise RuntimeError("tool discovery state is not bound to the effective registry")

        business_definitions = [
            definition
            for definition in definitions
            if schema_name(definition) not in RESIDENT_TOOL_NAMES
        ]
        selected = select_tool_schemas(
            business_definitions,
            state.loaded_names,
            discovery_enabled=True,
        )
        resident_definitions: list[dict[str, Any]] = []
        resident_names: set[str] = set()
        for definition in definitions:
            name = schema_name(definition)
            if name in RESIDENT_TOOL_NAMES and name not in resident_names:
                resident_definitions.append(definition)
                resident_names.add(name)
        return SchemaSelection(
            definitions=[*selected.definitions, *resident_definitions],
            deferred_names=selected.deferred_names,
        )

    # 追加“请给出最终答案”模板消息，供 _request_finalization_retry 使用。
    @staticmethod
    def _finalization_retry_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        retry_messages = list(messages)
        retry_messages.append(build_finalization_retry_message())
        return retry_messages

    # 达到 max_iterations 后的最后一次“抢救”：发起一次无工具的收尾请求；
    # 若该请求本身出错、仍要工具调用、或最终内容为空，则放弃并让调用方使用兜底文案。
    async def _try_finalize_after_max_iterations(
        self,
        spec: AgentRunSpec,
        hook: AgentHook,
        messages: list[dict[str, Any]],
        usage: LLMUsage | None,
        *,
        request_state: ModelRequestState,
        round_usages: list[LLMUsage],
    ) -> tuple[str | None, LLMUsage | None]:
        compaction = request_state.compaction
        request_messages = (
            compaction.request_messages(messages, observe=spec.context_config.mode == "observe")
            if compaction is not None
            else messages
        )
        retry_messages = self._budget_exhausted_finalization_messages(request_messages)
        try:
            response = await self._request_no_tools(
                spec,
                retry_messages,
                request_state=request_state,
                transcript=messages if compaction is not None else None,
            )
        except Exception:
            logger.exception(
                "Budget-exhausted finalization failed for {}; using fallback",
                spec.session_key or "default",
            )
            return None, usage

        raw_usage = self._record_request_usage(spec, request_state, response)
        round_usages.append(raw_usage)
        usage = self._merge_usage(usage, raw_usage)
        if response.finish_reason == "error" or response.has_tool_calls:
            logger.warning(
                "Budget-exhausted finalization returned finish_reason='{}' "
                "with {} tool call(s) for {}; using fallback",
                response.finish_reason,
                len(response.tool_calls),
                spec.session_key or "default",
            )
            return None, usage

        context = AgentHookContext(
            iteration=spec.max_iterations,
            messages=messages,
            response=response,
            usage=raw_usage,
            session_key=spec.session_key,
        )
        clean = hook.finalize_content(context, response.content)
        if is_blank_text(clean):
            return None, usage
        return clean, usage

    # 发起一次不带工具定义的模型请求（非流式），用于各类“强制收尾”场景；
    # 同样走超时与 provider 压缩汇总，但不处理畸形工具调用重试（本身就没有工具）。
    async def _request_no_tools(
        self,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
        *,
        request_state: ModelRequestState,
        transcript: list[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        source_messages = messages
        messages, provider_context = await self.context_governor.prepare_request(
            request_state,
            messages,
            tool_definitions=None,
            transcript=transcript,
        )
        kwargs = self._build_request_kwargs(
            spec,
            messages,
            tools=None,
        )
        coro = spec.runtime.provider.chat_with_retry(
            **kwargs,
            provider_context=provider_context,
        )
        self.context_governor.record_dispatch(request_state)
        timeout_s = self._resolve_llm_timeout_s(spec)
        try:
            response = (
                await coro
                if timeout_s is None
                else await asyncio.wait_for(coro, timeout=timeout_s)
            )
        except asyncio.TimeoutError:
            response = LLMResponse(
                content=f"Error calling LLM: timed out after {timeout_s:g}s",
                finish_reason="error",
                error_kind="timeout",
            )
        await self.context_governor.summarize_provider_compaction(
            request_state,
            response,
            current_request_boundary=(len(transcript) if transcript is not None else None),
        )
        self.context_governor.record_response(request_state, response)
        request_state.provider_compaction_applied |= response.provider_compaction_applied
        if (
            spec.context_config.mode == "enforce"
            and "L4" in spec.context_config.enabled_layers
            and not request_state.overflow_l4_attempted
            and self._is_context_overflow_response(response)
        ):
            request_state.overflow_l4_attempted = True
            request_state.force_l4_reason = "overflow"
            compaction = request_state.compaction
            retry_messages = (
                compaction.request_messages(transcript)
                if compaction is not None and transcript is not None
                else source_messages
            )
            try:
                retried = await self._request_no_tools(
                    spec,
                    retry_messages,
                    request_state=request_state,
                    transcript=transcript,
                )
            except ContextPlanError:
                request_state.force_l4_reason = None
                logger.warning(
                    "No-tools context-overflow L4 retry could not establish a checkpoint for {}",
                    spec.session_key or "default",
                )
            else:
                if response.usage is not None:
                    retried.usage = (
                        response.usage
                        if retried.usage is None
                        else response.usage + retried.usage
                    )
                return retried
        return response

    # 解析所有模型请求路径共用的墙钟超时：优先用 spec 显式指定，否则读环境变量
    # NANOBOT_LLM_TIMEOUT_S（默认 300s，设为 0 表示不限时）。
    @staticmethod
    def _resolve_llm_timeout_s(spec: AgentRunSpec) -> float | None:
        """Resolve the wall-clock limit shared by every model request path."""
        timeout_s = spec.llm_timeout_s
        if timeout_s is None:
            # Default to a finite timeout to avoid per-session lock starvation when an LLM
            # request hangs indefinitely (e.g. gateway/network stall).
            # Set NANOBOT_LLM_TIMEOUT_S=0 to disable.
            raw = os.environ.get("NANOBOT_LLM_TIMEOUT_S", "300").strip()
            try:
                timeout_s = float(raw)
            except (TypeError, ValueError):
                timeout_s = 300.0
        return timeout_s if timeout_s > 0 else None

    # 追加"迭代预算已耗尽，请立即给出最终答案"模板消息，供 _try_finalize_after_max_iterations 使用。
    @staticmethod
    def _budget_exhausted_finalization_messages(
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        retry_messages = list(messages)
        retry_messages.append(build_budget_exhausted_finalization_message())
        return retry_messages

    # 抢救性收尾请求也失败时的最终兜底文案：优先用调用方自定义模板，否则用内置模板。
    @staticmethod
    def _max_iterations_fallback(spec: AgentRunSpec) -> str:
        if spec.max_iterations_message:
            return spec.max_iterations_message.format(
                max_iterations=spec.max_iterations,
            )
        return render_template(
            "agent/max_iterations_message.md",
            strip=True,
            max_iterations=spec.max_iterations,
        )

    # 优先使用 provider 返回的真实用量；若缺失（如出错或某些 provider 不返回），
    # 按错误/正常两种情形分别回退到空用量或本地估算，并叠加计时信息。
    def _usage_or_estimate(
        self,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
        response: LLMResponse,
        *,
        tool_definitions: list[dict[str, Any]] | None,
    ) -> LLMUsage:
        usage = response.usage
        if response.finish_reason == "error":
            if usage is None or usage.total_tokens == 0:
                usage = LLMUsage.empty_request()
        elif usage is None or usage.total_tokens == 0:
            usage = self._estimate_response_usage(
                spec,
                messages,
                response,
                tool_definitions=tool_definitions,
            )
        return usage.with_timing(
            generation_ms=response.generation_ms,
            ttft_ms=response.ttft_ms,
        )

    # 计算并把本次请求的用量写入 request_state.usage（供上下文治理器后续参考），同时返回该用量。
    def _record_request_usage(
        self,
        spec: AgentRunSpec,
        state: ModelRequestState,
        response: LLMResponse,
    ) -> LLMUsage:
        assert state.messages is not None
        state.usage = self._usage_or_estimate(
            spec,
            state.messages,
            response,
            tool_definitions=state.tool_definitions,
        )
        return state.usage

    # provider 未返回真实 token 用量时的本地估算：按 provider/模型的分词规则估算
    # prompt 与生成内容的 token 数（近似值，仅用于展示/统计，不影响计费判断）。
    def _estimate_response_usage(
        self,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
        response: LLMResponse,
        *,
        tool_definitions: list[dict[str, Any]] | None,
    ) -> LLMUsage:
        prompt_tokens, _ = estimate_prompt_tokens_chain(
            spec.runtime.provider,
            spec.runtime.model,
            messages,
            tool_definitions,
        )
        assistant_message = build_assistant_message(
            response.content or "",
            tool_calls=[tc.to_openai_tool_call() for tc in response.tool_calls],
            reasoning_content=response.reasoning_content,
            thinking_blocks=response.thinking_blocks,
        )
        completion_tokens = estimate_message_tokens(assistant_message)
        return LLMUsage.estimated(
            input_tokens=max(0, prompt_tokens),
            output_tokens=max(0, completion_tokens),
        )

    # 合并两次用量统计（None 视为零值单位元），用于把重试/恢复请求的用量叠加到本轮。
    @staticmethod
    def _merge_usage(
        left: LLMUsage | None,
        right: LLMUsage | None,
    ) -> LLMUsage | None:
        if left is None:
            return right
        if right is None:
            return left
        return left + right

    # 若调用方注册了 checkpoint_callback，把当前阶段的运行状态回调出去（供落盘恢复）。
    async def _emit_checkpoint(
        self,
        spec: AgentRunSpec,
        payload: dict[str, Any],
    ) -> None:
        callback = spec.checkpoint_callback
        if callback is not None:
            await callback(payload)

    # 把最终文本写入 messages：若末尾已是一条无工具调用的 assistant 消息则原地替换
    # （避免重复追加），否则追加一条新的 assistant 消息。
    @staticmethod
    def _append_final_message(messages: list[dict[str, Any]], content: str | None) -> None:
        if not content:
            return
        if (
            messages
            and messages[-1].get("role") == "assistant"
            and not messages[-1].get("tool_calls")
        ):
            if messages[-1].get("content") == content:
                return
            messages[-1] = build_assistant_message(content)
            return
        messages.append(build_assistant_message(content))

    # LLM 请求出错时，若末尾还没有一条 assistant 记录，补一条占位消息，
    # 避免消息序列以 user/tool 结尾而在下一轮请求时违反 provider 的角色交替约束。
    @staticmethod
    def _append_model_error_placeholder(messages: list[dict[str, Any]]) -> None:
        if messages and messages[-1].get("role") == "assistant" and not messages[-1].get("tool_calls"):
            return
        messages.append(build_assistant_message(_PERSISTED_MODEL_ERROR_PLACEHOLDER))
