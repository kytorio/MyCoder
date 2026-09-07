# N11 配置兼容与交付门禁 Implementation Plan

> **For agentic workers:** 按 REVIEW 已记录决策执行；使用 superpowers:subagent-driven-development 或 superpowers:executing-plans。任务已完成并停在人工启用门禁，不自动 commit/push。

**Goal:** 交付可关闭、可追溯的配置/SDK说明和离线验证证据。

**Architecture:** 收束已有局部配置与注入路径，不新增 UI、安全平台或生产自动启用。

**Tech Stack:** Python 3.11+、asyncio、Pydantic 2、pytest，复用现有依赖。

**Spec:** [S00](../S00-development-boundary.md)、[S01](../S01-context-compression.md)、[S02](../S02-explicit-memory.md)、[S03](../S03-evaluation.md)；强制遵守 [S00](../S00-development-boundary.md) 与 [TASKS 全局约束](../TASKS.md)。

## Global Constraints

仅在 MyCoder 的 codex/agent-governance 开发，基线 455533169d5a641300dd63d260b1ff5543c4093c；不改用户既有 diff，不运行 ruff format，不碰生产根/真实 API，不创建新工作树。

依赖：N10。状态：**已完成**。默认仍为 observe + memory disabled；没有 live 调用或生产启用。

## 文件与接口

修改 `nanobot/config/schema.py`、`loader.py` 中确需兼容验证的位置、`nanobot/nanobot.py`/SDK 注入文档；
新增 `tests/config/test_context_memory_roundtrip.py`、
`docs/engineering/context-memory-guide.md`、
`docs/engineering/context-memory-release-checklist.md`。
更新本 spec/TASKS 的真实状态。前端 `webui/` 与 package-lock 不在范围，现有设置 API 不应保存时丢失新配置；若其通用透传存在缺陷，先在记录列出精确后端文件并补回归，不能顺便增建面板。

消费 N03 tools.memory、N05 context、N01A/N10 离线结果。产出兼容 config JSON 样例、SDK 根注入说明、关闭/恢复指引和发布清单，不产生生产启用动作。

- [x] **1 写失败测试。**按用户“不用写失败测试，直接实现再做测试”的明确要求跳过前置失败测试；没有伪造 RED 记录。

```python
def test_default_config_does_not_enable_new_mutations():
    from nanobot.config.schema import Config
    config = Config.model_validate({})
    assert config.agents.context.mode == "observe"
    assert config.tools.memory.enabled is False
    assert config.agents.defaults.dream.interval_h == 2
    serialized = config.model_dump(by_alias=True)
    restored = Config.model_validate(serialized)
    assert restored.agents.context.mode == "observe"
    assert restored.tools.memory.enabled is False
```

- [x] **2 RED。**按同一用户授权跳过前置 RED，改为实现完成后的 focused/compatibility 验证。
- [x] **3 最小实现。**复核确认 camelCase roundtrip、旧默认、原子保存和环境变量模板保持均可复用现有 `Base`/loader/WebUI path-scoped read-modify-write，无需修改 loader。仅补两个缺口：memory transaction/journal 字节上限拒绝 bool；`AgentLoop.from_config` 在 context enforce 或 memory_save enabled 且无绑定/显式 runtime root 时立即报错。新增配置/SDK/关闭恢复指南和发布清单。

```text
default -> observe, tools.memory disabled
reviewed offline evidence -> separately approved test config enable
memory_save disabled -> recovery/protection remain active
production rollout / live eval / commit / push -> separate explicit authorization
```

说明 H03 单进程同根约束、H04 SDK/restore 冲突、人工纠正方式、artifact/journal 空间失败恢复、未知 token 解释。无自动迁移用户 profile 根，无自动删除 raw/artifact。配置示例使用非敏感测试路径，不内嵌 API key。
- [x] **4 GREEN。**新增 9 项门禁用例与指定配置/SDK回归合并为 143 passed、1 skipped；`test_config_paths.py::test_workspace_path_is_explicitly_resolved` 首次仅因沙箱禁止写用户目录失败，在授权环境原样重跑 1 passed。受影响的 tool-batch/memory commit/memory_save/evaluation runner 另有 107 passed。Ruff `--no-cache` clean，basedpyright 0 errors/warnings/notes。按用户资源门禁不重复运行 N10 六模式实验，复用 N10 已保存的 full 59/59 与逐层消融证据。
- [x] **5 人工交付。**已检查分支/基线和任务边界，补齐默认关闭、SDK root、单所有者、纠正/Dream 冲突、磁盘/事务恢复、降级归档和逐项人工启用说明。没有 live 质量评测、生产启用、服务启动、合并、commit 或 push；交付停在人工授权门禁。

详细证据见 `.superpowers/sdd/CONTEXT-MEMORY-V2/N11-report.md`，操作说明见 `docs/engineering/context-memory-guide.md` 和 `docs/engineering/context-memory-release-checklist.md`。
