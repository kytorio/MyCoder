# N07 消费边界与 L2/L3 Implementation Plan

> **For agentic workers:** 按 REVIEW 已记录决策执行；使用 superpowers:subagent-driven-development 或 superpowers:executing-plans。任务已完成，不自动 commit/push。

**Goal:** 仅对有证据的旧上下文做合法剪裁和重复结果缩短。

**Architecture:** 先修正 accepted H 的证据，再复用合法 turn/tool 组算法；raw 与派生视图隔离。

**Tech Stack:** Python 3.11+、asyncio、Pydantic 2、pytest，复用现有依赖。

**Spec:** [S01](../S01-context-compression.md)；强制遵守 [S00](../S00-development-boundary.md) 与 [TASKS 全局约束](../TASKS.md)。

## Global Constraints

仅在 MyCoder 的 codex/agent-governance 开发，基线 455533169d5a641300dd63d260b1ff5543c4093c；不改用户既有 diff，不运行 ruff format，不碰生产根/真实 API，不创建新工作树。

依赖：N06（已验收）。状态：**已完成**。实现与复审证据见 `spec/IMPLEMENTATION.md` 及 N07 报告。

## 文件与接口

修改 `nanobot/agent/context_plan.py`（ContextRequestOutcome）、`context_governance.py` CompactionState/from_transcript/accept_request/snip、`runner.py` 普通/no-tools/最终化入口、`nanobot/providers/base.py` 及需要证据的具体 adapter（逐个列入执行记录，不无差别重写）；`nanobot/session/summary.py` 仅必要版本 metadata。
新增 `tests/agent/test_context_acceptance.py`、`test_context_history_layers.py`；扩展 `tests/agent/test_runner_errors.py`、`test_runner_fallback.py`。

已核对的适配器写集：`openai_compat_provider.py`、`openai_responses/parsing.py`、
`anthropic_provider.py`、`bedrock_provider.py`、`azure_openai_provider.py`、
`openai_codex_provider.py`、`xai_grok_provider.py`、`fallback_provider.py`；均位于
`nanobot/providers/`。复用已有结束事件/状态解析，不改 wire 格式或原生续接条件。
新增 `tests/providers/test_context_acceptance.py` 和真实 SDK 跨轮次/恢复/组合预算
测试 `tests/agent/test_context_restart_acceptance.py`。

N07-R1 最小接线裁决：同一进程跨轮次须保留已经验证的消费证据，避免每轮重建
都丢失 H。允许 RunSpec/RunResult 增加可选消费快照，Session 增加仅内存字段，
Loop 仅在现有 runner.run 前后传递。Session.clear 清除证据；不序列化、不新增
持久化格式，导入和进程恢复仍 unknown。快照须校验历史前缀及实际接受的派生表示，
不能把仅发过 preview/ref 的原文重新冒充为全文已消费；不改变观察模式的旧渲染。
新增跨轮次/清空/重启和派生表示回归；后续 N09 复用此边界，不再建立第二条状态链。

N07-R2：按 S01 的原生私有状态证据限制，在现有 base 回执绑定处保守处理；
新增真实 Codex 离线 HTTP 的恢复压缩态、调用内再次压缩，以及恢复后独立全文
重发测试，证明 opaque replay 不冒充全文消费、重发后才可推进。最终回退叶子
独立发送全文时保留其已绑定凭据。原续聊 wire、候选 state 和恢复协议不变。

产出 `ContextRequestOutcome(request_id: str, lineage_id: str, attempt: int, status: str)`，status 只允许 accepted/rejected/unknown；
`should_accept(outcome: ContextRequestOutcome) -> bool`。
向现有 accept_request 加显式 outcome 参数，只接受匹配当前请求的 accepted，拒绝过期 ID；L2/L3 由 Governor 原入口返回带 reason 的新 plan。

- [ ] **1 写失败测试。**

```python
import pytest

@pytest.mark.parametrize("status,expected", [
    ("accepted", True), ("rejected", False), ("unknown", False),
])
def test_acceptance_requires_evidence(status, expected):
    from nanobot.agent.context_plan import ContextRequestOutcome, should_accept
    outcome = ContextRequestOutcome("r1", "lineage1", 0, status)
    assert should_accept(outcome) is expected
```

另用真实 Runner + scripted provider 分别产生正常、溢出、异常转换 response、断流、no-tools 和最终化，检查 raw_accepted_boundary 不被错误推进；不能仅以上纯函数通过验收。
- [ ] **2 RED。**运行 `.venv/Scripts/python.exe -m pytest tests/agent/test_context_acceptance.py tests/agent/test_context_history_layers.py -q -p no:cacheprovider`。
- [ ] **3 最小实现。**在最靠近真实 provider 的返回边界产生 outcome；超时未知、明确未接受为 rejected，不能 response 对象存在就 accepted。from_transcript 当前把旧 history 天然视为 accepted，需给 imported/recovered/legacy 无证据部分标 unknown；下一真实成功请求后才可推进，不能伪造历史消费凭据。

```text
request_id + raw boundary snapshot -> provider
accepted matching outcome -> advance H; otherwise keep H + delta
L2 -> legal complete old groups -> archive interval once -> reference marker
L3 -> accepted old result AND same path+version -> shorten to preview/ref
```

用户当前输入、未发送 delta、未知消费组保留；空间不足可用 L1 安全保存但不冒充旧已读。L2 去重键含原始区间+hash；L3 路径相同而内容不同不可去重。归档失败不删除内容；调用结果必须原子配对，不删 call 留 result。消费 metadata 只是证据，保存 checkpoint 不是接受事件。
- [ ] **4 GREEN。**focused + `tests/agent/test_runner_errors.py tests/agent/test_runner_fallback.py tests/agent/test_runner_governance.py tests/agent/test_runner_injections.py tests/agent/test_runner_persistence.py tests/providers/test_conversation_state.py tests/session/test_recovery.py`。覆盖旧导入历史、重复恢复、错误转成功回退和请求 ID 变化。
- [ ] **5 审查。**检查所有模型请求入口而非只主循环，确认任何错误路径不推进 H、最新结果首读未丢、无重复归档/重放工具。不更改 Goal continuation 状态，兼容回归 `test_runner_goal_continue.py`。
