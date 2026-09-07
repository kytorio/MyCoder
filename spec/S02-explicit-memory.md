# S02 LLM 记忆保存工具与 Dream 协作

> 2026-09-06 按用户 H01 修订：主模型自行决定调用 memory_save；H02–H05 已确认沿用建议。取代 revision 70 第四节中“输入到达即自动识别、首次 LLM 前写入”的旧接线设计；其他记忆一致性要求保留。任务 N02–N04，排在评测和上下文模块之后。

## 最小变更方案

新增一个普通写工具 `nanobot/agent/tools/memory_save.py`，复用 Tool/ToolLoader/ToolRegistry、RequestContext、现有 execute_tool_calls 和工具结果回传。工具内部把本次调用转换为 MemoryRememberRequested，同步等待受控提交完成；这就是事件触发点。观察通知可以使用现有 RuntimeEventBus，但 fail-open observer 不是提交执行器。

不新增输入识别器、MemoryRememberService、UserInputEnvelope、单独模型或模型路由；不改正常输入/pending 队列的接纳状态机，也不在每次输入前额外调用 LLM。普通对话是否保存、何时保存由主模型根据工具描述与上下文判断。

区别必须明确：不再保证首次 LLM 请求前已存记忆；成功调用后，tool result 返回提交凭据，后续同轮请求立即可见。主模型漏调用记为漏存，不能偷偷补一个输入分类器。失败通过既有 ToolResult.error 回传，主模型可以解释、澄清或继续不依赖记忆保存的部分，不新增“混合任务全部短路”机制。

## 工具契约

`MemorySaveTool(Tool)` 名称 `memory_save`，read_only=False，exclusive=True；通过现有加载机制注册。构造时注入共享 MemoryStore；ToolContext 只增加一个 typed memory 引用，不重写 loader/registry。当前会话来源、message/turn ID、owner 和项目范围取自 current_request_context()/宿主 scope，模型不能传任意路径或自行声明权限。

模型可传参数：

| 字段 | 约束 |
| --- | --- |
| target | user / soul / memory，映射到三个既有规范文件 |
| key | 稳定语义键，1–128 字符，例如 reply.language；不得作为文件路径 |
| content | 要保存的一条事实/偏好，1–2048 字符；不允许整文件覆盖 |
| source_excerpt | 用户原话中的依据，1–2048 字符；必须能在绑定的可信用户请求中找到 |
| replaces_entry_id | 可空；纠正已有显式条目时指定，仍须有用户明确纠正依据，不是强制覆盖授权 |

v1 工具保存用户明确表达的长期偏好/已确认事实；Dream 继续负责整理和推断，不借 H01 扩大为自动收集所有对话。用户要求记住文件中的事实时，本轮先由主模型概括并取得明确确认，再提交，不能把工具输出当用户原话。是否值得长期保存由主模型判断，不做另一个语义分类调用；source_excerpt 校验只证明引用存在，不声称能形式化证明自然语言授权。对引用、否定、临时指令的误调用必须纳入负例评测。

成功返回 JSON 文本，包含 status=committed、operation_id、revision、entry_ids、replayed 和已保存事实摘要。失败返回既有 ToolResult.error，明确 reason_code 与“未确认保存”，不能返回同形 committed。只有真正提交成功才生成记忆保存成功的工具结果；模型应在成功后才对用户说“已记住”。不增加任意自然语言输出拦截器，不声称可以杜绝模型无工具却自称已记住；这种虚假确认单独评测。

工具未调用时规范文件零变更；disabled 时不注册 schema；Dream 使用自己的受控文件工具，不装载 memory_save；导入/恢复会话没有模型工具执行，不能因此触发保存。

## 共享存储契约与幂等

新增 `nanobot/agent/memory_writes.py`，集中存放小型 dataclass/异常与 MemoryWriteCoordinator；不再预设单独 memory_contracts、memory_remember、memory_tools、dream_cycle 四个模块。只有实际实现确实需要拆分时，先记录职责与测试理由，不以“统一框架”为目的拆分。

| 类型 | 字段/语义 |
| --- | --- |
| MemoryScope | instance_id、project_id（可空） |
| MemoryFact | entry_id、target、key、text、scope、replaces_entry_id（可空） |
| MemoryRememberRequested | operation_id、message_ref、original_text_hash、owner_id、scope、facts；由工具构造，不是 LLM 可传的权限声明 |
| MemorySnapshot | revision、file_hashes、contents、explicit_entries；一致读取 |
| MemoryMutation | operation_id、source（explicit/dream/sdk/restore）、base_revision、proposed_files、facts；只有三个规范目标 |
| MemoryCommitResult | operation_id、status（committed/conflict/failed）、revision、entry_ids、replayed、reason_code |

`MemoryWriteCoordinator(root, runtime_root, *, authorize)`、`snapshot() -> MemorySnapshot`、`commit(mutation: MemoryMutation) -> MemoryCommitResult` 均同步。按规范根共享短期锁，注入所有 MemoryStore；原 _append_lock 只保护单实例 history，不够用。模型调用、工具通知和 Dream 生成都在锁外。

在已有 MemoryStore 增加窄入口 `remember(request: MemoryRememberRequested) -> MemoryCommitResult`，复用同一 merge/commit。工具从宿主来源身份、scope、target、key、归一化内容 hash 构造稳定 operation_id；同一次工具重试/恢复与相同事实再次调用不能新增条目。没有渠道 message ID 时复用已保存 turn 身份；仍无法证明同一调用时，以 scope+target+key+内容相等保证语义 no-op，不伪称所有调用具有 exactly-once delivery。相同 operation_id 不同 payload 为 conflict。

