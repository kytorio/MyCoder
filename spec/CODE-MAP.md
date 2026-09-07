# 实际代码定位与复用裁决

2026-09-06 按 H01 和评测先行修订，核对 MyCoder 基线及保留的注释变更。行号仅为本次定位，实施时按符号搜索；所有“新增”路径尚未实现。旧归档中的 lifecycle/write_gateway 不属于可复用生产能力。

## 上下文

| 现有位置 | 复用 | 必要修改 / 不可直接照搬 | 任务 |
| --- | --- | --- | --- |
| `nanobot/agent/context.py:75` TranscriptInput；`:91` ContextBuilder；`:105` build_system_prompt；`:280` build_transcript | bootstrap、Skill、memory、summary 的原渲染逻辑 | 拆为可估算来源与延迟 render；保留 observe；MemoryStore 显式注入；实际 agent/project 根区分 | N03、N05、N08 |
| `nanobot/agent/context_governance.py:110` Config；`:123` CompactionState；`:191` ModelRequestState；`:562` prepare_request | 单一请求治理入口、预算检查、H/delta 结构 | 添加 plan/manifest/store；新层次策略；不能把持久化历史天然当已消费；保存与接受分离 | N05–N09 |
| 同文件 `:656` normalize_tool_result；`:853` apply_tool_result_budget；`:874` snip_history | 结果规范化、合法历史区间算法 | 单条+整批 token 预算；L3 版本去重；原始数据不可变 | N06、N07 |
| `nanobot/utils/helpers.py:545` _write_text_atomic；`:569` maybe_persist_tool_result | 原子文件写方法、preview 形式 | 旧 helper 在 workspace 写结果并按 TTL/桶数删除旧目录，且同 call_id 不覆盖已有内容；不能作为新有引用 artifact 的可靠存储直接复用 | N06 |
| 同文件 `:445` recent_message_start_index、`:471` find_legal_message_start、`:825` estimate_prompt_tokens_chain | 完整组边界、现有模型估算和 schema 计数 | 保证 schema 只算一次；图片成本未知不为 0 | N05、N07、N08 |
| `nanobot/agent/runner.py:493` accept_request；`:1300` _request_no_tools | runner 控制流、工具批次和最终化 | 普通/错误/重试/no-tools/最终化统一提供真实 outcome；不能以存在 response 判断 accepted | N07、N09 |
| `nanobot/agent/loop.py` _build_turn、_save_turn、_insert_summary_checkpoint、process_direct | 原有会话保存与安全接纳点 | 仅接入新记忆快照/来源与 root；checkpoint 失败不更新生效边界；保留原注释 | N03、N05、N09 |
| `nanobot/providers/conversation_state.py`；`nanobot/session/summary.py`；`nanobot/agent/memory.py:1042` summarize_transcript | provider continuation、摘要 checkpoint、已有 Consolidator | 结构化摘要与恢复验证，不创建第二个压缩服务 | N09 |
| `nanobot/agent/skills.py`；`nanobot/agent/tools/registry.py:86` get_definitions；`nanobot/agent/tools/mcp.py:595` MCPToolWrapper | Skill 加载、全 registry、MCP schema 规范化 | 发送 schema 选择与执行 registry 分离；MCP 连接和安全校验不因按需 schema 改写 | N08 |

新增：`nanobot/agent/context_plan.py`（来源/计划/清单）；`context_artifacts.py`（可恢复引用）；`context_sources.py`（图片/Skill/schema/memory 选择）；`tools/context_artifact.py`（有界回读）；`tools/tool_discovery.py`（按需 schema 发现）。模块归 Governor 调度，非独立 runtime。

## 记忆与 Dream

