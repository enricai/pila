# Centella — Implementation Reference

> **This document describes the current code, not the design.** It is true only
> against the present state of `orchestrator/centella.py`, the worker prompts,
> and the shell scripts. A change to the code that is not reflected here makes
> *this document* wrong — unlike `DESIGN.md`, which describes the architecture
> and stays correct across reimplementation. When this document and the code
> disagree, the code is authoritative. When this document and `DESIGN.md`
> disagree, `DESIGN.md` defines what *should* be true.
>
> Read `DESIGN.md` first for the *why*; this document is the *what* and *where*.

---

## 1. Repository layout

```
centella/
├── .claude-plugin/plugin.json     plugin manifest
├── centella                        executable entry-point wrapper (chmod +x)
├── orchestrator/centella.py        the orchestrator — all control flow (chmod +x)
├── prompts/
│   ├── classifier.md              Phase 1 worker system prompt
│   ├── planner.md                 Phase 2 worker system prompt
│   ├── implementer.md             Phase 5 implementer worker system prompt
│   └── integrator.md              conflict-resolution worker system prompt
│   (the validator's system prompt is the `VALIDATOR_SYSTEM` constant in
│    `orchestrator/centella.py`, not a file — it is short and has no
│    behavioral tuning that would benefit from out-of-tree editing)
├── scripts/
│   ├── setup-staging.sh           create staging branch + worktree (idempotent)
│   ├── new-worktree.sh            create/reuse a per-subtask worktree
│   ├── integrate.sh               merge a subtask branch into staging
│   ├── finalize.sh                merge staging into the working branch
│   └── cleanup.sh                 remove worktrees (and optionally branches)
├── commands/centella.md            thin plugin skill — launches the orchestrator
├── docs/DESIGN.md                 the theory (architecture and rationale)
├── docs/IMPLEMENTATION.md         this document
├── tests/                         pytest suite (see §10)
├── pytest.ini                     pytest configuration
└── README.md                      top-level user-facing readme
```

Maps to `DESIGN.md`: §3 (architecture / phases), §2 (why a program, not a skill).

---

## 2. Installation and usage

```bash
# From the root of the target git repository:
/path/to/centella/centella "Fix the login timeout bug and add a regression test"

# Resume an interrupted run:
/path/to/centella/centella --resume

# Skip clarification entirely (DESIGN §11). Intent questions from the
# classifier are dropped; the source-of-truth question is satisfied from
# CENTELLA_SOURCE_OF_TRUTH / centella.toml if set, and otherwise defaults
# to `codebase` with a logged warning.
/path/to/centella/centella "task" --no-clarify

# Pre-supply clarification answers:
/path/to/centella/centella "task" --answers answers.json

# Override caps:
/path/to/centella/centella "task" --max-workers 60 --max-parallel 6

# Dial how persistent workers are at building confidence before they exit
# blocked (default: 8 rounds inside each planner / implementer):
/path/to/centella/centella "task" --confidence-rounds 12
export CENTELLA_CONFIDENCE_ROUNDS=12

# Verbosity controls how much per-worker activity surfaces inline.
# Default is `stream`: one-line summary per worker event. -q drops to
# centella's pre-streaming terse output; -qq is fully quiet (errors
# still emit). -vv adds raw payloads. Per-worker .centella/logs/<sid>.log
# files are always written regardless of level.
/path/to/centella/centella "task"        # default: stream
/path/to/centella/centella "task" -q      # normal (pre-streaming)
/path/to/centella/centella "task" -qq     # quiet (errors only)
/path/to/centella/centella "task" -vv     # debug
/path/to/centella/centella "task" --verbosity normal
export CENTELLA_VERBOSITY=stream

# Set the source-of-truth preference globally so centella does not ask
# (or pass --source-of-truth on the command line for a one-off override):
export CENTELLA_SOURCE_OF_TRUTH=codebase    # or: research, both, ask
/path/to/centella/centella "task" --source-of-truth codebase

# Choose the model for all workers (default: sonnet). Use the env var for a
# sticky preference, the CLI flag for a one-off, or centella.toml for the
# committed repo default. Per-worker overrides also exist — see §2.
export CENTELLA_MODEL=sonnet                # or: opus, haiku
/path/to/centella/centella "task" --model opus
/path/to/centella/centella "task" --model-implementer opus --model-classifier haiku

# Recommended backstop for worker auto-compaction
# (Claude Code CLI variable — not consumed by centella itself):
export CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=70
```

Requirements: the `claude` CLI on `PATH` and logged in interactively (no API
key — subscription auth); Python 3.10+; a git repository with `user.email` and
`user.name` configured.

Via the plugin skill, from inside Claude Code:

```
claude --plugin-dir /path/to/centella
/centella <task>
```

### Source-of-truth preference

For feature work, centella needs to know whether to draw conventions from the
codebase, from online research, from both (codebase first; research as
fallback), or to ask the user. Resolution order (highest priority first):

1. **`--source-of-truth`** CLI flag, values `codebase` | `research` | `both` |
   `ask`. Argparse rejects anything else before the orchestrator runs.

2. **`CENTELLA_SOURCE_OF_TRUTH`** environment variable, same value set.

3. **`centella.toml` at the repo root** (committed, so the preference travels
   with the repo). Plain `key=value` syntax:

   ```
   source_of_truth = codebase
   ```

4. **Default `ask`.** When unset, centella surfaces the question on a feature
   task and prints a hint that setting the env var or the per-repo file
   will skip it next time.

An invalid value in env or file is rejected at startup via `die()` — bad
config is caught before any worker spawns.