显式条目使用稳定 entry_id 的受管段；metadata 记录用户来源、时间、版本和 scope。明确纠正替换同一条目，重复相同事实返回 no-op committed。正文仍是事实来源，metadata 不是另一个独立记忆数据库。

## H02 范围与本轮可见性

USER.md、SOUL.md、memory/MEMORY.md 仍在 agent workspace。个人偏好在单实例共享，项目事实在 MEMORY 中标记当前项目 ID，加载时过滤；旧未标记内容保留旧语义，不迁移为各项目实体文件。USER 保存沟通偏好，MEMORY 保存项目事实，SOUL 仅保存安全范围内的习惯，不能把用户文本升级成系统/工具权限。即时工具不创建 Skill，不保存密钥。

tool result 是当前未消费 delta，自然进入下一轮模型请求并受上下文保护；同时在既有 Governor.prepare_request 的来源收集处发现 memory revision 变化，刷新规范记忆派生视图，避免旧 USER 块和刚提交的偏好冲突。只加这一局部刷新，不另建事件分发/循环监听。下一会话复用 ContextBuilder/MemoryStore 读取。

当前 include_memory=False 仅影响 MEMORY，不影响 USER/SOUL；不能据此声称已关闭全部记忆。遵守已有会话可见性约束，禁止通过新增 runtime block 绕过关闭策略。评测的 memory_off 在独立 fixture 中移除三类候选视图，而不是修改生产策略。当前真实已提交条目在 S01 中 required，旧摘要不能覆盖它。

## 提交原子性、H03 与失败恢复

保留现有规范文件与历史格式。MemoryStore.write_* 当前直接 write_text；复用 helpers._write_text_atomic 与 history 原子写模式，仅补规范文件/元数据所需协调。authorize 适配现有路径与读写守卫；无法写不能换根、换工具或把事实改存 runtime 目录。

短锁内：恢复已知未完成事务 → 权限/版本/身份/显式保护全量预检 → PREPARED journal → canonical 临时文件 flush/fsync/replace → 校验全部 hash → metadata/receipt 原子提交 → committed。journal 位于显式 runtime_data_dir/memory 下，含恢复所需旧/新 hash 与字节，不写普通日志。单事务默认 1MiB，journal 默认 64MiB，超限先失败，不删未恢复记录。

恢复只在当前文件 hash 等于已知旧/新值时继续；第三种内容、损坏 journal 返回 conflict 并保留现场，不覆盖人工编辑。snapshot 经恢复屏障，不读半提交；receipt 不是 canonical 的替代品。取消发生在提交中时，按真实恢复结果报告，不承诺已撤销。已完成 journal 可在 receipt 验证后清理，幂等身份不能随 TTL 丢失。Windows 目录 fsync 能力差异明确记录，不承诺所有文件系统断电原子性。

H03 已确认：同一规范根只保证单 Gateway/进程所有权，跨会话共享协调器；多进程同根不支持。不给 threading.Lock 包装成跨进程保证。不拦截用户编辑器、任意 shell 或外部进程。

## Dream 与 H04 覆盖保护

沿用 build_dream_prompt、build_dream_tools、增量 history、cursor、GitStore 和原周期（agents.defaults.dream.intervalH 默认 2）。分别对 gateway_runtime.on_cron_job 和 command/builtin.cmd_dream 做窄改动，不强制抽新 cycle 服务；两入口有同样的并发/失败测试。

在 MemoryStore.build_dream_tools 的既有注册点，对三个 owned 文件的 write/edit/apply_patch 增加小适配器，提交经同一协调器；Skill 文件仍用旧工具和权限。read 绑定可信 base version；旧 Dream 候选保留最新显式条目，非受管区段可作确定性非重叠三方合并，重叠返回冲突，重新读后最多一次重试。不可整文件 last-write-wins，不能推断偏好过期后删除保护。

同批 canonical 变更先全检；owned/Skill 混合 patch 首版明确拒绝并拆开，不声称跨存储原子性。仅正常完成且无未解决写冲突才原子推进 cursor；即时工具不推进 cursor。内容提交与 cursor 间中断的重跑需幂等，不跳过未整理历史。Git commit 不作为保存凭据，Git 失败单独报告已提交事实。

H04 已确认：SDK write_* / MemoryClient.write 和 /dream-restore 改变或移除受保护条目时返回冲突；无冲突成功仍保持同步返回 None，冲突为 MemoryConflictError，不用 asyncio.run。restore 先取候选内容，经协调器提交，不能先 GitStore.revert 后检查。用户通过主模型调用 memory_save 并提供明确纠正依据来更新条目，模型不能通过普通整文件工具获得强制覆盖能力。

## 配置、改动面与验收

新增 tools.memory（复用现有工具配置/懒加载模型注册）：enabled=false、transactionMaxBytes=1048576、journalMaxBytes=67108864。MemoryToolConfig 跟工具放置，并在 config/schema.py 显式声明；不保留 agents.explicitMemory、intentMode、intentTimeout/input/outputTokenLimit 或 remember 专用模型。关闭工具只停止新调用，不关闭已有 journal 恢复与显式保护。

主要新增文件仅 memory_writes.py 和 tools/memory_save.py。修改 MemoryStore、ToolContext/构造注入、memory Skill、两 Dream 入口、SDK/restore 调用点，以及 Governor 的版本刷新；loader、registry、runner 工具执行流程尽量原样复用。

测试先用正式 registry 执行成功/错误/重复调用，再验证真实 Runner 选择工具后的 tool result、下一请求偏好、重启读回、漏调用/虚假确认、负例、纠正、只读、故障恢复与 Dream 交错。语义误调用/漏调用需要真实模型评测，脚本只证明执行与控制流，不能假定 LLM 必然调用。
