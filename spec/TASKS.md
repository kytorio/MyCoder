# 评测先行的上下文与记忆 Implementation Plan

> **For agentic workers:** H01–H05 已按用户答复记录。用户已授权按本方案修改代码；逐任务验证，证据见各任务与 IMPLEMENTATION.md。不得自动 commit/push。

**Goal:** 在 MyCoder 先交付可重复评测，再以最小改动更新上下文压缩及 LLM 记忆工具。

**Architecture:** N00/N01/N01A 先建立真实 runtime 的评测系统、指标、两类任务及旧行为基线。然后改现有 Governor，再通过普通 memory_save 工具扩展 MemoryStore，最后组合验证；不再建设输入识别服务。

**Tech Stack:** Python 3.11+、asyncio、Pydantic 2、pytest，复用现有依赖。

**Spec:** [S00](S00-development-boundary.md)、[S01](S01-context-compression.md)、[S02](S02-explicit-memory.md)、[S03](S03-evaluation.md)、[pico 参考与案例](EVALUATION-CASES.md)；先读 [REVIEW](REVIEW.md) 和 [CODE-MAP](CODE-MAP.md)。

## Global Constraints

- 唯一项目 E:\Code\Agent\MyCoder；分支 codex/agent-governance；main 基线 455533169d5a641300dd63d260b1ff5543c4093c。不重置、不新建 worktree、不自动提交或推送。
- 用户 H01 为主模型调用工具，H02–H05 已确认；旧 T 完成状态不继承，所有 N 任务重新验收。
- 最小变更：先复用 ToolLoader/Registry/RequestContext/execute_tool_calls、MemoryStore、ContextGovernor；不重写 loop/runner，不创建输入分类器/额外模型。
- 保留用户既有 diff，不更新依赖/lockfile，不运行 ruff format，不读生产配置/会话，不执行 live 模型评测。
- 每任务 RED→最小实现→GREEN→审查。代码样例为待实现接口，不是当前测试结果；每次选一个场景完成五步，证据写回任务文档。
- 资源门禁（2026-09-06 用户调整）：任务内只运行证明 RED/GREEN 所需的定向单元、回归与静态检查；不为每个小改重复运行 51 例全套或 12 例上下文实验矩阵。N07/N08/N09 完成并关闭 S01 四层管道后集中运行一次 S01 实验；N02/N03/N04 完成并关闭 S02 后，再由 N10 运行记忆及组合/消融实验。审查不能用“尚未跑集中实验”否定已有定向测试证明的局部实现，但模块完成仍须通过对应集中实验。
- N00/N01/N01A 完成前，禁止改生产记忆或上下文来“帮助评测跑通”。新 feature 可声明 not_implemented，不能 mock 成已通过。新工具/压缩上线仍依赖后续各自验收。

## 强制执行顺序

任务编号是稳定标识，不代表数字排序；N01A 是新增的评测前置任务。

| 顺序 | 任务 | 依赖 | 可独立验收结果 | 状态 |
| --- | --- | --- | --- | --- |
| 1 | [N00 隔离基线](tasks/N00-baseline.md) | 无 | 正式入口特征测试、隔离根、当前版本证据 | 已完成：8 新测试 + 145 回归，独立审查通过 |
| 2 | [N01 EvalRunner](tasks/N01-eval-harness.md) | N00 | 案例执行/可执行判定/trace/CLI | 已完成，独立审查通过 |
| 3 | [N01A 两类任务与指标](tasks/N01A-eval-scenarios.md) | N01 | 长上下文和记忆依赖任务、指标/比较报告、旧行为基线 | 已完成：172 回归通过，51 例基线，审查通过 |
| 4 | [N05 ContextPlan observe](tasks/N05-context-plan.md) | N01A | 来源账本、清单、原 payload 兼容 | 已完成：240 回归，12 例兼容矩阵，复审通过 |
| 5 | [N06 L1 工具结果](tasks/N06-artifact-budget.md) | N05 | 单条/整批预算、可回读原文 | 已完成：归档/重启/预算验收，定向复审通过 |
| 6 | [N07 H/delta 与 L2/L3](tasks/N07-history-layers.md) | N06 | 消费证据、合法剪裁和版本去重 | 已完成：消费凭据、L2/L3、跨轮证据及复审问题均关闭 |
| 7 | [N08 来源策略](tasks/N08-source-policies.md) | N05、N06 | 图片/Skill/MCP/既有 memory 的策略 | 已完成：23 聚焦 + 141 相邻回归，静态检查通过 |
| 8 | [N09 L4 与恢复](tasks/N09-summary-recovery.md) | N07、N08 | 结构化摘要、统一手动/溢出入口 | 已完成；S01 离线矩阵 12/12，控制器审查无阻断项 |
| 9 | [N02 记忆受控提交](tasks/N02-memory-commit.md) | N01A、N09 | 规范文件幂等/原子提交/恢复 | 已完成：21 新测试 + 108 相邻/SDK 回归，静态检查通过 |
| 10 | [N03 memory_save 工具](tasks/N03-remember-input.md) | N02、N09 | 主模型工具调用→提交→下一请求生效 | 已完成：39 项聚合验收，真实 A→B 会话评测通过 |
| 11 | [N04 Dream 协作](tasks/N04-dream-coordination.md) | N02、N03 | 两 Dream 入口和 SDK/restore 保护 | 已完成：11 focused + 142 聚合回归，静态检查通过；审查超时后控制器收口 |
| 12 | [N10 组合评测](tasks/N10-evaluation-gates.md) | N01A、N04、N09 | 保存/压缩组合反例、消融与质量报告 | 已完成：full 59/59；逐层消融均有非空反例；124 + 556 回归通过 |
| 13 | [N11 交付门禁](tasks/N11-rollout.md) | N10 | 配置/SDK说明、兼容性与启用证据 | 已完成：默认关闭；251 项集中/定向通过、1 skip；停在人工启用门禁 |