> The CLI/env > file order reflects that the CLI flag and env var are
> session-scoped knobs (a user reaching for them is making a one-off
> override), while `centella.toml` is the committed default for the repo.

### Confidence rounds

Planners and implementers self-gate on confidence (DESIGN §8) and loop their
evidence-gate up to `confidence_rounds` times before they exit `blocked`.
Default 8. Increase if the user wants workers to push harder on hard
diagnoses; decrease for cheaper, faster runs that accept earlier
escalations.

Resolution order (highest priority first):

1. **`--confidence-rounds N`** CLI flag. Argparse rejects non-positive
   integers.
2. **`CENTELLA_CONFIDENCE_ROUNDS`** environment variable, same value set.
3. **`centella.toml` at the repo root**, `confidence_rounds = N`.
4. **Default `8`** (`DEFAULT_CAPS["confidence_rounds"]`).

An invalid value in env or file is rejected at startup via `die()`. The
resolved value is written into `caps["confidence_rounds"]` and passed in
each planner / implementer's user prompt — the cap is prompt-governed (see
§6 "Worker-internal caps" and DESIGN §13), the user-visible knob is real.

### Verbosity

Controls how much of the per-worker activity surfaces to the
orchestrator log. Per-worker `.centella/logs/<sid>.log` files are
always written with the full raw event stream — verbosity governs
only the *inline* summary lines. Four named levels with stackable
`-v`/`-q` shortcuts, following the clig.dev / cargo / kubectl
convention.

