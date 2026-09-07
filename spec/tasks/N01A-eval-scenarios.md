# N01A 长上下文与记忆依赖评测 Implementation Plan

> **For agentic workers:** 按 REVIEW 已记录决策执行；使用 superpowers:subagent-driven-development 或 superpowers:executing-plans。本任务已完成并验证；不自动 commit/push。

**Goal:** 在业务改动前交付两类固定任务、指标与比较报告。

**Architecture:** 基于 N01 的正式 runtime 复用旧 MemoryStore，固定场景/ground truth/变体；不模拟新模块。

**Tech Stack:** Python 3.11+、asyncio、Pydantic 2、pytest，复用现有依赖。

**Spec:** [S03](../S03-evaluation.md)、[EVALUATION-CASES](../EVALUATION-CASES.md)；强制遵守 [S00](../S00-development-boundary.md) 与 [TASKS 全局约束](../TASKS.md)。

## Global Constraints

仅在 MyCoder/codex/agent-governance 开发，基线 455533169d5a641300dd63d260b1ff5543c4093c；不改用户既有 diff，不运行 ruff format，不碰生产根/真实 API，不新建 worktree。先完成评测再改业务模块。

依赖：N01。状态：**已完成并经独立审查，2026-09-06**。各场景分别按五步完成；证据见 IMPLEMENTATION.md。

## 文件与接口

新增 `nanobot/evaluation/scenarios.py`、`metrics.py`；
`tests/evaluation/fixtures/context_matrix.json`、`memory_dependency.json`；
`tests/evaluation/test_scenarios.py`、`test_metrics.py`、`test_baseline_suites.py`。
修改 N01 的 models/runner/providers/verifiers/__main__；新增 `docs/engineering/context-memory-evaluation.md`。
生产 memory.py/context.py/governor/runner 均不修改。

产出 build_context_cases(parameters: dict) -> list[EvalCase]（默认12组），build_memory_cases(parameters: dict) -> list[EvalCase]（12任务×3数据变体=36个独立case）；
EvalCase 增加 scenario/parameters/memory_variant/requires_capabilities/setup_kind。
新增 EvalUsageRecord(request_id,purpose,input_tokens,output_tokens,cached_tokens)、
summarize_usage(records) -> UsageTotals（actual_input_tokens、request_count、unknown_request_count、known_total）、
compare_results(baseline,candidate) -> dict。同ID去重、冲突记录报错、未知不填零。

- [x] **1 写失败测试。**

```python
def test_pico_style_matrix_is_fixed_and_reproducible():
    from nanobot.evaluation.scenarios import build_context_cases, build_memory_cases
    params = {
        "history_counts": [4, 12, 24], "memory_counts": [2, 10],
        "request_styles": ["short", "long"], "seed": 7,
    }
    context = build_context_cases(params)
    assert len(context) == 12
    assert [case.id for case in context] == [
        case.id for case in build_context_cases(params)
    ]
    memory = build_memory_cases({"seed": 7, "instances_per_category": 4})
    assert len(memory) == 36
    assert {case.memory_variant for case in memory} == {
        "memory_on", "memory_off", "memory_irrelevant"
    }
```

- [x] **2 RED。**运行 `.venv/Scripts/python.exe -m pytest tests/evaluation/test_scenarios.py tests/evaluation/test_metrics.py tests/evaluation/test_baseline_suites.py -q -p no:cacheprovider`，预期缺少生成器/指标契约。
- [x] **3 最小实现。**按 EVALUATION-CASES 的预算压力生成合法历史与噪声，目标事实只放规定来源，不把答案泄漏到末轮请求/工具schema/verifier可读目录。记忆任务分 A/B 阶段，B 用新 session；三种记忆视图在各自独立根准备，旧 MemoryStore 可预置，但明确标 preseeded。

```text
fixed case generator -> independent roots + bootstrap/probe
 -> actual runtime requests/tools -> executable task verifier
 -> correctness + visible-source evidence + repeated fact reads + tokens
 -> baseline artifact + compatible-case comparison + mechanism_only flag
```

指标覆盖实际/估算token、时延、配对、同版本事实源重读、任务正确率。prompt-sensitive fake 只能读实际 prompt/工具结果，不读取 ground truth；fixed fake 只测试控制流。长上下文必须证明到达目标压力；关键事实移除控制不能仍靠隐藏答案成功。live CLI 验证显式配置/上限，未授权不调用。
- [x] **4 GREEN。**focused + 整个 tests/evaluation。运行两个 fixture 套件的 baseline 和记忆三变体，重复生成/执行核对 case hash；未知 usage=null、失败/缺能力完整记录。现有功能性能/正确率不佳可以作为基线失败，不修改业务代码“修复”基线；评测器自身应正确判失败。
- [x] **5 前置门禁。**交付可运行 CLI、指标、trace、fixture、旧行为报告。报告把 retrieval baseline 与 memory_save 端到端区分；后者未实现则显式标记。N01A 未验收不得开始 N05 或 N02；N10 不是本任务延期的替代。