先上下文后记忆是本次采用的顺序；共同前提是评测系统先可用。N05/N08 不导入尚不存在的新 memory 类型，使用旧文件/hash；N03 再接入 committed entry/revision 和当前回合刷新。N10 不承担基础 harness、指标或两类任务的首次实现。

## 最小实现面与配置

| 项目 | 方案/默认 | 负责 |
| --- | --- | --- |
| 评测 | 复用正式 runtime；scenario/metrics 仅组织测试；不用第二个 AgentLoop | N01/N01A |
| 记忆工具 | 新 tools/memory_save.py；复用 Tool/Loader/Registry；ToolContext 增加 MemoryStore 引用 | N03 |
| 存储 | 新 memory_writes.py 放小型契约和协调器；复用 MemoryStore 与原子写方法；不预设其他四个新 memory 服务文件 | N02 |
| Dream | 原两入口分别窄改，注册点包装 owned-file 写，不强制抽 dream_cycle | N04 |
| agents.context.mode / enabledLayers | observe / [L1,L2,L3,L4]；observe 不激活新裁剪，禁重复/未知层 | N05 |
| highWatermark / targetRatio | 0.85 / 0.65；0 < target < high < 1 | N05 |
| safetyMarginTokens | null 为 max(1024, ceil(window×0.05))，显式为正整数 | N05 |
| toolResultTokenBudget / toolBatchTokenBudget | 2048 / 8192，single ≤ batch，仍服从总输入预算 | N06 |
| artifactMaxBytes / schemaDiscovery | 134217728 / false；不删除仍引用 artifact | N06/N08 |
| tools.memory.enabled | false；关闭时不注册 memory_save | N03 |
| tools.memory.transactionMaxBytes / journalMaxBytes | 1048576 / 67108864，正数且后者 ≥ 前者 | N02/N03 |
| agents.defaults.dream.intervalH | 保留 2 及原 cron/modelOverride | N04 |

取消 agents.explicitMemory 和 intentMode/intentTimeoutSeconds/intentInputTokenLimit/intentOutputTokenLimit；无 remember 分类模型。关闭工具不会关闭既有显式保护和 journal 恢复。根由 Config.runtime_data_dir 注入，不用业务 workspace 偷作 artifact/receipt fallback。

所有建议参数是配置初值，非已测收益；未知 usage 为 null，enforce 缺可靠预算/根时明确拒绝。baseline 使用原治理，新工具专项缺功能标明未实现。

## 验证约定

所有命令从 MyCoder 运行，例如：

```powershell
.venv/Scripts/python.exe -m pytest tests/evaluation/test_isolation.py -q -p no:cacheprovider
```

N00 fixture 为各测试注入独立 config/runtime/workspace 并恢复配置选择；旧测试需固定 config root 时沿用 ROLLBACK 的 set_config_path 隔离方式。不得以 uv sync 随便更新环境。

每任务 focused + 指定兼容回归通过，新增失败路径有有效断言，diff 未越界才能记录完成。N01A 必须保存旧行为 baseline 和机制局限；N10 对同 fixture/hash 比较，真实 LLM 质量另行授权。文档修订和既有 128 项回退测试均不算 N 任务完成。
