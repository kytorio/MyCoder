# 实施记录 — spec/TASKS.md

2026-09-06：用户明确授权按已确认方案修改代码。沿用 MyCoder 的
codex/agent-governance；HEAD/main = 455533169d5a641300dd63d260b1ff5543c4093c。
不自动提交、推送、创建 worktree、更新依赖或调用真实模型。

## 当前状态

N00/N01/N01A/N05/N06/N07/N08/N09/N02/N03/N04/N10/N11 已完成；交付停在人工生产启用门禁。N08 的独立审查进程
在 3 分钟资源上限内未形成有效结论，控制器审查无阻断发现，详见 N08-report.md。
生产 context、记忆受控提交及可选 memory_save 已实现；memory_save 默认关闭，不启用生产 enforce。

N00 测试证据：新增 8 项通过（主会话独立执行，1.01s）；指定旧回归
145 项通过（6.27s）。独立审查规格与质量均 Approved，无待修问题；Ruff --no-cache 通过。Windows asyncio 会在初始化
建立本地自管道，因此离线拒绝守卫在事件循环建立后安装、关闭前恢复，
不解除模型或工具的联网限制。

Ruff 首次因不可写缓存失败；使用 --no-cache 只读检查，不更改权限或缓存目录。

## 接口复核

| 生产者 → 消费者 | 核对结果 |
| --- | --- |
| N00 → N01/N01A | 隔离 workspace/runtime/config；真实 Nanobot.run，无网络替身 AgentLoop |
| N01 → N01A/N10 | EvalCase/Result、脚本 provider、白名单验证与请求前预算；N01A 再扩场景 |
| N01A → N05/N02 | 旧行为基线先完成，新 feature 诚实报告 not_implemented |
| N05 → N06/N08 | 来源账本先 observe；无新 MemorySnapshot 依赖 |
| N06 → N07/N08 | artifact 完整落盘后才提供引用；不使用旧 TTL 清理 |
| N07/N08 → N09 | accepted H/delta 与来源策略共同驱动摘要和一次恢复 |
| N09 → N02/N03 | 上下文先使用旧 memory 文本/hash，记忆提交后再刷新版本 |
| N02/N03 → N04 | 同步 coordinator、显式保护、两现有 Dream 入口窄改 |
| N04/N09 → N10 → N11 | 组合/消融与兼容性验收；不以脚本证明自然语言质量 |

各任务的写集与消费者一致；稳定编号不按数字执行。
前置评测的 smoke 不等于压缩/记忆新功能完成。

## 实施约定

已有用户差异保持不动；工作树未提交，因此审查直接针对各任务新增文件，
而非空的 HEAD..HEAD 差异。现有独立任务文档作为完整 brief，不重复生成需求。
N00 特征测试与文档记录可并行；N01/N01A 共享评测接口，串行实施。

## N01 / N01A 验证记录

- N01 真实 SDK harness、白名单 verifier、预算/隔离/错误清理和 CLI 已完成。
  独立审查提出的兼容 observe、元数据脱敏、清理异常、验证耗时及 CRLF 回归均已修复并复审通过。
- N01A 已新增 12 上下文案例、36 记忆变体、usage 指标与 JSON/Markdown 比较。
  完整评测 + SDK 回归：167 passed，48.17s；Ruff --no-cache 与 basedpyright 均无问题。
- 首次及独立重复运行均为 49 案例、45 passed / 4 failed。失败是旧行为高压力上下文任务，
  不是测试程序失败：8653 token 候选压至 2899–3075，却只保留 2/3 标注事实，丢失部署值。
  低/中压力 3329/5991 token 案例通过；没有修改生产治理以改变这个分数。
- 记忆 36 变体任务均通过；memory_on 事实源重读 0 次，off/irrelevant 各 12 次。
  这是 preseeded 检索测试，不证明 LLM 会主动调用保存工具。
- 实际 CLI compare 验证 49 案例 case/config/model/render/dependency 兼容、任务状态逐项相同。
  实际 usage 均未知，保留 null；所有结果标 mechanism_only。
