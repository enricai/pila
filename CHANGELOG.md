# Changelog

All notable changes to Pila will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- **Ctrl-C is now resumable.** Earlier versions treated SIGINT as an
  explicit "throw this away" gesture and ran a full purge — worktrees,
  branches, and the run dir all deleted, `--resume` impossible.
  Ctrl-C now follows the same conservative contract as every other
  abnormal exit: worktrees are torn down (re-created idempotently on
  resume), state.json + branches + checkpoints all survive. The
  explicit full-purge gesture is `scripts/cleanup.sh --run-id <id>
  --branches`. README, DESIGN.md §6, IMPLEMENTATION.md §5, and the
  signal-cleanup pin test are updated to match.
- **`max_total_workers` default 40 → 60.** Empirically (May 2026)
  18-subtask runs hit the cap mid-conformance, aborting with
  `worker budget exhausted`. Structural budget for an 18-subtask plan
  is ≈ 1 classifier + 2 planners + 1 reconciler + 18 implementers +
  ~18 conformers + a few continuations / integrators ≈ 45–55 workers
  worst-case; the new default leaves margin without inviting runaway
  cost. `PILA_MAX_WORKERS` env var and `max_workers` in
  `pila.toml` are new escape hatches (same precedence as
  `--confidence-rounds`: CLI > env > TOML > default).
- **Protected-path scope narrowed.** The diff-scope check that gates
  implementers and conformers previously rejected any write under
  `.claude/` wholesale. It now protects only `.pila/`, `.git/`,
  and top-level `.claude/` files (`settings.json`,
  `settings.local.json`); the three documented Claude Code
  user-deliverable subtrees (`.claude/agents/`, `.claude/commands/`,
  `.claude/skills/`) are exempt. Pila's own self-healing skill
  instructs downstream consumers to write subagent files at
  `.claude/agents/<name>.md`; the over-broad protection previously
  blocked the very pattern the skill teaches. DESIGN.md §9,
  IMPLEMENTATION.md, and `prompts/conformer.md` are updated to match.
- **`--no-clarify` is now `--clarify`; no-questions is the new
  default.** The flag's polarity is inverted: by default pila runs
  without surfacing intent questions to the user. The classifier's
  codebase→research filter still runs and the implementer applies the
  same filter before any mid-execution decision — "no questions" never
  means "skip the rigor." Pass `--clarify` (or set
  `PILA_CLARIFY=true` / `clarify = true` in `pila.toml`) to
  opt into surfacing the questions that survive the filter.
- **Clarification filter is DRY-ed across the prompts.** The wording
  shown to workers now lives in a single shared fragment
  (`prompts/_clarification_filter.md`), included into
  `prompts/classifier.md` and `prompts/implementer.md` at load time
  by a new `load_prompt()` helper in `orchestrator/pila.py`.
  Previously the same filter was restated three times and could
  drift. Worker-facing text now also pushes back explicitly on the
  base model's training prior to ask questions liberally — ~90% of
  apparent intent questions are closable by deeper investigation.

### Added

