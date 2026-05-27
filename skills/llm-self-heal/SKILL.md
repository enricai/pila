---
name: llm-self-heal
description: "Autonomous self-healing loop for centella worker prompts that produced captured failures. For each call_type with failures, runs a measured n=N baseline (unpatched), then iterates: invoke slm-patch-generator subagent → apply proposed patch → replay patched arm → score → check convergence (SUCCESS/PLATEAUED/BUDGET_EXHAUSTED/TIMEOUT/REGRESSED). Writes a healing-<call_type>.md report per call_type with the best patch and the iteration history. Production prompts in prompts/ stay manual — the skill proposes patches with measured evidence."
argument-hint: "<run-id-or-ndjson-path> [--call-type <name>] [--max-iterations <N>] [--n-replays <N>] [--success-threshold <0..1>]"
allowed-tools:
  - Read
  - Write
  - Edit
  - Bash
  - Glob
  - Grep
  - Agent
---

<objective>
Drive the autonomous heal loop for one or more centella `call_type`s that
produced failures in a judge-llm-batch run. The loop iterates:

1. **Baseline** — run n=N unpatched replays per failing sample via
   `claude -p`, score each, establish noise floor.
2. **Loop** — invoke the `slm-patch-generator` subagent to propose a
   minimal patch to the system prompt, apply the patch, replay the
   patched arm, score, check convergence.
3. **Report** — write `<heal-dir>/<call_type>/healing-<call_type>.md`
   with the verdict (SUCCESS / PLATEAUED / BUDGET_EXHAUSTED / TIMEOUT /
   REGRESSED), the best patch found, and the full iteration history.

**Output:** per call_type with failures, a heal report under the run's
`<heal_subdir>/` directory (default `heal-out/`; configurable via
`--heal-dir` / `CENTELLA_HEAL_DIR` / `centella.toml heal_dir`).

Production prompts in `prompts/` are NOT modified by this skill.
Patches are proposed evidence — applying them is a separate manual step.
</objective>

<execution_context>
Arguments parsed from `$ARGUMENTS`:
- First positional: `<run-id>` or path to a `calls.ndjson` file or its
  parent directory. If a run-id is given, the skill resolves
  `.centella/runs/<run-id>/calls.ndjson` and the corresponding heal
  output dir `.centella/runs/<run-id>/heal-out/`.
- `--call-type <name>` (optional): heal only this call_type; default
  heals all call_types that have failing verdicts in the verdict files
  found under `judge-out/`.
- `--verdict-dir <dir>` (optional): where judge-llm-batch wrote its
  verdict JSON files. Defaults to `<run-dir>/judge-out/`.
- `--heal-dir <dir>` (optional): where to write heal-loop state and
  reports. Defaults to `<run-dir>/heal-out/` or the value resolved from
  `CENTELLA_HEAL_DIR` / `centella.toml heal_dir`.
- `--max-iterations <N>` (default 10, `HEAL_MAX_ROUNDS_DEFAULT`):
  hard cap on loop iterations per call_type.
- `--n-replays <N>` (default 5, `HEAL_N_REPLAYS_DEFAULT`): replays per
  arm (baseline or each patched iteration) per failing sample.
- `--success-threshold <0..1>` (default 0.9,
  `HEAL_SUCCESS_THRESHOLD_DEFAULT`): pass-rate target for SUCCESS exit.
- `--plateau-window <N>` (default 3, `HEAL_PLATEAU_WINDOW_DEFAULT`):
  consecutive iterations of small delta → PLATEAUED exit.
- `--plateau-delta <0..1>` (default 0.03, `HEAL_PLATEAU_DELTA_DEFAULT`):
  "small delta" threshold in pass-rate units.
- `--model <alias>` (default `sonnet`, `MODEL_DEFAULT_PER_WORKER["heal"]`):
  model alias passed to `claude -p` for replay arms. Override via
  `CENTELLA_MODEL_HEAL` or `--heal-model` on the main orchestrator, or
  pass `--model` directly to this skill invocation.