- 产物：.superpowers/sdd/CONTEXT-MEMORY-V2/N01A-baseline-first/suite-rbbrvb7i/suite-results.json；
  N01A-baseline-repeat/suite-2ahuswn6/suite-results.json；N01A-repro-comparison.json/.md。
  报告目录相对 MyCoder，忽略生成产物，不提交真实/合成会话内容。
- 只读 compare 首次受 sandbox 权限限制，针对上述隔离报告提权后成功；未触及生产数据。
- 四个受保护用户文件哈希保持原值：.gitignore、agent/loop.py、agent/runner.py、package-lock.json。
- N01A 审查补强：压力偏离目标时判 invalid_pressure_sample；高压力没有压缩/明确失败也不算有效样本。
  probe 前完整工作区快照与工具写入路径共同验证“只改目标”，不豁免 LLM 对 archive 路径的写入。
  新增超大首读、引用回读缺能力专案；前者实际执行，后者 fail-closed not_implemented。
  三项发现复审均已关闭；CLI/model/聚合小改由主会话核对，实际 CLI 加载验证。
- 最新回归：172 passed，53.06s；Ruff 和 basedpyright 无问题。
  最新基线共 51：46 passed / 4 failed / 1 not_implemented，invalid_pressure=0。
  路径：.superpowers/sdd/CONTEXT-MEMORY-V2/N01A-reviewed-baseline/suite-jgzcfvqt/suite-results.json。
  原 49 案例双跑与比较作为早期检查点保留；新增引用能力未实现不冒充通过。
- 最新 51 案例也已在生产改动前独立重复，状态/兼容哈希全部一致，eligible=50，
  not_implemented=1，invalid_scenario=0；N01A-reviewed-comparison.json/.md 已保存。
  N05 生产写入在重复进程完成后才解除短暂冻结，避免版本混用。

## N05 验收记录

- EvalRunner 的 observe 已接真实 Governor prepare/dispatch/response 观察点；
  显式注入隔离 runtime 根与 1024 评测 margin，清单不导出正文。
- 三项新增验收先 RED（placeholder/缺清单），再 GREEN：真实清单与请求哈希关联、
  失败响应脱敏、含 USER/SOUL/MEMORY 与实际 read_file 的 baseline/observe payload 相等。
  比较仅归一化两个用例独立分配的临时根，未删除请求字段。
- 完整评测/SDK 回归 174 passed（70.23s）；eval Ruff/basedpyright 无问题。
  当时 N05 独立审查尚未完成；最终复审见下文，四层管道仍未全部完成。
- 主会话独立补验 110 项 context/原 Goal continuation 回归通过（8.38s）；
  N05 production + eval 的 Ruff 和 basedpyright 全部通过。
- actual CLI observe 长上下文矩阵 12 例，8 passed / 4 failed，invalid=0；
  与冻结基线的正式 compare 通过，逐例首请求 token、约束保留率、任务状态和调用数均不变。
  证据见 N05-context-comparison.json 和 N05-controller-report.md（同忽略产物目录）。

- 最终两项审查发现（enforce 工具配对与合并消息中的显式 Skill 来源）均已修复。
  主会话独立 240 项回归通过（43.20s），复审 Epicurus Approved。
  修复后的 12 案例矩阵仍为 8 passed / 4 旧行为 failed，invalid=0，正式基线比较通过。
  证据：N05-reviewed-comparison.json、N05-review-findings.md。

## N06 在途验证记录

复用原子写 helper 增加精确保留换行的选项，旧调用默认行为不变。
enforce 原始结果进入 transcript/checkpoint，L1 只改变请求副本；Loop/ToolContext/RunSpec
传递同一显式 runtime store，observe 不注册回读工具、不创建归档目录。
已完成基础与真实 Runner 并行成功/错误结果的 RED→GREEN（12 项）；
继续验证批次边界、结构化结果、总预算、scope 和有界回读。独立审查尚未开始。

组合验证后：142 项 focused/Runner 回归、116 项 SDK/工具加载/配置回归、
7 项独立 tokenizer 测试通过；扩展评测/Goal/Builder 回归在短临时根下 204 项通过。
第一次扩展回归的 4 个 FileNotFoundError 来自 263 字符的旧会话检查点临时路径，
保留失败记录，未修改生产代码或断言来消除它。

