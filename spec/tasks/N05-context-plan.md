# N05 ContextPlan observe Implementation Plan

> **For agentic workers:** 按 REVIEW 已记录决策执行；使用 superpowers:subagent-driven-development 或 superpowers:executing-plans。本任务已完成并验证，独立复审通过，不自动 commit/push。

**Goal:** 在拼接全文前建立来源、估算和理由账本，默认不改变请求。

**Architecture:** Builder 收集/渲染，Planner 决策，Governor 调度；旁路 manifest 与实际请求关联。

**Tech Stack:** Python 3.11+、asyncio、Pydantic 2、pytest，复用现有依赖。

**Spec:** [S01](../S01-context-compression.md)；强制遵守 [S00](../S00-development-boundary.md) 与 [TASKS 全局约束](../TASKS.md)。

## Global Constraints

仅在 MyCoder 的 codex/agent-governance 开发，基线 455533169d5a641300dd63d260b1ff5543c4093c；不改用户既有 diff，不运行 ruff format，不碰生产根/真实 API，不创建新工作树。

依赖：N01A（已验收）。状态：**已完成**。240 项兼容回归、12 例实际 CLI observe 矩阵及独立复审通过，详见 IMPLEMENTATION.md 与 N05-controller-report.md。

## 文件与接口

新增 `nanobot/agent/context_plan.py`、`tests/agent/test_context_plan.py`、`tests/agent/test_context_manifest.py`、`tests/config/test_context_plan_config.py`。
修改 `nanobot/agent/context.py`、`context_governance.py`、`runner.py` AgentRunSpec、`loop.py` 注入、`nanobot/config/schema.py`。复用 helpers token estimator；不全量重写 builder/runner。
SDK 注入链复核：`nanobot/nanobot.py` 的 from_config 已委托 AgentLoop.from_config，
`nanobot/sdk/runtime.py` 负责每调用路由而非实例存储配置，两处复用不加无效转发。
独立 SDK 通过 AgentLoop/AgentRunSpec 显式传 context_config/runtime_data_dir；Config.runtime_data_dir 属性也复用既有实现。

消费现有 ContextBuilder/MemoryStore 和 N01A 基线，不导入后置 N02/N03。产出 S01 类型，ContextSource 的 revision/content_hash 均为可空字符串但至少一个非空；priority int，allowed_strategies 为 tuple[str,...]。
`ContextPlanner().plan(sources: list[ContextSource], input_budget: int) -> ContextPlan`；
`RenderedContext(messages: list[dict], tool_definitions: list[dict])`；
`ContextBuilder.collect_sources(transcript: TranscriptInput) -> list[ContextSource]` 与 `render_plan(plan: ContextPlan) -> RenderedContext` 按 S01。
本任务计划只有 selection，L1–L4 由后续接入，未接入层不能记成功。

- [x] **1 写失败测试。**

```python
def test_required_source_kept():
    from nanobot.agent.context_plan import ContextPlanner, ContextSource
    source = ContextSource(
        source_id="current:1", kind="user_current", revision="1", content_hash=None,
        estimated_tokens=10, estimate_method="fixture", priority=100,
        required=True, allowed_strategies=("keep",), render_key="current:1",
    )
    plan = ContextPlanner().plan([source], input_budget=100)
    assert plan.decisions[0].source_id == "current:1"
    assert plan.decisions[0].action == "keep"
    assert plan.predicted_total <= plan.input_budget
```

- [x] **2 RED。**运行 `.venv/Scripts/python.exe -m pytest tests/agent/test_context_plan.py tests/agent/test_context_manifest.py tests/config/test_context_plan_config.py -q -p no:cacheprovider`。
- [x] **3 最小实现。**将已有 system prompt 的来源边界提取为 lazy catalog，不先调用旧 full builder 再拆分；catalog 私有 renderer 按 render_key 查找，不在日志序列化 callable/正文。observe 仍调用旧 renderer发请求；enforce 的纯选择先覆盖基础 required/预算失败。窗口未知 observe=unknown，enforce=配置错误。schema/envelope 只记一次。

```text
collect sources + estimates -> immutable plan
observe -> legacy render -> sanitized manifest with parity comparison
enforce -> render selected plan -> final estimate -> send or explicit rejection
response usage -> append actual tokens to same request manifest
```

冻结 TASKS 默认；新增 Config.runtime_data_dir 注入链，直接 SDK 开启相关存储无根时拒绝。日志字段白名单，未知 actual tokens=null；archive/ref 失败和 irreducible_floor 也产出 manifest。Renderer 修正估算后需更新最终决策，不能日志宣称删除但实际发全量。
- [x] **4 GREEN。**focused + `tests/agent/test_context_builder.py tests/agent/test_context_prompt_cache.py tests/agent/test_context_governance.py tests/utils/test_token_estimation.py tests/test_nanobot_facade.py tests/config/test_config_paths.py`。用 N01 baseline/observe 捕获实际 payload 逐字段相等，涵盖 schema、多媒体、summary、USER/SOUL/MEMORY。
- [x] **5 审查。**核对所有来源只计一次、catalog 在全文前、日志无正文/base64/密钥、默认不启用新增裁剪、不记录虚构实际成本。此时不声称四层已完成。

先从原 memory 文本/hash 形成 source，不要求新版本化写入。N03 后置接入 committed entry/revision；完成后立即跑 N01A 长上下文矩阵回归，不等 N10。
