# S01 ContextPlan 与四层上下文压缩

> 2026-09-06 修订；来源：[技术方案第三节](https://kcnilb4v5r43.feishu.cn/wiki/RDSBwIsZIiFvkxk2vFtcwkW0nde#doxcnnhJ2YjKOMkg2od7BMGT6je)，revision 70。任务 N05–N09，依赖 N01A 评测基线；先复用现有 MemoryStore，后续 N03 接入工具提交版本与本轮刷新，不反向依赖尚未实现的记忆模块。

## 所有权与现有基础

ContextBuilder 收集带来源的候选；ContextGovernor 规划、裁剪并决定最终请求；AgentRunner 仅调用该治理入口。复用 ContextCompactionState 的 H/delta、Consolidator、SessionSummaryCheckpoint、ProviderConversationStateController。不能在 loop 再创建第二条摘要链路。

目前 build_system_prompt 先拼接全文；prepare_request 已有预算、snip 和摘要，但缺少完整来源及选择理由。Runner 普通响应后无条件 accept_request，错误响应也可能推进 H；no-tools/finalization 路径必须一起核对。现有方法名不等于已满足新语义。

## 契约

新增 `agent/context_plan.py`，定义不可变 ContextSource、ContextDecision、ContextPlan、ContextManifest。正文/renderer 不进入日志。

| 类型 | 必需字段 |
| --- | --- |
| ContextSource | source_id、kind、revision/content_hash、estimated_tokens、estimate_method、priority、required、allowed_strategies、render_key |
| ContextDecision | source_id、action（keep/shorten/defer/reference）、stage（selection/L1/L2/L3/L4）、reason_code、before_tokens、after_tokens、artifact_ref |
| ContextPlan | schema_version、plan_id、request_id、model、input_budget、sources、decisions、predicted_total、rendered_estimate |
| ContextManifest | plan 的脱敏投影、实际发送/拒绝状态、actual_input_tokens（可空）、估算偏差、耗时 |

`ContextBuilder.collect_sources(transcript: TranscriptInput) -> list[ContextSource]` 在整篇 render 前收集来源；从既有 MemoryStore 读取候选；N03 接入后自动使用其一致快照，不改变该签名。允许为估算读文本/缓存 hash，不允许先拼大 prompt 再声称 pre-plan。`ContextPlanner.plan(sources, input_budget) -> ContextPlan` 决策后，由 `ContextBuilder.render_plan(plan) -> RenderedContext` 渲染入选项；RenderedContext 含 messages 和 tool_definitions。observe 模式仍使用旧 renderer，计划仅旁路记录。

kind 至少区分 system_policy、user_current、history、tool_result、image、skill_full、skill_index、mcp_schema、memory、runtime、summary。SOUL/USER/MEMORY 归 memory 子类别，系统硬约束仍 required；相同内容不得因多个分类重复收费。

sources 的 source_id 唯一；同一来源跨多层的 decisions 是变换链，predicted_total 只累计最终表示，不把各层 before/after 相加。拆出当前显式条目后，其原 USER/SOUL/MEMORY 区块不再重复渲染该条目；维护原文件区段与版本映射。必要的消息封套作为独立开销计一次。

## 预算与保护

```text
input_budget = context_window - max_output_tokens - safety_margin
estimated_messages + estimated_tool_schemas + envelope_tokens <= input_budget
```

输出和工具 schema 各只扣一次；窗口/图片成本未知不能按 0 处理。无可验证输入预算时 observe 报 unknown，enforce 明确配置错误，不能猜无限窗口。建议 safety margin=max(1024, window×5%)，正预算校验失败不发请求。

| 来源 | 策略 | 硬边界 |
| --- | --- | --- |
| 系统约束、当前请求、当轮新提交显式偏好 | required，优先级 100 | 保留原义；用户最新纠正不能被旧 memory 或旧摘要覆盖 |
| 当前批次/未发送 delta | required，95 | call/result 配对和有意义的首读内容保留；大结果可经 L1 保存后缩为 preview+ref，不能直接抹掉 |
| 图片 | 当前必需图片保留；旧图可引用化 | 按模型能力/尺寸/数量估算，不把 base64 字符数当 token；不支持时明确失败 |
| Skill | 显式/活跃 Skill 全文及约束保护；其他保留索引/加载入口 | 不截断硬约束，不因仅目录化就宣称原技能已被读取 |
| MCP schema | 默认全量兼容；显式开按需发现后只计实际发送部分 | 完整 schema 不截参数/enum；执行 registry 不注销隐藏工具；发现后下一请求再加载 |
| memory | 按本实例/当前项目相关性选择；来源与版本可追溯 | 不删除规范文件；当前新偏好 required，历史条目可不注入/摘要/引用 |
| 旧 history/tool result | 完整 turn/工具原子组裁剪 | 不改 raw transcript；保留可回取原文 |

已有 Goal 内容按其原本任务约束保留，不引入 Goal 判定、生成、独立模型或新状态。

## L1–L4 执行顺序

1. **L1 工具结果预算。**复用 normalize_tool_result/apply_tool_result_budget，增加整批结果总预算。`ToolResultArtifactStore.put(session_key, call_id, content) -> ArtifactRef` 原子保存完整原文并核验 hash 后才返回 ref；`read(ref, scope, max_bytes) -> str` 做会话/范围校验，超读取限额明确失败；模型工具通过 `read_page(ref, scope, *, offset_chars=0, max_chars=16384) -> ArtifactPage` 分页回读，page 含 text、next_offset_chars、content_hash，能完整重组原文。失败保留原文，仍超限明确报错。最新结果 preview 不是空占位。
2. **L2 历史剪裁。**复用 snip_history/_legal_history_tail/find_legal_message_start，按完整 turn/并行工具组剪中段，归档区间并记原因；同一原文区间重复治理不得反复归档。系统、当前输入和 delta 不进入可剪区间。
3. **L3 旧结果缩短。**只缩已经出现在确认被模型消费的请求中的旧结果，不缩当前批次首读；相同路径必须同时同内容版本才去重。未知消费状态按未消费保护，可经 L1 保存引用化，不能当旧已读结果删除。
4. **L4 语义摘要。**前三层不足、手动请求或模型明确溢出时调用同一入口。结构化摘要保留当前任务/验收要求、约束、显式偏好、决策、文件变更、错误、证据引用和剩余工作；只摘要 accepted H，delta 原样保留。

建议新 enforce 高水位 0.85、目标 0.65，基于 input_budget；不替换 observe 的既有行为。手动 compact 等工具批次保存点，不在执行副作用中途截断；溢出按 request lineage 最多一次 L4 重试，换 request_id 不重置额度。

## 接受与持久化边界

新增窄类型 `ContextRequestOutcome(request_id, lineage_id, attempt, status)`，status 为 accepted/rejected/unknown，归 context 模块所有，不恢复旧生命周期平台。Runner 的正常、no-tools、finalization 及相关 adapter 内部回退须提供真实依据；LLMResponse 代表返回对象，不天然代表 accepted。异常被转换为错误响应、超时或断流不可推进 H。

现有 from_transcript 会把全部旧 history 当作 accepted；导入、恢复与旧版本缺少消费证据的部分需保守标 unknown，不能仅因保存在会话文件就可做 L3/L4。下一次真实成功请求后再推进。手动压缩缺少可摘要 H 时返回无操作原因，而不是构造消费凭据。

N07-R1：同进程跨轮次复用 Session 中非持久化的消费快照，由原 RunSpec/RunResult
和 Loop 调用点传递。快照需匹配原始历史前缀与接受的派生表示，不将 preview/ref
还原成“原文全文已消费”的证据；clear 后失效。进程重启/导入不恢复此内存证据，
不新增会话持久化协议。此窄接线不改变 Goal 或观察模式的既有渲染。

N07-R2 审查澄清：原生续聊的私有状态可能已压缩，终止事件只证明实际 wire
请求结束，不能证明逻辑 messages 中所有原文都被消费。当前接口没有私有状态到
逻辑全文的可验证映射，带该状态的叶子请求保守记 unknown，不推进全文 H；
已有明确绑定的最终回退叶子凭据仍按原请求校验。此限制不修改原生续聊、
压缩或 sidecar 格式，也不清除此前已验证的 H。未来若放宽须提供表示对应证据。

checkpoint 原子保存失败时保留旧 summary 和边界。成功提交后更新摘要替代范围，并按现有 controller 重建 provider continuation。保存 checkpoint 不代表新请求已发送或已接受；两种事实不能共用一个标志。恢复不重复摘要 delta、不重复执行工具。

## Artifact 与审计

artifact 位于显式 Config.runtime_data_dir 下的隔离存储，通过 from_config → Loop/RunSpec → Governor 注入；独立 SDK 启用此能力时须传 root/store，无配置根不偷用业务 workspace。引用只授予读取该内容的能力，不放宽文件工具的目录范围。

首版不按 TTL 自动删除仍被会话/checkpoint 引用的 artifact。可配置容量上限，空间不足失败；以后 GC 只能删除已证明无引用的条目。日志每次请求及超限拒绝均输出清单，reason 包含 kept_required、within_budget、offloaded、snipped、micro_compacted、summarized、schema_deferred、memory_not_selected、image_reference、archive_failed、irreducible_floor。actual usage 只用于事后校准，不倒改当时决策。

新增 `AgentsConfig.context`（外部 agents.context）：mode=observe/enforce、enabledLayers、highWatermark、targetRatio、safetyMarginTokens、toolResultTokenBudget、toolBatchTokenBudget、artifactMaxBytes、schemaDiscovery。默认 observe，任何新增裁剪须显式开启。单条/批次限额必须为正并受最终总预算约束；所有建议默认在 N05/N06 测试冻结，不能作为已测性能值。

## 验收

observe payload 逐字段等价；所有来源恰计一次；enforce 请求拟合或明确拒绝；首读/当前偏好/delta 不丢；call/result 始终合法；归档可回取且不能越权；失败和恢复不推进错误边界。语义保真和任务成功率由 S03 测量，不以脚本通过宣称压缩质量提升。

## 与后置记忆工具的接入边界

N05/N08 先用原 USER/SOUL/MEMORY 文本与 hash，不创建或依赖新 MemorySnapshot 模块。N03 实现 memory_save 后，利用原工具结果进入 delta；在同一 prepare_request 检查 memory revision 更新来源，保护 committed entry，不能提前把尚未调用工具的请求标成已保存。首个模型请求可能尚无新记忆，这是 H01 的明确语义。上下文阶段的 synthetic fixture 可测试 required memory source 策略，端到端提交刷新留给 N03/N10。
