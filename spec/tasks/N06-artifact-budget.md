# N06 L1 工具结果与归档 Implementation Plan

> **For agentic workers:** 按 REVIEW 已记录决策执行；使用 superpowers:subagent-driven-development 或 superpowers:executing-plans。已验收，不自动 commit/push。

**Goal:** 大工具结果先完整保存，再按单条/整批预算提供 preview 和引用。

**Architecture:** 独立 artifact store 由 runtime 根注入；既有 normalize/budget 调用新存储，不复用旧 TTL 删除。

**Tech Stack:** Python 3.11+、asyncio、Pydantic 2、pytest，复用现有依赖。

**Spec:** [S01](../S01-context-compression.md)；强制遵守 [S00](../S00-development-boundary.md) 与 [TASKS 全局约束](../TASKS.md)。

## Global Constraints

仅在 MyCoder 的 codex/agent-governance 开发，基线 455533169d5a641300dd63d260b1ff5543c4093c；不改用户既有 diff，不运行 ruff format，不碰生产根/真实 API，不创建新工作树。

依赖：N05（已验收）。状态：**已完成，定向复审通过**。每个测试场景按下面五步分别完成，不一次性铺满生产代码。

## 文件与接口

新增 `nanobot/agent/context_artifacts.py`、`nanobot/agent/tools/context_artifact.py`、`tests/agent/test_context_artifacts.py`、`tests/agent/test_tool_batch_budget.py`。
修改 `nanobot/agent/context_governance.py` normalize_tool_result/apply_tool_result_budget、`runner.py` 批次收集、`nanobot/utils/helpers.py` 的兼容调用边界、`nanobot/agent/tools/context.py` ToolContext 注入。不让新 store 进入旧 _cleanup_tool_result_buckets。
接线复核 N06-R1：在 `loop.py` 的既有构造/ToolContext/RunSpec 位置窄加同一个 store 注入，
不创建第二条治理链。helpers 原子写复用并支持不转换换行的文本写入，原有调用默认行为保持不变。
归档回读页本身应有界且不得递归归档成另一个预览；并行页无法同时放入保护预算时明确拒绝。
接线复核 N06-R2：真实 SDK 重启测试发现 Loop 的最终落盘仍会截断工具文本，
因此 enforce 的最终落盘须保留完整文本/文本块，observe 和图片脱敏维持原样。
`session/manager.py` 的 get_history 仅透传宿主保存的布尔 tool_result_error 元数据，
使重启后预览仍保留真实错误状态；不得顺带持久化/透传其他私有运行时 metadata。

产出 `ToolResultArtifactStore(root: Path, *, max_bytes: int)`；
`put(session_key: str, call_id: str, content: str) -> ArtifactRef`；
`read(ref: ArtifactRef, scope: str, max_bytes: int) -> str`（scope 为宿主当前 session_key，超 read 限额报错，不静默截断）；
`read_page(ref, scope, *, offset_chars=0, max_chars=16384) -> ArtifactPage`，page 含 text/next_offset_chars（末页 None）/content_hash，支持完整有界回读。
ArtifactRef 含不透明 ID、content_hash、byte_length、scope；不得将 ref 当任意本地文件路径。

- [x] **1 写失败测试。**

```python
import pytest

def test_artifact_roundtrip_and_scope(tmp_path):
    from nanobot.agent.context_artifacts import ToolResultArtifactStore
    store = ToolResultArtifactStore(tmp_path, max_bytes=1048576)
    original = "中文结果\n" * 2000
    ref = store.put("session:a", "call:1", original)
    assert store.read(ref, "session:a", max_bytes=1048576) == original
    with pytest.raises(PermissionError):
        store.read(ref, "session:b", max_bytes=1048576)
```

- [x] **2 RED。**运行 `.venv/Scripts/python.exe -m pytest tests/agent/test_context_artifacts.py tests/agent/test_tool_batch_budget.py -q -p no:cacheprovider`。
- [x] **3 最小实现。**hash+原子 replace 完成后才返回引用；identity 包含会话/call/内容版本，防同 call_id 旧内容碰撞。文本原样保存；结构化结果用版本化 JSON 保留原始 blocks，图片不转成无来源字符串。preview 留工具名/错误/有效首读信息。预算先单条 2048，再分配批次 8192，再受总输入预算限制；无法同时容纳 protected 组时拒绝，不拆配对。

```text
all tool results -> estimate single + batch -> choose oversized items
 -> persist full content + verify -> preview/ref -> recalculate full request
persist failure -> retain full original -> if still over budget: explicit refusal
```

read 工具仅接受 ref/分页参数，验证 session、hash、容量和 symlink；禁止任意路径和自动加载另会话引用。恢复后分页拼接应与原文逐字符一致。128MiB 上限前置校验；无自动 TTL 删除，仍引用 artifact 保留。raw transcript 仍可追溯原始结果，不因 prompt preview 替换而丢失。
- [x] **4 GREEN。**focused + `tests/agent/test_runner_tool_execution.py tests/agent/test_context_governance.py tests/agent/test_runner_governance.py tests/utils/test_token_estimation.py`。验证批次刚好等于/超限、并行 error result、首读、磁盘满、断写/损坏、重复治理、重启分页回读。observe 请求逐字段不变。
- [x] **5 审查。**检查旧 helper 的 TTL 路径不作用于新引用、无 raw 删除、无扩大 read_file 范围、没有先丢内容再异步保存。记录 manifest 中 offloaded/archive_failed 与真实行为一致。
