"""Scripted external responses; the ordinary provider retry/observer path remains real."""

from __future__ import annotations

import json
import re
import time
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Literal, ParamSpec, TypeVar, cast

from pydantic import Field, JsonValue

from nanobot.evaluation.models import EvalBudget, EvalModel, canonical_hash
from nanobot.providers.base import (
    GenerationSettings,
    LLMProvider,
    LLMResponse,
    LLMUsage,
    ProviderCallContext,
    ToolCallRequest,
)
from nanobot.utils.helpers import estimate_prompt_tokens_chain

_P = ParamSpec("_P")
_T = TypeVar("_T")


class EvalScriptError(RuntimeError):
    pass


class EvalBudgetExceededError(RuntimeError):
    pass


@dataclass
class RunBudget:
    limits: EvalBudget
    started: float = field(default_factory=time.monotonic)
    requests: int = 0
    tool_steps: int = 0
    reserved_tokens: int = 0

    def check_time(self) -> None:
        if time.monotonic() - self.started >= self.limits.max_wall_seconds:
            raise EvalBudgetExceededError("wall_time_budget")

    def check_request(self, estimated_input: int, max_output: int) -> None:
        self.check_time()
        if self.requests >= self.limits.max_requests:
            raise EvalBudgetExceededError("request_budget")
        reservation = estimated_input + max_output
        if self.reserved_tokens + reservation > self.limits.max_tokens:
            raise EvalBudgetExceededError("token_budget")

    def commit_request(self, estimated_input: int, max_output: int) -> None:
        self.requests += 1
        self.reserved_tokens += estimated_input + max_output

    def reserve_request(self, estimated_input: int, max_output: int) -> None:
        self.check_request(estimated_input, max_output)
        self.commit_request(estimated_input, max_output)

    def check_tools(self, count: int) -> None:
        self.check_time()
        if self.tool_steps + count > self.limits.max_steps:
            raise EvalBudgetExceededError("tool_step_budget")

    def commit_tools(self, count: int) -> None:
        self.tool_steps += count

    def reserve_tools(self, count: int) -> None:
        self.check_tools(count)
        self.commit_tools(count)

    def remaining_wall_seconds(self) -> float:
        return max(0.0, self.limits.max_wall_seconds - (time.monotonic() - self.started))


@dataclass
class CompositeRunBudget:
    """Atomically admit work against a case budget and an optional suite budget."""

    primary: RunBudget
    shared: RunBudget | None = None

    def _budgets(self) -> tuple[RunBudget, ...]:
        return (self.primary,) if self.shared is None else (self.primary, self.shared)

    @property
    def limits(self) -> EvalBudget:
        return self.primary.limits

    @property
    def started(self) -> float:
        return self.primary.started

    @property
    def requests(self) -> int:
        return self.primary.requests

    @property
    def tool_steps(self) -> int:
        return self.primary.tool_steps

    @property
    def reserved_tokens(self) -> int:
        return self.primary.reserved_tokens

    def check_time(self) -> None:
        for budget in self._budgets():
            budget.check_time()

    def reserve_request(self, estimated_input: int, max_output: int) -> None:
        budgets = self._budgets()
        for budget in budgets:
            budget.check_request(estimated_input, max_output)
        for budget in budgets:
            budget.commit_request(estimated_input, max_output)

    def reserve_tools(self, count: int) -> None:
        budgets = self._budgets()
        for budget in budgets:
            budget.check_tools(count)
        for budget in budgets:
            budget.commit_tools(count)

    def remaining_wall_seconds(self) -> float:
        return min(budget.remaining_wall_seconds() for budget in self._budgets())


class ScriptToolCall(EvalModel):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    arguments: dict[str, JsonValue]


class ScriptUsage(EvalModel):
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cached_tokens: int | None = Field(default=None, ge=0)


class ScriptResponse(EvalModel):
    content: str | None = None
    finish_reason: str = "stop"
    tool_calls: list[ScriptToolCall] = Field(default_factory=list)
    expect_contains: list[str] = Field(default_factory=list)
    expect_purpose: Literal["worker", "summary", "dream"] | None = None
    usage: ScriptUsage | None = None


