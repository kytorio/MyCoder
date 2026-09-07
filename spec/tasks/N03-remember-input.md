# N03 LLM memory_save 工具 Implementation Plan

> **For agentic workers:** 按 REVIEW 已记录决策执行；不自动 commit/push。2026-09-06 用户明确要求先实现、再集中测试，故本任务不伪造 RED 记录。

**Goal:** 主模型通过普通工具事件触发可确认记忆保存。

**Architecture:** 直接复用 Tool/Loader/Registry/RequestContext 和 MemoryStore.remember；没有输入识别或额外模型。

**Tech Stack:** Python 3.11+、asyncio、Pydantic 2、pytest，复用现有依赖。

**Spec:** [S02](../S02-explicit-memory.md)；强制遵守 [S00](../S00-development-boundary.md) 与 [TASKS 全局约束](../TASKS.md)。

## Global Constraints

仅在 MyCoder/codex/agent-governance 开发，基线 455533169d5a641300dd63d260b1ff5543c4093c；不改用户既有 diff，不运行 ruff format，不碰生产根/真实 API，不新建 worktree。先完成评测再改业务模块。

依赖：N02、N09；H01–H05 已确认。状态：**已完成**。

## 文件与接口

新增 `nanobot/agent/tools/memory_save.py`、`tests/tools/test_memory_save.py`、`tests/agent/test_memory_tool_integration.py`、`tests/config/test_memory_tool_config.py`。
修改 `nanobot/agent/tools/context.py` 的 ToolContext 注入、`nanobot/config/schema.py` ToolsConfig.memory 及懒加载引用、`nanobot/agent/loop.py` 构造注入、`nanobot/nanobot.py`/`sdk/runtime.py` 必要 root 注入。
修改 `nanobot/agent/context.py`/`context_governance.py` 的 memory 版本刷新、`nanobot/skills/memory/SKILL.md` 的使用说明。
ToolLoader、ToolRegistry 和 runner 工具执行顺序原样复用，不改输入/pending 接纳状态机。

消费 N02 MemoryStore.remember、MemoryRememberRequested、MemoryCommitResult；N09 prepare_request。
产出 MemorySaveTool(memory: MemoryStore)，名称 memory_save；参数 target/key/content/source_excerpt/replaces_entry_id 按 S02。MemoryToolConfig 跟工具定义，ToolsConfig 显式引用，config_key=memory，默认 enabled=False。
构造来源/operation_id 在工具内部完成，LLM 不传路径、owner、scope、权限或 committed 标志。

- [x] **1 添加验收测试。** 测试在实现完成后集中补齐，遵循用户对测试顺序的明确要求。

```python
import json

async def test_tool_commits_before_return(tmp_path):
    from nanobot.agent.memory import MemoryStore
    from nanobot.agent.memory_writes import MemoryWriteCoordinator
    from nanobot.agent.tools.memory_save import MemorySaveTool
    from nanobot.agent.tools.context import RequestContext, request_context
    writer = MemoryWriteCoordinator(
        tmp_path / "workspace", tmp_path / "runtime", authorize=lambda mutation: None
    )
    memory = MemoryStore(tmp_path / "workspace", writer=writer)
    tool = MemorySaveTool(memory)
    ctx = RequestContext(
        channel="cli", chat_id="test", session_key="cli:test", message_id="m1",
        sender_id="owner", original_user_text="记住，以后用中文回答"
    )
    with request_context(ctx):
        result = await tool.execute(
            target="user", key="reply.language",
            content="以后用中文回答", source_excerpt="以后用中文回答"
        )
    assert json.loads(str(result))["status"] == "committed"
    assert "以后用中文回答" in memory.read_user()
```

- [x] **2 RED。**按用户要求跳过失败先行阶段；没有把实现后的失败冒充为 RED。
- [x] **3 最小实现。**工具 schema 有界，read_only=False/exclusive=True；从 current_request_context 与宿主 memory 根解析来源/scope，source_excerpt 不存在/只读/无来源/secret/受保护冲突均 ToolResult.error。语义是否值得保存由主模型负责，不另加分类模型；更新 Skill 说明只在成功工具结果后确认。

```text
LLM tool call -> existing validation/execution
 -> MemoryRememberRequested -> MemoryStore.remember -> committed receipt
 -> tool result in delta -> next prepare_request refreshes changed memory revision
no call -> no save; error -> ordinary tool error, no input short-circuit
```

测试 registry 实际发现/加载、disabled 不发schema、一次调用和重复调用、纠正、只读、失败后无成功凭据。完整 Runner fixture 包含“模型不调用工具但自称记住”，判为 false confirmation，不让宿主悄悄补写；包含误调用负例，不把结构校验当语义授权保证。工具结果与下一请求都必须可追溯同 revision。
- [x] **4 GREEN。**N02/N03 与真实 `setup=tool_write` 聚合 39 passed；此前指定 N03 相邻回归 128 passed。Ruff `--no-cache` 与 basedpyright 均 clean。评测先由 A 会话真实调用 `memory_save`，再由全新 B 会话使用写入内容；baseline 仍诚实返回 not_implemented。完整记忆矩阵继续留到 N04 关闭后的 S02 门禁。
- [x] **5 审查。**控制器核对新增入口仍只是一个 Tool，核心改动限于 MemoryStore 注入、配置和请求前 memory 刷新；未新增意图模型、服务或 admission 状态机。漏调用不会保存，误调用由来源绑定/secret/权限/保护区校验拒绝。S02 独立模块审查按计划在 N04 后统一进行，最多等待一次 3 分钟。

实现证据见 `.superpowers/sdd/CONTEXT-MEMORY-V2/N03-report.md`。