初次独立审查通过，但对真实落盘恢复给出未验证项；主会话补充 SDK 重启测试后发现
Loop 最终落盘仍截断原文。现处于 N06 修复轮 1：仅修正 enforce 最终文本/文本块保存
和宿主错误状态的窄透传，保持 observe/图片脱敏；复审通过前不算 N06 完成。

N06 修复轮 1 已关闭：worker 65 项覆盖 + 1 项冷重载通过；主会话独立恢复/回放/
注入/归档回归 200 passed（18.91s）。9 个生产文件 Ruff/BasedPyright clean。
Halley 定向复审确认 F1 ADDRESSED、无新问题；真实 SDK 文本/文本块/错误结果
的跨重启测试关闭先前未验证项，N06 验收。当前开始 N07。
- SDK 文件清单依据最小改动调整：N05-R1 复用已有 from_config 与 Config.runtime_data_dir，
  不在 per-call routing helper 新增实例配置转发。主会话已直接核对代码。
- 独立核验用户注释：loop 141 行、runner 72 行，遗漏为 0；.gitignore/package-lock 哈希仍为原值。

## N09 与 S01 模块门禁

- N09 复用 `ContextGovernor`、`Consolidator`、`SessionSummaryCheckpoint` 和既有 sidecar，
  新增严格 `StructuredContextSummary` v1。模型结果先由宿主校验显式偏好和 session-scoped
  artifact 引用，校验及 checkpoint 原子提交成功后才切换内存摘要；旧纯文本摘要仍可读取。
- 自动压力、显式 SDK compact 和 provider overflow 共用 Governor L4；overflow 每个 lineage
  最多重试一次。accepted H 与未发送 delta 分离，保存/重启只物化合法 staged checkpoint，
  不重复已完成工具。无 accepted H 的自动路径保留 `irreducible_floor`，不会误报可压缩。
- L2 history artifact 增加 256 字符有界预览，用于在不读正文时选择相关引用；
  `context_artifact_read` 继续有界分页，未完成页可穿过 L4 由原样 delta 继续。L4 manifest
  只把 transcript 类来源标为 summarized，不把重建的 system/memory/Skill/schema 误记为 L4 淘汰。
- 用户明确要求不写前置失败测试，因此本任务先实现再统一验证，未伪造 RED 证据。
  评测模块回归 113 passed（82.78s）；N09/S01 聚合回归 482 passed 后发现 1 个旧 manifest
  reason 断言，最小修正后该断言 3 个参数全部通过。Ruff `--no-cache` 与 basedpyright 均 clean。
- 最终离线 `context-only` 12 案例矩阵全部通过：h4 候选 3329 token、无需压缩；
  h12 候选 5991、发送 2695–2871、每例 1 次 L4；h24 候选 8653、发送 4429–4586、
  每例 1 次 L4，并有 4–6 个工具步骤完成 artifact 恢复。12 例约束保留率均为 1.0，
  工作区只修改目标文件且 tool pair 全部合法。产物：
  `C:/Users/Kitorio/Documents/ChatGPT/nanobot学习/.tmp/s01final/suite-jklue2lk/suite-results.json`。
  该结果为离线脚本模型机制验证，不代表真实模型语义质量或生产启用结论。
- Goal 相关设计和代码保持在本轮边界之外；N09 关闭后下一任务为 N02 记忆受控提交。
- N09 独立审查按用户限制只等待一次，3 分钟内未返回结论后已终止。控制器只读核对
  session→consolidation 锁顺序、artifact 引用 grounding/分页、sidecar 写后切换与 L4 来源归因，
  未发现开放的 Critical/Important 问题；N09 关闭，N02 可开始。

## N02 记忆受控提交

- 新增同步 `MemoryWriteCoordinator` 和小型 dataclass 契约；同一规范根使用进程内共享短锁，
  `snapshot` 先完成恢复屏障。生产 `AgentLoop.from_config` 使用显式 runtime root 和
  workspace authorizer 注入唯一 writer；未引入额外服务、模型、Goal 或输入状态机。
