# Context / memory offline evaluation

This is the offline evaluation gate for N00/N01/N01A and the completed N10
combination/ablation work in spec/TASKS.md. It exercises the production context
and explicit-memory mechanisms in isolated roots, but does not enable them in
production or make live-model quality claims.

## Run

From the MyCoder checkout, with the existing environment:

```powershell
.venv/Scripts/python.exe -m nanobot.evaluation run --suite tests/evaluation/fixtures --mode baseline --output-root .superpowers/evaluation/baseline
.venv/Scripts/python.exe -m nanobot.evaluation compare --baseline <suite-results.json> --candidate <suite-results.json> --format markdown
```

Each run creates a new suite directory and independent workspace/runtime/config/session
roots per case. result.json and hash-only trace.json sit beside the case workspace;
suite-results.json contains the batch. No user output directory is cleared or reused.
Exit codes: 0 all passed, 1 any failure/error/budget exhaustion/missing capability,
2 invalid input/configuration. Compare accepts suite or individual result files and
defaults to JSON. It refuses unequal case/config hashes or incompatible model,
render and dependency evidence. Code revisions may differ. Deltas are candidate minus baseline.

Supported modes are baseline, observe, context-only, memory-only, full and the
no-l1/no-l2/no-l3/no-l4 ablations. N05 wires observe to the real ContextGovernor
lifecycle and records observe_placeholder=false.
Trace context_manifests contains each logical request's latest sanitized snapshot;
payload hashes link all matching physical worker request IDs (not an assumed one-to-one mapping).
Mode wiring is explicit: a case whose declared capability is unavailable reports
not_implemented or fails its verifier, never a pass. Context and memory modes use
separate case roots and keep the same request, tool and workspace safety limits.
Live calls are hard-disabled pending separate authority; configuration/limits
cannot silently select a real provider.

## Fixed suites

| Fixture | Cases | Actual scope |
| --- | ---: | --- |
| basic.json | 1 | Real SDK smoke, not semantic quality |
| context_matrix.json | 12 | 4/12/24 history messages × 2/10 unrelated notes × short/long request |
| memory_dependency.json | 36 | 3 categories × 4 instances × on/off/irrelevant views |
| tool_context.json | 3 | Oversized first-read, artifact SHA-256 paged roundtrip, L3 duplicate-version read |
| explicit_memory.json | 1 | Model-selected memory_save, durable receipt/canonical validation, fresh-session recall |
| memory_negatives.json | 3 | No-save, secret and unsupported/unsafe save decisions |
| dream_conflict.json | 1 | Explicit save interleaved with stale Dream candidate |
| recovery.json | 1 | Canonical visibility after a controlled runtime restart seam |
| existing_safety.json | 1 | Non-vacuous denied workspace side-effect attempt |

Total: 59 cases.

Reference: local pico 473de5dd05512efff651ecfa15a278f9a22614c8. Exact source locations
and rejected shortcuts are in spec/EVALUATION-CASES.md. No pico code is imported
and no pico benchmark numbers are reused.

Context cases place an early field constraint, middle deployment value and current
replica correction. They require an actual JSON edit preserving its existing
version and an unrelated file. Noise is calibrated against the real ContextBuilder,
schema and estimator to approximately 0.5/0.9/1.3 × 6656 input tokens. Warm-up turns
run successfully with a 131072 window; only then does the existing runtime setter
select 8192 (512 output + 1024 margin). Persisted warm-up history is measured again
before the probe. No acceptance checkpoint is forged; the last acknowledgement
is not claimed as a separately consumed input.

The measured pressure ratio gates case validity (absolute tolerance 0.08). High-pressure
completed probes must show compression; otherwise scenario_valid=false and status=error,
not a passing quality sample. invalid_scenario_count is separately aggregated. Required
input overflow is an explicit negative control. Before every probe, bounded workspace
snapshots verify all changes, not only one sentinel file. Existing runtime archive files
are exempt from snapshot differences, but model-tool writes to them are not exempt.

Memory A-stage actually reads the source. Between stages, fixture data is preseeded
in the existing USER/SOUL/MEMORY locations. B-stage uses a different session with no
A transcript, in the same isolated case root. Each variant starts fresh.
This is a retrieval baseline, not evidence that an LLM chooses to save memory.
The explicit-memory case separately uses the actual registered memory_save tool,
validates both the canonical entry and durable receipt, then verifies recall in a
fresh session. Negative fixtures have eligible_save_requests=0 and must produce no
commit or false confirmation. Controlled Dream/restart setup kinds are fixed host
operations, not fixture-supplied executable code.

The prompt-sensitive provider receives only actual request messages: no case,
filesystem, verifier or expected answer. EVAL_FACT/EVAL_TASK records are a small
synthetic protocol, not natural-language reasoning. Missing facts trigger a real
source read or an insufficient-evidence response. Summaries retain only records
visible in their requests. Tests remove facts and sources, change source versions,
inject wrong/conflicting memory, overflow required input and fail actual archive
calls. A wrong answer without rereading is a task failure, not a saving.

## Accounting

Physical requests use the existing retry/observer path. Locally assigned request IDs
deduplicate identical usage observations; conflicting observations are rejected.
Worker/summary labels observe the real archive path; bootstrap/probe phases are
separate. Admitted failed calls count; local budget refusal does not invent a call.

Reported input/output/cache fields stay null when unknown; estimates and reservations
are separate. known_total is the known input subtotal. unknown_request_count counts
requests missing any usage field. Script-supplied usage is synthetic, not paid model
consumption. All reports are mechanism_only=true.