@dataclass
class RequestRecord:
    request_id: str
    purpose: str
    messages: list[dict[str, Any]] = field(repr=False)
    tools: list[dict[str, Any]] = field(repr=False)
    estimated_input_tokens: int
    estimate_method: str
    phase: Literal["bootstrap", "probe"] = "probe"
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_tokens: int | None = None

    def public_record(self) -> dict[str, JsonValue]:
        return {
            "request_id": self.request_id, "purpose": self.purpose,
            "phase": self.phase,
            "payload_hash": canonical_hash([self.messages, self.tools]),
            "message_count": len(self.messages), "schema_count": len(self.tools),
            "estimated_input_tokens": self.estimated_input_tokens,
            "estimate_method": self.estimate_method,
            "input_tokens": self.input_tokens, "output_tokens": self.output_tokens,
            "cached_tokens": self.cached_tokens,
        }


class ScriptedProvider(LLMProvider):
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        super().__init__(provider_name="evaluation-scripted")
        self.generation = GenerationSettings(temperature=0, max_tokens=512)
        self.responses = [ScriptResponse.model_validate(value) for value in responses]
        self.requests: list[RequestRecord] = []
        self.budget: RunBudget | CompositeRunBudget = RunBudget(EvalBudget())
        self.failure: EvalScriptError | EvalBudgetExceededError | None = None
        self._purpose: ContextVar[str] = ContextVar("evaluation_purpose", default="worker")
        self.phase: Literal["bootstrap", "probe"] = "probe"

    @property
    def purpose(self) -> str:
        return self._purpose.get()

    def label_purpose(self, callback: Callable[_P, Awaitable[_T]],
                      purpose: Literal["summary", "dream"]) -> Callable[_P, Awaitable[_T]]:
        """Observe the existing call path without implementing or predicting it."""
        async def observed(*args: _P.args, **kwargs: _P.kwargs) -> _T:
            token = self._purpose.set(purpose)
            try:
                return await callback(*args, **kwargs)
            finally:
                self._purpose.reset(token)
        return observed

    def get_default_model(self) -> str:
        return "evaluation-scripted"

    def next_response(self, messages: list[dict[str, Any]]) -> ScriptResponse:
        if not self.responses:
            raise EvalScriptError("script_exhausted")
        return self.responses.pop(0)

    async def chat(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None,
        model: str | None = None, max_tokens: int = 4096, temperature: float = 0.7,
        reasoning_effort: str | None = None, tool_choice: str | dict[str, Any] | None = None,
    ) -> LLMResponse:
        if self.failure:
            raise self.failure
        try:
            estimate, method = estimate_prompt_tokens_chain(self, model, messages, tools)
            self.budget.reserve_request(estimate, max_tokens)
            record = RequestRecord(
                f"request-{len(self.requests) + 1}", self.purpose,
                deepcopy(messages), deepcopy(tools or []), estimate, method, phase=self.phase,
            )
            self.requests.append(record)
            response = self.next_response(messages)
            visible_text = str(messages)
            if any(marker not in visible_text for marker in response.expect_contains):
                raise EvalScriptError("script_request_mismatch")
            if response.expect_purpose is not None and response.expect_purpose != self.purpose:
                raise EvalScriptError("script_purpose_mismatch")
            usage = None
            if response.usage is not None:
                record.input_tokens = response.usage.input_tokens
                record.output_tokens = response.usage.output_tokens
                record.cached_tokens = response.usage.cached_tokens
                usage = LLMUsage.reported(
                    input_tokens=record.input_tokens, output_tokens=record.output_tokens,
                    cache_read_tokens=record.cached_tokens,
                )
            return LLMResponse(
                content=response.content, finish_reason=response.finish_reason, usage=usage,
                tool_calls=[ToolCallRequest(id=t.id, name=t.name, arguments=t.arguments) for t in response.tool_calls],
                error_should_retry=False if response.finish_reason == "error" else None,
                context_acceptance=(
                    "rejected" if response.finish_reason == "error" else "accepted"
                ),
            )
        except (EvalScriptError, EvalBudgetExceededError) as exc:
            # _safe_chat may convert this into an error response. Keep the typed
            # cause so even archive fallback or a convincing final text cannot pass.
            self.failure = exc
            raise


