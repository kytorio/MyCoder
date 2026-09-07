# 上下文压缩与显式记忆配置指南

本文说明 S01/S02 功能的配置、SDK 存储根、关闭和恢复语义。它不是生产启用授权；新功能的
安全默认值仍是 `agents.context.mode=observe`、`tools.memory.enabled=false`。

## 默认行为

| 配置 | 默认值 | 作用 |
| --- | --- | --- |
| `agents.context.mode` | `observe` | 生成脱敏 ContextPlan 清单，不启用 L1–L4 新裁剪 |
| `agents.context.enabledLayers` | `L1,L2,L3,L4` | 仅在 mode=enforce 时生效 |
| `agents.context.highWatermark` / `targetRatio` | `0.85` / `0.65` | 按可用输入预算决定治理与目标 |
| `agents.context.safetyMarginTokens` | `null` | 运行时取 `max(1024, ceil(window*0.05))` |
| `agents.context.toolResultTokenBudget` / `toolBatchTokenBudget` | `2048` / `8192` | L1 单结果/整批预算 |
| `agents.context.artifactMaxBytes` | `134217728` | 当前 runtime root 下 artifact 容量上限 |
| `agents.context.schemaDiscovery` | `false` | MCP schema 默认保持全量兼容 |
| `tools.memory.enabled` | `false` | 不注册 `memory_save`；不影响已有显式条目保护和事务恢复 |
| `tools.memory.transactionMaxBytes` / `journalMaxBytes` | `1048576` / `67108864` | 单事务和未恢复 journal 上限 |
| `agents.defaults.dream.intervalH` | `2` | 保留既有 Dream 周期，不因本功能改变 |

预算必须为正整数且不能使用布尔值；memory journal 不得小于单事务上限；Context target 必须
小于 high watermark，单工具结果预算不得超过整批预算，enabledLayers 不得重复或包含未知层。

## 仅供人工审批的测试配置样例

以下样例不含 API key，不会被本交付自动写入。把配置保存在一个明确的测试实例目录；
`Nanobot.from_config(config_path)` 会把配置文件所在目录绑定为 runtime data root。

```json
{
  "agents": {
    "defaults": {
      "workspace": "C:/nanobot-test/workspace"
    },
    "context": {
      "mode": "enforce",
      "enabledLayers": ["L1", "L2", "L3", "L4"],
      "highWatermark": 0.85,
      "targetRatio": 0.65,
      "safetyMarginTokens": 1536,
      "toolResultTokenBudget": 2048,
      "toolBatchTokenBudget": 8192,
      "artifactMaxBytes": 134217728,
      "schemaDiscovery": false
    }
  },
  "tools": {
    "memory": {
      "enabled": true,
      "transactionMaxBytes": 1048576,
      "journalMaxBytes": 67108864
    }
  }
}
```

环境变量引用应继续以 `${NAME}` 放在原始 config 中。`load_config`/`save_config` 和 WebUI 的
path-scoped read-modify-write 会保留模板；运行前的 `resolve_config_env_vars` 只用于内存中的运行时
配置。不要把已经解析出真实密钥的 Config 对象作为设置源另行持久化。

## Runtime root 与 SDK

状态文件不能回退到业务 workspace。正式 SDK 入口应使用：

```python
from nanobot import Nanobot

bot = Nanobot.from_config("C:/nanobot-test/config.json")
```

此时 runtime root 是 `C:/nanobot-test`。L1 artifact 位于其 `context-artifacts/`，显式记忆
journal/receipt 位于其 `memory/`，session checkpoint 位于其 `sessions/`。规范记忆正文仍在
agent workspace 的 `USER.md`、`SOUL.md`、`memory/MEMORY.md`。

低层 `AgentLoop.from_config(Config(...))` 如果启用 context enforce 或 memory_save，必须先
`config.bind_source_path(path)` 或显式传 `runtime_data_dir=Path(...)`；否则构造立即失败。默认
observe + memory disabled 仍可在无绑定路径的单元测试/嵌入场景中使用。

同一规范 memory 根只支持一个 Gateway/进程所有者。可以在同一进程内跨会话共享协调器；不要
让两个进程同时写同一 USER/SOUL/MEMORY 根。runtime root 也不能在多个不相关实例间混用。

## 保存、纠正与 Dream

`memory_save` 只在启用时注册，由主模型决定是否调用。成功必须返回 durable receipt，下一次
模型请求和新会话才可依赖已提交条目。未调用、失败或仅在文本中声称“已记住”都不是保存成功。

