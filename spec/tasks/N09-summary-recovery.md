# N09 L4 结构化摘要与有限恢复 Implementation Plan

> **For agentic workers:** 按 REVIEW 已记录决策执行；使用 superpowers:subagent-driven-development 或 superpowers:executing-plans。不自动 commit/push。

**Goal:** 让自动、手动和溢出摘要共享同一边界，恢复时不丢 delta 或重复工具。

**Architecture:** 继续使用 Governor/Consolidator/SessionSummaryCheckpoint；结构化摘要只替代有接受证据的 H。

**Tech Stack:** Python 3.11+、asyncio、Pydantic 2、pytest，复用现有依赖。

**Spec:** [S01](../S01-context-compression.md)；强制遵守 [S00](../S00-development-boundary.md) 与 [TASKS 全局约束](../TASKS.md)。

## Global Constraints

仅在 MyCoder 的 codex/agent-governance 开发，基线 455533169d5a641300dd63d260b1ff5543c4093c；不改用户既有 diff，不运行 ruff format，不碰生产根/真实 API，不创建新工作树。

依赖：N07、N08。状态：**已完成**。用户于 2026-09-06 明确要求跳过先写失败测试，改为直接实现后统一测试；本任务按该授权执行，不补造 RED 记录。

## N09-R1 最小接缝裁定（实现前锁定）

- `StructuredContextSummary` 是严格的 Pydantic 模型：`schema_version` 只允许 `1`，八个列表字段全部必填，额外字段拒绝。上下文压缩返回值使用新的宿主类型（可读 `text` + 严格 `structured`），不用一个未验证的字符串同时表示模型输出与已接受检查点。
- 模型输出只是候选摘要；宿主在提交前校验。`explicit_preferences` 的每项必须可在 accepted H 的 user 文本中按空白归一化后精确找到；`evidence_refs` 必须是 accepted H 中已出现、且可由当前 session-scoped artifact store 解析的引用。任一项校验失败都不降级为“信任模型”或 raw 摘要，而是不提交 L4。
- `SessionSummary.text` 继续是旧读取器可直接消费的人类可读文本；严格结构以 `_last_summary.structured = {schema_version: 1, ...}` 附加保存。旧的只有 `text`/`last_active` 记录仍可读；未知结构版本不参与新的可信恢复。`SessionSummaryCheckpoint` 扩展为可选的结构化载荷，保留现有 `summary`/`transcript_boundary` 兼容面。
- L4 不直接写 `active_summary`。Governor 先生成并校验候选 checkpoint，再调用 `AgentRunSpec` 上的单一 async checkpoint-commit 回调。回调复用现有 `SessionManager.save_runtime_checkpoint` 原子 sidecar，在其中保存结构化摘要、原始 transcript boundary 和已有的回合恢复数据；只有该写入成功后才更换内存 H 视图、清空 provider continuation 并追加原样 delta。写入失败时旧 summary、旧 boundary、provider state 和 delta 均不变。
- 正常回合落盘时，已持久化的 staged summary checkpoint 通过现有 `_save_turn` 边界逻辑合并到主 session，然后删除 sidecar。重启时 `session/recovery.py` 只在 staged checkpoint 版本、边界和已存在的 pending/tool 配对都有效时物化它；已完成工具结果不重放。这是为恢复语义必要的窄扩展，允许修改 `nanobot/session/recovery.py`。
- 自动压力、手动 SDK compact 和 provider overflow 都调用同一 Governor L4 方法，不再各自实现摘要边界。手动/空会话/导入或重启后没有可验证 accepted H 时返回 typed no-op reason，不推进 `last_archived`。闲时 Memory 入库仍可保留旧日志归档职责，但不得把它的未验证历史当作 L4 accepted H。
- provider overflow 额度绑定 `ModelRequestState.lineage_id`，每个 lineage 最多一次；request id、provider 内部重试、fallback model 都不重置。L4 只在 L1→L2→L3 后仍超预算、明确 manual 或明确 overflow 时运行，不对 summary 自身递归压缩，不重放工具。
- 本任务仅运行新增摘要/恢复测试及列明的相关回归；N09 审查收口后再统一跑 S01 完整实验矩阵。

