# N00 context and memory baseline

Recorded on 2026-09-06 in `E:/Code/Agent/MyCoder`, branch
`codex/agent-governance`, HEAD `455533169d5a641300dd63d260b1ff5543c4093c`.
The user's implementation authorization supersedes the earlier documentation-only
gate. This delivery covers N00 only and does not enable new context or memory
features. Task/status documents remain controller-owned.

## Evidence

The implementation agent's latest focused run passed **8 tests in 1.03s**, exit 0,
with no warnings. The controller independently confirmed the same code:
**8 passed in 1.01s**, exit 0. The controller also supplied the specified legacy
regression result: **145 passed in 6.27s**, exit 0, no warnings. These are separate
runs, not a combined 153-test execution. Commands, RED failures, isolation audit,
and self-review are in the [N00 report](../../.superpowers/sdd/CONTEXT-MEMORY-V2/N00-report.md).

```powershell
.venv/Scripts/python.exe -m pytest tests/evaluation/test_isolation.py tests/evaluation/test_baseline_runtime.py -q -p no:cacheprovider
```

## Assembly and exact settings

`tests/evaluation/conftest.py` returns frozen
`IsolatedRoots(workspace: Path, runtime: Path, config: Path)` with:

| Value | N00 setting |
| --- | --- |
| workspace | `tmp_path / "workspace"` |
| runtime | `tmp_path / "runtime"` |
| config | `runtime / "config.json"` |
| canonical sessions root | `runtime / "sessions"`; real store adds a workspace namespace |
| legacy sessions root | `runtime / "legacy-sessions"` |
| provider and model | `n00-scripted` |
| temperature / max output tokens | `0` / `128` |
| context window / max iterations | `100000` / `4` |
| timezone | `UTC` |
| workspace restriction | `True` |
| session TTL / idle compact interval | `0` / `0` |
| registry | Empty, or only the actual restricted `ReadFileTool` |
| cron, local triggers, MCP provider | Not attached |
| random seed | Not applicable; responses are scripted and no random scenario generator is used |

The generated JSON contains only the absolute synthetic workspace and
`tools.restrictToWorkspace=true`; it contains no provider credentials.
`NANOBOT_*` environment variables are temporarily removed and restored.

The path is `Nanobot.run -> AgentLoop.process_direct -> AgentRunner` with the
existing `LLMRuntime`, Governor, retry/error conversion, hooks, registry and
session persistence. There is no mocked loop, runner, prompt builder, or memory
store. Only the external provider responses and discovery/configuration boundaries
are substituted. Successful scripts return `LLMResponse`; the failure script
raises a synthetic provider exception to exercise the real `_safe_chat` handling.
The provider deep-copies actual `chat` arguments and receives physical-call records
through the existing LLM call observer. Exhausted scripts fail rather than calling
a live provider; unused responses fail fixture teardown.

Prompt estimation is the explicit synthetic `n00-bytes` estimator: UTF-8 byte
length of JSON containing messages and tool definitions. It is not measured model
usage or a semantic token benchmark. Successful responses without usage may obtain
runtime-generated usage estimates; those must not be reported as real provider
measurements. The tested failed call retains unavailable usage as `None`.

## Four characterization scenarios

| Scenario | Evidence asserted |
| --- | --- |
| Normal reply | Actual provider payload includes the current request and system message, model/generation settings match, SDK returns the scripted reply, and a newly constructed SessionManager reads the persisted assistant reply. |
| Parallel tool-call batch | One assistant response contains `call-alpha` and `call-beta`; the real read tool reads two synthetic files. The next provider payload and SDK transcript contain both matching results with the correct file evidence, and the schema contains only `read_file`. This checks batch pairing, not a timing/speedup claim. |
| Dream cursor | Real MemoryStore appends cursors 1 and 2; preparing a one-entry Dream prompt and writing canonical memory do not advance `.dream_cursor`. Explicitly setting it to 1 survives reconstruction, leaves entry 2 available, and does not change history bytes. |
| Provider-declared context overflow | A synthetic raw exception carrying status 400 passes through the real safe-chat path. The SDK reports an error containing context information; the physical-call record has `finish_reason="error"`, status 400 and unavailable usage. Earlier session messages and the current request remain present. |

Four isolation tests additionally cover local/distinct/frozen roots, canonical
and legacy session targets, synchronous/asynchronous network denial, and config
restoration after an exception and when the previous selector was `None`.

## Limits and concerns

