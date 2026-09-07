# 旧方案回退记录

2026-09-06 按用户最新指令停止原实施并退回规划阶段。两个子代理已关闭，无未完成的实施工具调用或后台测试进程。

## 已回退

- 旧 spec 全目录（含旧 TASKS 和逐项任务）移出活动路径。
- 新增的 `nanobot/agent/lifecycle.py`、`write_gateway.py` 移出源码路径。
- 新增的 `test_lifecycle.py`、`test_memory_write_recovery.py`、`test_runner_characterization.py`、`test_write_gateway.py`、context_baseline fixture 和 engineering baseline 文档移出活动路径。
- `nanobot/agent/hook.py`、`nanobot/bus/runtime_events.py`、`tests/bus/test_runtime_events.py` 恢复到 HEAD 文本；`git diff --exit-code` 对这三项返回 0。
- 原 SDD 工作记录和测试临时目录整体归档，不作为新排期的完成依据。

可恢复归档：`E:\Code\Agent\MyCoder\.superpowers\sdd\RETIRED-governance-v1-20260906`。该目录由现有 sdd/.gitignore 忽略，包含回退前副本；没有永久删除这些旧产物，也未改变 Git 历史。

## 保留与限制

`.gitignore`、loop/runner 既有注释、`webui/package-lock.json` 与归档副本 SHA-256 一致。AGENTS.md 整体恢复被回退保护拒绝，因此保留其原内容，仅添加最新人工门禁，明确旧排期说明已失效。没有绕过保护去删除它。

现有虚拟环境及测试依赖保留，未卸载或改 lockfile；未修改生产配置、memory、会话或任何远端页面。Git 在 Windows 下可能仍显示文件换行/统计缓存状态，以文本 diff 核验回退，不用全仓 reset/clean 消除显示。

## 回退验证（非新设计验收）

使用独立配置和 OS 临时根运行既有 bus、CompositeHook、Goal continue、MemoryStore、Dream 测试：初次 **128 passed in 8.18s**；文档交付前重新运行同组测试，**128 passed in 8.02s**，exit 0。

执行入口为项目 `.venv/Scripts/python.exe`；用 `set_config_path` 指向 `.superpowers/sdd/CONTEXT-MEMORY-REPLAN/runtime-check/config.json`，pytest 目标：

```text
tests/bus/test_runtime_events.py
tests/agent/test_hook_composite.py
tests/agent/test_runner_goal_continue.py
tests/agent/test_memory_store.py
tests/agent/test_dream.py
```

最终复验参数：`-q --tb=short --basetemp=C:/Users/Kitorio/AppData/Local/Temp/mycoder-rollback-20260906-final -p no:cacheprovider`。这是回退后的兼容性核验，不是全仓测试、真实模型评测或新 N 任务完成证明。