class LiveEvaluationProvider(LLMProvider):
    """Bound and record a real provider without exposing evaluation payloads."""

    def __init__(
        self,
        inner: LLMProvider,
        *,
        model: str,
        budget: CompositeRunBudget,
        generation: GenerationSettings,
    ) -> None:
        super().__init__(provider_name=inner.provider_name)
        self.inner = inner
        self.model = model
        self.budget = budget
        self.generation = deepcopy(generation)
        self.requests: list[RequestRecord] = []
        self.failure: EvalBudgetExceededError | None = None
        self._purpose: ContextVar[str] = ContextVar("live_evaluation_purpose", default="worker")
        self.phase: Literal["bootstrap", "probe"] = "probe"

    @property
    def purpose(self) -> str:
        return self._purpose.get()

    def label_purpose(
        self,
        callback: Callable[_P, Awaitable[_T]],
        purpose: Literal["summary", "dream"],
    ) -> Callable[_P, Awaitable[_T]]:
        async def observed(*args: _P.args, **kwargs: _P.kwargs) -> _T:
            token = self._purpose.set(purpose)
            try:
                return await callback(*args, **kwargs)
            finally:
                self._purpose.reset(token)

        return observed

    def get_default_model(self) -> str:
        return self.model

    async def _dispatch(
        self,
        callback: Callable[..., Awaitable[LLMResponse]],
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        model: str | None,
        max_tokens: int,
        temperature: float,
        reasoning_effort: str | None,
        tool_choice: str | dict[str, Any] | None,
        provider_context: ProviderCallContext | None = None,
    ) -> LLMResponse:
        if self.failure is not None:
            raise self.failure
        try:
            estimate, method = estimate_prompt_tokens_chain(
                self.inner,
                model or self.model,
                messages,
                tools,
            )
            self.budget.reserve_request(estimate, max_tokens)
        except EvalBudgetExceededError as exc:
            self.failure = exc
            raise
        record = RequestRecord(
            request_id=f"request-{len(self.requests) + 1}",
            purpose=self.purpose,
            messages=deepcopy(messages),
            tools=deepcopy(tools or []),
            estimated_input_tokens=estimate,
            estimate_method=method,
            phase=self.phase,
        )
        self.requests.append(record)
        kwargs: dict[str, Any] = {
            "messages": messages,
            "tools": tools,
            "model": model or self.model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "reasoning_effort": reasoning_effort,
            "tool_choice": tool_choice,
        }
        if provider_context is not None:
            kwargs["provider_context"] = provider_context
        response = await callback(**kwargs)
        if response.usage is not None:
            record.input_tokens = response.usage.input_tokens
            record.output_tokens = response.usage.output_tokens
            record.cached_tokens = response.usage.cache_read_tokens
        return response

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        reasoning_effort: str | None = None,
        tool_choice: str | dict[str, Any] | None = None,
    ) -> LLMResponse:
        return await self._dispatch(
            self.inner.chat,
            messages=messages,
            tools=tools,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            reasoning_effort=reasoning_effort,
            tool_choice=tool_choice,
        )

    async def chat_with_context(
        self,
        *,
        provider_context: ProviderCallContext,
        **kwargs: Any,
    ) -> LLMResponse:
        return await self._dispatch(
            self.inner.chat_with_context,
            provider_context=provider_context,
            **kwargs,
        )


class VisibleFact(EvalModel):
    topic: str
    key: str
    value: JsonValue
    revision: int = Field(default=1, ge=1, strict=True)


class VisibleTask(EvalModel):
    action: Literal["read", "answer", "edit", "remember"]
    topic: str
    required: list[str] = Field(default_factory=list)
    source: str | None = None
    target: str | None = None