All default values match IMPLEMENTATION.md §2 "Heal-loop convergence
parameters".
</execution_context>

<context>
Centella records every `claude -p` worker invocation to
`.centella/runs/<run-id>/calls.ndjson` (one line per call, appended
immediately after each call returns). The judge-llm-batch skill produces
verdict JSON files in `judge-out/`. This skill consumes those verdicts
to close the loop: it replays failing samples against patched prompts
to find a patch that raises the pass rate above the success threshold.

### call_type → prompt-file mapping

Each call_type has exactly one system-prompt source
(IMPLEMENTATION.md §10):

| call_type   | Prompt source |
|-------------|---------------|
| classifier  | `prompts/classifier.md` |
| planner     | `prompts/planner.md` |
| reconciler  | `prompts/reconciler.md` |
| implementer | `prompts/implementer.md` |
| integrator  | `prompts/integrator.md` |
| conformer   | `prompts/conformer.md` |

The heal loop reads the current file from `prompts/` as the base
prompt text for any call_type it heals.

### Heal-loop on-disk state layout

Under `<heal-dir>/<call_type>/`:

```
state.json           — loop state (history, best-so-far, baseline)
iter-<N>/
  patch-request.json   — inputs for the slm-patch-generator subagent
  patch-response.json  — subagent's structured output
  applied-patch.txt    — the patched system prompt text
  arm-results.json     — n-replay results per failing sample
  scores.json          — per-sample per-replay pass/fail verdicts
```

This matches IMPLEMENTATION.md §8 "Coordination directory layout".

### Replay mechanics

Each replay runs `claude -p` with:
- `--append-system-prompt <patched-prompt-text>` (the prompt under test)
- `--json-schema <schema>` (the same schema used for this call_type in
  the live orchestrator, per `SCHEMAS` in `orchestrator/centella.py`)
- `--model <model>` (the heal model alias)
- User content from the captured `user_content` field

A replay **passes** when `structured_output` is present and schema-valid
(`parsed_ok=true` equivalent). Schema validity is the primary pass
criterion — the same bar the live orchestrator uses.
</context>

<workflow>

## Step 1: Pre-flight

- Confirm CWD contains `orchestrator/centella.py` and `prompts/`. If
  not, abort: `llm-self-heal must run from the centella repo root`.
- Resolve the input path. If a run-id string, check
  `.centella/runs/<run-id>/calls.ndjson` exists. If a directory or
  file path, resolve accordingly.
- Confirm `judge-out/` (or `--verdict-dir`) contains at least one
  `*-verdicts.json` file with `pass=false` entries. If none, emit:
  `No failures found — nothing to heal.` and stop.
- Resolve `--call-type`. If absent, collect all call_types that have
  failing verdict entries.
- Estimate total cost: `failures × n_replays × 2 × max_iterations` LLM
  calls. Warn the user if this is large (>50 calls) before proceeding.

## Step 2: Per-call_type loop

For each call_type in scope:

### 2a. Baseline

For each failing sample (those with `pass=false` in the verdict file):
- Extract `system_prompt`, `user_content`, and `call_type` from the
  matching `calls.ndjson` line (join on `call_id`).
- Run `n_replays` unpatched `claude -p` calls using the captured
  `system_prompt` verbatim. Record pass/fail for each.
- Persist per-sample baseline pass rates to
  `<heal-dir>/<call_type>/state.json`.
- Samples that pass at majority vote in baseline are "noise-floor" cases
  — the captured failure didn't reproduce, so patch measurement is
  unreliable for them. Note them in the report.

### 2b. Iteration loop (until convergence or cap)

1. **Read base prompt:** load from `prompts/<call_type>.md`.

2. **Build patch request:** create
   `<heal-dir>/<call_type>/iter-N/patch-request.json` with:
   - `call_type`
   - `current_prompt` (the current best prompt text — base on iter 0,
     the last applied patch on subsequent iterations)
   - `failing_samples` (array of `{call_id, user_content,
     captured_response, judge_rationale}` for samples that still fail)
   - `prior_attempts` (array of `{iter, anchor, replacement, strategy,
     pass_rate}` from `state.json` history)