显式纠正应由用户给出新依据，模型调用 memory_save 并传 `replaces_entry_id`。SDK 全文写、Dream
和 `/dream-restore` 都不能删除或覆盖受保护显式块；冲突时需保留现状、重新读取并让用户决定。
关闭 memory_save 只阻止新工具调用：writer 仍在启动/读取边界恢复已知事务，SDK/Dream/restore
仍执行显式块保护。

## 空间、失败与恢复

- artifact 容量不足、归档失败或最终输入不可约时，enforce 明确失败，不静默丢正文。
- journal/事务超限在规范文件变更前失败；损坏 journal、第三种文件 hash 或人工重叠修改会
  fail-closed 并保留现场。
- snapshot/读取先经过恢复屏障；不要手工删除 pending journal 来“解除”冲突。
- artifact、raw transcript、receipt 和 journal 不由本配置自动 GC；仍被引用的数据不得删除。
- ContextPlan 日志只含 token/来源/hash/decision/reason 等脱敏清单，不应写入来源正文。
- usage 的 null 表示 provider 没有返回实际值，不代表 0 token 或 0 费用。

出现磁盘错误时先停止新请求，保留 workspace 与 runtime root 的完整副本，再根据日志中的
reason_code 检查容量、权限和已知 hash。只有在当前二进制完成恢复或人工确认一致性后才继续。

## 关闭与降级

紧急关闭新行为时，在同一所有者进程停止接收新回合后：

1. 将 `agents.context.mode` 改回 `observe`，将 `tools.memory.enabled` 改为 `false`。
2. 用当前版本重新加载一次配置，确认没有 pending recovery 错误；停止进程。
3. 归档 workspace 中三个规范文件，以及 runtime root 的 `memory/`、`context-artifacts/`、
   `sessions/` 和配置文件。保留文件权限、hash 和时间信息。
4. 若要降级旧二进制，确保没有其他进程继续写同一根。旧版本可能不理解显式块/receipt，
   不得直接编辑或丢弃 metadata 后继续写。

关闭工具不会撤销已保存事实。删除或纠正显式条目必须走用户授权的 memory_save 纠正流程；
本交付不提供强制覆盖或自动迁移用户 profile 根。

## 评测边界

N10 的 full 离线 scripted suite 为 59/59，并证明四层消融、canonical+receipt、Dream/restart 和
安全不变量的控制流。它全部是 `mechanism_only`，actual token usage 未知。真实模型是否选择正确
保存、自然语言质量、时延和费用必须由单独授权的 bounded live evaluation 决定，不能由本指南
或离线结果推断。

## 真实 API 评测

真实评测复用同一套 scenario、AgentLoop、隔离 workspace、工具白名单、verifier、ContextPlan 和
memory 指标，只把 scripted provider 换成配置文件指定的真实 provider。L4 summary 也经过该
provider，因此其请求数和 token 会按 `purpose=summary` 单独计入。`trace.json` 记录模型、provider、
请求用途、估算/实际 token 和时延，但不记录 API key、prompt 正文或 response 正文。

为评测建立独立配置，不要传生产实例的配置文件。示例：

```json
{
  "providers": {
    "openai": {
      "apiKey": "${MYCODER_EVAL_OPENAI_API_KEY}"
    }
  },
  "modelPresets": {
    "liveEval": {
      "provider": "openai",
      "model": "gpt-5",
      "maxTokens": 512,
      "contextWindowTokens": 128000,
      "temperature": 0
    }
  },
  "agents": {
    "defaults": {
      "modelPreset": "liveEval",
      "fallbackModels": []
    }
  }
}
```

将模型名替换为测试账号实际可用的模型。设置环境变量后，用三个 suite 级硬上限显式运行：

```powershell
$env:MYCODER_EVAL_OPENAI_API_KEY = "<test-key>"
.venv\Scripts\python.exe -m nanobot.evaluation run `
  --suite C:\nanobot-eval\context-live-smoke.json `
  --mode full `
  --output-root C:\nanobot-eval\results `
  --live `
  --config C:\nanobot-eval\config.json `
  --max-requests 40 `
  --max-tokens 200000 `
  --max-wall-seconds 300
```

`--config` 和三个正数上限缺一即拒绝；不会回退读取默认配置，也不允许 fallback model。请求/token/
时间上限覆盖整次 suite，case 自带预算仍作为第二层限制。`basic` scripted case 不支持 live；使用
`context_pressure`、`memory_dependency` 及由这些场景承载的上下文/记忆专项任务。该命令会产生
真实 provider 用量，CI 和普通离线测试不会自动执行。
