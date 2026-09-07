# N01 离线 EvalRunner Implementation Plan

> **For agentic workers:** 按 REVIEW 已记录决策执行；使用 superpowers:subagent-driven-development 或 superpowers:executing-plans。本任务已完成并验证；不自动 commit/push。

**Goal:** 提供可重复的真实 runtime 任务执行和可执行判定。

**Architecture:** 使用 Nanobot.run 与 ScriptedProvider，固定输入/根/预算，白名单 verifier 读取实际结果。

**Tech Stack:** Python 3.11+、asyncio、Pydantic 2、pytest，复用现有依赖。

**Spec:** [S03](../S03-evaluation.md)；强制遵守 [S00](../S00-development-boundary.md) 与 [TASKS 全局约束](../TASKS.md)。

## Global Constraints

仅在 MyCoder 的 codex/agent-governance 开发，基线 455533169d5a641300dd63d260b1ff5543c4093c；不改用户既有 diff，不运行 ruff format，不碰生产根/真实 API，不创建新工作树。

依赖：N00。状态：**已完成，2026-09-06**。实现、失败路径和独立审查已通过；证据见 IMPLEMENTATION.md。

## 文件与接口

新增：`nanobot/evaluation/__init__.py`、`__main__.py`、`models.py`、`runner.py`、`providers.py`、`verifiers.py`（均在 evaluation 下）；`tests/evaluation/test_runner.py`、`test_cli.py`、`fixtures/basic.json`。复用 `nanobot/nanobot.py`、`nanobot/providers/base.py`，不得复制 AgentLoop。

消费：N00 隔离约定。产出 S03 的 EvalCase/EvalResult/VerifierResult、`EvalRunner().run_case(case, *, mode, output_root)`、CLI run。Pydantic EvalCase 为可选空初始消息/文件设空列表/字典，budget 使用 S03 默认；`answer_contains` 仅供 harness 烟测，正式任务成功以后由文件/结构 verifier 判定。

- [x] **1 写失败测试。**

```python
from nanobot.evaluation.models import EvalCase
from nanobot.evaluation.runner import EvalRunner

async def test_scripted_runtime_smoke(tmp_path):
    case = EvalCase.model_validate({
        "schema_version": 1, "id": "smoke", "description": "runtime smoke",
        "user_inputs": ["回复 pong"],
        "scripted_responses": [{"content": "pong", "finish_reason": "stop"}],
        "allowed_tools": [],
        "verifiers": [{"name": "answer_contains", "expected": "pong"}],
    })
    result = await EvalRunner().run_case(
        case, mode="baseline", output_root=tmp_path
    )
    assert result.status == "passed"
    assert result.provenance["case_hash"]
    assert result.trace_ref
```

- [x] **2 RED。**运行 `.venv/Scripts/python.exe -m pytest tests/evaluation/test_runner.py tests/evaluation/test_cli.py -q -p no:cacheprovider`；预期缺新模块/契约失败。
- [x] **3 最小实现。**ScriptedProvider 继承现有 provider，匹配预期 purpose/调用顺序并返回 LLMResponse；耗尽或不匹配抛 EvalScriptError，永不使用真实 provider fallback。run_case 校验相对路径→创建唯一根→构造正式 bot→执行输入→关闭 bot→运行白名单 verifier→写结果。每请求/工具步执行前扣预算；超额以 budget_exceeded 收束，不继续工具。

```text
case validation -> isolated config + allowed registry -> Nanobot.run
 -> capture requests/tool results -> executable verifiers -> EvalResult
exceptions -> error + sanitized evidence; finally -> bot.aclose
```

加入路径逃逸、脚本耗尽、超预算、两次隔离、失败 verifier、finally 清理测试。fixture 不允许任意 shell/verifier import；HTTP/socket 意外访问测试中直接失败。
- [x] **4 GREEN。**重跑上述 focused 和 N00 测试、`tests/test_nanobot_facade.py`。CLI 在新 tmp 输出根执行 basic.json，检查版本、case hash、commit、模式；此时只支持 baseline/observe-compatible 占位配置映射，不声称 context/full 已实现，未接入模式报 unsupported。
- [x] **5 审查。**确认 trace 来自实际 runtime，不只测 fixtures；脚本测试不用于宣称语义质量；记录错误/预算和报告格式。不得启动 live 评测。

## 交接给 N01A

本任务 smoke/harness 验收后必须完成 N01A 的指标、两阶段场景和旧行为基线，不能直接进入生产修改。ScriptedProvider 构造为 ScriptedProvider(responses: list[dict])；N01A 可扩展 prompt-sensitive 模式，但不得从 ground truth 直接取答案。metrics/比较报告前置至 N01A，N10 只扩展组合评测。
