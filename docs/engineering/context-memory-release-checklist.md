# 上下文与记忆发布门禁

此清单停在人工启用之前。完成清单不等于授权部署、live 评测、提交、推送或合并。

## 构建与配置

- [ ] 记录待发布 commit、dirty diff、Python/依赖版本和配置 hash。
- [ ] 确认 `agents.context.mode=observe`、`tools.memory.enabled=false` 的旧默认 roundtrip 不变。
- [ ] 确认 camelCase 的 Context 与 memory 配置经 load/save/WebUI path-scoped 更新不丢失。
- [ ] 确认非法 layer、比例、token、artifact、transaction、journal 配置 fail-closed。
- [ ] 为每个实例指定独立 config path、runtime root 和 workspace；禁止 workspace fallback。
- [ ] 确认同一规范 memory 根只有一个 Gateway/进程所有者。
- [ ] API key 仅使用环境变量模板或既有凭据存储；配置样例/日志/报告不含密钥。

## 离线证据

- [ ] `tests/evaluation` 全部通过。
- [ ] N02–N09、SDK、workspace/Exec/MCP/Goal 不变性回归全部通过或有明确允许的 skip。
- [ ] Ruff 与 basedpyright 对本轮文件通过。
- [ ] full 固定套件全部通过；baseline 的 not_implemented 与失败没有被改写为 pass。
- [ ] no-l1/no-l2/no-l3/no-l4 每层至少有一个非空失败信号。
- [ ] missed/unauthorized/false/duplicate/stale-overwrite/safety-bypass 不变量为 0。
- [ ] 每个结果包含 commit、dirty diff、case/config/model hash 与独立根证据。
- [ ] unknown actual usage 保留 null；estimated 与 actual 分开，不生成虚假费用。

## 预发布备份与恢复

- [ ] 备份 config、USER/SOUL/MEMORY、runtime `memory/`、`context-artifacts/` 和 `sessions/`。
- [ ] 记录备份 hash、ACL/权限、创建时间、恢复负责人和保留期限。
- [ ] 确认没有 pending/corrupt journal；不得通过删除 journal 规避恢复。
- [ ] 演练磁盘满、artifact 写失败、journal 超限、权限拒绝和进程中断的 fail-closed 行为。
- [ ] 演练关闭 memory_save 后显式条目保护与事务恢复仍生效。
- [ ] 演练旧 Dream/SDK write/restore 与新显式条目冲突时不覆盖、不推进错误 cursor。
- [ ] 降级前停止所有同根 writer，并验证归档可恢复；旧二进制不得直接接管显式块写入。

## 需要单独人工授权

- [ ] 批准 bounded live evaluation 的 provider/model、测试数据、请求/token/时间/费用上限。
- [ ] 人工解释真实模型质量、漏存/误存、时延和费用结果；脚本机制分数不能替代该判断。
- [ ] 批准测试或灰度实例的 `context.mode=enforce`；记录 enabledLayers 和回退阈值。
- [ ] 单独批准 `tools.memory.enabled=true`；确认用户授权、纠正和隐私处理流程。
- [ ] 指定监控、值班、暂停条件和回退负责人。
- [ ] 批准部署/重启/合并/commit/push；没有批准时保持默认关闭并停止在本门禁。

## 明确不在本次发布内

- 新 Goal/watchdog、Goal 模型或 Goal 降级逻辑。
- 新通用 HITL/PolicyEngine 或前端设置面板。
- 多进程共享同一规范 memory 根。
- 自动迁移用户 profile、自动删除 raw/artifact、强制覆盖显式记忆。
- 未授权的真实模型调用或生产启用。