- The overflow case characterizes a provider rejection. It does not demonstrate
  local oversized-input fitting, successful overflow recovery, summary quality,
  checkpoint atomicity, or correct accepted-history cursor accounting. Error
  placeholders and potentially incorrect acceptance bookkeeping are deliberately
  not asserted as required behavior. A future recovery implementation must review
  the finite error script rather than preserve the absence of recovery.
- The cursor case tests MemoryStore behavior, not the full scheduled/manual Dream
  transaction or failure/crash recovery. `set_last_dream_cursor` currently uses
  `write_text`; its passing persistence test is not evidence of atomic durability.
- Scripted results establish control-flow and persistence evidence, not natural
  language quality, automatic memory saving, compression savings, cost reduction,
  or live-model task success. No paid/live evaluation ran.
- Temporary paths, runtime timestamps, built-in skill availability and measured
  durations can vary. There is no claim that entire payloads are byte-identical
  across machines. The provider's estimates are separate from actual usage.
- Network patches cover the exercised Python socket/event-loop entrypoints. They
  are not an OS sandbox or proof against arbitrary native code/subprocess egress.
  External tool plugins are prevented from loading, and executable/network tools
  are not assembled. Windows event-loop self-pipe setup precedes the guard.

## Runtime provenance

Versions were read from the existing virtualenv; dependencies were not changed.

| Component | Observed version |
| --- | --- |
| Python | `3.12.14 (main, Aug 25 2026, 14:01:42) [MSC v.1944 64 bit (AMD64)]` |
| executable | `E:\Code\Agent\MyCoder\.venv\Scripts\python.exe` |
| platform | `Windows-11-10.0.26200-SP0` |
| nanobot-ai | `0.3.0` |
| pytest / pytest-asyncio | `9.1.1` / `1.4.0` |
| pydantic / pydantic-settings | `2.13.5` / `2.15.0` |
| httpx / openai / anthropic | `0.28.1` / `3.8.0` / `0.125.0` |
| tiktoken / filelock | `0.14.0` / `3.32.5` |
| dulwich / loguru / ruff | `0.25.2` / `0.7.3` / `0.16.6` |

The inherited tracked content diff against HEAD consists of the five paths below.
Their byte hashes matched the implementation-time observations and the subsequent
documentation-only read. Git status also listed hook/runtime-event paths; their
observed hashes are preserved here without attributing those user-owned changes
to N00. No existing user diff was edited by the implementation agent.

| Path | SHA-256 of current file bytes |
| --- | --- |
| `.gitignore` | `039e3ca8a5ba30c7aa7546161a708478dc1e1f7c60c16f4dbb011101b350ee9b` |
| `AGENTS.md` | `b62c0817db53276025c3fe33dac0ac713560b214e67e33e5fc7d775d26af96b1` |
| `nanobot/agent/loop.py` | `2fc0c22a6547894577d93b3d5c277c8e313fcc46910eb99b3b7bb8d3c3504cbd` |
| `nanobot/agent/runner.py` | `4d2c326b2b40f15a688a512bbe7d8ede5c143c192a8fc9e8d703a7e88fce9b3d` |
| `webui/package-lock.json` | `8262a4f14a9f797abb86ce88ec3555763889950fff37bca53230cec280594e77` |
| `nanobot/agent/hook.py` | `5b491fff1a14a58cdb01067e90216985402cd1b908472473be2961f6f0fbd930` |
| `nanobot/bus/runtime_events.py` | `8a9b617aeec9b0cbb3faeacde5fadaa14945974aa4e99133c7d874d7aca5a16c` |
| `tests/bus/test_runtime_events.py` | `838678853bd10a0405fcf4f2fac6b1580f76d185f3abf014b7b76be809534d42` |

Observed tracked-diff fingerprint:
`a7f2308d6d81a31eb0686999803d604bb3a180a2648b9eb699863366cb6b3468`.
This is SHA-256 of UTF-8 encoding of PowerShell's line-joined
`git diff --no-ext-diff --binary HEAD` stdout plus one trailing LF. It excludes
untracked specification/N00 files and is not a raw binary stdout hash. The
per-file byte hashes above provide the unambiguous preservation evidence.

N00 executable-source hashes, read after the final focused run and rechecked
during documentation completion:

| Path | SHA-256 |
| --- | --- |
| `tests/evaluation/conftest.py` | `c39ea2cdfa6900c62f6ef494bec751e44886e03aee1825439909c813a8345077` |
| `tests/evaluation/test_isolation.py` | `5f8742f25bf81833cab345ac9240d3bdf906a4bed438628a9d62b8b797d8dd18` |
| `tests/evaluation/test_baseline_runtime.py` | `184e8db3dbff73198e56ae74f0366af22f7535fa2500fb31048013fc65341d86` |
