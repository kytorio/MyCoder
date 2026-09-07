# S03 任务级评测与回归

> 2026-09-06 修订，评测先行；来源：[技术方案第六节](https://kcnilb4v5r43.feishu.cn/wiki/RDSBwIsZIiFvkxk2vFtcwkW0nde#doxcnJman3W1AHe9hW9dK9rcd6f)，revision 70。任务 N00/N01/N01A 先交付评测和两类基线；N10/N11 后续补组合/发布验收。

> 资源执行约束（2026-09-06）：实验矩阵按大模块集中执行，不随每个小改动重复运行。S01 的 N07/N08/N09 全部实现并审查后运行一次完整上下文实验；S02 的 N02/N03/N04 全部实现并审查后运行记忆及组合实验。开发期间仍保留最小的定向 RED/GREEN 测试和静态检查，它们不替代模块实验。

## 边界

复用 Nanobot.run、真实 AgentLoop/Runner/Governor、MemoryStore、工具 registry 和已有 pytest 辅助 provider；只替换外部模型响应、时钟与测试外部系统。不得另写“模拟 AgentLoop”来证明真实 runtime 正确。现有 utils/evaluator.py 是 heartbeat 通知判断，不是 benchmark，也不是新 Goal 判断器，不改它承担本任务。

首版为离线可重复的轻量 Python 模块/CLI，不新建服务、UI 或数据平台。脚本模型验证控制流和不变量，不证明自然语言压缩质量；真实模型任务验证后才可以报告语义收益。本次具体核对本地 pico 的 BenchmarkEvaluator、context stress matrix 和 memory dependency experiment，参考位置与局限见 [EVALUATION-CASES](EVALUATION-CASES.md)，不照搬其收益数字。

## 文件与契约

新增 `nanobot/evaluation/{__init__,__main__,models,runner,providers,verifiers,metrics,scenarios}.py`，fixture 放 `tests/evaluation/fixtures/`，测试放 `tests/evaluation/`。不修改生产 session 来运行案例。

`EvalCase` 使用带 schema_version 的 JSON：id、description、initial_messages、workspace_files（相对路径文本）、user_inputs、scripted_responses、allowed_tools、budget（max_requests/max_steps/max_wall_seconds/max_tokens）、verifiers、tags，及可选 scenario/parameters/memory_variant/requires_capabilities/setup_kind（N01A）。拒绝绝对路径、..、symlink 逃逸和非白名单 verifier；不从 fixture 执行任意 Python/shell。媒体由测试本地合成，禁止远程下载。

`EvalRunner.run_case(case: EvalCase, *, mode: str, output_root: Path) -> Awaitable[EvalResult]` 每次在唯一子目录构造 config/runtime/workspace/session，禁用生产 cron/channels/外部 MCP、默认拒绝网络。可注入 ScriptedProvider；未匹配响应和脚本耗尽直接失败，不回退真实 API。固定 seed/虚拟时钟控制可确定部分，时延性能单独记录实时时钟。

`EvalResult`：schema_version、case_id、mode、status（passed/failed/error/budget_exceeded/not_implemented）、verdicts、metrics、trace_ref、provenance。`VerifierResult`：name、status（passed/failed/not_applicable）、evidence_refs、reason。result 不以模型“已完成”字符串作为任务成功依据。not_implemented 用于当前阶段缺少所需 feature 的专项案例，须单列，不算通过；全功能最终验收不可借此跳过必需能力。

复用正式 hook/usage 观察入口记录，不改变生产失败语义。trace 包含请求、实际发送的 source 清单、tool call/result 配对、commit receipt、Dream cursor、验证结果；完整合成数据可以放隔离 trace，默认导出仅白名单字段。真实模型 trace 默认不导出正文/凭据。

## 比较模式

| mode | 含义 |
| --- | --- |
| baseline | 同一代码内关闭 memory_save 与新 enforce，保留原有治理；不是完全不压缩 |
| observe | 新计划记录，但 payload 与 baseline 逐字段相同 |
| context-only | 新四层启用，memory_save 关闭 |
| memory-only | 新记忆与 Dream 协作启用，上下文 observe |
| full | 两模块同时启用 |
| no-l1 / no-l2 / no-l3 / no-l4 | full 基础逐层消融，仍保留最终超限拒绝与保护规则 |

所有模式独立复制相同初始数据，不共享上次运行的 memory/cursor/artifact。既有缺陷可以标明 baseline 失败，但不能作为新行为可忽略的理由。用相同 fixture/hash 比较，同步报告模型/preset/参数变更。

## 覆盖矩阵与可执行判定

| 场景 | 首要 verifier / 不变量 | 接入任务 |
| --- | --- | --- |
| 长对话早期约束、文件变更、末轮任务 | 目标文件内容/结构或有限白名单测试通过，证据与约束保留 | N01A、N09、N10 |
| 巨型结果、并行结果、首读 | hash 回读、批次预算、每个 call 对应 result，原始会话未改 | N06、N07 |
| 图片、Skill 全文、MCP schema、项目 memory | 来源恰计一次，必要来源存在，未知成本不记零，延迟工具可发现/调用 | N05、N08 |
| 显式“以后中文简短回答” | 主模型先调用 memory_save；canonical、receipt、提交后下一 request 同时有事实；重启一致 | N03 |
| 引用、否定、临时要求、转述、重投与纠正 | 非授权条目数为零；相同 ID 不新增；纠正旧 entry_id | N02、N03 |
| Dream 旧快照与新偏好交错 | 新偏好未被覆盖；即时不动 cursor；失败不提前消费历史 | N04 |
| SDK 全文写、restore、同批 patch | 受保护条目冲突返回且文件未部分改写 | N04 |
| 超限、断流、异常转响应、checkpoint 失败 | accepted H 不误推进；恢复不重复工具副作用 | N07、N09 |
| 当前已有只读/目录/Exec/MCP 限制 | 真正守卫拒绝、越界副作用计数为零 | N10 |
| 新通用工具 HITL | H05 下 baseline/full 均 not_applicable，并解释尚未实现；不伪造通过 | N10 |

## 指标定义

task_success_rate=passed executable tasks / eligible tasks；错误和超预算计失败，not_applicable 排除并列出数量。constraint_retention 只对有明确标签的约束计数；LLM 辅助评价单列，不覆盖可执行失败。

prompt/completion/cached tokens 按 provider 实际 usage 记录，不可用为 null；estimates 单列。purpose 为 worker/summary/dream；memory_save 本身无 LLM 调用，决定调用的模型成本已计 worker，工具保存延迟单列，失败调用已报告的消耗也计入，不能只算主模型。每个底层请求有 request_id 防止 hook 重复计数。live 成本仅按用户提供/确认的费率快照计算，未知为 null。

保存延迟：memory_save 执行开始→committed，用户体验延迟：user admission→模型确认（仅确有 committed 的样本），主模型决策耗时另计，分别记样本数和 p50/p95；失败单列而不是按 0ms。误写率=不应写的输入中发生非授权 commit 的条数/负例输入数；重复新增数=同一 operation_id 新增的额外条目；stale_dream_overwrite_count=旧 Dream 改变新显式内容的次数；safety_bypass_count=禁止的副作用实际发生次数。压缩比、各层触发数、丢弃理由、回读次数、summary 消耗、墙钟时间和 budget_exceeded 同时报告，不能只报告节省 token。

provenance 必须含 git commit、dirty diff hash（不导出正文）、case/config hash、模型/参数、运行版本、seed、开始时间、模式和依赖版本。固定案例之外的质量结论注明样本局限。

## 执行与门禁

CLI：`.venv/Scripts/python.exe -m nanobot.evaluation run --suite tests/evaluation/fixtures --mode baseline --output-root <新建隔离目录>`；比较命令 `compare --baseline <result.json> --candidate <result.json>` 校验 fixture 兼容后输出 JSON/Markdown。离线默认；真实 API 必须另加 `--live --config <显式测试配置> --max-requests N --max-tokens N --max-wall-seconds N`，缺失正数上限拒绝，不读取默认生产配置，也不允许 fallback model。三个 CLI 上限按整次 suite 累计，每个案例的 fixture 预算作为第二层限制。每个案例默认离线上限 20 requests、50 steps、60秒、100000估算 token；provider 实际消耗可能事后才知，发送前按估算输入加本次 max_output 预留。

发布硬门禁为本规格覆盖到的不变量：误写、重复新增、旧 Dream 覆盖、越界副作用在对应固定测试集均为零；必需字段完整、回读完整、工具配对合法、observe payload 不变。它们不是开放世界零风险承诺。每项必须有案例，不能空集通过。任务成功率/压缩收益/延迟先建立基线和差值报告，不沿用旧固定案例数或百分比承诺；质量回退须人工解释并决定能否启用。

2026-09-06 已完成 N00/N01/N01A 离线实施；2026-09-07 增加复用生产 provider factory 的 bounded live 路径和真实 usage 记录，证据见 IMPLEMENTATION.md。实现与自动测试均未进行付费调用；回退时通过的 128 项测试不是以上评测结果。

## N01A 前置的两类任务和指标

必须在生产模块修改前完成 [长上下文矩阵与记忆依赖任务](EVALUATION-CASES.md)：12 组上下文压力配置、3 类共 12 个记忆依赖参数实例，均可配置但纳入 fixture hash。记忆 memory_on/off/irrelevant 为独立数据维度，旧 MemoryStore 预置 fixture 即可测检索；保存功能专项以后真实调用工具，不把预置数据算自动保存成功。

N01A 已完成前置 metrics/compare、两阶段隔离与 follow-up 事实源重复读统计；N10 只扩展新功能组合。正确率必须与 repeated_fact_read_count/correct_without_fact_reread_rate 并报，必要版本重读和编辑前目标读取不算浪费。模型 eligible_save_requests/missed_save_requests/false_memory_confirmation_count 由含 ground truth 的保存案例评测，脚本只能验证计数机制。
