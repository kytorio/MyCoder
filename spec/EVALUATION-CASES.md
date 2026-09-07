# 评测先行：pico 参考与固定任务设计

## 参考代码与取舍

本次读取本地 `E:\Code\Agent\pico`，origin 为 [htxoffical/pico](https://gitee.com/htxoffical/pico)，commit `473de5dd05512efff651ecfa15a278f9a22614c8`，读取时工作区无内容修改。不是名称相近的 PicoClaw。只作参考，不修改、运行或导入其项目。

| 已核对的 pico 位置 | 实际做法 | MyCoder 采用 / 不照搬 |
| --- | --- | --- |
| `pico/evaluation/evaluator.py:376` BenchmarkEvaluator；`benchmarks/coding_tasks.json` | fresh fixture、正式 Pico runtime、allowed_tools、step_budget、verifier、report/trace、commit/fixture hash | 采用隔离、任务产物判定与可复现记录；不照搬 shell=True 任意 verifier，用内置白名单 |
| `pico/evaluation/metrics.py:438` run_context_stress_matrix | history 4/12/24 × notes 2/10 × 短/长请求，共 12 组；比较 prompt 字符量和当前请求保留 | 保留可配置矩阵；主要比较真实发送 payload 的估算/实际 token，字符数仅辅助；补任务正确性和强制触发检查 |
| 同文件 `:330` MEMORY_EXPERIMENT_TASKS、`:381` _run_memory_task_variant | 12 个任务，fact_lookup/edit_dependency/history_reference；memory_on/off/irrelevant | 保留两阶段及三对照组；新增错记忆/新会话控制，避免答案仍在 transcript |
| 同文件 `:219` _MemoryExperimentModelClient | 假模型检查 prompt 中预设事实，决定直接回答还是重读 | 只证明检索与控制流；不能作为真实 LLM 记忆质量、调用决策的证据 |
| 同文件 `:809` _followup_trace_metrics、`:852` run_real_memory_experiment、`:918` run_real_context_experiment | 真实模型补实验；记忆实验移除旧 read 内容并加噪声；从 trace 计 follow-up read | 借鉴噪声与真实任务评价；不改生产 raw history，使用新 session 或 fixture 派生视图；只将同版本事实源重读计重复，不把所有 read 都当浪费 |
| 同文件 `:1567` run_context_ablation_v2、`:1580` run_memory_ablation_v2；results 下 DATA_PROVENANCE.md | 版本化实验产物，区分脚本快照与 live 来源 | 保留来源说明，不复制 pico 的数字为 MyCoder 承诺，不把字符压缩率说成 token 节省率 |

## 前置交付门禁

N00→N01→N01A 完成后才能改生产上下文/记忆：评测 CLI、案例加载、隔离、trace、verifier、指标、baseline/比较报告均能运行。两类案例先用 MyCoder 当前 MemoryStore/Builder/Runner 建立基线。

新 feature 专项（memory_save、L1–L4 manifest 等）按 capability 标 not_implemented，不能伪造通过，也不能阻止已有基线案例运行。N10 只负责追加组合反例与运行最终消融，不允许把核心评测器/指标/两类案例推迟到 N10 才写。

## A. 长上下文压缩任务

固定生成矩阵：history_count={4,12,24}、memory_count={2,10}、request_style={short,long}，默认 12 个配置；fixture 参数可调整但须进入 hash。history_count 指消息数，保持 user/assistant/tool 合法组。

默认最终 context_window=8192、max_output=512、margin=1024，输入预算 B=6656。根据当前估算器生成不同长度的合成段落，让总候选估算分别接近 0.5B、0.9B、1.3B；记录实际测得值，不能只靠“24条”就宣称长上下文。暖身阶段用较大窗口经正式 runtime 成功消费历史，再在最终 probe 通过现有运行时设置接口收紧预算，消费证据不伪造。预装历史结构测试与已消费历史的 L3/L4 测试分别标记。

每组植入：

- 早期约束：输出 JSON 的字段名必须为 deployment_region，不得改成 region。
- 中段事实：合成部署区域 eu-test-2、输出路径 result.json，夹入无关区域/旧值干扰。
- 最新纠正：用户将副本数 2 改为 3，当前请求明确只改指定文件、保留原 version 字段。
- 最终任务：按已知约束生成/更新 result.json；verifier 校验全部字段/值及其他文件未改。答案不放在最终请求、schema 或可见 verifier 中。
- 必要时增加并行工具结果组；专门案例含超大首读结果、引用回读与拒绝下限。

必记：任务正确率、当前请求与关键约束保留率、原始/发送估算 token、provider actual usage（可能 null）、各层触发/淘汰原因、工具配对、恢复重放次数、时间。高压力 full 案例必须实际触发压缩或明确不可约失败；没有触发的样本不能当压缩质量通过。baseline 仍用原治理，不冒充“完全无压缩”。

独立控制：无关记忆替换、将关键事实从所有可用上下文移除、必需内容自身超预算、归档故障。验证器只读运行产物；LLM 不可读取 ground truth。

## B. 记忆依赖任务

借鉴 pico 的 3 类各 4 个参数实例，默认 12 个任务：

| 类别 | bootstrap（A阶段） | probe（B阶段）与 verifier |
| --- | --- | --- |
| fact_lookup | 获得四类合成事实：区域、路径前缀、预算值、超时值 | 不在问题里重复答案，查询旧事实；exact/结构化答案与源证据匹配 |
| edit_dependency | 读取/确认字段名、保留字段、目标位置、格式约束 | 在另一目标文件完成实际修改；verifier 验证文件内容而非仅口头复述约束 |
| history_reference | 确认结论及来源文件/版本、旧标记、历史选择 | 新会话回忆原结论及来源；不把当前文件新内容混成历史结论 |

A阶段结束后保存可复现快照；B阶段从新 session、同实例/项目的独立根副本启动，不能复用含答案的 A-stage transcript。噪声只加入合成测试 session。旧实现 baseline 的读取测试允许 fixture 预置 USER/SOUL/MEMORY，必须标 setup=preseeded；它不证明模型会保存。N03 完成后另外增加 setup=tool_write，A阶段必须真实走 memory_save，才可声称端到端保存与再使用。

每个任务固定三对照：memory_on（相关事实）、memory_off（记忆视图为空）、memory_irrelevant（同量无关事实）。另以过期/矛盾记忆作对抗案例。view variant 与 baseline/full feature mode 是独立维度，报告不可混淆。

B阶段源文件可有两种显式条件：可重读，测重复读与工具成本；不可用，测记忆依赖和不足时澄清，不能误猜。重复事实读只计 A阶段已读的同 path+内容 hash（适用时含相同 range），exclude 为编辑安全而读取目标文件或内容版本变化后的必要重读。“不读但答错”是失败，不得当优化。

报告：正确率、correct_without_fact_reread_rate、repeated_fact_read_count、总工具步数、所有模型 token、时延、source/版本命中；unknown 单列。主模型是否调用保存工具另计 eligible_save_requests、missed_save_requests、unauthorized_commit_count、false_memory_confirmation_count，不从脚本预设调用算真实决策能力。

## C. 案例数据契约与实现位置

N01A 在 `tests/evaluation/fixtures/context_matrix.json`、`memory_dependency.json` 声明矩阵及任务；`nanobot/evaluation/scenarios.py` 只做固定生成/两阶段编排，不另写 AgentLoop。
EvalCase 增加可选 `scenario`、`parameters`、`memory_variant`、`requires_capabilities`、`setup_kind`。scenario 只允许 basic/context_pressure/memory_dependency；无法识别则拒绝。ground_truth 和 verifier 配置不进入模型可读工作目录。

`build_context_cases(parameters: dict) -> list[EvalCase]` 与 `build_memory_cases(parameters: dict) -> list[EvalCase]` 由 N01A 定义；固定 seed 和 canonical JSON hash，预算、模型、渲染版本均入 provenance。两阶段 trace 分开记录，bootstrap 步数不混入 follow-up 重复读但应计入总成本。

N01A 离线支持 fixed 和 prompt-sensitive scripted provider：后者仅按实际可见事实决定是否重读，不能访问 expected answer/ground truth。所有脚本报告标 mechanism_only=true。live runner 使用同样案例、独立 provider 和请求/token/时间上限，需另行授权，不在本次运行。