## 文件与接口

修改 `nanobot/agent/context_plan.py`、`context_governance.py` _compact_request_history/prepare_request、`memory.py` Consolidator.summarize_transcript、`loop.py` checkpoint 持久化、`nanobot/session/summary.py`、`nanobot/sdk/clients.py` compact_session；
`nanobot/providers/conversation_state.py` 仅必要 continuation 重建。
新增 `tests/agent/test_context_summary_pipeline.py`、`tests/agent/test_context_checkpoint_recovery.py`。

消费 N07 accepted H/outcome、N08 source policies、N06 artifact；产出 `StructuredContextSummary`（context_plan.py 的 Pydantic model）：schema_version=1、tasks/constraints/explicit_preferences/decisions/file_changes/errors/evidence_refs/remaining_work 均为 list[str]，字段必需，未知字段拒绝。显式偏好与证据引用由宿主核对，模型不可编造 artifact。现有 SessionSummary.text 保留可读渲染，结构体以版本化附加 metadata 保存，兼容旧纯文本读取。

- [x] **1 写失败测试（按用户授权跳过 RED）。**测试在实现后补齐，覆盖严格 schema、accepted H/delta、checkpoint 故障、overflow 单次重试、手动 compact、重启恢复与 L2 artifact 跨 L4 分页。

```python
import pytest
from pydantic import ValidationError

def test_summary_requires_constraint_and_evidence_fields():
    from nanobot.agent.context_plan import StructuredContextSummary
    with pytest.raises(ValidationError):
        StructuredContextSummary.model_validate({
            "schema_version": 1, "tasks": ["修改配置"], "remaining_work": ["运行测试"]
        })
```

另构造已消费 H + 当前 delta 的实际 Runner 案例，模拟摘要完成后 checkpoint 保存失败，断言旧 summary/边界仍有效且工具执行计数不变。
- [x] **2 RED（按用户授权跳过）。**没有把实现后的失败冒充 RED；实际验证证据见 `N09-report.md` 与 `IMPLEMENTATION.md`。
- [x] **3 最小实现。**前三层后仍过高水位、L2 已产生语义引用、manual 明确请求或 provider overflow 时进入统一 L4；无 accepted H 的自动路径不假装可压缩。摘要只输入 accepted H，delta 排除；新显式偏好与系统约束另外 required，摘要中的旧冲突内容不得覆盖。新结构验证失败不提交，无限递归“压缩摘要”禁止。

```text
L1 -> L2 -> L3 -> still pressured / manual / overflow -> summarize accepted H
 -> validate structure + source references
 -> save checkpoint atomically -> swap derived summary/boundary
 -> reset provider continuation -> append unchanged delta -> budget check
```

provider 明确 overflow 时同一 lineage 仅允许一次 L4 重试；request_id/fallback model 变化不刷新额度，且不能重放已完成工具。SDK compact/空历史/无接受证据时安全无操作并说明原因，不能把导入的未消费历史假装 accepted。checkpoint 写入前后/continuation 重建前后故障分别测试；失效摘要保留旧 checkpoint。
- [x] **4 GREEN。**评测回归 113 passed；N09/S01 相关聚合 482 passed 后发现 1 个既有 manifest reason 断言，修正后该断言 3 个参数全部通过；最终离线 S01 context-only 矩阵 12/12 passed。h12/h24 均运行严格 L4 且约束保留率 1.0；h24 覆盖 L2 预览筛选、artifact 分页与跨 L4 continuation。Ruff、basedpyright 通过。未运行真实模型，结论仅限结构、控制流和离线机制。
- [x] **5 审查。**确认手动/溢出/普通路径同 Governor；每层清单顺序正确，摘要 token 计入 summary purpose；恢复无 raw 删除/错误接受推进/重复工具。Goal 原样保留。独立审查进程按用户限制只等待一次、满 3 分钟无结论后终止；控制器随后只读核对锁顺序、artifact grounding/分页、L4 清单归因、sidecar 原子替换及 Goal 路径，未发现开放的 Critical/Important 问题。
