# N10 组合评测与成本账本 Implementation Plan

> **For agentic workers:** 按 REVIEW 已记录决策执行；使用 superpowers:subagent-driven-development 或 superpowers:executing-plans。任务已完成，不自动 commit/push。

**Goal:** 对模块组合和逐层消融形成可复核结果，不用脚本测试冒充语义收益。

**Architecture:** 扩展已完成的 N01A 模式、指标和案例，以可执行 verifier 为主并汇总全部模型 purpose 的真实成本。

**Tech Stack:** Python 3.11+、asyncio、Pydantic 2、pytest，复用现有依赖。

**Spec:** [S03](../S03-evaluation.md)；强制遵守 [S00](../S00-development-boundary.md) 与 [TASKS 全局约束](../TASKS.md)。

## Global Constraints

仅在 MyCoder 的 codex/agent-governance 开发，基线 455533169d5a641300dd63d260b1ff5543c4093c；不改用户既有 diff，不运行 ruff format，不碰生产根/真实 API，不创建新工作树。

依赖：N01A、N04、N09。状态：**已完成**。按用户 2026-09-06 的明确要求，先完成实现、再集中测试；未为每个小改重复运行整套实验。

## 文件与接口

扩展 N01A 已有 `nanobot/evaluation/metrics.py`、`providers.py`、`scenarios.py`、`tests/evaluation/test_metrics.py`；新增 `tests/evaluation/test_regression_suite.py`；
复用 `context_matrix.json`、`memory_dependency.json`、`tool_context.json`，新增 fixtures：`explicit_memory.json`、`memory_negatives.json`、`dream_conflict.json`、`recovery.json`、`existing_safety.json`，没有重复创建 `long_context.json` 或 `tool_batches.json`。
修改 `nanobot/evaluation/models.py`、`runner.py`、`__main__.py`、`verifiers.py`；使用现有 `nanobot/llm_usage/context.py`/provider 观察接口，不做旧全平台 schema 迁移。
更新 N01A 已有 `docs/engineering/context-memory-evaluation.md`，保留旧行为基线。

复用 N01A EvalUsageRecord/summarize_usage/compare_results 和 CLI compare，不重新实现基础指标。新增 `evaluate_memory_save_attempt(*, expected_to_save: bool, committed: bool, claimed_saved: bool) -> dict[str, int]`，返回 missed_save_requests、unauthorized_commit_count、false_memory_confirmation_count。committed 来自真实 receipt+canonical 校验，expected 标签只在 verifier 中可见。

- [x] **1 写失败测试。** 用户明确要求“不用写失败测试，直接实现再做测试”，本步按授权跳过；没有补写或宣称 RED 证据。

```python
def test_claim_without_tool_commit_is_not_success():
    from nanobot.evaluation.metrics import evaluate_memory_save_attempt
    row = evaluate_memory_save_attempt(
        expected_to_save=True, committed=False, claimed_saved=True
    )
    assert row["missed_save_requests"] == 1
    assert row["false_memory_confirmation_count"] == 1
    assert row["unauthorized_commit_count"] == 0
```

- [x] **2 RED。**按同一用户授权跳过前置 RED；实现后集中执行该组测试及完整回归。
- [x] **3 最小实现。**已接入 S03 baseline/observe/context-only/memory-only/full/no-l1…no-l4，每模式独立根；所有模式保留原安全约束/最终输入上限。fixture verifiers 覆盖文件结果、tool pair、artifact hash、canonical+receipt、本轮请求偏好、Dream cursor、重复/旧 Dream 覆写和安全副作用。受控 setup 仅允许 Dream 交错、重启探针和 read-cache reset，不允许 fixture 任意执行代码。

```text
identical case/hash + separate roots -> baseline & candidate runs
 -> executable verdicts + actual usage by purpose + estimated budget
 -> invariant failures block; quality/latency deltas require human interpretation
HITL unsupported -> not_applicable with reason, never passed
```

指标定义按 S03，负例/冲突/安全场景不可空集通过；重复/纠正/引用/旧 Dream/SDK restore/磁盘故障/中断均有明确证据。失败请求的已知用量也计入；summary/dream 与 worker 分开并合并总成本；memory_save 决策计 worker，工具延迟单列，不增加虚构 remember 模型费用。CLI live 默认拒绝，无显式测试配置和正数请求/token/时间上限不运行。
- [x] **4 GREEN。**`tests/evaluation` 为 124 passed（177.37s）；N02–N09 与指定安全/Goal 不变性回归为 556 passed、25 skipped（29.09s）；Ruff `--no-cache` clean，basedpyright 0 errors/warnings/notes。离线 58 例全矩阵完成后只补跑新增 L3 定向案例，合并后的 59 例结果为：baseline 47 pass / 5 fail / 7 not_implemented；full 59 pass；no-l1 58 pass / 1 error；no-l2 55 pass / 4 error；no-l3 58 pass / 1 failed；no-l4 58 pass / 1 error。
- [x] **5 审查。**控制器核对 commit、dirty diff、config/model/case hashes、逐模式独立根、错误与 unknown usage。四层均有非空反例：L1/L4 缺失使 artifact 案例错误，L2 缺失使 4 个高压力案例错误，L3 缺失使重复版本案例失败。full 的保存遗漏、未授权提交、虚假确认、重复条目、旧 Dream 覆写和安全绕过均为 0。全部请求为 `evaluation-scripted`、`mechanism_only=true`，没有 live 调用；360 个脚本请求仅有 1,289,077 estimated input tokens，actual input/output/cache usage 全部为 null，不据此宣称真实模型质量、时延或费用收益。Goal 源码未改，HITL 不在本任务交付范围。

详细实现、运行路径、拆分实验口径和限制见 `.superpowers/sdd/CONTEXT-MEMORY-V2/N10-report.md`。