| 现有位置 | 复用 | 必要修改 / 风险 | 任务 |
| --- | --- | --- | --- |
| `nanobot/agent/memory.py:72` 构造；`:232/:240/:248` write_* | canonical 路径、legacy 内容读取 | write_text 改受控提交；原 append_lock 是单实例历史锁，不够共享规范写 | N02 |
| 同文件 `:471` _write_entries；`:498` cursor；`:543` build_dream_prompt | 原子历史重写方法、增量切片、cursor | 即时提交不碰 cursor；Dream cursor 原子保存，不复用 append_history 充当事务 | N02、N04 |
| 同文件 `:575` build_dream_tools | 原工具 registry 和精确文件 allowlist、Skill 写入 | 原 write/edit/apply_patch 直接写 canonical，需 owned-file 适配器；多目标预检 | N04 |
| `nanobot/cli/gateway_runtime.py:561` on_cron_job；`nanobot/command/builtin.py` cmd_dream、cmd_dream_restore | 周期和手动启动、停止判断、Git diff | 两个 Dream 入口分别窄改并同样测试；不强制抽 cycle；restore 先预检不能先落盘 | N04 |
| `nanobot/utils/gitstore.py` GitStore.revert | 历史版本查询和备份能力 | 普通 revert 直接写文件；规范记忆恢复调用方改为候选→受控提交，不重构整个 GitStore | N04 |
| `nanobot/sdk/clients.py:155` MemoryClient、`:32` ingest、`:101` restore | 同步 MemoryClient.write、会话导入能力 | 同步成功返回 None；保护冲突为明确异常；导入/恢复不是用户新意图 | N02–N04 |
| `nanobot/agent/loop.py` run、_dispatch_command、_build_turn、pending 输入与 process_direct | 正式入站/排队/执行路径 | 保留原输入/pending 流程；仅向工具构造注入 MemoryStore；提交后刷新归 Governor | N03 |
| `nanobot/skills/memory/SKILL.md`；`nanobot/templates/agent/dream.md` | 分类与历史搜索指导、Dream 整理流程 | 改“仅 Dream 能写”的旧描述，明确显式提交与推断保护，保留 Skill 能力 | N03、N04 |

新增仅 `nanobot/agent/memory_writes.py`（小型契约/共享协调器）和 `nanobot/agent/tools/memory_save.py`（LLM保存工具）。复用 `nanobot/agent/tools/base.py` 的 Tool/ToolResult、`loader.py` 自动发现、`registry.py` 参数校验/执行、`context.py` RequestContext、`execution.py` 工具执行/checkpoint；ToolContext 增加 typed MemoryStore 注入。Dream 适配器放已有 build_dream_tools 所属模块，不预设四个新服务文件。

## 配置、评测及不动边界

| 位置 | 处理方式 | 任务 |
| --- | --- | --- |
| `nanobot/config/schema.py` AgentsConfig、ToolsConfig、DreamConfig、Config.runtime_data_dir；`nanobot/config/loader.py` | 新增 agents.context / tools.memory，复用工具 config lazy refs，camelCase/环境变量/安全保存回归；Dream 两小时间隔保持 | N03、N05、N11 |
| `nanobot/nanobot.py:93` from_config；`nanobot/sdk/runtime.py`；`nanobot/agent/runner.py` AgentRunSpec | 注入参数与存储根；直接 SDK 缺根时显式失败 | N03、N05 |
| `nanobot/utils/llm_runtime.py` LLMRuntime；`nanobot/providers/base.py` provider/usage | 没有记忆分类模型；可选最小 outcome/usage 证据补充 | N07、N10 |
| `nanobot/nanobot.py:144` run；`tests/agent/runner_helpers.py`；`tests/test_nanobot_facade.py` | 真实链路和 scripted provider 的复用基础 | N00、N01 |
| `nanobot/llm_usage/`；现有 AgentHook/RuntimeEventBus | 使用现有观察/usage 捕获，summary/dream 与 worker 分开记用量，memory_save 无独立模型费；观察错误不作为事务失败 | N05、N10 |
| `nanobot/utils/evaluator.py` | 现有 heartbeat 通知判断，不是任务评测；不改为 Goal 或基准评测器 | 不实施 |
| `nanobot/security/workspace_policy.py`、workspace_access.py、工具 filesystem/shell/mcp | 保持现有守卫；新记忆入口继承当前可用拒绝条件，不假定旧撤销的只读配置已存在 | N02、N10 |
| `nanobot/session/goal_state.py`、`nanobot/agent/tools/long_task.py`、现有 Goal/Stop | 不改；仅兼容回归 | 不实施 |
| `webui/`、锁文件、第三方项目/原 nanobot checkout | 不新增面板或依赖变更，不作为实现位置 | 不实施 |

新增评测路径见 S03；测试文件的精确新增/修改范围见各 N 任务。不在表中的生产文件若确有调用链需要，先在任务变更记录解释原因；扩大到 Goal/§2/§5 必须重新取得用户授权。

## 评测先行定位

N01A 新增 `nanobot/evaluation/scenarios.py` 与 `metrics.py`，扩展 N01 模块；先建 `tests/evaluation/fixtures/context_matrix.json`、`memory_dependency.json` 和旧行为报告。复用 MemoryStore.read/write_* 在临时根准备记忆视图，复用 Nanobot.run/工具 trace，不复制 runtime，不依赖后置 memory_save。pico 精确位置、矩阵和假模型局限见 [EVALUATION-CASES](EVALUATION-CASES.md)。
