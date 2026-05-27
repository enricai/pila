# Changelog

All notable changes to Centella will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
  `CENTELLA_MODEL=sonnet`, or `model = sonnet` in `centella.toml`.
  Per-worker overrides (`--model-<worker>`, `CENTELLA_MODEL_<WORKER>`,
  `model_<worker>`) let you dial individual workers independently.

### Deprecated

### Removed

### Fixed

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
  `sonnet` / `opus` / `haiku`). Env equivalents `CENTELLA_MODEL` and
  `CENTELLA_MODEL_<WORKER>`; TOML keys `model` and `model_<worker>` in
  `centella.toml`. Resolution order, highest first: per-worker CLI →
  global CLI → per-worker env → global env → per-worker TOML → global
  TOML → default. Invalid values rejected at startup. Models are
  re-resolved on `--resume` (not persisted in state).
- `--source-of-truth` CLI flag for one-off overrides of the
  `CENTELLA_SOURCE_OF_TRUTH` env var and `centella.toml`.

### Changed

- Source-of-truth resolution precedence flipped: env var now beats
  `centella.toml` (and the new `--source-of-truth` flag beats both).
  CLI/env are session-scoped knobs; `centella.toml` is the committed
  repo default.

[Unreleased]: https://github.com/enricai/centella/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/enricai/centella/releases/tag/v0.2.0