- 提交顺序为恢复、授权/版本/受管区全量预检、持久 PREPARED journal、逐文件原子替换、
  全目标 hash 校验、持久 receipt、清理 journal。journal 自带完整 mutation hash；旧/新 hash
  可继续，第三种 hash 或损坏记录 fail-closed 并保留现场。
- 显式条目带版本、scope、来源和记录时间；同一请求重放复用 receipt 和 revision，同一事实新
  operation 为语义 no-op，纠正必须引用 `replaces_entry_id`。SDK 同步全文写可修改未受管内容，
  但不能删除或篡改显式条目；调用链没有 `asyncio.run`。
- Windows 集成回归发现冒号 operation_id 不能直接作为文件名，已将私有文件名固定为 ID 的
  SHA-256，receipt/journal 正文仍保留原始 operation_id。新增核心/恢复测试 21 passed；既有
  MemoryStore/cursor/GitStore/workspace policy 107 passed；SDK memory 集成 1 passed；相关
  Ruff `--no-cache` 与 basedpyright 均 clean。按用户要求没有预写失败测试，也未运行记忆评测矩阵。
- N02 未修改 Dream 调度、memory_save schema、配置默认或 Goal；这些分别属于 N03/N04，
  S02 集中实验仍在 N02/N03/N04 全部关闭后执行。

## N03 memory_save 工具

- 新增普通 `MemorySaveTool`，复用现有 ToolLoader/Registry/RequestContext/MemoryStore；
  `tools.memory.enabled` 默认 false，关闭时不注册 schema。LLM 只传 target/key/content/
  source_excerpt/replaces_entry_id，owner、scope、operation/entry id 和权限均由宿主构造。
- 保存必须绑定当前可信用户原文中的精确 excerpt；secret、缺来源、非法 key/target、只读策略、
  受保护条目冲突均返回明确失败结果，只有 durable receipt 后才返回 committed。漏调用不会触发
  隐式保存，也没有输入分类模型、MemoryRememberService 或新的 admission 状态机。
- `ContextGovernor.prepare_request` 通过既有 transcript builder 刷新文件派生 system context；
  memory 变化时丢弃旧 provider continuation 并重新记账，因此同轮工具结果后的下一次模型请求及
  新会话均可见已提交 revision，未变化请求保持原路径。
- 离线评测新增真实 `setup=tool_write`：A 会话根据显式用户文本调用 memory_save，B 会话从规范
  memory 回答；baseline 仍为 not_implemented。修复该链路时发现 Windows 深路径下 journal
  的原子临时名超过旧 260 字符限制，私有文件名改为 192-bit 截断哈希，正文仍保存完整 operation id。
- 用户明确要求先实现再集中测试。本任务聚合验收 39 passed，指定相邻回归此前 128 passed；
  Ruff `--no-cache` 和 basedpyright clean。没有运行完整记忆实验矩阵，留待 N04 关闭后的 N10/S02 门禁。
- N03 未修改 Dream 或 Goal；memory_save 仍是默认关闭的可选工具，不代表已生产启用。

## N04 Dream 与显式记忆协作

- `MemoryStore.build_dream_tools` 仍只暴露原四个文件工具；小型适配器把 USER/SOUL/MEMORY
  的 write/edit/apply_patch 候选提交到 N02 共享 writer，Skill 文件继续使用原文件工具。
  Dream 生成期间不持锁，也不注册 `memory_save`。
- canonical read 绑定该 Dream 会话的可信 base；未受管文本执行确定性三方合并，非重叠改动可合并，
  重叠改动返回 conflict。`nanobot-explicit-memory:v1` 块必须与所见 base 字节一致，提交时始终
  重新附着 latest 块；冲突后需重读并重试，未解决状态阻止游标推进。
- 同一次 apply_patch 若同时包含 canonical 与 Skill 路径会在任何写入前拒绝；拆分后两条原路径
  分别提交。多 canonical 文件在生产 writer 路径内作为一个 mutation 全量预检和提交。