def visible_facts(messages: list[dict[str, Any]]) -> list[VisibleFact]:
    """Parse synthetic evidence ONLY from the payload supplied to the model."""
    facts: list[VisibleFact] = []
    for message in messages:
        text = message.get("content")
        if not isinstance(text, str):
            continue
        for match in re.finditer(r"EVAL_FACT (\{[^\n]*\})", text):
            try:
                facts.append(VisibleFact.model_validate_json(match.group(1)))
            except ValueError:
                continue  # Truncated/malformed evidence is not a fact.
    return facts


def visible_artifact_refs(
    messages: list[dict[str, Any]],
    *,
    topic: str | None = None,
) -> list[str]:
    """Collect artifact IDs that are explicitly visible in the model payload."""
    refs: list[str] = []

    def add(value: object) -> None:
        if (
            isinstance(value, str)
            and re.fullmatch(r"[0-9a-f]{64}", value)
            and value not in refs
        ):
            refs.append(value)

    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            try:
                content_value = json.loads(content)
            except ValueError:
                content_value = None
            if isinstance(content_value, dict):
                marker = cast(dict[str, Any], content_value)
                preview = marker.get("preview")
                if topic is None or not isinstance(preview, str) or topic in preview:
                    add(marker.get("artifact_id"))
            for match in re.finditer(r"(?m)^- ([0-9a-f]{64})$", content):
                add(match.group(1))

        calls_value = cast(object, message.get("tool_calls"))
        for raw_call in (
            cast(list[object], calls_value) if isinstance(calls_value, list) else []
        ):
            if not isinstance(raw_call, dict):
                continue
            function_value = cast(dict[str, Any], raw_call).get("function")
            if not isinstance(function_value, dict):
                continue
            function = cast(dict[str, Any], function_value)
            if function.get("name") != "context_artifact_read":
                continue
            arguments_value = function.get("arguments")
            try:
                arguments_value = (
                    json.loads(arguments_value)
                    if isinstance(arguments_value, str)
                    else arguments_value
                )
            except ValueError:
                continue
            if isinstance(arguments_value, dict):
                add(cast(dict[str, Any], arguments_value).get("ref"))
    return refs


def artifact_visible_facts(messages: list[dict[str, Any]]) -> list[VisibleFact]:
    """Recover facts only from complete artifact pages present in the request."""
    calls: dict[str, tuple[str, int]] = {}
    pages: dict[str, dict[int, str]] = {}
    for message in messages:
        calls_value = cast(object, message.get("tool_calls"))
        for raw_call in (
            cast(list[object], calls_value) if isinstance(calls_value, list) else []
        ):
            if not isinstance(raw_call, dict):
                continue
            call = cast(dict[str, Any], raw_call)
            function_value = cast(object, call.get("function"))
            if not isinstance(function_value, dict):
                continue
            function = cast(dict[str, Any], function_value)
            arguments_value = function.get("arguments")
            try:
                arguments_value = (
                    json.loads(arguments_value)
                    if isinstance(arguments_value, str)
                    else arguments_value
                )
            except ValueError:
                continue
            call_id = call.get("id")
            if (
                function.get("name") != "context_artifact_read"
                or not isinstance(call_id, str)
                or not isinstance(arguments_value, dict)
            ):
                continue
            arguments = cast(dict[str, Any], arguments_value)
            ref_id = arguments.get("ref")
            offset = arguments.get("offset_chars", 0)
            if isinstance(ref_id, str) and type(offset) is int:
                calls[call_id] = (ref_id, offset)

        call = calls.get(str(message.get("tool_call_id", "")))
        if message.get("role") != "tool" or call is None:
            continue
        try:
            page_value = json.loads(str(message.get("content", "")))
        except ValueError:
            continue
        if not isinstance(page_value, dict):
            continue
        page = cast(dict[str, Any], page_value)
        text = page.get("text")
        if isinstance(text, str):
            ref_id, offset = call
            pages.setdefault(ref_id, {})[offset] = text

    facts: list[VisibleFact] = []
    for artifact_pages in pages.values():
        payload = "".join(
            artifact_pages[offset] for offset in sorted(artifact_pages)
        )
        try:
            archived_value = json.loads(payload)
        except ValueError:
            # L1 archives the tool result body itself.  L2/L3 history archives
            # may instead contain a JSON message array, so support both forms.
            facts.extend(visible_facts([{"role": "tool", "content": payload}]))
            continue
        if not isinstance(archived_value, list):
            continue
        archived = [
            cast(dict[str, Any], message)
            for message in cast(list[object], archived_value)
            if isinstance(message, dict)
        ]
        facts.extend(visible_facts(archived))
    return facts