| Level    | Flag             | What you see inline |
| -------- | ---------------- | ------------------- |
| `quiet`  | `-qq` / `--verbosity quiet` | Phase boundaries, final result, errors only |
| `normal` | `-q` | Phase boundaries + per-subtask status changes (centella's pre-streaming behavior) |
| `stream` | `-v` / (default) | `normal` + one-line summary per worker event |
| `debug`  | `-vv` / `--verbosity debug` | `stream` + raw event payloads, tool I/O, schema diffs, retry diagnostics |

Resolution order (highest priority first):

1. **`--verbosity LEVEL`** CLI flag, values `quiet` / `normal` /
   `stream` / `debug`. Argparse rejects anything else.
2. **`-v` / `-vv` / `-q` / `-qq`** shortcuts. These anchor to
   `normal` (not to the resolved default), so `-v` always means
   "show me the streaming feature" and `-q` always means "back to
   the pre-streaming terse output", independent of what
   env-var / TOML defaults are set to.
3. **`CENTELLA_VERBOSITY`** environment variable.
4. **`centella.toml`**, `verbosity = "stream"`.
5. **Default `stream`** (`VERBOSITY_DEFAULT`).

An invalid value in env or file is rejected at startup via `die()`.
Errors always emit at every level (clig.dev "errors emit at every
level" anti-pattern guard) — `quiet` does NOT suppress error
messages, only the per-event chatter.

The resolved value lives on `st.data["verbosity"]` and is
re-resolved fresh on every run, including `--resume` — the user
can dial up or down at resume time without editing state.

### Model selection

Every worker shells out to `claude -p`. The model passed via `--model` to that
subprocess is resolved per worker type, so the same run can use `opus` for
heavy work and `haiku` for cheap work. Valid values: `sonnet` | `opus` |
`haiku` (aliases — the `claude` CLI resolves them to the current model
version). Default: `sonnet`.

Resolution order for each worker type `W` (highest priority first):

1. **`--model-<W>`** CLI flag (e.g. `--model-implementer opus`)
2. **`--model`** CLI flag (sets the global default for this run)
3. **`CENTELLA_MODEL_<W>`** env var (e.g. `CENTELLA_MODEL_IMPLEMENTER=opus`)
4. **`CENTELLA_MODEL`** env var (sets the global default)
5. **`model_<w>`** key in `centella.toml`
6. **`model`** key in `centella.toml`
7. **Default `sonnet`**.

Five worker types, each independently overridable:

| Worker      | env var                       | CLI flag                | TOML key            |
|-------------|-------------------------------|-------------------------|---------------------|
| (global)    | `CENTELLA_MODEL`              | `--model`               | `model`             |
| classifier  | `CENTELLA_MODEL_CLASSIFIER`   | `--model-classifier`    | `model_classifier`  |
| planner     | `CENTELLA_MODEL_PLANNER`      | `--model-planner`       | `model_planner`     |
| implementer | `CENTELLA_MODEL_IMPLEMENTER`  | `--model-implementer`   | `model_implementer` |
| integrator  | `CENTELLA_MODEL_INTEGRATOR`   | `--model-integrator`    | `model_integrator`  |
| validator   | `CENTELLA_MODEL_VALIDATOR`    | `--model-validator`     | `model_validator`   |

An invalid value in env or file is rejected at startup via `die()`. CLI
values are validated by argparse `choices=` and rejected with the standard
argparse error.

Models are not persisted in `.centella/state.json`. On `--resume`, models are
re-resolved from the current environment, so changing `CENTELLA_MODEL` between
the original run and the resume is intentional and takes effect.

### The `--answers` file

A JSON object keyed by classifier-assigned question `id`, plus
`source_of_truth` set to `"codebase"`, `"research"`, or `"both"` when the
source-of-truth question was asked:

```json
{ "q1": "answer text", "source_of_truth": "codebase" }
```

Maps to `DESIGN.md`: §11 (clarification procedure).

---

## 3. Worker invocation contract

Each worker is one `claude -p` headless process. Flags used:

| Flag | Purpose |
|------|---------|
| `-p` | non-interactive single-shot |
| `--output-format stream-json --verbose` | streams one JSON event per stdout line as the worker runs; the final `result` event is the envelope (same shape as `--output-format json`'s single output — `cost`, `usage`, `terminal_reason`, `structured_output`). `_invoke` writes raw events to `.centella/logs/<sid>.log` and emits per-event inline summaries gated by `state.json["verbosity"]` |
| `--json-schema <inline>` | the payload schema; serialized inline as a JSON string — a file path is silently ignored (verified against Claude Code 2.1.143) |
| `--append-system-prompt` | injects the worker's role prompt — read from `prompts/*.md` for classifier/planner/implementer/integrator, or the `VALIDATOR_SYSTEM` constant in `centella.py` for the validator |
| `--allowedTools` | tool allowlist; three buckets — **read-only** (`READ_TOOLS`: Read/Grep/Glob/WebSearch/WebFetch) for classifier and planner; **acting** (`ACT_TOOLS`: read-set + Bash/Write/Edit) for implementer and integrator; **run-and-read** (`RUN_TOOLS`: read-set + Bash) for the validator — Bash to execute criteria, no Write/Edit so the prompt's "you do not modify code" rule is enforced mechanically per DESIGN §12 |
| `--max-turns` | per-worker turn cap (values in §6) |
| `--model` | model alias for this worker — `sonnet` / `opus` / `haiku`. Value comes from per-worker resolution (see §2 *Model selection*) |
| `--dangerously-skip-permissions` | acting *and* run-and-read workers (implementer, integrator, validator) — suppresses all permission prompts for unattended Bash and file writes |

`claude_p()` is `async`; every caller awaits it. Internally it awaits
`_invoke()`, which spawns the worker via the `run_proc` helper
(`asyncio.create_subprocess_exec` + `communicate()` with an optional timeout).
Shell scripts in `scripts/*.sh` are invoked via `run_script()`, a thin async
wrapper that resolves the script path and forwards to `run_proc`.

The validated payload is read from `structured_output` on the envelope. On a
missing or schema-invalid payload, `claude_p()` retries once with the violation
quoted into the prompt; a second failure raises `WorkerError`.

`WorkerError` handling by worker type — per DESIGN §7's salvage rule
("salvage if there is something to salvage; abort cleanly otherwise"):
- **implementer** — `run_implementer()` catches it, converts to an
  `incomplete-handoff` result; a fresh implementer continues from the checkpoint.
- **classifier, planner, integrator, validator** — not caught locally;
  propagates to `main()`, which aborts with state saved for `--resume`.

`claude_p()` logs a non-fatal warning when the envelope `terminal_reason` is not
`"completed"` (e.g. `"max_turns"`).

Maps to `DESIGN.md`: §7 (worker contract), §2 (CLI subprocess form).

---

## 4. Phase walkthrough (`centella.py`)

| Phase | Function(s) | What it does |
|-------|-------------|--------------|
| Preflight | `preflight` | git identity, clean working tree, no stale `centella/*` branches or worktrees, live `claude -p` smoke test. Bypassed by `--skip-smoke`; skipped entirely on `--resume` |
| 1 Classify | `phase_classify` | one classifier worker → categories + questions. Returned categories are filtered against the 8-name whitelist in `CATEGORIES` (mirrors DESIGN §4); `die()` if none survive |
| 0 Clarify | `gather_answers` | if questions and interactive: collect; non-interactive: write `pending-questions.json`, exit code 10; `--no-clarify` skips clarification entirely per DESIGN §11 — intent questions dropped, source-of-truth resolved from preference or defaulted to `codebase` with a warning |
| 2 Plan | `phase_plan` | one planner worker per category, awaited concurrently via `gather_or_cancel` (a small wrapper around `asyncio.gather` defined in `centella.py`) under an `asyncio.Semaphore(max_parallel)`; the first worker exception cancels its siblings and propagates to `main()` |
| 3 Schedule | `schedule`, `validate_plan` | merge plans, build the global DAG, Kahn topological sort into waves; cycle → `die()` |
| 4 Setup | `phase_execute` head → `setup-staging.sh` | create staging branch + worktree |
| 5 Execute | `phase_execute`, `settle_subtask`, `integrate_wave`, `validate_wave` | per wave: implementers awaited concurrently via `gather_or_cancel` under a fresh `asyncio.Semaphore(max_parallel)` (separate instance from Phase 2's), then integrate, then re-validate. If any subtask in the wave ends `blocked` or `failed`, `phase_execute` aborts the run *before* `integrate_wave` is called — the blocker is recorded in `state.json` and the run resumes with `--resume` |
| 6 Finalize | `phase_finalize` → `finalize.sh`, `cleanup.sh` | merge staging into working branch; post-merge sanity checks |

`phase_classify` runs before `gather_answers` because the question set depends
on the classification.

Between Phase 3 and Phase 4, `write_plan()` persists the merged plan
(`.centella/plan.json`) and per-subtask spec files
(`.centella/subtasks/<id>.json`), and `detect_test_runner()` scans for a
deterministic test harness (pytest, npm, go, cargo, make) — stored in
`state['test_runner']` for `validate_wave`'s fast path.

Maps to `DESIGN.md`: §3.

---

## 5. Deterministic enforcement points

All in `centella.py`, in execution order. This is the concrete catalogue behind
`DESIGN.md` §12 ("prompts advisory, code enforces").

### Preflight (before any LLM work)
| Check | Catches |
|-------|---------|
| `resolve_source_of_truth()` at startup | invalid value in `centella.toml`, `CENTELLA_SOURCE_OF_TRUTH`, or `--source-of-truth` — caught before any worker spawns, not mid-planner |
| `resolve_models()` at startup | invalid model alias in `centella.toml`, any `CENTELLA_MODEL[_*]` env var, or any `--model[-*]` CLI flag — caught before any worker spawns |
| `git user.email` / `user.name` set | commits would fail silently without identity |
| working tree clean | dirty tree → ambiguous diffs, corrupt merge history |
| no stale `centella/*` branches | name collisions with this run |
| no stale worktrees | branch checkout failures |
| `claude --version` ≥ `MIN_CLAUDE_CLI` (currently `(2, 1, 22)`) | CLI too old for `--json-schema` (introduced for `claude -p` in v2.1.22) — replaces the cryptic "unknown option" message a stale CLI used to produce |
| live `claude -p` smoke test | auth failure or network problem |

`--skip-smoke` bypasses only the live smoke test (used by the test harness); the CLI version check still runs because it is local and read-only, and skipping it would let a stale CLI through to every worker.

### Phase 1 checks — `phase_classify`
| Check | Catches |
|-------|---------|
| classifier-returned categories filtered against the 8-name whitelist `CATEGORIES` (mirrors DESIGN §4) | classifier hallucinating a category outside the eight |
| `die()` if no category survives the filter | a run with no valid domain for any planner |

### Plan validation — `validate_plan` (after scheduling, before persisting the plan)
| Check | Catches |
|-------|---------|
| ids match domain prefix (`bugfix-`, `feat-`, `refactor-`, `perf-`, `test-`, `deps-`, `config-`, `docs-`) | cross-domain collisions, audit ambiguity |
| no `size: large` subtasks | planner violated the sizing constraint |
| no empty `success_criteria_seed` | implementer has no criteria starting point |
| every `depends_on` id exists | dangling edges silently dropped by the scheduler |
| every `requires` tag has a provider | unresolvable cross-domain dependency |

### Per-subtask checks — in `settle_subtask`, every worker result
| Check | Catches | On failure |
|-------|---------|-----------|
| `validate_result()` cross-field invariants | `complete` with empty/failing criteria; `handoff` with no checkpoint file; `blocked` with no blocker; `failed` with no summary; `needs-clarification` with no `clarification_question` or no `checkpoint_path` | **Terminal** |
| `validate_result()` criteria file exists | fabricated `criteria_results`, no real criteria file | **Terminal** |
| `check_branch_has_commits()` | `complete` claim, nothing committed | **Retryable** |
| dirty worktree check | uncommitted changes that vanish on integration | **Retryable** |
| `verify_criteria_lock()` — before every re-invocation | criteria file changed after the hash was stored | raises `WorkerError`, run aborts |
| `lock_criteria()` | stores the sha256 of the criteria file on first settled result | — |

**Proposal-only criteria revision (DESIGN §9, both halves):**

| Check | Catches | On failure |
|-------|---------|-----------|
| `_proposal_structurally_valid()` — when implementer result includes `criteria_revision_proposal` | empty fields; evidence that cites no real path in the worktree | **Rejected**: criteria file unchanged, lock unchanged, rejection logged to `state.json["criteria_revisions"]` |
| `apply_criteria_revision()` + `record_criteria_revision()` — when proposal passes | (n/a: this is the approval path) | **Approved**: new criteria file written, lock re-hashed to match, approval logged; if the implementer's status was `failed`, one retry against the revised criteria is granted (`revision_retries`, hardcoded cap 1) |

The orchestrator approves only on the **structural minimum** — non-empty
proposed text + evidence that references at least one path which actually
exists in the worktree — per DESIGN §12: code judges only what can be
checked mechanically, never semantic merit. A reviewer wanting stronger
judgment reads `state.json["criteria_revisions"]` after the run.

| `check_diff_scope()` | `.centella/` `.git/` `.claude/` in the diff | **Terminal** (protected path); scope-volume warning is non-fatal (triggered when `files_likely_touched` is non-empty *and* touched > max(3× expected, 5), or when touched > 15 regardless of the planner's estimate) |
| `validate_checkpoint()` — on `incomplete-handoff` | required section missing; required section empty/whitespace; required section contains only a placeholder token (`none`/`n/a`/`tbd`/…); a path listed under `## Files touched` no longer exists in the worktree and is not flagged `[deleted]` | returns `blocked` |
| `_retryable_failure(summary)` — on `status='failed'` returned by the worker itself | worker self-report of failure | routed through the retry policy using the worker's `summary` as the reason; because `summary` is freeform text it almost never matches a retryable marker, so in practice a self-reported `failed` is **terminal** on first occurrence |

### Wave-level checks (after integration, before validation)
| Check | Catches |
|-------|---------|
| `check_criteria_files_exist()` | missing criteria files, before spending validation workers |
| test-runner short-circuit | a passing deterministic runner (pytest/npm/go/cargo/make) skips the LLM validator |
| `scan_conflict_markers()` | unresolved `<<<<<<<` markers in staging after integration |

On a re-validation failure round, the orchestrator re-runs `settle_subtask`
for each failing subtask (which may produce additional fixing commits) and
then `integrate.sh` to re-merge the delta into staging, before the next
round of validation. The cap on this loop is `wave_revalidation_rounds`
(5) — exceeding it aborts the run with the failing subtask ids.

### Post-integrator checks (after an integrator handles a conflict)
These verify the integrator honored DESIGN §6's *behavioral* conflict-
resolution contract — the integrator prompt itself
(`prompts/integrator.md`) carries the behavioral spec (read every
involved subtask's intent and frozen criteria, preserve each side's
intent, call irreconcilable cases a `design-conflict`); the
orchestrator only checks the outcome.

| Check | Catches |
|-------|---------|
| `check_merge_committed()` | integrator returned `resolved` but left the worktree mid-merge (`MERGE_HEAD` present) or with staged-uncommitted changes — **terminal**: merge aborted, run stops |
| `check_integrator_commit()` | integrator merge commit touched `.centella/` files — non-fatal warning, recorded to `state.json` |
| integrator status `design-conflict` / `failed` | unresolvable conflict — **terminal**: in-progress merge aborted, staging left clean at last good wave, diagnosis saved, run stops |

### Post-finalize checks
Both are **non-fatal warnings** (logged, not `die()`) — the user is told to
verify manually; the run does not abort.

| Check | Catches | On failure |
|-------|---------|-----------|
| most-recent merge subject contains `'centella:'` (read from `git log --merges -1 --format=%s HEAD`) | finalize merged to the wrong branch | non-fatal warning |
| `git diff --stat centella/staging..HEAD` empty | merge silently dropped changes | non-fatal warning |

### Resume integrity — `validate_resume_state()`
Enforces (one half of) DESIGN §6's "staging is the resume contract"
invariant — state.json's `waves`/`completed_waves` say *which* wave to
resume; the never-reset `centella/staging` branch holds *the work* every
prior wave produced. Both must be coherent for resume to be safe.

On `--resume`: asserts `task` is present and non-empty; asserts `waves`,
`completed_waves`, `subtask_status` are well-formed *if present*. `waves` is
intentionally optional — a run interrupted before scheduling has none, and
`main()` handles that case with a clearer message. Rejects corrupt or
hand-edited state without rejecting a legitimately-early interruption.

`orchestrate()` also re-resolves the source-of-truth preference on every
`--resume` and overwrites `state.json`'s `source_of_truth_pref` with the
fresh value, so a change to `centella.toml` or `CENTELLA_SOURCE_OF_TRUTH`
between runs takes effect on resume.

Per-worker models are likewise re-resolved on every `--resume` from the
current CLI flags, env, and `centella.toml`. They are *not* persisted in
`state.json` (they are startup config, not run state), so a change to
`CENTELLA_MODEL`, `--model`, or the per-worker overrides between runs
takes effect immediately on resume.

### Concurrency model
The orchestrator runs on a single `asyncio` event loop. Each `claude -p`
worker is spawned via `asyncio.create_subprocess_exec` (wrapped by the
`run_proc` helper) and awaited; parallel workers within a wave run
concurrently via `gather_or_cancel` — a small `asyncio.gather` wrapper that,
on the first exception, cancels every other in-flight task and awaits its
finalization before re-raising — under an `asyncio.Semaphore` bounded by
`max_parallel`. Because every mutator runs on the single loop, `State` carries
no lock — coroutines only interleave at `await` points, which never fall inside
a `st.data[k] = v; st.save()` pair. `State.save()` still writes to a temp file
then `os.replace()` for atomicity against process crash. On `KeyboardInterrupt`,
`asyncio.run` cancels pending tasks; `run_proc`'s catch-all `BaseException`
handler kills any in-flight child process before re-raising, so no `claude`
or `git` subprocesses are orphaned.

---

## 6. Caps and their values

Defaults in `DEFAULT_CAPS` and the per-worker `claude_p` call sites.

### Code-enforced caps (the orchestrator counts these)
| Loop | Cap | On cap |
|------|-----|--------|
| subtask continuations (re-spawns of an implementer for the same subtask — both context-exhaustion handoffs *and* mid-execution clarifications consume from the same budget) | 3 (`subtask_continuations`) | return `blocked`; fatal at wave boundary |
| corrective retries of a *retryable* failure per subtask (`failed_retries`) | 1 | return `failed` |
| wave staging re-validation rounds | 5 | abort run, name failing subtasks |
| total worker invocations per run | 40 (`--max-workers`) | abort, state saved for `--resume` |
| concurrent workers within a wave | 4 (`--max-parallel`) | throughput throttle |
| turns per `claude -p` call | per worker (below) | worker stops; implementer → `incomplete-handoff` |
| per-worker wall-clock (`worker_timeout_sec`) | 5400 s (90 min) | worker killed; implementer → `incomplete-handoff` |

`--max-turns` by worker: classifier 20, planner 40, validator 40, integrator
60, implementer 120. For the implementer, 120 turns and 90 minutes both apply —
whichever trips first.

A seventh cap, `revision_retries`, is hardcoded to **1** (not in
`DEFAULT_CAPS`): an implementer that returns `failed` and proposes an
approved criteria revision gets at most one retry against the new
criteria. The literal is intentional — DESIGN §9's lock discipline
exists to prevent a stuck model from weakening its own bar, and a
tunable that allowed repeated revisions would re-open exactly that
loophole. The cap is 1 because DESIGN §9's burden of proof ("hard
evidence") is hard to meet once and harder to meet twice, not because
the architecture forbids >1; promoting it to `DEFAULT_CAPS` would
invite values that defeat the lock.

### Worker-internal caps (prompt-governed — NOT counted by the orchestrator)
These iterate inside one worker; the orchestrator sees only the final result.
The real backstop is the worker's `--max-turns` above.

| Loop | Instructed limit | Instructed outcome |
|------|------------------|--------------------|
| evidence-gate iterations (implementer) | `confidence_rounds` (default 8) | return `blocked` |
| evidence-gate iterations (planner) | `confidence_rounds` (default 8) | emit `status: "blocked"`, empty subtasks, gap analysis |
| validate-against-criteria iterations (implementer) | 5 | return `failed` |

The `confidence_rounds` cap is user-tunable (see §2 "Confidence rounds")
even though the iterations themselves are counted inside the worker. The
guarantee remains prompt-governed per DESIGN §13.

Per DESIGN §10 #1, **granular sizing is the primary defense** against
context exhaustion — these caps are a safety net, not the main path.
If they fire often, the planner is under-decomposing (DESIGN §5); look
there first when handoffs become routine.

Maps to `DESIGN.md`: §13. The code-enforced / prompt-governed split there is
*the* point — do not present the second table as a code guarantee.

### The two-tier retry policy — `_retryable_failure(reason)`
One classifier function decides retryable vs. terminal. It substring-matches
the failure reason; the markers must stay in sync with the strings the check
functions actually emit. The coupling test in
`tests/test_retryable_failure.py` enforces this — if you change a marker
in `_retryable_failure` without updating the matching check string (or
vice versa), the test fails. When adding a new retryable failure mode,
edit `_retryable_failure` and the check function in the same change.

| Failure | Tier | Marker / source |
|---------|------|-----------------|
| branch has no commits ahead of staging | Retryable | `"no commits ahead of staging"` from `check_branch_has_commits` |
| worktree left dirty | Retryable | `"uncommitted change"` from the dirty-worktree check |
| cross-field invariant violation | Terminal | `validate_result` |
| diff touched a protected path | Terminal | `check_diff_scope` |
| worker-level error (timeout, schema-invalid twice) | Terminal | `WorkerError` path |

`settle_subtask` routes every failure through `_retryable_failure` via the
`fail()` helper. Retryable consumes the retry cap; terminal ends the subtask on
first occurrence.

---

## 7. Git worktree mechanics (`scripts/*.sh`)

| Script | Behavior |
|--------|----------|
| `setup-staging.sh` | Creates `centella/staging` **only if absent** — never force-resets it (an existing branch carries completed waves; resetting it would destroy resume state). Records the working branch to `.centella/working-branch` on first run only. Adds the staging worktree if missing. Idempotent — safe on `--resume`. |
| `new-worktree.sh <id>` | Creates `centella/<id>` worktree branched off the current `centella/staging` tip; reuses an existing worktree/branch if present (resume after handoff). Prints the absolute worktree path. |
| `integrate.sh <id>` | From repo root, inside the staging worktree: `git merge --no-ff centella/<id>`. Exit 0 clean; exit 1 on conflict, leaving the worktree mid-merge for an integrator; exit 2 on precondition failure (staging worktree or subtask branch missing) — `integrate_wave` treats exit 2 as fatal via `die()` and does *not* spawn an integrator, since the worktree-less case would fail in confusing ways. |
| `finalize.sh` | Checks out the working branch (recorded by `setup-staging.sh`), merges `centella/staging` into it. On conflict: `git merge --abort`, restore the working branch clean, exit non-zero with manual-merge instructions; staging left intact. |
| `cleanup.sh [--branches]` | Removes all `.centella/worktrees/*`, prunes worktree metadata. Keeps `centella/*` branches as an audit trail unless `--branches` is passed. |

`centella/staging` is never reset once created — this is the invariant `--resume`
depends on. See `DESIGN.md` §6 ("staging is the resume contract").

Maps to `DESIGN.md`: §6.

---

## 8. Coordination directory layout (`.centella/`)

Created in the main repository (not in any worktree — worktrees are disposable).
`setup-staging.sh` git-excludes `.centella/` by appending it to the target
repo's `.git/info/exclude` rather than to the user's tracked `.gitignore`
(we deliberately do not modify files the user has committed).

```
.centella/
├── state.json              run state — see field table below
├── working-branch          the branch finalize.sh returns to
├── plan.json               merged planner output
├── subtasks/<id>.json      per-subtask spec handed to each implementer
├── criteria/<id>.md        frozen success criteria, sha256-locked
├── checkpoints/<id>.md     handoff checkpoints (7-section schema)
├── logs/<sid>.log          per-worker raw stream-json event log (one file
│                           per claude_p invocation by sid; always written
│                           regardless of verbosity; append-only across
│                           handoffs / clarifications)
├── worktrees/staging       the staging worktree
├── worktrees/<id>          per-subtask worktrees
├── pending-questions.json  written when clarification needs a non-interactive relay
├── pending-clarifications.json  written when an implementer hits a §11
│                                mid-execution clarification (non-interactive)
└── answers.json            written by the plugin skill when relaying
                            clarification answers; passed back via --answers
```

`state.json` fields. This table is canonical: every field the orchestrator
writes to `st.data` must appear here, and every field listed here must be
written somewhere in `orchestrator/centella.py`. The coupling test in
`tests/test_state_fields.py` enforces parity in both directions against the
`STATE_FIELDS` tuple in `centella.py`.

| Field | Shape | Purpose |
|-------|-------|---------|
| `task` | str | the task description passed on the command line |
| `started_at` | ISO-8601 str | wall-clock time at run start |
| `finished_at` | ISO-8601 str | wall-clock time at successful finalize |
| `waves` | list[list[str]] | scheduled subtask ids per wave (from `schedule`) |
| `completed_waves` | int | index of the next wave to run (resume cursor) |
| `subtask_status` | dict[str, str] | per-subtask terminal status |
| `criteria_locks` | dict[str, str] | sha256 per subtask — structural enforcement of DESIGN §9 |
| `criteria_revisions` | list[dict] | append-only audit log of every proposed revision (approved and rejected, DESIGN §9 proposal channel) |
| `blocked` | dict[str, str] | per-subtask blocker reason when a wave aborts |
| `worker_count` | int | running total of `claude -p` invocations against `max_total_workers` |
| `telemetry` | dict | calls, cost_usd, input_tokens, output_tokens — printed at run end |
| `categories` | list[str] | classifier output, post-whitelist filtering |
| `classifier_questions` | list[dict] | intent questions the classifier surfaced |
| `answers` | dict[str, str] | user answers to classifier questions (and source-of-truth) |
| `needs_source_of_truth` | bool | whether classifier asked for source-of-truth disambiguation |
| `source_of_truth_pref` | str | resolved preference (`codebase` / `research` / `both` / `ask`) |
| `no_clarify` | bool | whether `--no-clarify` was passed |
| `verbosity` | str | resolved verbosity level (`quiet` / `normal` / `stream` / `debug`); re-resolved fresh on every run, including `--resume`, so the user can dial up or down without editing state |
| `test_runner` | list[str] | detected short-circuit test command |
| `integrator_failure` | dict | unresolvable conflict from `integrate_wave` (non-fatal signal log) |
| `integrator_warnings` | dict[str, str] | non-fatal commit warnings from `integrate_wave` (non-fatal signal log) |
| `scope_warnings` | dict[str, dict] | oversized-diff warnings from `check_diff_scope` (non-fatal signal log) |

`pending-questions.json` (written by `gather_answers` on non-TTY exit, read by
the plugin skill in `commands/centella.md`):

| Field | Shape | Notes |
|-------|-------|-------|
| `questions` | array of `{id, question, why_underivable?}` | the classifier-surfaced intent questions not already in `--answers` |
| `source_of_truth` | bool | true if the user still needs to answer the source-of-truth question |
| `source_of_truth_hint` | string \| null | the env-var/`centella.toml` hint to show the user when `source_of_truth` is true |

`answers.json` (written by the plugin skill, passed back via
`--answers .centella/answers.json`):

| Field | Shape | Notes |
|-------|-------|-------|
| `<question id>` | string | one entry per question id from `pending-questions.json.questions[].id` |
| `source_of_truth` | `"codebase"` / `"research"` / `"both"` | required only when `pending-questions.json.source_of_truth` was true |

The checkpoint schema — seven required sections, enforced by
`validate_checkpoint()`: *Frozen success criteria*, *Current status*, *Files
touched*, *Decisions made*, *Evidence gate status*, *Next action*, *Open
unknowns*. The validator enforces three layers: (a) every section header
must be present; (b) every section must carry non-whitespace content; (c)
the five "must carry handoff context" sections reject single-token
placeholder content (`none`/`n/a`/`tbd`/`—`/`-`/`?`) — the two
"nothing-to-report-is-OK" sections (*Decisions made*, *Open unknowns*)
accept these. When a `worktree_root` is passed, `validate_checkpoint()`
also runs a freshness check: every path listed under *Files touched* must
either still exist in the worktree or carry a `[deleted]` annotation,
catching stale checkpoints whose paths were removed by partial work after
the snapshot was written.

In the same vein, `claude_p()` logs a context-decay warning when a worker
returns at ≥80% of its `--max-turns` budget (`num_turns` from the CLI
envelope). This is a proxy, not a hard guard: the schema only validates
the *shape* of the worker's final output, not whether the reasoning
chain that produced it ran against a healthy context. A 9.x confidence
score from a near-cap worker should be read with appropriate scepticism.
The warning sits alongside the existing `terminal_reason` warning at the
`claude_p` return path.

Maps to `DESIGN.md`: §10 (handoff, coordination-artifact location), §9 (criteria
locking).

---

## 9. Structured-output schemas

`claude_p()` validates each worker's payload against a schema keyed by worker
type. Required fields, current shape:

- **classifier** — required: `categories` (array). Optional: `questions`
  (array of `{id, question, why_underivable?}` — only `id` and `question`
  are required on each question), `source_of_truth_question` (bool). The
  classifier only flags whether the source-of-truth question is relevant;
  the orchestrator's preference resolution (see §2) decides whether to
  actually ask.
- **planner** — required: `domain`, `subtasks`, `status`, `confidence`.
  `status` is the enum `ready` / `blocked` (DESIGN §8 planner gate): when
  the planner's evidence gate could not clear within `confidence_rounds`,
  it emits `blocked` with an empty subtasks list and the gap analysis in
  `confidence.gap_to_close`. `confidence` is the worker-internal self-gate
  object: required keys `task_understanding` (number 1–10),
  `decomposition_quality` (number 1–10), `basis` (string), `falsifiers_tested`
  (array of strings — what would-disprove probes were run and what they
  showed), `contradictions_reconciled` (array of strings — any contradictions
  with the worker's own prior statements, named with the kept version's
  evidence), `gap_to_close` (object with optional `task_understanding` and
  `decomposition_quality` strings — populated when either score is below
  9.0). Optional: `source_of_truth` (enum `codebase` / `research` / `both`).
  The `source_of_truth` enum is *defensive*: the orchestrator does not
  currently consume the planner's echoed value (it reads
  `answers["source_of_truth"]` instead); the enum future-proofs against a
  future consumer reading a garbled value. Each subtask is `{id, title,
  success_criteria_seed (all required), intent, scope_note,
  files_likely_touched, depends_on, requires, provides, size,
  investigation_notes}`. `size` is `small` or `medium` — `large` is
  rejected by `validate_plan`. The schema's required-ness of `confidence`
  and `status` is the structural part of DESIGN §8's discipline: a worker
  that skipped self-gating fails its own JSON schema before the orchestrator
  reads the payload.
- **implementer** — required: `subtask_id`, `status` (`complete` /
  `incomplete-handoff` / `blocked` / `failed` / `needs-clarification`).
  Optional: `branch`, `criteria_results` (array of
  `{criterion, met, evidence}`), `confidence` (worker-internal self-gate,
  not consumed by the orchestrator: required keys when present are
  `root_cause` and `solution` (numbers 1–10), `basis` (string),
  `falsifiers_tested` (array of strings), `contradictions_reconciled`
  (array of strings), and `gap_to_close` (object with optional
  `root_cause` and `solution` strings — populated when either score is
  below 9.0); see DESIGN §8 for the disciplines these fields make
  mechanically required), `checkpoint_path`, `blocker`, `summary`,
  `clarification_question` (DESIGN §11 mid-execution exception channel:
  `{id, question, why_underivable}` — all three required when the
  object is present; emitted only with `status: "needs-clarification"`,
  required to carry `checkpoint_path` as well so the work-in-progress
  survives the question to the user; orchestrator surfaces the question
  through the same interactive/non-interactive paths used by the
  Phase-1 classifier),
  `criteria_revision_proposal` (DESIGN §9 proposal channel:
  `{proposed_text, evidence}` — both required when the object is present;
  orchestrator decides via the structural-minimum check described in §5).
- **integrator** — required: `incoming_subtask`, `status` (`resolved` /
  `design-conflict` / `failed`). Optional: `resolution_summary`,
  `diagnosis` (read as a fallback for `resolution_summary` when
  diagnosing a non-`resolved` outcome).
- **validator** — required: `results` (array of `{subtask_id,
  all_criteria_met (both required), failing?}`). `failing` is optional in the
  schema; when omitted, the orchestrator treats it as an empty list.

Schemas are embedded as Python dicts in `centella.py` and serialized inline.

Maps to `DESIGN.md`: §7.

---

## 10. Verification status of the code

Mirrors `DESIGN.md` §15, at the code level.

**Tested.** A pytest suite under `tests/` exercises the deterministic
enforcement functions:

| Test file | Function under test |
|-----------|----------------------|
| `test_resolve_source_of_truth.py` | `resolve_source_of_truth()` |
| `test_resolve_models.py` | `resolve_models()` — per-worker precedence (CLI > env > TOML), defaults, validation, empty/whitespace handling |
| `test__read_toml_key.py` | `_read_toml_key()` — the shared `centella.toml` line parser used by both resolvers |
| `test_gather_answers_validation.py` | the source-of-truth validation gate in `gather_answers()` |
| `test_retryable_failure.py` | `_retryable_failure()`, **including a coupling test** that the retryable markers actually appear in the strings emitted by `check_branch_has_commits` and the inline dirty-worktree check |
| `test_state_fields.py` | `STATE_FIELDS` tuple parity, in both directions: against the §8 field table, and against every `st.data[...] = …` / `setdefault(...)` write in `centella.py`. This is the mechanism §8's "this table is canonical" claim relies on |
| `test_validate_plan.py` | `validate_plan()` (every rule in §5) |
| `test_validate_result.py` | `validate_result()` (every status-branch invariant) |
| `test_check_merge_committed.py` | `check_merge_committed()` (real-git fixtures) |
| `test_criteria_revision.py` | `_proposal_structurally_valid()`, `apply_criteria_revision()`, `record_criteria_revision()` (DESIGN §9 proposal channel) |
| `test_validator_tools.py` | `RUN_TOOLS` composition and `validate_wave`'s wiring — pins that the validator gets `Bash` but never `Write`/`Edit`, enforcing the DESIGN §12 "you do not modify code" rule mechanically (per §3 of this document) |

Run with `pytest tests/` from the repo root. The suite completes in
under two seconds end to end.

**CI surface.** GitHub Actions runs three independent workflows on every
pull request to `main` (and on pushes to `main`):

| Workflow | What it does |
|----------|--------------|
| `.github/workflows/test.yml` | `pytest tests/ -ra` across Python 3.10 / 3.11 / 3.12, with `pytest-cov` reporting line coverage to the job summary (no gate per CLAUDE.md). Coverage XML is uploaded as a 7-day artifact from the 3.12 job. Dev dependencies (`pytest`, `pytest-cov`) installed inline per CLAUDE.md's "pytest is the only dev dependency" stance. |
| `.github/workflows/syntax.yml` | The AST parse from CLAUDE.md's task-completion checklist, plus the same parse over every file under `tests/`. Path-filtered to `orchestrator/**/*.py` and `tests/**/*.py` for fast feedback ahead of the full pytest matrix. |
| `.github/workflows/shellcheck.yml` | `shellcheck -x scripts/*.sh` — the worktree mechanics scripts are load-bearing (DESIGN §6). Path-filtered to `scripts/**/*.sh`. |

Each workflow has a `concurrency:` block keyed on `github.ref` with
`cancel-in-progress: true`, so a force-push or rapid pushes do not
leave superseded jobs in flight. Dependabot (`.github/dependabot.yml`)
tracks the GitHub-Actions ecosystem on a weekly cadence.

**Not tested.** No worker has run against a live `claude -p`. The flag
contract in §3 is from CLI documentation, not from observed runs. The
worker invocation function (`claude_p`) is not unit-tested because
meaningful testing requires a stub or live `claude` binary — that's a
separate end-to-end tier.

First real step: one run on a throwaway repo with a small, fully-specified
task.
