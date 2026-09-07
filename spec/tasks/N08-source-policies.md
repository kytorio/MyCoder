# N08 图片、Skill、MCP 与记忆策略 Implementation Plan

> **For agentic workers:** 按 REVIEW 已记录决策执行；使用 superpowers:subagent-driven-development 或 superpowers:executing-plans。当前已完成，不自动 commit/push。

**Goal:** 按来源分别记账和选择，同时保护当前任务所需内容。

**Architecture:** Context sources 提供策略，完整执行 registry 保留；发现 schema 与发送 schema 解耦。

**Tech Stack:** Python 3.11+、asyncio、Pydantic 2、pytest，复用现有依赖。

**Spec:** [S01](../S01-context-compression.md)、[S02](../S02-explicit-memory.md)；强制遵守 [S00](../S00-development-boundary.md) 与 [TASKS 全局约束](../TASKS.md)。

## Global Constraints

仅在 MyCoder 的 codex/agent-governance 开发，基线 455533169d5a641300dd63d260b1ff5543c4093c；不改用户既有 diff，不运行 ruff format，不碰生产根/真实 API，不创建新工作树。

依赖：N05、N06。状态：**已完成（2026-09-06）**。验证证据见
`.superpowers/sdd/CONTEXT-MEMORY-V2/N08-report.md`；S01 集中实验按资源门禁延后至 N09 完成。

## 文件与接口

新增 `nanobot/agent/context_sources.py`、`nanobot/agent/tools/tool_discovery.py`、`tests/agent/test_context_source_policies.py`、`tests/tools/test_tool_discovery.py`。
修改 `nanobot/agent/context.py`、`skills.py`、`context_governance.py`、`tools/registry.py`；`tools/mcp.py` 只补来源标记/版本关联，不改 transport 安全与生命周期。helpers 模型图像估算需调整时仅改估算函数并补测试。

消费 N05 ContextSource、现有 MemoryStore 文本/hash、N06 artifact；不依赖后置 memory_save。
产出 `select_tool_schemas(definitions: list[dict], loaded_names: set[str], *, discovery_enabled: bool) -> SchemaSelection`；SchemaSelection 含 definitions/deferred_names。关闭 discovery 时全量；开启时返回已加载项及单独常驻的发现/回读工具完整 schema。上述函数只选择传入业务 definitions，常驻工具由 Builder 单独加且只记一次。

- [x] **1 写失败测试。**

```python
def test_schema_selection_never_truncates_parameters():
    from nanobot.agent.context_sources import select_tool_schemas
    definitions = [
        {"type": "function", "function": {
            "name": "mcp_a", "parameters": {"type": "object", "required": ["x"],
            "properties": {"x": {"enum": ["first", "second"]}}}
        }},
        {"type": "function", "function": {
            "name": "mcp_b", "parameters": {"type": "object"}
        }},
    ]
    selected = select_tool_schemas(definitions, {"mcp_a"}, discovery_enabled=True)
    assert selected.definitions == definitions[:1]
    assert selected.deferred_names == ("mcp_b",)
```

- [x] **2 RED。**运行 `.venv/Scripts/python.exe -m pytest tests/agent/test_context_source_policies.py tests/tools/test_tool_discovery.py -q -p no:cacheprovider`。
- [x] **3 最小实现。**图片按模型能力/尺寸/数量估算；无法估算标 unknown，不按字符算 base64。当前必需图保护，旧图引用化通过可回取资源映射。显式或活跃 Skill 全文保持，索引支持后续加载；不可截去硬约束。memory 分 source ID/版本/项目选择，先测试旧 memory 与 synthetic required 条目；N03 接入后，committed receipt 指向的事实 required，不能用旧 summary 抵消。

```text
source kind -> policy -> estimated tokens + allowed strategies
discovery result -> authorized tool names loaded for next request
registry execution set unchanged; selected schemas retain complete parameters
memory selection -> scoped snapshot + latest committed preferences
```

发现只返回当前真实 registry 已授权可用工具，不创建工具权限；加载过的 schema 在当前活动调用组完成前保护。请求模型切换重新估算图像/schema 成本；不能重用不兼容 tokenizer 缓存。record 每个淘汰原因，对 irrelevant memory 只不注入、不改文件。
- [x] **4 GREEN。**focused + `tests/agent/test_context_builder.py tests/agent/test_context_prompt_cache.py tests/utils/test_token_estimation.py tests/agent/test_context_aware.py`；在 N01 真实 runtime 案例中执行“发现→下一请求有 schema→工具成功”，测试 registry 数量/工具能力不因隐藏减少。图像 synthetic、MCP 使用本地 stub，不联网。
- [x] **5 审查。**确认 source 账本互斥、旧文件不被删除、未知成本不为零、required 超限拒绝；关闭 schemaDiscovery 全量兼容。不得顺便改 MCP transport 或通用审批能力。

不在本任务实现记忆保存/新持久化 scope；旧未标记内容沿用既有语义。N03 新增项目标记、纠正和提交后刷新，并做端到端测试。N01A memory_on/off/irrelevant 基线保持可独立运行。

## N08-R1 最小接线裁决（实施前）

- schema discovery 使用每次 `AgentRunner.run` 独立的状态并通过 ContextVar 绑定到
  `tool_discovery`；状态只持有本次实际 `ToolRegistry`，不能捕获 Loop 的全局未过滤
  registry。允许为此窄改 Runner、Loop 与 ToolContext：Loop 只按已有
  `context.schemaDiscovery` 注册常驻发现工具，Runner 在每次模型请求前选择 schema。
  registry 的执行集合始终不变；已加载名称单调增加到本轮结束，因而活动调用组的
  schema 不会在下一请求消失。关闭 discovery 时不注册发现工具且发送全量 schema。
- `tool_discovery` 只返回当前 registry 中已授权工具的名称和有界描述；只有显式
  `load` 的精确名称进入下一请求，未知/未授权名称返回错误而不改变状态。发现工具与
  `context_artifact_read` 作为常驻 schema 单独添加一次，不计入被延迟的业务定义。
- 图片估算每次按当前 model 与实际 data URL 重新计算，不缓存跨模型结果。只对有
  官方、可实现公式的已知 OpenAI/Anthropic 模型族以及可解析 PNG/JPEG/GIF/WebP
  尺寸返回 token；其他模型、远程 URL、坏图或未知 detail 返回 unknown，enforce
  继续明确拒绝而不是按字符或零计算。当前图片始终 required。
- 已消费旧图只在 `_meta.path` 位于当前 workspace、文件仍存在且字节 hash 与发送
  内容一致时，可替换为含相对路径/hash/`read_file` 的引用；否则保留原图或在预算
  不可证明时拒绝。不得借引用放宽 ReadFileTool 的 workspace 边界。
- 既有 USER/SOUL/MEMORY 在 N03 前仍按单实例语义分别用内容 hash 记账并默认保留；
  不新增自然语言相关性分类器。`include_memory=False` 沿用现有不注入语义，不删除
  文件；synthetic selected/protected memory 仅验证策略接口。显式/always Skill 全文
  沿用现有 required 来源，其他 Skill 只保留已有索引与 read_file 加载入口。