def artifact_page_cursors(messages: list[dict[str, Any]]) -> dict[str, int | None]:
    """Return only cursors proven by artifact tool results in this payload."""
    calls: dict[str, str] = {}
    cursors: dict[str, int | None] = {}
    for message in messages:
        calls_value = cast(object, message.get("tool_calls"))
        for raw_call in (
            cast(list[object], calls_value) if isinstance(calls_value, list) else []
        ):
            if not isinstance(raw_call, dict):
                continue
            call = cast(dict[str, Any], raw_call)
            function_value = cast(object, call.get("function"))
            if not isinstance(function_value, dict):
                continue
            function = cast(dict[str, Any], function_value)
            arguments_value = function.get("arguments")
            try:
                arguments_value = (
                    json.loads(arguments_value)
                    if isinstance(arguments_value, str)
                    else arguments_value
                )
            except ValueError:
                continue
            call_id = call.get("id")
            if (
                function.get("name") == "context_artifact_read"
                and isinstance(call_id, str)
                and isinstance(arguments_value, dict)
                and isinstance(cast(dict[str, Any], arguments_value).get("ref"), str)
            ):
                calls[call_id] = cast(str, cast(dict[str, Any], arguments_value)["ref"])
        ref_id = calls.get(str(message.get("tool_call_id", "")))
        if message.get("role") != "tool" or ref_id is None:
            continue
        try:
            page_value = json.loads(str(message.get("content", "")))
        except ValueError:
            continue
        if not isinstance(page_value, dict):
            continue
        page = cast(dict[str, Any], page_value)
        if not isinstance(page.get("text"), str):
            continue
        next_offset = page.get("next_offset_chars")
        if next_offset is None or type(next_offset) is int:
            cursors[ref_id] = next_offset
    return cursors