- **Rate-limit-aware hard exit with optional auto-resume.** Pila now
  detects the Claude Code subscription session-limit message
  (`"You've hit your session limit · resets <time> (<tz>)"`) in worker
  output, and the protocol-level `rate_limit_event` whose `status`
  field falls outside the known-allowed set
  `{"allowed", "allowed_warning"}` (defensive match against future
  terminal status strings — Anthropic's terminal value is
  internal/unobserved). Either signal raises a new `RateLimitedExit`;
  main() runs the worktree-only cleanup (state + branches preserved)
  and, when the reset clause parses unambiguously (text path:
  wall-clock + IANA tz; protocol path: Unix `resetsAt` timestamp),
  sleeps until the reset moment + 30s margin then `os.execvp`'s the
  launcher with `--resume --run-id <id>` for a fresh orchestrator
  process. The `--max-workers` budget is NOT reset across the re-exec
  (it persists via state.json's `worker_count`) so a run that
  repeatedly hits the rate-limit still respects the user's cap. When
  the parse fails (malformed
  time, unknown timezone, future format change), pila exits with code
  75 and prints the manual resume command — never a wrong-time sleep.
  CLI-only overrides on the original launch (`--model`,
  `--max-workers`, etc.) are *not* propagated across the re-exec; set
  them via env (`PILA_*`) or `pila.toml` if you want them to survive.
  Empirical anchor: the verbatim message text matched identically
  across three independent runs in May 2026, and the broad
  `"rate-limit"` pattern false-matches legitimate worker text
  discussing rate-limit code, so the detector keys only on the
  literal marketing-copy prefix.
- **Belt-and-suspenders retry for the
  `incomplete-handoff`-with-missing-checkpoint case.** When the
  rate-limit detector misses (e.g. Anthropic changes the message
  format), the worker's empty-checkpoint envelope previously hit
  `_retryable_failure` and was classified terminal. The retry
  classifier now treats the validate_result line-2314 wording
  (`checkpoint_path '...' does not exist on disk`) as retryable via a
  prefix-match — tight enough that the sibling needs-clarification
  case (line 2350) which shares both substrings stays terminal.
- **Cross-planner file-overlap warning at plan-validation time.** When
  two planners both list the same path in `files_likely_touched`,
  pila now logs a warning right after reconciliation (before the
  scheduler builds the DAG) instead of waiting for the integrator to
  crash mid-wave. Empirically (n=3 historical runs) the signal is
  clean: the one successful run had zero overlaps; both failed runs
  had ≥9. The warning is non-fatal — same-file overlap is sometimes
  legitimate (one planner adds scaffolding the other consumes) — but
  it surfaces the structural risk early. The full autonomous
  resolution (extending the reconciler's action vocabulary to handle
  file-claim conflicts the same way it handles capability-tag
  vocabulary drift) is tracked as follow-up work.
- `PILA_MAX_WORKERS` env var and `max_workers` key in
  `pila.toml` resolve through the new `resolve_max_workers()`
  helper, mirroring `resolve_confidence_rounds()`'s precedence.
  `--max-workers` argparse type is now `_positive_int` (was `int`):
  bad values (0, -1, "nope") are rejected at parse time with a clean
  argparse error instead of falling through to a downstream default.
- `is_protected_path(path)` module-level helper in
  `orchestrator/pila.py` is the new single source of truth for
  what the diff-scope check rejects. `check_diff_scope()` and
  documentation reference it; the previous inline tuple is gone.
- `PILA_CLARIFY` env var and `clarify` key in `pila.toml`
  (same precedence as `--source-of-truth`: CLI > env > file > default
  `False`). New helper `_resolve_bool_pref` factors the resolution
  shape shared with `--no-push` to keep them from drifting.

### Removed

- **All legacy / backwards-compat code paths.** Pila now has **no
  migration path from prior versions** — start fresh. Specifically:
  the `cleanup.sh --legacy` mode and the `.pila/state.json`
  detection guard in `main()` (which together migrated installations
  off the pre-per-run layout) are deleted; the `validate_resume_state`
  check that rejected pre-inversion `no_clarify` state files is
  deleted (legacy state's orphan key now does nothing); the
  `ask`-value-specific rejection tests and doc sentences are deleted
  (the underlying validation gates still reject any unknown value —
  they are not legacy-specific).
- **`ask` source-of-truth value.** The four-value preference
  (`codebase` / `research` / `both` / `ask`) collapses to three.
  Default is now `both` (codebase first; research as fallback) — the
  preference is never surfaced as an interactive question, because
  setting `--source-of-truth` / `PILA_SOURCE_OF_TRUTH` /
  `source_of_truth` in `pila.toml` already expresses an explicit
  intent, and an unset preference implicitly accepts `both`.
  `gather_answers` no longer prompts for source-of-truth or emits the
  `source_of_truth` / `source_of_truth_hint` fields in
  `pending-questions.json`.

### Added

- `reconciler` worker. Spawned by the orchestrator between `phase_plan`
  and `schedule` when parallel planners disagree on capability-tag
  vocabulary across domains. The reconciler resolves the mismatch via
  renames, added `provides`, or new connector subtasks; genuinely
  unresolvable gaps abort the run with the worker's diagnosis instead
  of the prior opaque "nothing provides X" error. Short-circuits with
  no worker invocation when planners already agreed (DESIGN.md §5,
  §14). Reconciler-emitted subtask `id` collisions — both with
  existing subtasks and with other reconciler-emitted ids — now fail
  loud; the prior silent-overwrite path through `schedule()`'s
  dict-flatten would have lost a subtask from the DAG.

### Changed

- **Finalize no longer merges the run branch into the working branch
  locally.** Phase 6 now verifies the run branch is non-empty, pushes it
  to `origin`, and opens a PR via `gh pr create --base <working-branch>
  --head pila/runs/<run-id>`. The working branch is **not** modified
  locally; the PR is the proposed integration. Previously, a successful
  run landed a `pila: integrate completed run into <working-branch>`
  merge commit on the working branch *and* opened a PR with the same
  base, duplicating the same change in two places. `--no-push` still
  skips the push + PR step (the run branch is left local-only; the
  working branch is unchanged). The `scripts/finalize.sh` script is now
  a thin verifier (no `git checkout`, no `git merge`); the two
  post-merge sanity checks in `phase_finalize` are removed (they
  assumed a merge had just happened on HEAD).

- **Per-subtask branches are auto-deleted at finalize.** A new
  `cleanup.sh --subtask-branches` flag (mutually exclusive with
  `--branches`) is now invoked from `phase_finalize` after push+PR. It
  deletes every `pila/subtasks/<run-id>/*` branch and keeps the
  run branch `pila/runs/<run-id>` (the PR head must outlive the
  orchestrator). The per-subtask commits remain reachable from the run
  branch's `--no-ff` merge graph; the per-worker audit trail is now
  `git log pila/runs/<run-id> --graph`. Previously every successful
  run left ~17–20 orphan subtask branches that the user had to delete
  by hand.

- **Model defaults flipped to a judgment-vs-implementation split.**
  Judgment workers (`classifier`, `planner`, `reconciler`,
  `integrator`, `validator`) now default to `opus`; `implementer`
  defaults to `sonnet`. Previously every worker defaulted to `sonnet`.
  The split prioritizes Opus-grade reasoning on the steps where a
  wrong call is most costly (decomposition, conflict resolution,
  cross-domain wiring, criterion judgment) while keeping the
  most-frequently-invoked worker on the cheaper model. **Cost note:**
  Opus is materially more expensive per token than Sonnet; a typical
  run is meaningfully more expensive than before. To restore the
  pre-0.3 all-sonnet behavior in one knob, set `--model sonnet`,
  `PILA_MODEL=sonnet`, or `model = sonnet` in `pila.toml`.
  Per-worker overrides (`--model-<worker>`, `PILA_MODEL_<WORKER>`,
  `model_<worker>`) let you dial individual workers independently.

- `validate_checkpoint()` rejects a wider set of placeholder tokens.
  The single-token noise list now includes `nothing`, `unknown`, `todo`,
  and `pending`, and a normalization step strips trailing `.`/`!`/`…`
  and collapses pure-`?` runs before the membership check — so `None.`,
  `TBD!`, and `???` are caught alongside the bare forms. The two
  "nothing-to-report-is-OK" sections (`Decisions made`, `Open unknowns`)
  continue to accept these. Effect: a previously-accepted thin handoff
  that used any of the new variants now fails the checkpoint validation
  and the orchestrator routes the subtask to `blocked` per the existing
  rule.

### Deprecated

### Removed

### Fixed

- **`phase_finalize` now passes `--run-id` to `cleanup.sh`.** The previous
  bare `cleanup.sh` invocation hit the script's interactive no-arg path,
  which scans for the most-recently-failed run and prompts y/N on stdin.
  The orchestrator runs cleanup non-interactively, so `read -r answer`
  silently saw EOF, the script exited 0 without doing anything, and the
  orchestrator continued past it. Every successful run was leaving its
  full set of subtask worktrees on disk under
  `.pila/runs/<run-id>/worktrees/` despite the "cleanup ran" log
  line. A defense-in-depth pin in `phase_finalize` now asserts the
  invocation includes the run id.

### Security

## [0.2.0] - 2026-05-24

### Added

- Initial public release. Deterministic Python orchestrator for Claude Code;
  six-phase classify → clarify → plan → schedule → execute → finalize
  pipeline; per-wave parallel implementers in isolated git worktrees;
  evidence-gated implement/validate loop; JSON-schema-validated worker
  outputs; resumable state; pytest suite covering deterministic
  enforcement functions.
- Per-worker model selection. Default `sonnet`; override with `--model`
  (sets all five workers) or `--model-<worker>` (per-worker; values:
  `sonnet` / `opus` / `haiku`). Env equivalents `PILA_MODEL` and
  `PILA_MODEL_<WORKER>`; TOML keys `model` and `model_<worker>` in
  `pila.toml`. Resolution order, highest first: per-worker CLI →
  global CLI → per-worker env → global env → per-worker TOML → global
  TOML → default. Invalid values rejected at startup. Models are
  re-resolved on `--resume` (not persisted in state).
- `--source-of-truth` CLI flag for one-off overrides of the
  `PILA_SOURCE_OF_TRUTH` env var and `pila.toml`.

### Changed

- Source-of-truth resolution precedence flipped: env var now beats
  `pila.toml` (and the new `--source-of-truth` flag beats both).
  CLI/env are session-scoped knobs; `pila.toml` is the committed
  repo default.

[Unreleased]: https://github.com/enricai/pila/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/enricai/pila/releases/tag/v0.2.0