- 手动 `/dream` 与原 cron Dream 均以 `normal completion AND no unresolved write conflict` 作为
  cursor 门槛。Git 审计失败被单独捕获和报告，不回滚已成功的 canonical 提交，也不改写上述
  cursor 判定；周期、模型 override 和增量 history 读取方式未变。
- `/dream-restore` 先通过 GitStore 的只读候选接口取得目标 commit 与 parent tree，再对当前所有
  显式块做保护校验并经 writer 提交；不会先调用通用 `git.revert` 落盘。校验冲突时 canonical、
  cursor 和 Git history 均不变；成功后才单独创建审计 commit。
- 用户要求先实现再测试。新增 11 项 focused 测试；包含原 Dream/session/cursor/command/GitStore/
  apply_patch/config 的聚合回归 142 passed（13.43s）；N04 生产文件 Ruff `--no-cache` 与
  basedpyright 均 clean。S02 的离线记忆/组合矩阵留给 N10 一次性执行。
- N04 未修改 Goal、安全策略大模块、调度周期或配置默认；单进程所有权边界保持不变。

## N10 组合评测与成本账本

- 复用 N01A 的真实 runtime、隔离根、fixture loader、usage observer、compare 和 scripted provider，
  接通 baseline/observe/context-only/memory-only/full/no-l1/no-l2/no-l3/no-l4。上下文、记忆和完整
  模式分别只打开声明的能力，缺失能力仍 fail-closed，不把不支持项冒充通过。
- 新增 canonical+receipt、Dream cursor、旧 Dream 覆写、重复显式条目、安全副作用、artifact
  SHA-256 分页回读和指定 Context layer 的可执行 verifier。受控 setup 只加入 Dream 交错、重启
  探针和 read-cache reset；fixture 仍不能执行任意代码。L3 定向案例通过同版本二次读取并在下一轮
  触发 micro-compaction，关闭 L3 时 verifier 明确失败。
- 59 个固定案例由 1 basic、12 context matrix、36 memory dependency、3 tool context、1 explicit
  memory、3 memory negatives、1 Dream conflict、1 restart recovery 和 1 existing safety 构成。
  `tests/evaluation` 最终为 124 passed（177.37s）；N02–N09 与指定安全/原 Goal 不变性聚合回归
  为 556 passed、25 skipped（29.09s）；Ruff `--no-cache` clean，basedpyright 0 errors/warnings/notes。
- 为遵守资源门禁，先运行 58 例六模式全矩阵，再只对新增 L3 案例补跑六模式定向矩阵，没有重跑
  其余 58 例。合并结果：baseline 47 pass / 5 fail / 7 not_implemented；full 59 pass；no-l1
  58 pass / 1 error；no-l2 55 pass / 4 error；no-l3 58 pass / 1 failed；no-l4 58 pass / 1 error。
  L1/L4 的反例为 artifact 完整回读，L2 的反例为 4 个高压力上下文任务，L3 的反例为同版本重复读取。
- full 的 missed save、unauthorized commit、false confirmation、duplicate entry、stale Dream overwrite
  和 safety bypass 均为 0；负例 eligible=0 且没有提交，安全场景实际尝试 1 个受限工具步骤，非空集通过。
  上下文 manifest 对所有 context-mode 案例汇总 layer trigger、eviction reason 和 recovery replay。
- 全矩阵证据位于 `C:/Users/Kitorio/Documents/ChatGPT/nanobot学习/.tmp/n10matrix`；L3 补充证据位于
  `C:/Users/Kitorio/Documents/ChatGPT/nanobot学习/.tmp/n10layers`。两批均记录 git commit、dirty diff、
  case/config hash、model 和独立 case roots；首批 dirty hash 为 `44598437...`，补充批为 `762d637f...`。
- 两批 full 合计 360 个 scripted 请求、1,289,077 estimated input tokens；所有 actual input/output/cache
  usage 都未知并保留 null。模型为 `evaluation-scripted`，全部 `mechanism_only=true`，没有真实 API、
  费用或生产会话，因此结果只证明机制和不变量，不证明自然语言质量、真实时延或成本收益。