class PromptSensitiveProvider(ScriptedProvider):
    """A tiny synthetic task protocol, not a language-quality or decision benchmark.

    This object receives no case, filesystem, expected answer or verifier. It
    can answer only from actual visible EVAL_FACT records and real tool results.
    """

    def __init__(self, *, archive_error: bool = False) -> None:
        super().__init__([])
        self.archive_error = archive_error
        # Navigation state only: the deterministic offline driver may span an
        # L4 checkpoint while paging.  Facts are never cached here and still
        # must be visible in the current model request before an answer passes.
        self._artifact_next_offsets: dict[str, int | None] = {}

    def next_response(self, messages: list[dict[str, Any]]) -> ScriptResponse:
        for ref_id, next_offset in artifact_page_cursors(messages).items():
            previous = self._artifact_next_offsets.get(ref_id, -1)
            if previous is None:
                continue
            if next_offset is None or next_offset > previous:
                self._artifact_next_offsets[ref_id] = next_offset
        if self.purpose == "summary":
            if self.archive_error:
                return ScriptResponse(content="synthetic archive failure", finish_reason="error")
            facts = [*visible_facts(messages), *artifact_visible_facts(messages)]
            strict_context_summary = any(
                "strict JSON working-memory checkpoint" in str(message.get("content", ""))
                for message in messages
            )
            if strict_context_summary:
                tasks: list[str] = []
                task_topics: list[str] = []
                for message in messages:
                    content = message.get("content")
                    if not isinstance(content, str):
                        continue
                    for match in re.finditer(r"EVAL_TASK (\{[^\n]*\})", content):
                        try:
                            task = VisibleTask.model_validate_json(match.group(1))
                        except ValueError:
                            continue
                        rendered = f"EVAL_TASK {task.model_dump_json(exclude_none=True)}"
                        if rendered not in tasks:
                            tasks.append(rendered)
                        if task.topic not in task_topics:
                            task_topics.append(task.topic)
                summary = {
                    "schema_version": 1,
                    "tasks": tasks,
                    "constraints": [
                        f"EVAL_FACT {fact.model_dump_json()}" for fact in facts
                    ],
                    "explicit_preferences": [],
                    "decisions": [],
                    "file_changes": [],
                    "errors": [],
                    "evidence_refs": visible_artifact_refs(
                        messages,
                        topic=task_topics[-1] if task_topics else None,
                    ),
                    "remaining_work": list(tasks),
                }
                return ScriptResponse(content=json.dumps(summary, ensure_ascii=False))
            return ScriptResponse(content="\n".join(f"EVAL_FACT {f.model_dump_json()}" for f in facts) or "No facts retained.")
        task_index = -1
        task_payload: str | None = None
        for index in range(len(messages) - 1, -1, -1):
            content = messages[index].get("content")
            if not isinstance(content, str):
                continue
            tasks = re.findall(r"EVAL_TASK (\{[^\n]*\})", content)
            if tasks:
                task_index = index
                task_payload = tasks[-1]
                break
        if task_payload is None:
            return ScriptResponse(content="noted")
        task = VisibleTask.model_validate_json(task_payload)
        turn = messages[task_index + 1:]
        completed: dict[tuple[str, str], str] = {}
        calls: dict[str, tuple[str, str]] = {}
        call_arguments: dict[str, dict[str, Any]] = {}
        artifact_calls: dict[str, tuple[str, int]] = {}
        artifact_next_offsets: dict[str, int | None] = {}
        for message in turn:
            tool_calls_value = cast(object, message.get("tool_calls"))
            tool_calls = (
                cast(list[object], tool_calls_value)
                if isinstance(tool_calls_value, list)
                else []
            )
            for raw_call in tool_calls:
                if not isinstance(raw_call, dict):
                    continue
                call = cast(dict[str, Any], raw_call)
                fn_value = cast(object, call.get("function"))
                if not isinstance(fn_value, dict):
                    continue
                fn = cast(dict[str, Any], fn_value)
                args = fn.get("arguments")
                arguments_value = json.loads(args) if isinstance(args, str) else args
                if not isinstance(arguments_value, dict):
                    continue
                arguments = cast(dict[str, Any], arguments_value)
                call_id = call.get("id")
                name = fn.get("name")
                if not isinstance(call_id, str) or not isinstance(name, str):
                    continue
                calls[call_id] = (name, str(arguments.get("path", "")))
                call_arguments[call_id] = arguments
                ref_id = arguments.get("ref")
                offset = arguments.get("offset_chars", 0)
                if (
                    name == "context_artifact_read"
                    and isinstance(ref_id, str)
                    and type(offset) is int
                ):
                    artifact_calls[call_id] = (ref_id, offset)
            if message.get("role") == "tool" and message.get("tool_call_id") in calls:
                completed[calls[message["tool_call_id"]]] = str(message.get("content", ""))
            artifact_call = artifact_calls.get(str(message.get("tool_call_id", "")))
            if message.get("role") != "tool" or artifact_call is None:
                continue
            try:
                page_value = json.loads(str(message.get("content", "")))
            except ValueError:
                continue
            if not isinstance(page_value, dict):
                continue
            page = cast(dict[str, Any], page_value)
            if not isinstance(page.get("text"), str):
                continue
            ref_id, offset = artifact_call
            next_offset = page.get("next_offset_chars")
            artifact_next_offsets[ref_id] = (
                next_offset if type(next_offset) is int else None
            )
        for ref_id, next_offset in artifact_next_offsets.items():
            previous = self._artifact_next_offsets.get(ref_id, -1)
            if previous is None:
                continue
            if next_offset is None or next_offset > previous:
                self._artifact_next_offsets[ref_id] = next_offset
        artifact_facts = artifact_visible_facts(turn)

        def read(path: str) -> ScriptResponse:
            return self._call("read_file", {"path": path})

        if task.action == "read":
            if task.source is None:
                raise EvalScriptError("missing_source")
            return ScriptResponse(content="noted") if ("read_file", task.source) in completed else read(task.source)
        available: dict[str, VisibleFact] = {}
        for fact in [*visible_facts(messages), *artifact_facts]:
            if fact.topic == task.topic and (fact.key not in available or fact.revision >= available[fact.key].revision):
                available[fact.key] = fact
        if task.action == "remember":
            committed_keys: set[str] = set()
            for message in turn:
                call_id = message.get("tool_call_id")
                if message.get("role") != "tool" or not isinstance(call_id, str):
                    continue
                arguments = call_arguments.get(call_id)
                if arguments is None or calls.get(call_id, (None,))[0] != "memory_save":
                    continue
                try:
                    result_value = json.loads(str(message.get("content", "")))
                except ValueError:
                    continue
                key = arguments.get("key")
                if (
                    isinstance(result_value, dict)
                    and cast(dict[str, object], result_value).get("status")
                    == "committed"
                    and isinstance(key, str)
                ):
                    committed_keys.add(key)
            for required_key in task.required:
                fact = available.get(required_key)
                memory_key = f"evaluation.{task.topic}.{required_key}"
                if fact is None:
                    return ScriptResponse(content='{"error":"missing_remember_evidence"}')
                if memory_key not in committed_keys:
                    evidence = f"EVAL_FACT {fact.model_dump_json()}"
                    return self._call(
                        "memory_save",
                        {
                            "target": "memory",
                            "key": memory_key,
                            "content": evidence,
                            "source_excerpt": evidence,
                        },
                    )
            return ScriptResponse(content="remembered")
        if any(key not in available for key in task.required):
            for ref_id, next_offset in self._artifact_next_offsets.items():
                if next_offset is not None:
                    return self._call(
                        "context_artifact_read",
                        {"ref": ref_id, "offset_chars": next_offset},
                    )
            history_refs = visible_artifact_refs(
                messages[: task_index + 1],
                topic=task.topic,
            )
            # A fresh L1 marker appears after the current task.  Its bounded
            # preview need not contain a fact near the end of the artifact, but
            # the reference itself is sufficient evidence to start a safe read.
            for ref_id in visible_artifact_refs(turn):
                if ref_id not in history_refs:
                    history_refs.append(ref_id)
            for ref_id in history_refs:
                if ref_id not in self._artifact_next_offsets:
                    return self._call(
                        "context_artifact_read",
                        {"ref": ref_id, "offset_chars": 0},
                    )
                next_offset = self._artifact_next_offsets[ref_id]
                if next_offset is not None:
                    return self._call(
                        "context_artifact_read",
                        {"ref": ref_id, "offset_chars": next_offset},
                    )
            if task.source and ("read_file", task.source) not in completed:
                return read(task.source)
            return ScriptResponse(content='{"error":"insufficient_visible_evidence"}')
        if task.action == "answer":
            return ScriptResponse(content=json.dumps({key: available[key].value for key in task.required}))
        if task.target is None:
            raise EvalScriptError("missing_target")
        if ("write_file", task.target) in completed:
            return ScriptResponse(content="done")
        if ("read_file", task.target) not in completed:
            return read(task.target)
        numbered = completed[("read_file", task.target)]
        text = "\n".join(match.group(1) for line in numbered.splitlines()
                         if (match := re.match(r"^\d+\| ?(.*)$", line)))
        try:
            output = json.loads(text)
        except ValueError:
            return ScriptResponse(content='{"error":"invalid_target_evidence"}')
        if not isinstance(output, dict) or "output_field" not in available or "output_value" not in available:
            return ScriptResponse(content='{"error":"invalid_visible_constraints"}')
        field = available["output_field"].value
        if not isinstance(field, str):
            return ScriptResponse(content='{"error":"invalid_field"}')
        output[field] = available["output_value"].value
        if "replicas" in available:
            output["replicas"] = available["replicas"].value
        return self._call("write_file", {"path": task.target, "content": json.dumps(output)})

    def _call(self, name: str, arguments: dict[str, JsonValue]) -> ScriptResponse:
        return ScriptResponse(finish_reason="tool_calls", tool_calls=[ScriptToolCall(
            id=f"eval-call-{len(self.requests)}", name=name, arguments=arguments,
        )])
