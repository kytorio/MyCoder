# N02 规范记忆可恢复提交 Implementation Plan

> **For agentic workers:** 按 REVIEW 已记录决策执行；不自动 commit/push。2026-09-06 已完成实现和定向验收。

**Goal:** 建立可确认、幂等且不覆盖第三方修改的记忆提交边界。

**Architecture:** 三个 canonical 文件和私有 receipt 由短同步协调器提交；模型调用在外，保留同步 SDK。

**Tech Stack:** Python 3.11+、asyncio、Pydantic 2、pytest，复用现有依赖。

**Spec:** [S02](../S02-explicit-memory.md)；强制遵守 [S00](../S00-development-boundary.md) 与 [TASKS 全局约束](../TASKS.md)。

## Global Constraints

仅在 MyCoder 的 codex/agent-governance 开发，基线 455533169d5a641300dd63d260b1ff5543c4093c；不改用户既有 diff，不运行 ruff format，不碰生产根/真实 API，不创建新工作树。

依赖：N01A、N09；H02/H03/H04 已确认。状态：**已完成**。用户明确要求跳过预写失败测试，先实现后集中测试。

## 文件与接口

新增：`nanobot/agent/memory_writes.py`（契约与协调器同模块）；`tests/agent/test_memory_commit.py`、`test_memory_commit_recovery.py`。修改 `nanobot/agent/memory.py` 的构造、read/write_*；`nanobot/sdk/clients.py` 的同步覆盖错误语义。复用 history 原子写方法、workspace_policy 的路径校验；不复用已归档 WriteGateway。

产出 S02 小型 memory 数据契约（放 memory_writes.py）；MemoryStore 保留 workspace/max_history_entries 参数，增加 keyword-only writer: MemoryWriteCoordinator | None 注入；新增 MemoryStore.remember(request: MemoryRememberRequested) -> MemoryCommitResult 委托以下实现：
`MemoryWriteCoordinator(root: Path, runtime_root: Path, *, authorize: Callable[[MemoryMutation], None])`、
`snapshot() -> MemorySnapshot`、
`commit(mutation: MemoryMutation) -> MemoryCommitResult`、
`build_explicit_mutation(request: MemoryRememberRequested, base: MemorySnapshot) -> MemoryMutation`。
authorize 是宿主现有访问约束适配器，生产不可默认 allow；拒绝时不产生 canonical 修改。共享工厂按规范根注入同实例，不能把模型 source=explicit 当用户授权。

- [x] **1 写失败测试（按用户要求跳过 RED，完成后补覆盖）。**以下最小样例之外，为各写入阶段注入 OSError、取消/进程中断、未知文件 hash，验证恢复。

```python
def test_replay_keeps_revision(tmp_path):
    from nanobot.agent.memory_writes import (
        MemoryFact, MemoryRememberRequested, MemoryScope,
    )
    from nanobot.agent.memory_writes import (
        MemoryWriteCoordinator, build_explicit_mutation,
    )
    scope = MemoryScope(instance_id="test", project_id=None)
    fact = MemoryFact(entry_id="e1", target="user", key="reply.language",
                      text="以后用中文回答", scope=scope, replaces_entry_id=None)
    request = MemoryRememberRequested(
        operation_id="op1", message_ref="user:1", original_text_hash="test-hash",
        owner_id="owner", scope=scope, facts=(fact,),
    )
    writer = MemoryWriteCoordinator(
        tmp_path / "workspace", tmp_path / "runtime", authorize=lambda mutation: None
    )
    mutation = build_explicit_mutation(request, writer.snapshot())
    first = writer.commit(mutation)
    second = writer.commit(mutation)
    assert first.status == second.status == "committed"
    assert second.replayed is True
    assert second.revision == first.revision
    assert "以后用中文回答" in writer.snapshot().contents["user"]
```

- [x] **2 RED（按用户要求不执行）。**未伪造失败记录；实现完成后统一运行定向与兼容回归。
- [x] **3 最小实现。**按 S02 journal 顺序实现；先全目标 hash/权限/显式区域预检，再 PREPARED、各原子 replace、全部 hash 验证、receipt。snapshot 先 recovery。相同 operation_id 不同 payload 返回 conflict；相同事实新消息为 no-op committed；只有 explicit correction 可替代 protected entry。同步 write_* 内转 mutation，成功 None，冲突 MemoryConflictError，不 await/asyncio.run。

```text
under shared root lock:
  recover known transactions
  authorize(mutation); validate identity, versions, protected entries
  durable prepare -> atomic canonical replaces -> verify -> durable receipt
  return committed
unknown hash/journal corruption -> conflict, preserve evidence, no overwrite
```

故障测试至少覆盖 canonical 前/中/后及 receipt 前后；从新 coordinator 实例恢复，测试混合旧新 hash、第三种 hash、相同 ID 内容不同、容量不足、symlink、两个会话交错、短锁不包围外部 await。1MiB/64MiB 默认来自 TASKS。
- [x] **4 GREEN 与回归。**新增 21 项全部通过；既有 MemoryStore/cursor/GitStore/workspace policy 107 项全部通过；Nanobot SDK memory 集成 1 项通过。同步 SDK 在运行中的 event loop 内不报 nested loop；显式提交前后 Dream cursor 不变。任务文档中的旧路径 `tests/utils/test_gitstore.py` 实际对应 `tests/agent/test_git_store.py`。
- [x] **5 审查。**控制器核对生产 `from_config` 注入唯一共享 writer、授权先于 PREPARED、私有日志不进入普通日志、snapshot recovery barrier、受管区保护及 Windows 文件名。journal/receipt 文件名改用 operation_id 的 SHA-256，正文保留原 ID；多进程共享根仍明确 unsupported。Windows 复用现有 fsync + atomic replace，目录 fsync 失败按 helper 既有 best-effort 语义处理，不宣称所有文件系统断电原子性。

最小变更：不创建 memory_contracts.py 或通用事务平台；复用现有原子写函数和访问守卫，不改 history 格式、输入接纳或 Dream 调度。配置由 N03 tools.memory 暴露，本任务构造函数接受同样默认限额与显式覆盖。