Redundant fact reads require the same path, content hash and range as a successful
A-stage source read. Necessary edit-target reads, missing files and changed versions
are excluded. Correctness is reported alongside repeated reads. Errors/budget
exhaustion count as failures; missing capabilities are excluded with explicit coverage.
Unknown samples remain visible. Baseline lacks L1–L4 manifests: layer triggers,
eviction reasons and recovery replay counts remain null, not invented zeros.

Memory accounting includes eligible/missed save requests, unauthorized commits,
false memory confirmations, duplicate explicit entries, stale Dream overwrites and
memory_save tool latency. A successful save requires canonical content plus a matching
durable receipt; answer text alone is never a commit. Safety scenarios separately count
attempted side effects and bypasses, so an empty action set cannot pass. Context-mode
cases aggregate L1–L4 triggers, eviction reasons and recovery replays from sanitized
ContextPlan manifests.

## Limits and isolation

Default limits: 20 requests, 50 attempted tool steps, 60 wall seconds, 100000 reserved
tokens. Context fixtures raise request/token limits for warm-up. Input plus maximum
output is reserved before a request; the whole tool batch before side effects.
Setup and verification count toward wall time. Evidence reads are capped at 1 MiB
and retain universal newline semantics. Cleanup errors do not erase case reports.

Use a dedicated CLI process. Existing global config/environment selectors are patched
temporarily; concurrent cases in one process are rejected. Default plugins, network,
MCP, shell, spawn, cron and channels are disabled. Only real filesystem tools scoped
to the fresh workspace are allowed. This is an in-process offline harness, not an
OS sandbox for arbitrary executable code. Fixtures cannot import Python or run shell
verifiers.

CLI conversation logging is disabled. Traces export hashes, counters, allowlisted
tool names, hashed IDs and sanitized errors. The programmatic API inherits caller
logging: do not embed it in a production Gateway or feed production data. Synthetic
session/workspace bodies remain inside isolated case roots for diagnosis.

Real-model semantic quality, latency and cost benefits require a separately authorized
live evaluation. Offline mechanisms are not a production enablement recommendation.

## First baseline (2026-09-06)

49 executable cases: 45 passed, 4 failed. All four high-pressure context cases failed:
candidate=8653 tokens (budget=6656), first sent request=2899–3075 tokens, labelled
constraint retention=2/3. The deployment value was lost; smaller payloads did not
mean correct task completion. Low/medium pressure cases (3329/5991 tokens) passed.
All 36 memory retrieval variants passed with the source available. This is old
runtime behavior; no production context or memory changes were made to repair scores.

Verification at this checkpoint: 167 pytest tests passed (evaluation + SDK, 48.17s);
Ruff --no-cache clean; basedpyright evaluation 0 errors/warnings/notes.

After review corrections: 51 cases, 46 passed / 4 failed / 1 not_implemented,
invalid pressure samples=0. The extra first-read case passed; reference roundtrip
is unavailable until N06/N10. Latest regression: 172 passed in 53.06s, static checks clean.
The initial 49-case batch was independently repeated with identical case/config hashes
and statuses; JSON and Markdown comparison outputs are preserved in the task report root.

## N10 combined gate (2026-09-06)

The completed suite has 59 cases. To honor the resource gate, the unchanged 58 cases
were run once across baseline/full/no-l1/no-l2/no-l3/no-l4, then the newly added L3
case was run across the same six modes. No single 59-case batch was rerun after adding
that case; the following totals combine those two preserved runs:

| Mode | Combined result | Non-empty mechanism signal |
| --- | --- | --- |
| baseline | 47 passed / 5 failed / 7 not_implemented | Legacy high-pressure and L3 cases fail; new capabilities are explicit gaps |
| full | 59 passed | All context, explicit-memory, Dream/restart and safety verifiers pass |
| no-l1 | 58 passed / 1 error | Artifact roundtrip cannot fit without L1 offload |
| no-l2 | 55 passed / 4 errors | Four high-pressure context cases cannot preserve the required evidence |
| no-l3 | 58 passed / 1 failed | Duplicate file version is not micro-compacted |
| no-l4 | 58 passed / 1 error | Artifact navigation cannot complete across pressure without L4 |

The broad run is under
`C:/Users/Kitorio/Documents/ChatGPT/nanobot学习/.tmp/n10matrix`; the L3 supplement is
under `C:/Users/Kitorio/Documents/ChatGPT/nanobot学习/.tmp/n10layers`. Results record
the same baseline commit `455533169d5a641300dd63d260b1ff5543c4093c`, per-run dirty
diff hashes, per-case config/case hashes, model identity and independent roots. The L3
full record has case hash `19f50d17...`, config hash `ebccdd80...` and one L3 trigger.

Full mode recorded zero missed eligible saves, unauthorized commits, false save
confirmations, duplicate explicit entries, stale Dream overwrites and safety bypasses.
The safety case attempted one denied tool step. The combined full runs made 360 scripted
requests and estimated 1,289,077 input tokens. Actual input/output/cache usage is unknown
for all 360 requests and remains null. Every result uses `evaluation-scripted` and
`mechanism_only=true`; no live provider, production session, paid usage or production
enablement was involved.

Verification for the final implementation: `tests/evaluation` 124 passed; the N02–N09
and specified safety/unchanged-Goal regression group 556 passed and 25 skipped; Ruff
`--no-cache` clean; basedpyright 0 errors, warnings or notes. These results establish
mechanism coverage and safety invariants only. Any real-model semantic-quality, latency
or cost comparison still needs separate user authorization and a bounded live protocol.
