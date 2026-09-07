# MyCoder：上下文、记忆与评测规格

> 状态：**N00–N11（按既定非数字顺序）均已验收；生产启用仍待单独人工授权**。2026-09-06，替代已回退的旧治理方案及 T00–T17 排期。实际进度见 [实施记录](IMPLEMENTATION.md)。

依据用户指定的[技术方案](https://kcnilb4v5r43.feishu.cn/wiki/RDSBwIsZIiFvkxk2vFtcwkW0nde)，本次读取版本为 **revision 70**，实际文档 ID 为 `I2gWde9xOoz8n3xKwI6cRsognpb`。仅落实第三节“上下文规划与四层压缩”、第四节“记忆保存与 Dream 周期更新”、第六节“评测与回归”。H01 按用户最新指令覆盖原输入前自动识别方案，改为主模型调用 memory_save 工具；最小变更、评测先行。

全部后续开发仍在 `E:\Code\Agent\MyCoder`、分支 `codex/agent-governance`；基线 `main@455533169d5a641300dd63d260b1ff5543c4093c`，本次未修改 Git 历史。原 nanobot checkout 只作参考。

## 阅读顺序

1. [S00 范围与开发门禁](S00-development-boundary.md)：哪些做、哪些不做。
2. [S01 上下文压缩](S01-context-compression.md)、[S02 显式记忆与 Dream](S02-explicit-memory.md)、[S03 评测](S03-evaluation.md)：模块边界、接口、失败语义与验收。
3. [CODE-MAP](CODE-MAP.md)：现有可复用代码与修改位置。
4. [TASKS](TASKS.md)：新编号 N00–N11（含前置 N01A） 的完成顺序及逐任务文档。
5. [人工确认单](REVIEW.md)：已确认选择与本次修订影响。
6. [回退记录](ROLLBACK.md)：旧变更归档位置、保护项与实际验证。

## 方案结构

采用“沿用现有 runtime、三个局部模块”的方案：ContextGovernor 继续负责请求；MemorySaveTool 通过既有工具执行链路触发记忆提交，MemoryWriteCoordinator 协调规范文件写入；Dream 保留周期整理；EvalRunner 复用 Nanobot 正式入口。事件通知不承担提交成功的判定。

没有恢复旧的 LifecycleDispatcher/WriteGateway/StopController 排期，没有 Goal 判断器或 watchdog，没有新增 PolicyEngine/HITL 平台。只为本轮功能增加必要的输入接线、存储协调、配置和脱敏日志。

**完成门禁：**先完成 N00/N01/N01A 评测能力和两类任务基线，再推进上下文与记忆；具体见 [评测案例与 pico 参考](EVALUATION-CASES.md)。回退验证不是新设计验收，文档更新不等于功能实现，也不授权付费评测、生产启用、提交或推送。本轮代码及进度记录在项目内，不自动写回飞书。