- N10 没有修改 Goal 源码、HITL 或生产默认开关；没有 commit/push/worktree/依赖变更。用户既有
  `.gitignore` 与 `webui/package-lock.json` 差异保持不动。详细清单见
  `.superpowers/sdd/CONTEXT-MEMORY-V2/N10-report.md`。

## N11 配置兼容与交付门禁

- 复核现有 `Base` alias generator、`load_config`/`save_config` 和 WebUI path-scoped
  read-modify-write 后确认 camelCase、旧配置默认、原子保存和 `${ENV}` 模板保持已满足要求，
  因此没有为了任务表面写集修改 loader 或设置 API。
- 两个实际缺口采用窄修正：`MemoryToolConfig` 的 transaction/journal 字节上限使用 strict int，
  拒绝 `true/false` 被当作 1/0；`AgentLoop.from_config` 在 context enforce 或 memory_save enabled
  且既无 Config source path、也无显式 `runtime_data_dir` 时立即报错。observe + memory disabled
  的低层嵌入保持无根兼容；`Nanobot.from_config(path)` 继续从配置路径自动绑定实例 root。
- 新增 `tests/config/test_context_memory_roundtrip.py` 9 项，覆盖默认不启用、alias roundtrip、
  环境变量模板、WebUI 配置透传、非法 bool、缺根拒绝、绑定/显式根和 observe 兼容。
  与指定配置/SDK组一起为 143 passed、1 skipped；唯一未完成项是既有 config path 测试因沙箱
  禁止写 `C:/Users/Kitorio/custom-workspace`，在授权环境原样重跑后 1 passed。另跑受影响
  context tool batch、memory integration/commit/save 和 evaluation runner 107 passed。
- N11 修改文件 Ruff `--no-cache` clean，basedpyright 0 errors/warnings/notes。按用户资源门禁没有
  重跑 N10 六模式离线实验，直接引用已保存的 full 59/59 与 L1–L4 非空消融证据。
- 新增 `docs/engineering/context-memory-guide.md`：配置样例、runtime root、SDK、单所有者、保存纠正、
  Dream、磁盘/journal 恢复、关闭和旧二进制降级；新增
  `docs/engineering/context-memory-release-checklist.md`：构建、离线证据、备份恢复和单独授权项。
- 默认仍为 context observe、memory_save disabled；关闭工具不取消已有显式条目保护和事务恢复。
  没有 live 评测、生产配置写入、服务启动、迁移、commit、push 或 merge。Goal/HITL/UI/多进程
  memory root 仍在边界外。详细证据见 `.superpowers/sdd/CONTEXT-MEMORY-V2/N11-report.md`。

## N12 真实 API 评测模式

- 增加 `EvalExecutionConfig`、case/suite 原子复合预算和 `LiveEvaluationProvider`。真实 provider 由
  显式测试配置经现有 `load_config`、环境变量解析和 `build_provider_snapshot` 创建；不复制 Pico
  provider 客户端，不新增第二套 AgentLoop。
- `--live` 必须同时提供存在的 `--config` 和正数 request/token/time 上限；上限按 suite 全局累计，
  case 预算继续生效。默认配置、fallback model、scripted basic case 均 fail-closed。
- live 仍使用隔离 workspace、内部 artifact、工具白名单和可执行 verifier；只允许选定 provider 的
  模型网络请求。worker/summary/dream 按物理请求记录实际 usage、估算 token 和时延，provenance 标记
  `execution_kind=live`、`mechanism_only=false`、模型/provider 与源配置 SHA-256。
- 结果只导出 request/payload hash、计数、usage 和判定，不导出 API key、prompt/response 正文或异常
  详情。L4 summary 通过同一真实 provider，能够单独核算摘要调用成本。
- 新增不联网的 provider、预算、CLI 和完整 24-history context live 集成测试；真实 API 命令仅记录在
  工程指南中。本次 `tests/evaluation` 集中回归 135 passed（144.31s），context acceptance 回归
  96 passed（8.86s），Ruff clean，basedpyright 0 errors/warnings/notes；未执行付费调用、未读取生产
  配置、未 commit/push/merge。
