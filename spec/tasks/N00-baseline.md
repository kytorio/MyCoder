# N00 隔离基线 Implementation Plan

> **For agentic workers:** 按 REVIEW 已记录决策执行；使用 superpowers:subagent-driven-development 或 superpowers:executing-plans。本任务已完成并验证；不自动 commit/push。

**Goal:** 冻结可重复的现有行为和隔离测试环境。

**Architecture:** 先记录当前代码与配置，再以正式入口做特征测试；不调整 runtime 行为。

**Tech Stack:** Python 3.11+、asyncio、Pydantic 2、pytest，复用现有依赖。

**Spec:** [S03](../S03-evaluation.md)；强制遵守 [S00](../S00-development-boundary.md) 与 [TASKS 全局约束](../TASKS.md)。

## Global Constraints

仅在 MyCoder 的 codex/agent-governance 开发，基线 455533169d5a641300dd63d260b1ff5543c4093c；不改用户既有 diff，不运行 ruff format，不碰生产根/真实 API，不创建新工作树。

依赖：H01–H05 已记录；本任务为评测前置起点。状态：**已完成**。证据见 [基线报告](../../docs/engineering/context-memory-baseline.md)：8 新测试、145 既有回归分别通过；Ruff --no-cache 通过，独立审查的规格与质量均 Approved。生产功能未改。

## 文件与接口

新增：`tests/evaluation/conftest.py`、`tests/evaluation/test_isolation.py`、`tests/evaluation/test_baseline_runtime.py`、`docs/engineering/context-memory-baseline.md`。只读复用 `tests/agent/runner_helpers.py`、`tests/test_nanobot_facade.py`、`tests/agent/test_runner_governance.py` 的构造方式，不复活旧 characterization 文件。

消费：现有 Nanobot.run、LLMRuntime、MemoryStore。产出：`isolated_roots` pytest fixture，返回 frozen `IsolatedRoots(workspace: Path, runtime: Path, config: Path)`，各路径均位于本测试 tmp_path；在 fixture 内设置/恢复 config 路径并禁止意外网络连接。

- [x] **1 写测试。**先验证目录分离及禁止引用生产路径，再补 baseline 正常回复、并行工具配对、Dream cursor 和 context overflow 特征。

```python
def test_roots_are_local_and_distinct(isolated_roots, tmp_path):
    roots = isolated_roots
    assert roots.workspace.is_relative_to(tmp_path)
    assert roots.runtime.is_relative_to(tmp_path)
    assert roots.config.is_relative_to(tmp_path)
    assert roots.workspace != roots.runtime
```

- [x] **2 RED。**运行 `.venv/Scripts/python.exe -m pytest tests/evaluation/test_isolation.py -q -p no:cacheprovider`，预期未定义 fixture 失败；若旧测试在环境导入失败，先记录环境问题，不修改生产代码掩盖。
- [x] **3 最小实现。**fixture 用 pytest tmp_path 创建 workspace/runtime，config=runtime/config.json；用 monkeypatch 临时替换配置选择并在 teardown 恢复。构建基线 provider 只使用 LLMResponse 脚本；捕获实际请求 payload，不能只截获 prompt builder。创建 Markdown 基线记录 commit、dirty 文件 hash、Python/依赖版本和实际命令。

```python
@dataclass(frozen=True)
class IsolatedRoots:
    workspace: Path
    runtime: Path
    config: Path
# 每测试独立目录；memory 构造/旧迁移也只可访问 roots.workspace。
```

- [x] **4 GREEN 与回归。**运行上述测试及 `tests/evaluation/test_baseline_runtime.py`；再运行 `tests/agent/test_runner_governance.py tests/agent/test_memory_store.py tests/agent/test_dream.py tests/agent/test_runner_goal_continue.py`，显式隔离配置路径。记录当前已知缺陷，不把缺陷断言写成新功能必须保留。
- [x] **5 审查记录。**确认无默认用户配置/网络/生产文件访问；基线报告包含失败与通过，不写未经实测的压缩收益。本任务不触发功能开关，证据回填本文件，不自动提交。
