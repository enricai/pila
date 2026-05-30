# CLAUDE.md

Guidance for Claude Code working in this repository. Read `docs/DESIGN.md`
before touching architecture; read `docs/IMPLEMENTATION.md` before touching
code surface; read this file first.

## Tech stack

Python 3.10+, stdlib-only orchestrator. The orchestrator shells out
to `claude -p` (Claude Code CLI, on the user's subscription — no API
key) and uses git worktrees for parallel implementer isolation.
`pytest` is the only dev dependency.

**Pila runs inside a container.** The `pila` launcher shells out to
`nerdctl run` to start a container per run (DESIGN §6 *Worker subtree
termination*). The orchestrator runs as PID 1 inside; every worker
(and every Bash tool call those workers make) lives in the same PID
namespace. On Ctrl-C / SIGTERM / SIGKILL / crash, the kernel reaps
the namespace — the abnormal-exit cleanup guarantee is the container
boundary, not Python signal handling.

Runtime: containerd + nerdctl. On Linux, native. On macOS, via
[Colima](https://colima.run) (a Lima-managed Linux VM). See
`docs/INSTALL.md` for per-OS install steps.

Python is provisioned *inside the container* by the image (Python 3
from Debian 12). The launcher itself is a portable bash script; it
no longer needs `uv` or a host Python. See `docs/IMPLEMENTATION.md`
§0 (install surface) and §0.5 (container shape).

Pila is small (~1600 LOC of Python) and stays small. All control
flow lives in one file: `orchestrator/pila.py`. The launcher and
Dockerfile are the only other moving parts.

## The three-layer rule (load-bearing — read first)

This repo deliberately separates *theory*, *mechanism*, and *code*, and
the layers are **top-down canonical**: each layer derives from and
conforms to the one above it.

- **`docs/DESIGN.md`** is the architecture and reasoning. It is
  canonical: the implementation spec and the code derive from it. A
  line goes stale here only when the *design* changes.
- **`docs/IMPLEMENTATION.md`** is the code-surface spec — function
  names, cap values, schemas, install steps — derived from DESIGN. It
  defines what the code must implement. It is canonical over the code.
- **The code** is derived from IMPLEMENTATION.md and conforms to it.

Precedence when they disagree:

- DESIGN.md vs IMPLEMENTATION.md → DESIGN.md wins; the spec is the
  defect.
- DESIGN.md vs code → DESIGN.md wins; the code is the defect.
- IMPLEMENTATION.md vs code → IMPLEMENTATION.md wins; the code is the
  defect.

When you change something: change the highest layer that the change
touches *first*, then propagate down. Changing how phases relate?
DESIGN.md, then IMPLEMENTATION.md, then code. Renaming a function or
changing a cap value? IMPLEMENTATION.md, then code. Pure mechanical
refactor that leaves the documented surface intact (rename a local
variable, restructure an unexported helper)? Code only.

If you find drift — the code does something the spec does not describe,
or contradicts what the spec describes — the resolution is *never*
"update the spec to match the code." Either the code is a defect (fix
it to match the spec) or the spec is missing something it should
specify (update the spec first, then verify the code still conforms).

## The central principle: prompts are advisory, code enforces

(`DESIGN.md` §12.) Any guarantee that *matters* and *can be checked
mechanically* lives in `orchestrator/pila.py`, not in a worker
prompt. A prompt can ask for any behavior, but a model can drift; a
real Python check cannot.

Do not move a check from `pila.py` into a prompt to "make the prompt
smarter" — that is the wrong direction. The reverse is correct: a
prompt-level rule that turns out to matter should become a code check
with the prompt downgraded to documentation.

## No subagent spawning

Workers are headless `claude -p` subprocess invocations, not in-session
subagents. The orchestrator is an ordinary Python program. (Constraint
1, DESIGN.md §2.) The Claude Code Agent tool is not available to the
orchestrator and not used anywhere in this repo.

## Mandatory requirements

- **Worker outputs are JSON-schema-validated.** New worker types must
  define a schema in `SCHEMAS` (pila.py:76+) and pass it via
  `--json-schema` in `claude_p()`.
- **Caps are real Python counters in `DEFAULT_CAPS`**, not prompt
  instructions. Adding a new cap means adding a counter and a check, not
  asking a worker to bound itself.
- **All run state goes through the `State` class.** Never write to
  `.pila/state.json` directly — `State.save()` writes a temp file then
  `os.replace()`s it for atomicity. The orchestrator runs on a single asyncio
  event loop, so no lock is needed: coroutines only interleave at `await`
  points and never inside a `st.data[k] = v; st.save()` pair.
- **Source-of-truth answers go through the validation gate in
  `gather_answers`.** Anything reading `answers["source_of_truth"]` can
  trust the value is in `SOURCE_OF_TRUTH_VALUES` (`codebase` /
  `research` / `both`).
- **Don't write to `.pila/` from inside a subtask worktree.** The
  worktree is disposable; coordination state must outlive it. The
  orchestrator writes to `.pila/`; workers commit code to their
  worktree branch only.

## Code style

- **Imports:** stdlib first, then third-party (currently none in
  production code), then local. Alphabetical within each group.
- **Naming:** `snake_case` for functions and variables, `PascalCase` for
  classes, `ALL_CAPS` for module constants.
- **Logging:** `log("...")` for normal output, `die("...", code=N)` for
  fatal exits. Never `print(...)` *except* for the interactive question UI in
  `gather_answers()` — `log()`'s timestamp prefix would mangle a question
  rendered next to `input("  > ")`. Never `sys.exit(...)` directly (use `die`)
  *except* for documented non-error structured exits like
  `EXIT_NEEDS_ANSWERS=10`, where `die()`'s `pila: error:` prefix would
  mislabel a non-error deferred-clarification signal. Both helpers live in
  `pila.py`.
- **Type hints** on every function signature. Use PEP 604 union syntax
  (`str | None`, not `Optional[str]`) — Python 3.10+ is the minimum.
- **Comments explain *why*, not *what*.** Well-named identifiers
  document what; comments are for non-obvious constraints, hidden
  invariants, or workarounds for specific bugs.
- **Functional first.** Pure functions over classes. The `State` class
  is the deliberate exception (encapsulates mutable shared state with a
  lock).

## File layout

```
orchestrator/pila.py    All orchestrator control flow (single file by design)
prompts/*.md                System prompts for each worker type
scripts/*.sh                Git worktree mechanics (setup, integrate, finalize, cleanup)
commands/pila.md        Thin plugin skill — launches the orchestrator
docs/DESIGN.md              Architecture and reasoning
docs/IMPLEMENTATION.md      Current code surface
tests/                      pytest suite
```

## Quick start

```bash
# One-time runtime setup (pila runs in a container — see docs/INSTALL.md):
#   macOS:  brew install colima && colima start --runtime containerd --mount-type virtiofs \
#             --cpu 4 --memory 8   # ~half-host; docs/INSTALL.md explains the auto-sizing
#             # Also add the swap-provision YAML block from docs/INSTALL.md
#             # "Memory pressure: swap configuration" to ~/.colima/default/colima.yaml.
#   Linux:  install containerd + nerdctl from your distro (apt, dnf, pacman, etc.)
#
# Install pila (one command — pick one):
#   Inside Claude Code:  /plugin marketplace add enricai/pila
#                        /plugin install pila@enricai-pila
#   From a terminal:     curl -fsSL https://raw.githubusercontent.com/enricai/pila/main/scripts/install.sh | bash
# See docs/INSTALL.md for details.

# Run on a task in the current git repo:
./pila "Fix the login timeout bug and add a regression test"

# Resume after an interruption:
./pila --resume

# Override the default source-of-truth preference (`both`) with an env
# var, the CLI flag, or a per-repo file:
export PILA_SOURCE_OF_TRUTH=codebase   # or: research, both
./pila "task" --source-of-truth codebase
# …or commit a pila.toml at the repo root with: source_of_truth = codebase

# Select the execution runtime (default: local). `fly` routes each worker
# through Fly.io machines instead of local nerdctl containers.
export PILA_RUNTIME=local              # or: fly
./pila "task" --runtime fly
# …or commit a pila.toml at the repo root with: runtime = fly

# Choose the model. Without overrides: judgment workers (classifier,
# planner, reconciler, provision, integrator) default to opus; acting
# workers (implementer, conformer) default to sonnet. Per-worker
# overrides exist via --model-<worker> / PILA_MODEL_<WORKER>. See
# docs/IMPLEMENTATION.md §2 "Model selection" for the full table.
export PILA_MODEL=sonnet               # or: opus, haiku
./pila "task" --model opus
./pila "task" --model-implementer opus --model-classifier haiku

# Dial how persistent each planner/implementer is at building confidence
# before exiting blocked (default 8 rounds; see DESIGN.md §8). CLI flag,
# env var, or `confidence_rounds = N` in pila.toml.
export PILA_CONFIDENCE_ROUNDS=12
./pila "task" --confidence-rounds 12

# Raise the per-run worker-invocation budget (default 60). Same precedence
# as confidence-rounds: CLI > env > pila.toml.
export PILA_MAX_WORKERS=80
./pila "task" --max-workers 80

# Skip the live `claude -p` smoke test during development:
./pila "task" --skip-smoke

# Waive §12 mechanical read-only enforcement on judgment workers
# (use on repos where the planner needs pnpm/tsc/vitest visibility —
# also PILA_DANGEROUSLY_SKIP_PERMISSIONS=1 or
# `dangerously_skip_permissions = true` in pila.toml):
./pila "task" --dangerously-skip-permissions

# Verbosity: default is `stream` (one-line summary per worker event).
# Per-worker .pila/logs/<sid>.log files are always written.
./pila "task" -q       # normal (pre-streaming terse output)
./pila "task" -qq      # quiet (errors + phase boundaries only)
./pila "task" -vv      # debug (raw event payloads + tool I/O)
export PILA_VERBOSITY=normal  # sticky default
```

## Testing

`pytest tests/` from the repo root. Tests cover the deterministic
enforcement functions (`resolve_source_of_truth`, `resolve_runtime`,
`gather_answers` validation gate, `_retryable_failure`,
`check_merge_committed`, `validate_result`, `validate_plan`,
`_validate_run_json`, `_derive_run_status`, `list_paused_runs`)
including a coupling test that the
retry-policy markers match the live check-function strings. The
remote (Fly.io) bash surface — `ensure_image`, `provision_machine`,
`stop_machine`, `decide_teardown`, `resume_machine`, and `lib.sh`'s
`update_run_json` — is tested via bash-harness subprocess tests with
stubbed `flyctl`. No coverage target is set — the suite was
introduced from scratch and a number now would be arbitrary.

The worker invocation path (`claude_p`) is not unit-tested; meaningful
testing requires a stub or live `claude` binary and lives in a separate
end-to-end tier.

## Task completion checklist

Before marking a change complete:

- [ ] Update `IMPLEMENTATION.md` if the change affected code surface
      described there.
- [ ] Update `DESIGN.md` only if the architecture itself changed.
- [ ] `pytest tests/` — all pass.
- [ ] `python3 -c "import ast; ast.parse(open('orchestrator/pila.py').read())"`
      as a static check.
- [ ] `grep -rn <removed-string> .` — confirm no stragglers if the change
      renamed or removed a string used elsewhere.
- [ ] `git diff --stat` — confirm the diff is scoped to what the change
      intended; no collateral edits.
- [ ] `python3 -c 'import json; json.load(open(".claude-plugin/plugin.json")); json.load(open(".claude-plugin/marketplace.json"))'`
      — if either manifest in `.claude-plugin/` was touched, confirm both
      are valid JSON and all referenced skill/command paths still exist.
      The `version` field is duplicated across the two manifests;
      `tests/test_version_flag.py` guards them from drifting.
- [ ] `python3 -c 'import json; [json.loads(l) for l in open(".pila/runs/<run>/calls.ndjson")]'`
      — if the telemetry writer (`capture_llm_call`) was touched, confirm a
      representative run produces a well-formed `calls.ndjson` (each line
      valid JSON with at least `call_type`, `prompt`, and `response` keys).