3. **Invoke patch-generator subagent:**
   ```
   Agent({
     subagent_type: "slm-patch-generator",
     description: "Propose patch for <call_type> iter-<N>",
     prompt: "<contents of patch-request.json formatted as delimited sections>"
   })
   ```
   Write the subagent's structured output to
   `<heal-dir>/<call_type>/iter-N/patch-response.json`.
   The subagent emits `{anchor, replacement, strategy, pivot_reason}`.
   If the output is not valid JSON, retry once with a reminder to emit
   only the JSON envelope.

4. **Apply patch:** substitute `anchor` → `replacement` in the current
   prompt text. Write the resulting prompt to
   `<heal-dir>/<call_type>/iter-N/applied-patch.txt`.
   If `anchor` is not found verbatim in the current prompt, skip this
   iteration and log a warning — do not apply a patch that cannot be
   cleanly located.

5. **Replay patched arm:** for each failing sample, run `n_replays`
   `claude -p` calls against the patched prompt. Record pass/fail.
   Write to `<heal-dir>/<call_type>/iter-N/arm-results.json` and
   `scores.json`.

6. **Score and check convergence:** compute pass rate across all
   non-noise-floor samples for this iteration.
   - `pass_rate ≥ success_threshold` → **SUCCESS**, stop.
   - Iterations within `plateau_window` all have `|Δpass_rate| <
     plateau_delta` → **PLATEAUED**, stop.
   - `iter == max_iterations` → **BUDGET_EXHAUSTED**, stop.
   - Pass rate dropped vs. prior best by > `plateau_delta` for
     `plateau_window` consecutive iters → **REGRESSED**, stop.
   - Otherwise → **CONTINUE**.
   Update `state.json` with this iteration's result and best-so-far.

### 2c. Write report

Write `<heal-dir>/<call_type>/healing-<call_type>.md` with:

```markdown
# Heal report: <call_type>

**Verdict:** SUCCESS | PLATEAUED | BUDGET_EXHAUSTED | TIMEOUT | REGRESSED
**Best pass rate:** <P>% (iter <N>)
**Baseline pass rate:** <B>%
**Iterations run:** <N>

## Best patch

**Anchor:**
```
<anchor text>
```

**Replacement:**
```
<replacement text>
```

**Strategy:** <one-line description>

## Iteration history

| iter | pass_rate | delta | verdict |
|------|-----------|-------|---------|
| 0 (baseline) | <B>% | — | — |
| 1 | <P1>% | <Δ> | CONTINUE |
...

## Notes

<noise-floor note if any samples didn't reproduce>
<structural-scoring warning if applicable>
```

## Step 3: Emit summary

For each call_type processed:

```
[<call_type>] iterations=<N> verdict=<X> best_pass_rate=<P>% → <heal-dir>/<call_type>/healing-<call_type>.md
```

Then stop. Do not apply any patch to `prompts/`.

</workflow>

<safety_constraints>
- This skill reads NDJSON captures, verdict JSON, and prompt files
- This skill writes only into `<heal-dir>/<call_type>/`
- This skill does NOT modify `prompts/*.md` or `orchestrator/centella.py`
- Patches are proposed with measured evidence — applying them is a
  separate manual step the user performs after reviewing the report
- Replays make real `claude -p` calls using the user's subscription
- The skill warns the user before starting if the estimated call count
  is large (>50 calls)
</safety_constraints>

<example_invocations>

Heal all call_types with failures in a run:

```
/centella:llm-self-heal fix-login-timeout-bug-b81e90
```

Heal only the implementer call_type:

```
/centella:llm-self-heal fix-login-timeout-bug-b81e90 --call-type implementer
```

Heal with tighter budget:

```
/centella:llm-self-heal fix-login-timeout-bug-b81e90 \
  --call-type planner \
  --max-iterations 5 \
  --success-threshold 0.85
```

</example_invocations>
