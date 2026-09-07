# N04 Dream 与保存工具协作 Implementation Plan

> **For agentic workers:** 按 REVIEW 已记录决策执行；使用 superpowers:subagent-driven-development 或 superpowers:executing-plans。任务已完成，不自动 commit/push。

**Goal:** 避免旧 Dream、全文写和恢复撤销新的显式偏好。

**Architecture:** 原两个 Dream 入口分别窄改，在已有工具注册点和 MemoryStore 加协调，不强制拆新 cycle 服务。

**Tech Stack:** Python 3.11+、asyncio、Pydantic 2、pytest，复用现有依赖。

**Spec:** [S02](../S02-explicit-memory.md)；强制遵守 [S00](../S00-development-boundary.md) 与 [TASKS 全局约束](../TASKS.md)。

## Global Constraints

仅在 MyCoder/codex/agent-governance 开发，基线 455533169d5a641300dd63d260b1ff5543c4093c；不改用户既有 diff，不运行 ruff format，不碰生产根/真实 API，不新建 worktree。先完成评测再改业务模块。

依赖：N02、N03；H03/H04 已确认。状态：**已完成，S02 集中评测已由 N10 关闭**。用户明确要求先实现再集中测试，因此本任务不记录虚假的 RED 阶段。

## 文件与接口

新增 `tests/agent/test_dream_memory_conflicts.py`、`tests/command/test_dream_restore_protection.py`。
修改 `nanobot/agent/memory.py` build_dream_tools/write_*/cursor；
`nanobot/cli/gateway_runtime.py` on_cron_job；
`nanobot/command/builtin.py` cmd_dream/cmd_dream_restore；
`nanobot/templates/agent/dream.md`。
GitStore 只补必要只读版本取内容，不重构其通用 revert。
不新增 dream_cycle.py/memory_tools.py 框架；小适配器放既有 build_dream_tools 所属模块，只有复杂度实证需要时再申请拆分。

消费 N02 writer/snapshot/commit 和 N03 已保存显式条目。
产出 memory_writes.merge_unmanaged_text(base: str, latest: str, proposed: str) -> str，重叠抛 MemoryConflictError；build_dream_tools 仍返回原 ToolRegistry，owned-file writes 绑定 read version 后提交。
保留现有两个入口签名、周期和停止结果。

- [x] **1 实现后补回归测试。**

```python
import pytest

def test_old_dream_conflicts_with_new_overlapping_text():
    from nanobot.agent.memory_writes import MemoryConflictError, merge_unmanaged_text
    with pytest.raises(MemoryConflictError):
        merge_unmanaged_text("language=old\n", "language=Chinese\n", "language=English\n")
```

再分别通过 cron/手动真实入口，用 asyncio.Event 暂停旧 Dream，在另一会话实际执行 memory_save 后恢复旧写；新偏好保留，冲突未解则 cursor 不推进。不能只测纯函数。
- [x] **2 用户覆盖 RED-first。**未先运行失败测试；生产实现闭环后再统一执行测试。
- [x] **3 最小实现。**保留两入口编排；在 owned-file write/edit/apply_patch 适配器集中预检和提交，Skill 写保留旧路径，混合 owned/Skill patch 先拒绝再拆开。Dream 生成不持锁，最新显式区域不可变；未保护区域非交叠可合并，重叠冲突后重读最多重试一次。

```text
original Dream entry -> versioned read -> controlled file write
completed AND no unresolved conflict -> atomic cursor
failed/cancelled/conflict -> cursor unchanged
SDK/restore candidate -> protected-entry precheck -> commit or conflict
```

无显式条目时保持 SDK/restore 旧使用方式；有冲突不能先落盘再修复。Git失败与canonical成功分开报告；cursor与内容间中断重跑幂等。Dream注册中不装载memory_save，不因其内容含“记住”自动调工具。
- [x] **4 GREEN。**focused 11 passed；连同既有 Dream/session/cursor/command/GitStore/apply_patch/config 回归共 142 passed；相关 Ruff 与 basedpyright clean。覆盖旧快照、显式条目保留、重叠冲突、重读重试、混合 Skill/canonical 拆分、手动和 cron 游标门控、restore 先验保护。
- [x] **5 审查。**只读审查按用户要求只等待一次，三分钟内未返回结论后已终止；不重复轮询。控制器结合 142 项回归核对两个入口、全文保护、锁边界、intervalH/cron/modelOverride 与 history/cursor 顺序，未发现开放的 Critical/Important 问题。
