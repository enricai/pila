#!/usr/bin/env python3
"""
Pila — deterministic task orchestrator for Claude Code.

Runs entirely on the Claude Code CLI / subscription. Every unit of LLM work is
a `claude -p` headless invocation. This script owns ALL control flow — phase
sequencing, wave scheduling, caps, retries, integration — in real Python, so
the orchestration cannot drift the way an LLM-driven controller can.

Each worker is a separate `claude -p` process, so there is no subagent nesting
anywhere. The script is the orchestrator; each `claude -p` call is a leaf.

Usage:
    pila "<task description>"
    pila --resume
    pila "<task>" --answers answers.json
    pila "<task>" --clarify             # opt into surfacing intent questions

Run it from the root of the target git repository.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from tenacity import (
    AsyncRetrying,
    RetryCallState,
    RetryError,
    retry_if_result,
    stop_after_delay,
    wait_exponential_jitter,
)

ROOT = Path(__file__).resolve().parent.parent       # pila plugin/repo root
PROMPTS = ROOT / "prompts"
SCRIPTS = ROOT / "scripts"


def _read_version() -> str:
    """Single source of truth: `.claude-plugin/plugin.json`'s `version`
    field. Read at every `main()` invocation (the f-string in the
    `--version` action evaluates eagerly), not at import time. The
    manifest is part of the distributed plugin, so a missing /
    malformed file means the install is broken and a clear runtime
    error is the right outcome."""
    return json.loads(
        (ROOT / ".claude-plugin" / "plugin.json").read_text()
    )["version"]

# `{{include: _foo.md}}` placeholder pattern used by load_prompt() to embed
# a shared prompt fragment into a worker prompt. Only files prefixed with
# `_` are eligible — that prefix marks an internal include, never a
# standalone worker prompt. One level deep; no recursion needed today.
_PROMPT_INCLUDE_RE = re.compile(r"\{\{\s*include:\s*(_[a-z0-9_]+\.md)\s*\}\}")


def load_prompt(name: str) -> str:
    """Read prompts/<name>.md and expand any {{include: _foo.md}}
    placeholders by inlining the named fragment. Replaces the prior
    `(PROMPTS / f"{name}.md").read_text()` pattern so the
    clarification-filter wording can live in one place
    (prompts/_clarification_filter.md, included by both the classifier
    and the implementer prompts). See DESIGN.md §11."""
    raw = (PROMPTS / f"{name}.md").read_text()
    return _PROMPT_INCLUDE_RE.sub(
        lambda m: (PROMPTS / m.group(1)).read_text(), raw)

# Minimum `claude` CLI version that supports `--json-schema` in `claude -p`
# mode. Anthropic CHANGELOG v2.1.22 (2026-01-28): "Fixed structured outputs
# for non-interactive (-p) mode." Earlier 2.1.x point releases may work but
# have no positive evidence in the release notes; v1.x and v2.0.x do not
# have the flag at all. Enforced at preflight by _check_claude_cli_version().
MIN_CLAUDE_CLI = (2, 1, 22)

# --- tunable caps --------------------------------------------------------
DEFAULT_CAPS = {
    "max_total_workers": 60,        # hard ceiling on claude -p invocations
    # Concurrent workers within a wave. Lowered from 4 to 2 because the
    # subprocess fan-out *inside* each `claude -p` worker (Bash tool, the
    # Task tool's background-job pattern, toolchain children like vitest
    # pools / webpack workers / tsc) is unbounded — the only orchestrator-
    # side knob that bounds total in-flight memory load is the worker count.
    # At max_parallel=4 a typical Next.js repo can run 3+ concurrent Node
    # toolchain processes (each 1-2 GiB RSS) before pila even notices, which
    # is exactly the load profile that OOM'd the finalmemoriam run.
    # max_parallel=2 keeps the worst-case peak within reach of a 16 GiB VM.
    # Users with larger VMs / lighter toolchains can opt up via --max-parallel.
    # Per-worker cgroup containment (DESIGN §6 *Memory containment*) is the
    # other half of the fix: with both, an OOM stays inside one worker's
    # cgroup instead of cascading to sshd / lima-guestagent.
    "max_parallel": 2,              # concurrent workers within a wave
    # Per-subtask re-spawn budget. Consumed by BOTH context-exhaustion
    # handoffs and DESIGN §11 mid-execution clarifications — a subtask
    # that mixes the two is still bounded by this single cap, so "ask
    # instead of research" cannot win extra budget. See DESIGN §11
    # mid-execution clarification subsection.
    "subtask_continuations": 3,
    "failed_retries": 1,            # re-spawns of a failed implementer
    # Orchestrator-level conformer re-runs per subtask (DESIGN §9 *Post-
    # work conformance*). Bounds the loop in `settle_subtask` that re-spawns
    # the conformer when its output is malformed or residuals remain.
    # Exhausting this cap is a *warning*, not a failure — the phase is
    # advisory and never produces a `failed` / `blocked` subtask status.
    "conformance_rounds": 2,
    "worker_timeout_sec": 5400,     # 90 minutes per worker process
    # If a worker emits no stdout events for this many seconds, log a
    # warning naming the worker, its PID, the elapsed silence, and any
    # stderr tail. Observation-only — does not kill the worker. The
    # 90-min `worker_timeout_sec` remains the only kill. Surfaces the
    # silent-hang failure class that otherwise gives the user zero
    # feedback between phase start and the 90-min hard kill.
    "worker_idle_warn_sec": 300,
    # Worker-internal evidence-gate iterations for planner and implementer
    # (DESIGN §8 + §13). User-tunable via --confidence-rounds /
    # PILA_CONFIDENCE_ROUNDS / pila.toml; see IMPLEMENTATION.md §2
    # "Confidence rounds". The orchestrator does not count these iterations
    # — the cap is passed into each worker's prompt and the worker bounds
    # itself. Surfacing the knob is for tuning persistence, not for
    # promoting a prompt-governed limit to a code guarantee.
    "confidence_rounds": 8,
    # Per-worker cgroup v2 memory cap (bytes). Each `claude -p` worker is
    # enrolled in its own child cgroup at /sys/fs/cgroup/pila-w-<sid>/ and
    # the cgroup's memory.max is set to this value. When a worker's tool
    # subtree (vitest, tsc, webpack workers, etc.) tries to allocate past
    # the cap, the kernel OOM-kills inside the cgroup — sshd / pid 1 /
    # other workers in the container are unaffected. This is the fix for
    # the OOM cascade from the finalmemoriam run (kernel ring on the
    # Colima VM showed agetty → journald → sshd → lima-guestagent killed
    # because a vitest worker blew past 1.85 GB RSS inside the
    # container's single memcg). Resolved at runtime by
    # resolve_worker_memory_max — CLI > env > pila.toml > default. The
    # default value of None means "auto-derive from /proc/meminfo at run
    # start" (see _auto_worker_memory_max).
    "worker_memory_max_bytes": None,
    # Per-worker cgroup v2 PID cap. Catches runaway fork-bomb behavior
    # from a worker's tool subtree. 256 is generous — webpack + vitest
    # pools regularly hit 50+ PIDs per worker, but a runaway shell loop
    # is in the thousands.
    "worker_pids_max": 256,
    # Auth/quota backoff budget. When `claude -p` returns an envelope
    # whose `api_error_status` is 401/429 or whose result message names
    # an auth/rate-limit failure, `claude_p()` retries with tenacity's
    # `wait_exponential_jitter(initial=15, max=120, jitter=5)` until
    # this many cumulative seconds have elapsed, then bails with a
    # WorkerError that names the Claude Code subscription cap. 300 s
    # (~4 retries: 15 + 30 + 60 + 120 = 225 s plus jitter) is enough
    # to ride out a brief gateway hiccup but small enough that a real
    # 5-hour subscription cap surfaces to the user quickly rather than
    # tying the run up overnight. See IMPLEMENTATION.md §3 *Auth/quota
    # backoff* and §6 caps row.
    "auth_retry_max_sec": 300,
}

# Every key the orchestrator writes to `st.data`. Canonical alongside the
# `state.json` field table in IMPLEMENTATION.md §8 — drift in either
# direction is caught by tests/test_state_fields.py.
STATE_FIELDS = (
    "task", "started_at", "finished_at",
    "waves", "completed_waves", "subtask_status",
    "blocked",
    "worker_count", "telemetry",
    "categories", "classifier_questions", "answers",
    "needs_source_of_truth", "source_of_truth_pref", "clarify",
    "dangerously_skip_permissions",
    "verbosity", "inspect_dirs",
    "integrator_warnings", "scope_warnings",
    "conformance",
    "provision",
    # external_preconditions: planner-declared `extent: external` requires
    # entries collected during phase_reconcile (DESIGN §5
    # `requires.extent`). Persisted so write_plan() can surface them in
    # plan.json's `preconditions` section. Empty list when no planner
    # declared any external requirement — the common case.
    "external_preconditions",
    # current_phase carries the orchestrator's active phase string so the
    # `_memory_sampler` (telemetry sidecar at memory.ndjson) can correlate
    # RSS growth with the code path that produced it. Updated at each
    # phase_* entry. Empty string before phase 1.
    "current_phase",
    # dropped_subtasks: subtasks soft-dropped by filter_offtree_subtasks
    # because their files_likely_touched resolved off-tree (most commonly
    # into an inspect-dir mount). Map of sid → {reasons: [str], files:
    # [str]}. Empty/absent when no drop fired. Audit trail only — the
    # run proceeds with the surviving subtasks.
    "dropped_subtasks",
)

CATEGORIES = [
    "feature-implementation", "bug-fixing", "refactoring",
    "performance-optimization", "testing", "dependency-migration",
    "configuration-build", "documentation",
]

# Short abbreviations used in the run_id branch-name prefix (DESIGN §6
# "The run identifier"). Every entry in CATEGORIES must have an abbrev —
# enforced by tests/test_run_id.py::test_category_abbrev_coverage.
CATEGORY_ABBREV = {
    "feature-implementation": "feat",
    "bug-fixing": "bugfix",
    "refactoring": "refactor",
    "performance-optimization": "perf",
    "testing": "test",
    "dependency-migration": "deps",
    "configuration-build": "config",
    "documentation": "docs",
}

# Paths an implementer (or conformer) may never write to. `.pila/` and
# `.git/` are coordination-only — the run state lives in the former, git
# plumbing in the latter; neither is the implementer's surface. Inside
# `.claude/`, the three documented user-deliverable subtrees are exempt
# (`agents/`, `commands/`, `skills/`) because they ARE legitimate
# deliverables — pila's own self-healing skill, for instance, instructs
# consumers to write a subagent file at `.claude/agents/<name>.md`.
# Top-level `.claude/` files (`settings.json`, `settings.local.json`,
# any future per-session state) stay protected — they are coordination
# and config, not deliverable customizations. See DESIGN §9.
_PROTECTED_PREFIXES = (".pila/", ".git/")
_CLAUDE_DELIVERABLE_PREFIXES = (
    ".claude/agents/", ".claude/commands/", ".claude/skills/",
)


def is_protected_path(path: str) -> bool:
    """Return True if `path` is a meta-directory the implementer must not
    write to. See `_PROTECTED_PREFIXES` and `_CLAUDE_DELIVERABLE_PREFIXES`
    for the rule."""
    if any(path.startswith(p) for p in _PROTECTED_PREFIXES):
        return True
    if path.startswith(".claude/"):
        return not any(path.startswith(p) for p in _CLAUDE_DELIVERABLE_PREFIXES)
    return False

_READ_BASE = "Read,Grep,Glob,WebSearch,WebFetch"
# INSPECT_TOOLS is the read-only-with-shell bucket for classifier, planner,
# and reconciler. These workers run in the real repo cwd (not a worktree),
# so the default is that they cannot use --dangerously-skip-permissions.
# Without pre-approval, Bash calls in -p mode are gated by the permission
# system, return is_error=true, and surface as "tool-fail" — even for
# benign commands like `ls foo 2>&1` whose redirection trips the
# multiple-operations splitter. The Bash(<verb>:*) prefix patterns
# pre-approve specific read-only verbs (verified against claude 2.1.150:
# the pattern matcher handles trailing redirection like `2>&1`).
# Write/Edit are deliberately omitted: by default, the §12 "read-only
# worker" contract stays mechanically enforced — anything outside this
# allowlist falls through and is rejected in non-interactive mode. The
# top-level `pila --dangerously-skip-permissions` flag (DESIGN §12 last
# paragraph) is the documented escape hatch: when set, claude_p passes
# --dangerously-skip-permissions to every worker, including the inspect
# bucket; the allowlist still names what the worker can call without
# prompting, but the gate that rejects everything else is lifted.
INSPECT_TOOLS = (
    f"{_READ_BASE},"
    "Bash(ls:*),Bash(find:*),Bash(cat:*),Bash(head:*),Bash(tail:*),"
    "Bash(wc:*),Bash(grep:*),Bash(rg:*),Bash(file:*),Bash(stat:*),"
    "Bash(tree:*),Bash(pwd),Bash(echo:*),"
    "Bash(git log:*),Bash(git show:*),Bash(git diff:*),"
    "Bash(git status),Bash(git branch:*),Bash(git ls-files:*)"
)
ACT_TOOLS = f"{_READ_BASE},Bash,Write,Edit"

# --inspect-dir preference: extra directories to grant the inspect-bucket
# workers (classifier, planner, reconciler, provision) read access to via the
# Claude Code CLI's --add-dir flag. Without this, Read/Grep/Glob and the
# allowlisted Bash verbs in INSPECT_TOOLS are sandboxed to the repo cwd,
# so cross-repo references like "~/src/enric/beacon" fail with "blocked,
# outside allowed working directories". Repeatable on the CLI; env var is
# colon-separated; TOML key is a comma-separated string. Empty by default.
INSPECT_DIRS_ENV = "PILA_INSPECT_DIRS"
INSPECT_DIRS_FILE = "pila.toml"

EXIT_NEEDS_ANSWERS = 10   # emitted when clarification is needed but no TTY

# Source-of-truth preference — see DESIGN.md §11. Resolution order:
# --source-of-truth CLI flag → PILA_SOURCE_OF_TRUTH env var →
# per-repo pila.toml → 'both'. CLI/env are session knobs, so they
# outrank the committed file default. The preference is never surfaced
# as an interactive question: any explicit setting overrides the
# default, and unset means the caller implicitly accepted 'both'.
SOURCE_OF_TRUTH_VALUES = ("codebase", "research", "both")
SOURCE_OF_TRUTH_ENV = "PILA_SOURCE_OF_TRUTH"
SOURCE_OF_TRUTH_FILE = "pila.toml"

# Runtime mode — see IMPLEMENTATION.md §2 "Runtime mode". Resolution order:
# --runtime CLI flag → PILA_RUNTIME env var → per-repo pila.toml → 'local'.
# CLI/env are session knobs and outrank the committed file default.
RUNTIME_VALUES = ("local", "fly")
RUNTIME_ENV = "PILA_RUNTIME"
RUNTIME_FILE = SOURCE_OF_TRUTH_FILE

# Confidence-rounds preference — see IMPLEMENTATION.md §2 "Confidence
# rounds". Resolution order: --confidence-rounds CLI flag →
# PILA_CONFIDENCE_ROUNDS env var → pila.toml → DEFAULT_CAPS
# fallback. The TOML file is shared with source-of-truth and model
# resolution.
CONFIDENCE_ROUNDS_ENV = "PILA_CONFIDENCE_ROUNDS"
CONFIDENCE_ROUNDS_FILE = SOURCE_OF_TRUTH_FILE

# max-workers preference. Same resolution shape as confidence_rounds.
# CLI --max-workers wins; then PILA_MAX_WORKERS env; then max_workers
# in pila.toml; then DEFAULT_CAPS fallback.
MAX_WORKERS_ENV = "PILA_MAX_WORKERS"
MAX_WORKERS_FILE = SOURCE_OF_TRUTH_FILE

# Per-worker memory cap (cgroup v2 memory.max). Same resolution shape:
# CLI --worker-memory-max wins; then PILA_WORKER_MEMORY_MAX env; then
# worker_memory_max in pila.toml; then auto-derive from /proc/meminfo
# at startup. Accepted suffixes: K, M, G, T (case-insensitive, IEC
# binary — 1G == 1024**3 bytes). See _parse_memory_size.
WORKER_MEMORY_MAX_ENV = "PILA_WORKER_MEMORY_MAX"
WORKER_MEMORY_MAX_FILE = SOURCE_OF_TRUTH_FILE

# --no-push preference (DESIGN §6 "Push + PR"): skip the push + open-PR
# step at finalize. Resolution order: --no-push CLI flag → PILA_NO_PUSH
# env → no_push in pila.toml → default False.
# --no-verify is CLI-only (no env/TOML mirror) to match CLAUDE.md's
# "never skip hooks unless asked" principle — env/TOML defaults for
# hook-skipping would dilute the "user explicitly asked" semantics.
NO_PUSH_ENV = "PILA_NO_PUSH"
NO_PUSH_FILE = SOURCE_OF_TRUTH_FILE

# --clarify preference (DESIGN §11): opt into surfacing intent questions
# to the user. Resolution order: --clarify CLI flag → PILA_CLARIFY
# env → clarify in pila.toml → default False. Same precedence and
# parse rules as --no-push; mirrored env+TOML because "ask me questions"
# is a stable per-user preference, unlike --no-verify (a per-invocation
# safety override).
CLARIFY_ENV = "PILA_CLARIFY"
CLARIFY_FILE = SOURCE_OF_TRUTH_FILE

# --dangerously-skip-permissions escape hatch (DESIGN §12). Forces
# every claude -p worker — including the judgment workers that run in
# the real repo cwd — to pass --dangerously-skip-permissions, waiving
# the mechanical §12 read-only enforcement on classifier / planner /
# reconciler / provision. Named identically to the underlying CLI flag
# on purpose: choosing it means the user understands they are removing
# a guardrail. Resolution order: --dangerously-skip-permissions CLI
# flag → PILA_DANGEROUSLY_SKIP_PERMISSIONS env → pila.toml → False.
DANGEROUS_SKIP_PERMS_ENV = "PILA_DANGEROUSLY_SKIP_PERMISSIONS"
DANGEROUS_SKIP_PERMS_FILE = SOURCE_OF_TRUTH_FILE

# --pr-template selector. When the target repo has multiple PR templates
# in a PULL_REQUEST_TEMPLATE/ directory, pick this one by name (the
# basename, with or without .md). When unset, the alphabetically first
# .md in the directory wins. Has no effect when the repo uses a single
# top-level template (pull_request_template.md / .github/...) or when
# no template exists at all. Resolution order: --pr-template CLI flag →
# PILA_PR_TEMPLATE env → pr_template in pila.toml → None.
PR_TEMPLATE_ENV = "PILA_PR_TEMPLATE"
PR_TEMPLATE_FILE = SOURCE_OF_TRUTH_FILE

# Verbosity — see IMPLEMENTATION.md §2 "Verbosity". Four levels with
# stackable -v/-q shortcuts following the clig.dev / cargo / kubectl
# convention. Default is `stream` because the user invoking pila
# is opening to watch; -q drops to pila's pre-streaming behavior;
# -qq goes fully quiet (errors still emit per clig.dev "errors emit at
# every level" anti-pattern guard).
VERBOSITY_VALUES = ("quiet", "normal", "stream", "debug")
VERBOSITY_DEFAULT = "stream"
VERBOSITY_ENV = "PILA_VERBOSITY"
VERBOSITY_FILE = SOURCE_OF_TRUTH_FILE

# Subtask statuses that count as "done" for the progress counter.
_TERMINAL_STATUSES = frozenset({"complete", "failed", "blocked"})

# Model selection — see IMPLEMENTATION.md §2 "Model selection". Aliases
# are passed straight to `claude --model`; the CLI resolves them to the
# current version. Each worker type has independent CLI/env/TOML
# overrides; falls back through global CLI/env/TOML/MODEL_DEFAULT.
MODEL_VALUES = ("sonnet", "opus", "haiku")
# Global default. Used when no per-worker default applies. DESIGN §5 +
# IMPLEMENTATION.md §2: judgment workers (everything except implementer)
# run on Opus by default; implementer's per-worker default is sonnet.
# Users can override globally with --model / PILA_MODEL / `model =`
# in pila.toml, or per-worker with --model-<worker> /
# PILA_MODEL_<WORKER> / `model_<worker> =`.
MODEL_DEFAULT = "opus"
# Per-worker defaults applied *after* user overrides (CLI/env/TOML) but
# *before* the global MODEL_DEFAULT fallback. Only workers that need a
# different default from MODEL_DEFAULT appear here.
MODEL_DEFAULT_PER_WORKER = {
    "implementer": "sonnet",
    "conformer": "sonnet",
    "judge": "sonnet",
    "heal": "sonnet",
    "pr_writer": "sonnet",
}
MODEL_ENV = "PILA_MODEL"
MODEL_FILE = "pila.toml"
# Effort selection — see IMPLEMENTATION.md §2 "Effort selection". The
# `claude -p` CLI exposes `--effort {low,medium,high,xhigh,max}` to dial
# reasoning depth. The CLI exposes no --temperature and no --seed, so
# effort is the strongest determinism dial available; pinning it removes
# the "this run thought harder than that one" axis on judgment workers.
# A worker that resolves to None gets no --effort flag (inherits Claude's
# default) — that is the intended behavior for acting workers.
EFFORT_VALUES = ("low", "medium", "high", "xhigh", "max")
EFFORT_DEFAULT: str | None = None
EFFORT_DEFAULT_PER_WORKER: dict[str, str] = {
    "classifier": "high",
    "planner": "high",
    "reconciler": "high",
    "provision": "high",
    "integrator": "high",
    "pr_writer": "high",
}
EFFORT_ENV = "PILA_EFFORT"
WORKER_TYPES = ("classifier", "planner", "reconciler", "provision",
                "implementer", "integrator", "conformer")
# Post-run skill workers — not in WORKER_TYPES because they don't run inside
# the main orchestrate loop, but they do get dedicated model resolution via
# --judge-model / --heal-model (and their env / TOML mirrors).
MODEL_JUDGE_ENV = "PILA_MODEL_JUDGE"
MODEL_HEAL_ENV = "PILA_MODEL_HEAL"
MODEL_PR_WRITER_ENV = "PILA_MODEL_PR_WRITER"

# Telemetry enabled/disabled — see IMPLEMENTATION.md §2 "Telemetry".
# Resolution order: --telemetry/--no-telemetry CLI → PILA_TELEMETRY env →
# telemetry in pila.toml → True (on by default). NDJSON events land in
# <run-dir>/<telemetry_subdir>/ which is already under .pila/ and thus
# covered by the existing .gitignore exclusion.
TELEMETRY_DEFAULT = True
TELEMETRY_ENV = "PILA_TELEMETRY"
TELEMETRY_FILE = "pila.toml"

# Telemetry event subdir — the directory name appended to <run-dir> where
# NDJSON event files are written. Resolution order: --telemetry-dir CLI →
# PILA_TELEMETRY_DIR env → telemetry_dir in pila.toml → "events".
TELEMETRY_SUBDIR_DEFAULT = "events"
TELEMETRY_SUBDIR_ENV = "PILA_TELEMETRY_DIR"
TELEMETRY_SUBDIR_FILE = "pila.toml"

# Judge output directory name — relative to <run-dir>. Holds LLM judge
# output files. Resolution order: --judge-dir CLI → PILA_JUDGE_DIR env →
# judge_dir in pila.toml → "judge-out".
JUDGE_DIR_DEFAULT = "judge-out"
JUDGE_DIR_ENV = "PILA_JUDGE_DIR"
JUDGE_DIR_FILE = "pila.toml"

# Heal output directory name — relative to <run-dir>. Holds LLM self-heal
# loop output files. Resolution order: --heal-dir CLI → PILA_HEAL_DIR env →
# heal_dir in pila.toml → "heal-out".
HEAL_DIR_DEFAULT = "heal-out"
HEAL_DIR_ENV = "PILA_HEAL_DIR"
HEAL_DIR_FILE = "pila.toml"

# Heal-loop convergence knobs — see IMPLEMENTATION.md §2 "Heal-loop convergence
# parameters". User-tunable knobs use the standard CLI/env/TOML/default
# resolution; non-user-tunable constants (window, delta, n) are fixed here.
HEAL_MAX_ROUNDS_DEFAULT = 10        # max iterations per call_type
HEAL_SUCCESS_THRESHOLD_DEFAULT = 0.9  # pass-rate bar for SUCCESS verdict
HEAL_PLATEAU_WINDOW_DEFAULT = 3     # look-back window for plateau detection
HEAL_PLATEAU_DELTA_DEFAULT = 0.03   # minimum improvement to avoid plateau
HEAL_N_REPLAYS_DEFAULT = 5          # replays per sample per iteration
HEAL_MAX_ROUNDS_ENV = "PILA_HEAL_MAX_ROUNDS"
HEAL_SUCCESS_THRESHOLD_ENV = "PILA_HEAL_SUCCESS_THRESHOLD"
HEAL_MAX_ROUNDS_FILE = "pila.toml"
HEAL_SUCCESS_THRESHOLD_FILE = "pila.toml"




def resolve_prompt(call_type: str) -> tuple[str, str, str]:
    """Return (source_kind, content, location_hint) for a worker call_type.

    source_kind is always 'file' — every worker's system prompt lives at
    `prompts/<call_type>.md`. location_hint is the stable relative path the
    heal loop uses to describe where to apply a patch.

    Raises ValueError for an unknown call_type.
    """
    if call_type not in WORKER_TYPES:
        raise ValueError(
            f"unknown call_type {call_type!r}; valid types: {WORKER_TYPES}"
        )
    hint = f"prompts/{call_type}.md"
    content = (PROMPTS / f"{call_type}.md").read_text()
    return ("file", content, hint)


# --- worker output schemas -----------------------------------------------
# Passed to `claude -p` via --json-schema. The CLI validates the worker's
# final output against the schema AFTER the run and exposes the validated
# object as `structured_output` in the JSON envelope. NOTE: --json-schema
# only accepts an INLINE schema string; a file path is silently ignored
# (verified against Claude Code 2.1.143), so these are embedded here.

# Shared shape for the conformer's build/lint/tests fields — three objects
# with the same {ran, passed, command, summary} schema. Pulled out to keep
# the conformer schema readable.
_CONFORMER_BLT_PROP = {
    "type": "object",
    "required": ["ran", "passed", "command", "summary"],
    "properties": {
        "ran": {"type": "boolean"},
        "passed": {"type": "boolean"},
        "command": {"type": "string"},
        "summary": {"type": "string"},
    },
}

# Shared shape for a single `requires` entry on a planner or reconciler
# subtask. The structural part is in the JSON schema — `tag` + `extent`
# must be present and `extent` is restricted to two values. The
# *conditional* invariant ("`reason` is required and non-empty when
# `extent == 'external'`") is not expressible in vanilla JSON Schema
# without `if/then`, so it is enforced in `validate_plan` instead, per
# CLAUDE.md "prompts are advisory, code enforces." See DESIGN §5
# `requires.extent` for the architectural contract.
_REQUIRES_ITEM = {
    "type": "object",
    "required": ["tag", "extent"],
    "properties": {
        "tag": {"type": "string"},
        "extent": {"type": "string", "enum": ["in_plan", "external"]},
        "reason": {"type": "string"},
    },
}

SCHEMAS: dict[str, dict] = {
    "classifier": {
        "type": "object",
        "required": ["categories"],
        "properties": {
            "categories": {"type": "array", "items": {"type": "string"}},
            "questions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["id", "question"],
                    "properties": {
                        "id": {"type": "string"},
                        "question": {"type": "string"},
                        "why_underivable": {"type": "string"},
                    },
                },
            },
            "source_of_truth_question": {"type": "boolean"},
        },
    },
    "planner": {
        "type": "object",
        "required": ["domain", "subtasks", "status", "confidence"],
        "properties": {
            "domain": {"type": "string"},
            # DESIGN §8 planner gate: a planner whose evidence gate cannot
            # clear within confidence_rounds emits status="blocked" with an
            # empty subtasks list and the gap analysis in
            # confidence.gap_to_close. The orchestrator surfaces a blocked
            # planner as a fatal run condition (the run cannot proceed with
            # no plan); confidence itself remains worker-internal.
            "status": {
                "type": "string",
                "enum": ["ready", "blocked"],
            },
            # Worker-internal self-gate (DESIGN §8 + §12): required at the
            # schema level so a planner that skipped self-gating fails its
            # own JSON validation before the orchestrator sees the payload.
            # The structure is code-enforced; the quality of the artifacts
            # the fields name is model-judged.
            "confidence": {
                "type": "object",
                "required": ["task_understanding", "decomposition_quality",
                             "basis", "falsifiers_tested",
                             "contradictions_reconciled", "gap_to_close"],
                "properties": {
                    "task_understanding": {"type": "number"},
                    "decomposition_quality": {"type": "number"},
                    "basis": {"type": "string"},
                    "falsifiers_tested": {
                        "type": "array", "items": {"type": "string"}},
                    "contradictions_reconciled": {
                        "type": "array", "items": {"type": "string"}},
                    "gap_to_close": {
                        "type": "object",
                        "properties": {
                            "task_understanding": {"type": "string"},
                            "decomposition_quality": {"type": "string"},
                        },
                    },
                },
            },
            "subtasks": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["id", "title", "success_criteria_seed"],
                    "properties": {
                        "id": {"type": "string"},
                        "title": {"type": "string"},
                        "intent": {"type": "string"},
                        "scope_note": {"type": "string"},
                        "files_likely_touched": {
                            "type": "array", "items": {"type": "string"}},
                        "depends_on": {"type": "array", "items": {"type": "string"}},
                        "requires": {"type": "array", "items": _REQUIRES_ITEM},
                        "provides": {"type": "array", "items": {"type": "string"}},
                        "success_criteria_seed": {"type": "string"},
                        "size": {"type": "string"},
                        "investigation_notes": {"type": "string"},
                    },
                },
            },
        },
    },
    "reconciler": {
        # Output of the reconciler worker (DESIGN §5 / §14). Spawned by
        # phase_reconcile after phase_plan when the merged planner output
        # has `requires` capability tags with no matching `provides`. The
        # worker reasons over the full task + merged subtasks + the list
        # of unresolved tags, and emits one of four actions per tag.
        # Each of the four output arrays is optional (any can be empty).
        # The orchestrator applies renames/added_provides/added_subtasks
        # mechanically; any `unresolvable` entry dies the run with the
        # worker's stated reason.
        "type": "object",
        "required": ["renames", "added_provides", "added_subtasks",
                     "unresolvable"],
        "properties": {
            "renames": {
                # Rewrite a `requires` tag on one subtask to match an
                # existing `provides` tag on another. The single most
                # common case (planners picked different words for the
                # same thing).
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["sid", "from", "to"],
                    "properties": {
                        "sid": {"type": "string"},
                        "from": {"type": "string"},
                        "to": {"type": "string"},
                    },
                },
            },
            "added_provides": {
                # A subtask actually produces the needed capability but
                # didn't declare the tag. Add it to that subtask's
                # `provides`.
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["sid", "tag"],
                    "properties": {
                        "sid": {"type": "string"},
                        "tag": {"type": "string"},
                    },
                },
            },
            "added_subtasks": {
                # Genuine gap — propose a new subtask to fill it. Shape
                # mirrors planner-output subtasks (same required fields)
                # plus the `_added_by_reconciler` traceability flag.
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["id", "title", "success_criteria_seed",
                                 "_added_by_reconciler"],
                    "properties": {
                        "id": {"type": "string"},
                        "title": {"type": "string"},
                        "intent": {"type": "string"},
                        "scope_note": {"type": "string"},
                        "files_likely_touched": {
                            "type": "array", "items": {"type": "string"}},
                        "depends_on": {"type": "array", "items": {"type": "string"}},
                        "requires": {"type": "array", "items": _REQUIRES_ITEM},
                        "provides": {"type": "array", "items": {"type": "string"}},
                        "success_criteria_seed": {"type": "string"},
                        "size": {"type": "string"},
                        "investigation_notes": {"type": "string"},
                        "_added_by_reconciler": {"type": "boolean"},
                    },
                },
            },
            "unresolvable": {
                # Gap with no plausible resolution. The orchestrator dies
                # with the worker's `reason` shown verbatim.
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["sid", "tag", "reason"],
                    "properties": {
                        "sid": {"type": "string"},
                        "tag": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                },
            },
        },
    },
    "implementer": {
        "type": "object",
        "required": ["subtask_id", "status", "confidence"],
        "properties": {
            "subtask_id": {"type": "string"},
            "status": {
                "type": "string",
                "enum": ["complete", "incomplete-handoff", "blocked",
                         "failed", "needs-clarification"],
            },
            "branch": {"type": "string"},
            "criteria_results": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "criterion": {"type": "string"},
                        "met": {"type": "boolean"},
                        "evidence": {"type": "string"},
                    },
                },
            },
            # Worker-internal: the implementer prompt uses this as a self-gate
            # ("proceed only when both scores ≥ 9.0"). The orchestrator does
            # not consume it. Kept in the schema — and with required fields
            # for the falsification, drift-reconciliation, and gap-surfacing
            # disciplines — so a worker that skipped self-gating fails its
            # own JSON schema before the orchestrator reads the payload (the
            # structural enforcement called out in DESIGN §8 / §12).
            "confidence": {
                "type": "object",
                "required": ["root_cause", "solution", "basis",
                             "falsifiers_tested",
                             "contradictions_reconciled",
                             "gap_to_close"],
                "properties": {
                    "root_cause": {"type": "number"},
                    "solution": {"type": "number"},
                    "basis": {"type": "string"},
                    "falsifiers_tested": {
                        "type": "array", "items": {"type": "string"}},
                    "contradictions_reconciled": {
                        "type": "array", "items": {"type": "string"}},
                    "gap_to_close": {
                        "type": "object",
                        "properties": {
                            "root_cause": {"type": "string"},
                            "solution": {"type": "string"},
                        },
                    },
                },
            },
            "checkpoint_path": {"type": ["string", "null"]},
            "blocker": {"type": ["string", "null"]},
            "summary": {"type": "string"},
            # DESIGN §11 mid-execution clarification exception. An
            # implementer that hits a genuine intent-question it cannot
            # derive from the codebase or research returns
            # status='needs-clarification' with this object set AND a
            # checkpoint of the work-in-progress; the orchestrator
            # surfaces the question to the user through the same
            # interactive / EXIT_NEEDS_ANSWERS paths used by the
            # Phase-1 classifier. The `why_underivable` field is
            # required for the same reason it is at Phase 1: to keep
            # the worker from drifting toward asking rather than
            # researching.
            "clarification_question": {
                "type": ["object", "null"],
                "properties": {
                    "id": {"type": "string"},
                    "question": {"type": "string"},
                    "why_underivable": {"type": "string"},
                },
                "required": ["id", "question", "why_underivable"],
            },
        },
    },
    "integrator": {
        "type": "object",
        "required": ["incoming_subtask", "status"],
        "properties": {
            "incoming_subtask": {"type": "string"},
            "status": {
                "type": "string",
                "enum": ["resolved", "design-conflict", "failed"],
            },
            "resolution_summary": {"type": "string"},
            "diagnosis": {"type": ["string", "null"]},
        },
    },
    "judge": {
        # Output of a judge worker invocation. Three dimensions mirror the
        # beacon scorer rubric but as an LLM judgment (not a hard-coded rule):
        # schema adherence, factual accuracy, hallucination-freeness. The
        # `passed` field is the aggregate verdict; the caller decides what
        # to do with a failing verdict (log, heal, or both).
        "type": "object",
        "required": ["passed", "dimensions", "rationale", "suggested_fixes"],
        "properties": {
            "passed": {"type": "boolean"},
            "dimensions": {
                "type": "object",
                "required": ["schema_ok", "factual_ok", "hallucination_ok"],
                "properties": {
                    "schema_ok": {"type": "boolean"},
                    "factual_ok": {"type": "boolean"},
                    "hallucination_ok": {"type": "boolean"},
                },
            },
            "rationale": {"type": "string"},
            "suggested_fixes": {"type": "array", "items": {"type": "string"}},
        },
    },
    "conformer": {
        # DESIGN §9 *Post-work conformance*: an advisory worker that runs
        # after the implementer's success path. Schema requires the
        # build/lint/tests objects so a worker that skipped the honesty
        # discipline fails its own JSON gate before the orchestrator reads
        # it; cross-field invariants (residuals require non-empty
        # rules_files_read, fixed-violations cite a rule, updates cite a
        # path) are enforced by validate_conformance_result().
        "type": "object",
        "required": [
            "subtask_id", "rules_files_read",
            "rule_violations_fixed", "rule_violations_residual",
            "docs_updates", "tests_updates",
            "build", "lint", "tests", "summary", "confidence",
        ],
        "properties": {
            "subtask_id": {"type": "string"},
            "rules_files_read": {
                "type": "array", "items": {"type": "string"}},
            "rule_violations_fixed": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["rule", "fix", "evidence"],
                    "properties": {
                        "rule": {"type": "string"},
                        "fix": {"type": "string"},
                        "evidence": {"type": "string"},
                    },
                },
            },
            "rule_violations_residual": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["rule", "why_not_fixed"],
                    "properties": {
                        "rule": {"type": "string"},
                        "why_not_fixed": {"type": "string"},
                    },
                },
            },
            "docs_updates": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["path", "reason"],
                    "properties": {
                        "path": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                },
            },
            "tests_updates": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["path", "reason"],
                    "properties": {
                        "path": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                },
            },
            "build": _CONFORMER_BLT_PROP,
            "lint": _CONFORMER_BLT_PROP,
            "tests": _CONFORMER_BLT_PROP,
            "summary": {"type": "string"},
            # Worker-internal: the conformer prompt asks the worker to run
            # the §8 disciplines (falsifier testing, drift reconciliation,
            # gap surfacing) and record the result here. The orchestrator
            # does not consume the score — it loops on observable signals
            # (residuals, failed build/lint/test) up to `conformance_rounds`.
            # Required at the schema level so a worker that skipped the
            # disciplines fails its own JSON schema before the orchestrator
            # reads the payload (the structural enforcement called out in
            # DESIGN §8 / §12).
            "confidence": {
                "type": "object",
                "required": ["conformance", "basis", "falsifiers_tested",
                             "contradictions_reconciled", "gap_to_close"],
                "properties": {
                    "conformance": {"type": "number"},
                    "basis": {"type": "string"},
                    "falsifiers_tested": {
                        "type": "array", "items": {"type": "string"}},
                    "contradictions_reconciled": {
                        "type": "array", "items": {"type": "string"}},
                    "gap_to_close": {
                        "type": "object",
                        "properties": {
                            "conformance": {"type": "string"},
                        },
                    },
                },
            },
        },
    },
    "patch_generator": {
        # Output of the patch-generator worker. The worker proposes a
        # minimal edit to the system prompt that addresses the observed
        # failure mode. `anchor` and `replacement` are the only required
        # fields; the heal loop validates that `anchor` is a literal
        # substring of the current prompt body before applying the patch
        # (per the prompts-are-advisory-code-enforces principle — the
        # check is in request_patch, not in the prompt).
        "type": "object",
        "required": ["anchor", "replacement"],
        "properties": {
            "anchor": {"type": "string"},
            "replacement": {"type": "string"},
            "strategy": {"type": "string"},
            "pivot_reason": {"type": ["string", "null"]},
        },
    },
    "pr_writer": {
        # DESIGN §6 *Finalization*: LLM-written PR title + body that
        # respects the target repo's PR template when one exists. The
        # launcher prepends "pila: " to the title (the worker must NOT)
        # so pila-opened PRs stay easy to spot in lists. `used_template`
        # is the repo-relative path of the template that was filled out,
        # or null when no template was found.
        "type": "object",
        "required": ["title", "body", "used_template"],
        "properties": {
            "title": {"type": "string", "minLength": 1, "maxLength": 200},
            "body": {"type": "string", "minLength": 1},
            "used_template": {"type": ["string", "null"]},
        },
    },
    "provision": {
        # LLM fallback for per-repo dependency provisioning (DESIGN §6½).
        # Fires only when detect_recipe_from_lockfiles() returns an empty
        # list — Java/Gradle, bare pyproject.toml, polyglot Makefile setups.
        # The recipe is structurally bounded here, then mechanically
        # validated by validate_provision_recipe() (§12 carve-out).
        "type": "object",
        "required": ["recipe"],
        "properties": {
            "recipe": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["kind", "command", "working_dir"],
                    "properties": {
                        # `none` means no install step is needed (pure docs
                        # repo). `install` is the dep-fetch step. `build`
                        # is a follow-on that prepares the workspace
                        # (e.g. `pnpm run build` for an app that only
                        # functions after a build pass). Both kinds are
                        # rendered into the implementer/conformer prompts
                        # by `_format_provision_recipe_section`; the
                        # worker decides whether and when to run each.
                        "kind": {"enum": ["install", "build", "none"]},
                        # argv list (NOT a shell string). argv[0] must be
                        # in the allowlist enforced by
                        # validate_provision_recipe; no shell metacharacters
                        # anywhere in the argv.
                        "command": {
                            "type": "array",
                            "items": {"type": "string"},
                            "minItems": 1,
                        },
                        # `.` or a relative path inside the repo. No
                        # absolute paths, no `..` segments — enforced by
                        # validate_provision_recipe.
                        "working_dir": {"type": "string"},
                        "timeout_s": {"type": "integer", "minimum": 1},
                    },
                },
            },
        },
    },
}


# =========================================================================
# small utilities
# =========================================================================
def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def log(msg: str) -> None:
    print(f"[pila {datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def die(msg: str, code: int = 1):
    print(f"pila: error: {msg}", file=sys.stderr, flush=True)
    sys.exit(code)


class InterruptedBySignal(BaseException):
    """Raised by signal handlers (SIGTERM, SIGHUP) installed in main().
    Inherits BaseException (not Exception) so the broad `except Exception`
    handlers inside orchestrate() don't swallow it. Caught only at
    main()'s top-level try/except, where it triggers worktree-only
    cleanup with state and branches preserved (DESIGN §6).

    SIGINT keeps Python's default KeyboardInterrupt — caught separately
    but follows the same worktree-only-cleanup contract (DESIGN §6
    *Cleanup on abnormal exit*). The explicit "throw this away"
    gesture is `scripts/cleanup.sh --run-id <id> --branches`, not
    Ctrl-C."""
    pass


class RateLimitedExit(BaseException):
    """Raised when claude -p reports the Claude Code subscription
    session-limit / rate-limit has been hit. Inherits BaseException so
    it propagates through asyncio's gather and the broad
    `except Exception` handlers without being swallowed — same pattern
    as InterruptedBySignal.

    Carries:
      - reset_at: datetime | None — parsed from the literal Claude Code
        message format when present. None means "could not parse
        unambiguously"; main() prints the manual --resume command and
        exits without auto-resume rather than guessing a wrong time.
      - raw_message: str — the verbatim message text (or a synthesized
        envelope for the protocol-level rate_limit_event path),
        surfaced to the user on exit.

    See DESIGN §6 *Cleanup on abnormal exit* for the auto-resume
    contract."""
    def __init__(self, reset_at: datetime | None, raw_message: str):
        super().__init__(raw_message)
        self.reset_at = reset_at
        self.raw_message = raw_message


# Literal Claude Code subscription rate-limit message format, observed
# verbatim across three independent runs (barnacle/stackpulse/substack)
# on 2026-05-27. Format:
#   "You've hit your session limit · resets <h>:<mm><am|pm> (<IANA TZ>)"
# Match case-insensitively but require the literal prefix — broader
# patterns false-match legitimate assistant text discussing rate-
# limiting code (a worker iterating on rate-limit handling could
# legitimately write "the hot path is rate-limited"). The prefix is
# Claude-Code-specific marketing copy; no other text plausibly
# contains it.
_SESSION_LIMIT_PREFIX = re.compile(
    r"you've hit your session limit", re.IGNORECASE)
_SESSION_LIMIT_RESET = re.compile(
    r"resets?\s+(\d{1,2}):(\d{2})\s*([ap]m)\s*\(([^)]+)\)",
    re.IGNORECASE)

# Known `status` values for a `rate_limit_event` payload that mean the
# limit has NOT been hit. Anything outside this set is treated as a
# terminal rate-limit signal — defensive against future Anthropic
# status strings ("exceeded", "denied", "blocked", etc.) without
# hardcoding a guess at the terminal value.
_RATE_LIMIT_ALLOWED_STATUSES = ("allowed", "allowed_warning")


def detect_session_limit(text: str) -> RateLimitedExit | None:
    """Return a RateLimitedExit if `text` matches the Claude Code
    session-limit message format, else None. Parse failures of the
    reset clause produce an exit with reset_at=None — the run still
    exits cleanly, just without auto-resume.

    Deliberately strict on the time-parse path: a wrong sleep is worse
    than no sleep, so we only return a reset_at when every step of the
    parse succeeds (regex match, integer conversion of hour and minute,
    range checks on each, AM/PM normalization, ZoneInfo lookup).
    Anything else → reset_at=None and the user gets a manual --resume
    instruction."""
    if not _SESSION_LIMIT_PREFIX.search(text):
        return None
    reset_at: datetime | None = None
    m = _SESSION_LIMIT_RESET.search(text)
    if m:
        hour_s, minute_s, ampm, tz_name = m.group(1), m.group(2), m.group(3), m.group(4)
        try:
            tz = ZoneInfo(tz_name)
            h = int(hour_s)
            mn = int(minute_s)
            if not (0 <= mn < 60):
                raise ValueError(f"minute out of range: {mn}")
            if ampm.lower() == "pm" and h != 12:
                h += 12
            elif ampm.lower() == "am" and h == 12:
                h = 0
            if not (0 <= h < 24):
                raise ValueError(f"hour out of range: {h}")
            now = datetime.now(tz)
            candidate = now.replace(hour=h, minute=mn, second=0, microsecond=0)
            # Reset is always in the future; if the parsed time is
            # earlier than now (or equal), it's tomorrow.
            if candidate <= now:
                candidate = candidate + timedelta(days=1)
            reset_at = candidate
        except (ValueError, ZoneInfoNotFoundError):
            reset_at = None
    return RateLimitedExit(reset_at=reset_at, raw_message=text)


def _install_signal_handlers() -> None:
    """Install SIGTERM/SIGHUP handlers that raise InterruptedBySignal.
    SIGINT is left to Python's default (KeyboardInterrupt). On Windows,
    SIGHUP doesn't exist and SIGTERM behaves differently — best-effort,
    guard with hasattr."""
    def _raise_intr(signum, frame):
        raise InterruptedBySignal(signal.Signals(signum).name)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _raise_intr)
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, _raise_intr)


_PROC_TREE_GRACE_SEC = 2.0


def _enumerate_descendants(root_pid: int) -> set[int]:
    """Return every PID reachable from `root_pid` via PPID links.

    A flat list of (pid, ppid) is enough because PPID points to a process's
    *current* parent — even after one fork-and-detach. POSIX guarantees a
    `ps -eo pid,ppid` snapshot is consistent enough for this; the snapshot
    races a process's reparenting to init, but `_terminate_proc_tree`'s
    SIGTERM-then-SIGKILL-after-grace pattern is robust to that race (any
    process we miss in pass 1 we catch in pass 2 because PPID-walk re-runs
    after the grace window, OR — if it was already reaped — it's gone).

    Returns the set of descendant PIDs *not* including root_pid itself.
    `ps` failures (e.g. no permission) return an empty set: callers fall
    back to the leader-only kill path."""
    try:
        out = subprocess.run(
            ["ps", "-eo", "pid,ppid"],
            capture_output=True, text=True, check=True, timeout=5,
        ).stdout
    except (subprocess.SubprocessError, OSError):
        return set()
    children_of: dict[int, list[int]] = {}
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 2:
            continue
        try:
            pid, ppid = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        children_of.setdefault(ppid, []).append(pid)
    seen: set[int] = set()
    stack = [root_pid]
    while stack:
        p = stack.pop()
        for c in children_of.get(p, []):
            if c not in seen:
                seen.add(c)
                stack.append(c)
    return seen


def _signal_pids(pids: set[int], sig: int) -> None:
    """Best-effort signal delivery to a set of PIDs. Drops ProcessLookupError
    (already dead) and PermissionError (not ours / already reaped). All other
    OSError variants are also swallowed — this is a cleanup path; we cannot
    let signal-delivery failure abort the teardown."""
    for pid in pids:
        try:
            os.kill(pid, sig)
        except (ProcessLookupError, PermissionError, OSError):
            pass


_DESCENDANT_POLL_SEC = 0.5


class _DescendantTracker:
    """Background poller that accumulates every PID ever observed as a
    descendant of `leader_pid` during the leader's lifetime.

    Why this is needed (DESIGN §6): Claude Code's Bash tool uses
    `run_in_background: true` to fire-and-forget long-running commands
    (test runners, builds, dev servers). Each such command is spawned in
    its own POSIX session (detached). The bash wrapper writes the
    background task ID to the worker's stream and exits. When that
    wrapper exits, its children (the actual long-running command) are
    immediately reparented to PID 1 by the kernel.

    Result: by the time `claude -p` itself exits and pila's `_invoke`
    can call a post-hoc `_enumerate_descendants(claude_p.pid)`, the
    backgrounded subprocesses are no longer descendants — they're
    orphans of init. A snapshot taken at exit-time finds nothing.

    Fix: take snapshots THROUGHOUT the worker's life, while the PPID
    chain is still intact, and remember every PID we ever saw. At exit
    time, SIGKILL the accumulated set. Anything that died naturally
    yields ProcessLookupError (swallowed); anything still alive gets
    reaped.

    Polling cost is negligible: ~10ms per `ps` call every 500ms ≈ 2%
    CPU during a worker's run. There is one tracker instance per
    worker; all of them share pila's single asyncio event loop, so
    even with `max_parallel` concurrent workers the polling stays on
    one CPU."""

    def __init__(self, leader_pid: int):
        self._leader_pid = leader_pid
        self._seen: set[int] = set()
        self._task: asyncio.Task | None = None
        self._stopped = False

    def start(self) -> None:
        """Spawn the polling task on the current event loop."""
        if self._task is None:
            self._task = asyncio.create_task(self._poll_loop())

    async def _poll_loop(self) -> None:
        try:
            while not self._stopped:
                descendants = _enumerate_descendants(self._leader_pid)
                self._seen.update(descendants)
                if not descendants and self._seen:
                    # Worker has been alive long enough to spawn at
                    # least one descendant AND that descendant is now
                    # gone (reparented to init, or naturally exited).
                    # Slow down to save ps calls during the worker's
                    # winding-down phase — our accumulated `_seen` set
                    # already holds the orphan PIDs we'll SIGKILL at
                    # stop_and_reap.
                    await asyncio.sleep(_DESCENDANT_POLL_SEC * 2)
                else:
                    # Either descendants ARE present (keep watching at
                    # full rate), or we have NEVER seen one yet (the
                    # leader may not have spawned its first child yet —
                    # slowing down now would miss the first batch as
                    # they appear). Stay at the fast poll rate.
                    await asyncio.sleep(_DESCENDANT_POLL_SEC)
        except asyncio.CancelledError:
            return

    async def stop_and_reap(self) -> int:
        """Stop polling, SIGKILL every accumulated PID, return the
        count signaled. Safe to call multiple times. Always runs at
        worker exit, success-path AND failure-path."""
        self._stopped = True
        if self._task is not None:
            # One final snapshot to catch anything spawned since the
            # last poll cycle (still cheap — one `ps` call).
            self._seen.update(_enumerate_descendants(self._leader_pid))
            # Fire-and-forget cancel: the poll loop notices `_stopped`
            # at its next iteration and exits cleanly. We don't `await`
            # the cancelled task here because doing so would block at
            # an `await` point, and a `CancelledError` propagating from
            # the caller would be silently caught by any local
            # exception handler — breaking asyncio's cancellation
            # contract. The orphaned task is harmless; the event loop
            # reaps it on shutdown.
            self._task.cancel()
            self._task = None
        if self._seen:
            _signal_pids(self._seen, signal.SIGKILL)
        return len(self._seen)


async def _terminate_proc_tree(proc: asyncio.subprocess.Process) -> None:
    """Terminate a subprocess AND every descendant process, then reap.

    Why a PPID-walk (not just `killpg`): Claude Code's Bash tool runs each
    command via `bash -c` started in a *new POSIX session* (own PGID).
    `os.killpg(claude_p_pgid)` does NOT reach those detached subprocesses
    because they no longer share `claude -p`'s process group. The PPID chain
    however stays intact while the parent lives, so a recursive walk through
    `ps -eo pid,ppid` reaches every descendant regardless of how many session
    layers separate them.

    Algorithm: SIGTERM the leader's process group AND every descendant we can
    enumerate; wait the grace window so well-behaved children flush; re-snapshot
    (catches anything spawned mid-teardown OR not visible in pass 1);
    SIGKILL the remainder; reap the leader. Init reaps any descendants we
    cannot, once they exit.

    Idempotent and exception-safe: this runs only from `_invoke`'s and
    `run_proc`'s `except` blocks (abnormal-exit paths). Success-path
    cleanup of detached backgrounded subprocesses (Claude Code's Bash
    tool with `run_in_background: true`) is handled separately by
    `_DescendantTracker`, because by the time a clean `claude -p` exit
    is observable to pila those subprocesses have already reparented to
    PID 1 and are no longer reachable from this helper's PPID walk.
    All signal-delivery races (process already gone, PGID recycled)
    are swallowed; the helper never raises.

    `asyncio.CancelledError` propagates out unhandled, AFTER the SIGKILL pass
    has fired in `finally`. Swallowing cancellation here would silently break
    asyncio teardown — callers' outer `raise` would still fire, but the
    event loop shutdown path expects `CancelledError` to surface."""
    leader_pid = proc.pid
    leader_pgid = proc.pid  # PGID == PID when spawned with start_new_session=True
    # Pass 1: enumerate descendants while parent is still alive (PPID chain
    # intact), then signal everything we found AND the leader's PG.
    descendants = _enumerate_descendants(leader_pid)
    try:
        os.killpg(leader_pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        pass
    _signal_pids(descendants, signal.SIGTERM)

    exited_cleanly = False
    try:
        await asyncio.wait_for(proc.wait(), timeout=_PROC_TREE_GRACE_SEC)
        exited_cleanly = True
    except asyncio.TimeoutError:
        pass
    finally:
        # Re-enumerate. Anything we missed in pass 1 (spawned in the gap,
        # or reparented before we read /proc), AND anything that ignored
        # SIGTERM, gets SIGKILLed here. We always run this pass, even on
        # `exited_cleanly` — the leader may be reaped but its detached
        # grandchildren are not in its PGID, so its exit doesn't take them
        # with it.
        survivors = _enumerate_descendants(leader_pid)
        try:
            os.killpg(leader_pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        _signal_pids(survivors, signal.SIGKILL)
        if not exited_cleanly:
            # Reap the leader so the OS doesn't accumulate a zombie.
            # `shield` keeps the reap running if the caller's task gets
            # cancelled mid-wait; the cancellation still surfaces.
            try:
                await asyncio.shield(proc.wait())
            except (ProcessLookupError, PermissionError, OSError):
                pass


def _cleanup_on_abnormal_exit(st: "State", *, full_purge: bool) -> None:
    """Clean up after an abnormal exit (signal, exception, WorkerError).

    Always: remove every git worktree under `st.run_dir / "worktrees"`,
    then `git worktree prune` to clear stale metadata. Per-worktree
    failures are caught — one bad worktree shouldn't block the others.

    If `full_purge` is True (the user's explicit Ctrl-C gesture):
    additionally delete the run branch (`pila/runs/<run-id>`) and
    every subtask branch (`pila/subtasks/<run-id>/*`), and
    recursively remove `st.run_dir`. The run is gone; `--resume` can't
    recover it.

    If `full_purge` is False (SIGTERM/SIGHUP/exception): state.json and
    the run branch are left intact so `--resume --run-id <id>` can
    continue the run. This is the conservative default for "external
    process killed me, user probably wants to recover.\""""
    if st is None or st.run_id is None:
        return
    worktrees_dir = st.run_dir / "worktrees"
    has_worktrees = worktrees_dir.is_dir() and any(worktrees_dir.iterdir())
    # full_purge requires a log line (it's removing the whole run dir);
    # worktrees-only with nothing to do is silent (e.g., preflight died
    # before setup-run.sh — no worktrees ever existed).
    if full_purge or has_worktrees:
        log(f"cleanup: {'full purge' if full_purge else 'worktrees only'} "
            f"for run {st.run_id}")
    # Remove worktrees. The 240s timeout is calibrated for realistic
    # worker workloads: a 868 MB / 41k-file worktree (npm install +
    # Next.js build) takes ~45-90s uncontested; under N-way concurrent
    # cleanup (e.g. 6 worktrees from a multi-subtask wave), per-worktree
    # time grows several-fold via disk contention. 240s covers the
    # observed worst-case + room for a 2-3 GB monorepo. Still bounded
    # so a genuinely hung git command (not just a slow rm-rf) doesn't
    # block cleanup indefinitely. Per-worktree failures are non-fatal —
    # the loop logs and continues — and a closing recovery-hint line
    # tells the user how to finish manually.
    worktree_remove_timeout = 240
    failed_removals = 0
    worktrees_dir_resolved = worktrees_dir.resolve() if worktrees_dir.is_dir() else None
    if worktrees_dir.is_dir():
        for entry in worktrees_dir.iterdir():
            if not entry.is_dir():
                continue
            try:
                subprocess.run(
                    ["git", "worktree", "remove", "--force", str(entry)],
                    capture_output=True, check=False,
                    timeout=worktree_remove_timeout,
                )
            except (OSError, subprocess.TimeoutExpired) as e:
                log(f"  cleanup: git worktree remove failed for {entry}: {e}")
            # Fall back to direct removal if the directory still exists.
            # Two real cases this catches:
            #   1) `git worktree remove` succeeded administratively
            #      (deregistered from git) but timed out mid-rmtree —
            #      directory survives with partial contents.
            #   2) git no longer tracks the worktree (already pruned)
            #      so `git worktree remove` returns nonzero without
            #      raising — directory survives untouched.
            # The user hit case 2 after an overnight run that crashed
            # while the old 30s timeout was still in place: cleanup
            # logged "failed", git later pruned the entry on its own
            # bookkeeping pass, and the surviving directory blocked
            # `--resume`'s new-worktree.sh from re-creating the
            # worktree at the same path. Safe to rm because the path
            # is sandboxed under .pila/runs/<run-id>/worktrees/<sid>;
            # we re-check via .resolve() to make sure a symlink or
            # refactor hasn't escaped the sandbox.
            if entry.exists():
                try:
                    resolved = entry.resolve()
                    if (worktrees_dir_resolved is not None
                            and resolved.parent == worktrees_dir_resolved):
                        shutil.rmtree(entry, ignore_errors=True)
                except OSError as e:
                    log(f"  cleanup: fallback rm failed for {entry}: {e}")
            if entry.exists():
                failed_removals += 1
                log(f"  cleanup: worktree {entry} survived removal")
    if failed_removals:
        log(f"  cleanup: {failed_removals} worktree(s) not removed within "
            f"{worktree_remove_timeout}s — run "
            f"`scripts/cleanup.sh --run-id {st.run_id}` to finish manually")
    try:
        subprocess.run(["git", "worktree", "prune"],
                       capture_output=True, check=False, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        pass
    if not full_purge:
        return
    # Full purge: delete branches and the run dir. The run branch lives
    # at pila/runs/<run-id> and subtask branches under
    # pila/subtasks/<run-id>/<sid> — see compute_run_branch for the
    # namespace-disjointness rationale.
    branch_globs = [
        f"refs/heads/pila/runs/{st.run_id}",
        f"refs/heads/pila/subtasks/{st.run_id}/",
    ]
    for glob in branch_globs:
        r = subprocess.run(
            ["git", "for-each-ref", "--format=%(refname:short)", glob],
            capture_output=True, text=True, check=False, timeout=10,
        )
        if r.returncode != 0:
            continue
        for ref in r.stdout.splitlines():
            ref = ref.strip()
            if not ref:
                continue
            subprocess.run(
                ["git", "branch", "-D", ref],
                capture_output=True, check=False, timeout=10,
            )
    if st.run_dir.exists():
        shutil.rmtree(st.run_dir, ignore_errors=True)


async def _reset_subtask_worktree(sid: str, pila_dir: Path, run_id: str) -> None:
    """Remove the per-subtask worktree directory and branch so a corrective
    retry can start clean from `new-worktree.sh`'s "fresh subtask" path.
    Without this, retrying after a `complete`-with-no-commits failure
    re-runs the script against a still-registered worktree and an existing
    branch — the second `git worktree add -b` fails with
    `fatal: a branch ... already exists`, the WorkerError escapes
    settle_subtask, and gather_or_cancel takes down the whole wave.

    Tolerates either being absent: both `git worktree remove --force`
    and `git branch -D` return nonzero when their target is missing,
    and that is the expected idempotent case. Mirrors the rmtree
    fallback in `_cleanup_on_abnormal_exit` for the case where git
    administratively succeeded but left the directory behind."""
    worktree = pila_dir / "worktrees" / sid
    branch = f"pila/subtasks/{run_id}/{sid}"
    await run_proc(["git", "worktree", "remove", "--force", str(worktree)])
    await run_proc(["git", "branch", "-D", branch])
    if worktree.exists():
        try:
            shutil.rmtree(worktree, ignore_errors=True)
        except OSError:
            pass


def _parse_claude_version(version_output: str | None) -> tuple[int, int, int] | None:
    """Pull MAJOR.MINOR.PATCH out of `claude --version` output.
    Returns None if the format is unrecognized — caller falls through to
    the live smoke test rather than failing closed on a regex."""
    m = re.match(r"(\d+)\.(\d+)\.(\d+)", (version_output or "").strip())
    return (int(m[1]), int(m[2]), int(m[3])) if m else None


def _check_claude_cli_version() -> None:
    """die() if `claude` is too old for --json-schema. Without this, a
    stale CLI surfaces as a cryptic 'unknown option' wrapped in the
    smoke-test error path — actionable for nobody. Existence on PATH is
    already enforced earlier in main() via shutil.which()."""
    try:
        out = subprocess.run(
            ["claude", "--version"],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except subprocess.TimeoutExpired:
        die("`claude --version` timed out — investigate the CLI install.")
    found = _parse_claude_version(out.stdout)
    if found is None:
        return  # unrecognized format — defer to smoke test
    if found < MIN_CLAUDE_CLI:
        die(
            f"claude CLI {'.'.join(map(str, found))} is too old; pila "
            f"requires >= {'.'.join(map(str, MIN_CLAUDE_CLI))} for "
            "--json-schema (introduced for `claude -p` in v2.1.22). "
            "Upgrade with the native installer: "
            "`curl -fsSL https://claude.ai/install.sh | bash`. "
            "(npm/pnpm installs are now an advanced/legacy option per the "
            "Claude Code docs.)"
        )


# `_check_gh_cli` was removed when finalize moved to the host launcher
# (DESIGN §6 *Finalization*). The launcher does `gh auth status` + the
# origin check itself, before spinning up the container — auth state
# lives on the host, so the check belongs there.


# --- run identifier (DESIGN §6 "The run identifier") --------------------
#
# A run_id namespaces a single pila invocation across its branch
# (`pila/runs/<run-id>`), state directory (`.pila/runs/<run-id>/`),
# and PR title (`pila: <run-id>`). Built from three deterministic
# inputs known by the end of Phase 1: short-category abbrev, sanitized
# task slug, and a 6-hex digest of `started_at`. Two concurrent runs
# in the same repo produce two different run_ids by construction.

# Limit on the kebab-case task slug embedded in the run_id. 30 chars
# leaves enough room for short_category (≤7 chars) + slug + shortid (6)
# + dashes (2) to fit under most filesystems' branch-name length sanity.
SLUG_MAX_LEN = 30

def _sanitize_slug(task: str, max_len: int = SLUG_MAX_LEN) -> str:
    """Turn a freeform task description into a kebab-case slug safe for
    git branch names, filesystem directory names, and JSON keys.

    Rules:
    - lowercase
    - replace any non-[a-z0-9-] with '-'
    - collapse repeated '-'
    - strip leading/trailing '-' and '.'
    - reject if the result contains '..' (path-traversal guard)
    - truncate to `max_len` on a '-' boundary so we never cut a word in half;
      fall back to a hard truncate if the slug has no dashes within `max_len`
    - if the result is empty (all-symbols task), return 'task' as a fallback

    Pure function: same input → same output. No I/O.
    """
    if not isinstance(task, str):
        task = "" if task is None else str(task)
    # Lowercase + non-alphanumeric → '-'. Keep digits, lowercase ASCII, '-'.
    s = re.sub(r"[^a-z0-9-]+", "-", task.lower())
    # Collapse repeated dashes.
    s = re.sub(r"-+", "-", s)
    # Strip leading/trailing dashes and dots.
    s = s.strip("-.")
    # Defensive: reject any residual '..' even after stripping (shouldn't
    # happen given the substitution, but the cost of the check is zero).
    if ".." in s:
        s = s.replace("..", "-")
        s = re.sub(r"-+", "-", s).strip("-.")
    if not s:
        return "task"
    if len(s) <= max_len:
        return s
    # Word-boundary truncate: find the last '-' within the limit so we
    # don't slice a word mid-character.
    cut = s.rfind("-", 0, max_len + 1)
    if cut <= 0:
        return s[:max_len].rstrip("-.")
    return s[:cut].rstrip("-.")


def compute_run_id(categories: list[str], task: str, started_at: str) -> str:
    """Compose the deterministic run identifier from a category list, the
    task description, and the run start timestamp. See DESIGN §6.

    The first entry in `categories` decides the short prefix; it must
    appear in CATEGORY_ABBREV (i.e., be one of the eight CATEGORIES). If
    the list is empty or has no recognized category, falls back to 'misc'
    — this is defensive only; phase_classify already dies before this
    function is reached when the classifier returns no recognized
    category, so 'misc' should never appear in a real run.

    `started_at` is hashed with sha1 and truncated to 6 hex chars for the
    shortid. The hash is a stable function of the microsecond-precision
    timestamp, so two invocations cannot collide unless they share the
    same `started_at` to the microsecond — extraordinarily unlikely, and
    detected at the directory-rename step as a hard preflight failure.

    Pure function: deterministic given the inputs."""
    short = "misc"
    for cat in categories or []:
        if cat in CATEGORY_ABBREV:
            short = CATEGORY_ABBREV[cat]
            break
    slug = _sanitize_slug(task)
    shortid = hashlib.sha1((started_at or "").encode("utf-8")).hexdigest()[:6]
    return f"{short}-{slug}-{shortid}"


def compute_run_branch(run_id: str) -> str:
    """The git branch name carrying a run's integrated work.

    The `pila/runs/` prefix is **mandatory**, not cosmetic. Subtask
    branches live under the sibling prefix `pila/subtasks/<run-id>/<sid>`
    (see `compute_subtask_branch`). Git's loose ref store represents each
    ref as a file inside `refs/heads/…/`, so a ref AT a path and a ref
    UNDER that same path cannot coexist. If both lived under
    `pila/<run-id>` the first `git worktree add` for a subtask would
    fail with `cannot lock ref …`. The disjoint `runs/` and `subtasks/`
    sub-namespaces make that collision structurally impossible."""
    return f"pila/runs/{run_id}"


def compute_subtask_branch(run_id: str, sid: str) -> str:
    """The git branch name for one subtask's worktree.

    Paired with `compute_run_branch` — see that function for the
    namespace-disjointness rationale. The bash side
    (`scripts/new-worktree.sh`, `scripts/integrate.sh`) constructs the
    same string; this helper exists so the shape is grep-able from
    Python and any future Python call site that needs a subtask branch
    name goes through one function."""
    return f"pila/subtasks/{run_id}/{sid}"


# --- run.json sidecar invariants (IMPLEMENTATION.md §8) -----------------

def _validate_run_json(data: dict) -> None:
    """Enforce the four logical invariants on a `run.json` sidecar.

    1. `pushed_at` and `push_error` are mutually exclusive (at most one
       is non-null).
    2. `pr_url` and `pr_error` are mutually exclusive.
    3. If `pr_url` is set, `pushed_at` must be set (cannot have a PR
       without a successful push).
    4. `paused_at` and `pushed_at` are mutually exclusive (a run cannot
       be both paused and finalized). If `paused_at` is set,
       `fly_machine_id` must also be set — you cannot pause a run
       without knowing where to resume it.

    Raises ValueError on any violation. Caller (e.g., `pila --list`)
    decides whether to die, warn, or render as `status=corrupt-sidecar`."""
    if not isinstance(data, dict):
        raise ValueError("run.json must be a JSON object")
    pushed_at = data.get("pushed_at")
    push_error = data.get("push_error")
    pr_url = data.get("pr_url")
    pr_error = data.get("pr_error")
    paused_at = data.get("paused_at")
    fly_machine_id = data.get("fly_machine_id")
    if pushed_at is not None and push_error is not None:
        raise ValueError(
            "run.json invariant: pushed_at and push_error are both set; "
            "exactly one must be null"
        )
    if pr_url is not None and pr_error is not None:
        raise ValueError(
            "run.json invariant: pr_url and pr_error are both set; "
            "exactly one must be null"
        )
    if pr_url is not None and pushed_at is None:
        raise ValueError(
            "run.json invariant: pr_url is set but pushed_at is null; "
            "PR cannot succeed without a successful push"
        )
    if paused_at is not None and pushed_at is not None:
        raise ValueError(
            "run.json invariant: paused_at and pushed_at are both set; "
            "a run cannot be both paused and finalized"
        )
    if paused_at is not None and fly_machine_id is None:
        raise ValueError(
            "run.json invariant: paused_at is set but fly_machine_id is null; "
            "you cannot pause a run without knowing where to resume it"
        )


# --- PR body composition (DESIGN §6 "Finalization") ---------------------

def compose_pr_body(state: dict, run_id: str) -> str:
    """Generate the deterministic fallback PR body from run state +
    run_id. No I/O.

    DESIGN §6 *Finalization*: this is the **fail-open fallback**. The
    primary PR body is now written by the `pr_writer` LLM worker (see
    `_compose_pr_via_llm`) and lives in `run.json` under `pr_body`; the
    host launcher uses that when present and only falls back to this
    deterministic shape when the worker errored or returned nothing.
    The bash launcher carries a structurally equivalent fallback inline
    so the launcher does not need to call back into Python; this
    function remains the canonical reference for the fallback's shape.

    Missing optional fields render as 'n/a' rather than the literal
    string 'None' — Python's f-string default would produce 'None' for
    a missing `finished_at`, which is unhelpful in a PR body."""
    def _or_na(value) -> str:
        return "n/a" if value in (None, "") else str(value)

    task = state.get("task", "")
    categories = state.get("categories") or []
    first_cat = categories[0] if categories else None
    answers = state.get("answers") or {}
    source_of_truth = answers.get("source_of_truth")
    started_at = state.get("started_at")
    finished_at = state.get("finished_at")
    waves = state.get("waves") or []
    wave_count = len(waves)
    subtask_count = sum(len(w) for w in waves)
    worker_count = state.get("worker_count")
    working_branch = state.get("working_branch")
    return (
        "## Task\n"
        "\n"
        f"{task}\n"
        "\n"
        "## Classification\n"
        "\n"
        f"- Category: {_or_na(first_cat)}\n"
        f"- Source of truth: {_or_na(source_of_truth)}\n"
        "\n"
        "## Run summary\n"
        "\n"
        f"- Run ID: {run_id}\n"
        f"- Started: {_or_na(started_at)}\n"
        f"- Finished: {_or_na(finished_at)}\n"
        f"- Waves: {wave_count}, subtasks: {subtask_count}\n"
        f"- Workers: {_or_na(worker_count)}\n"
        f"- Generated by pila on `{_or_na(working_branch)}`.\n"
        "\n"
        f"See `.pila/runs/{run_id}/state.json` for full run state.\n"
    )


def _write_run_json(run_dir: Path, **fields) -> None:
    """Merge fields into the run.json sidecar at `run_dir/run.json`,
    validate the result, and write atomically.

    Reads existing sidecar (if any), applies `fields` on top, validates
    via `_validate_run_json`, then writes via temp-file rename. Same
    atomicity pattern as `State.save()`. Fields with value `None` are
    written through as null (used to clear a previous error / status).

    Designed to be called at every push/PR state transition: run start,
    finalize success, push success, push failure, PR success, PR
    failure. Each call is idempotent given the same inputs."""
    sidecar = run_dir / "run.json"
    data: dict = {}
    if sidecar.exists():
        try:
            data = json.loads(sidecar.read_text())
            if not isinstance(data, dict):
                data = {}
        except (OSError, ValueError):
            data = {}
    data.update(fields)
    _validate_run_json(data)
    tmp = sidecar.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(sidecar)


# --- run discovery and resolution (DESIGN §6 multi-run resume) ----------

def discover_runs(pila_root: Path) -> list[dict]:
    """Enumerate `.pila/runs/*/state.json`, returning one summary
    dict per discovered run. Skip the `_bootstrap-*` directories silently
    (those are pre-classify, not real runs). Malformed state.json files
    are skipped with a logged warning, never raising.

    Returned dicts have at least: `run_id` (directory name), `path` (the
    state.json path), `task`, `started_at`, `finished_at`, `categories`.
    Other state.json fields are passed through unchanged. Sorted by
    `started_at` descending (newest first) for stable display in
    `pila --list`.

    Pure read; no writes. Returns [] if `pila_root/runs` doesn't
    exist."""
    runs_dir = pila_root / "runs"
    if not runs_dir.is_dir():
        return []
    out: list[dict] = []
    for entry in runs_dir.iterdir():
        if not entry.is_dir():
            continue
        if entry.name.startswith("_bootstrap-"):
            continue
        state_path = entry / "state.json"
        if not state_path.is_file():
            continue
        try:
            data = json.loads(state_path.read_text())
        except (OSError, ValueError) as e:
            log(f"warning: skipping malformed state.json at {state_path}: {e}")
            continue
        if not isinstance(data, dict):
            log(f"warning: state.json at {state_path} is not a JSON object")
            continue
        summary = dict(data)
        summary["run_id"] = entry.name
        summary["path"] = str(state_path)
        out.append(summary)
    # Newest first. Empty / missing `started_at` sorts last.
    out.sort(key=lambda r: r.get("started_at") or "", reverse=True)
    return out


def resolve_run_id(pila_root: Path, cli_run_id: str | None) -> str:
    """Pick the run_id to operate on. Used by `--resume` and `--list`.

    Policy (DESIGN §6 "the run branch is the resume contract"):
    - If `cli_run_id` is given, it must exactly match an existing run.
      Otherwise die with the available list (fails closed).
    - Elif exactly one run exists, use it. Preserves the common case
      where there's only one run in flight.
    - Else die: multiple runs and no `--run-id` is ambiguous.

    Never guesses across multiple runs. `--resume` against an ambiguous
    repo is a hard error, not a heuristic."""
    runs = discover_runs(pila_root)
    if cli_run_id is not None:
        for r in runs:
            if r["run_id"] == cli_run_id:
                return cli_run_id
        available = ", ".join(r["run_id"] for r in runs) or "(none)"
        die(
            f"--run-id {cli_run_id!r} does not match any known run. "
            f"Available: {available}. Use `pila --list` to enumerate."
        )
    if not runs:
        die(
            "no runs found under .pila/runs/. Start a new run with "
            "`./pila \"<task>\"`."
        )
    if len(runs) == 1:
        return runs[0]["run_id"]
    available = "\n  ".join(_format_run_for_disambiguation(r, pila_root)
                            for r in runs)
    die(
        "multiple runs present; pass --run-id <id> to disambiguate:\n  "
        f"{available}\nUse `pila --list` to see full details."
    )


def _format_run_for_disambiguation(run: dict, pila_root: Path) -> str:
    """Build the per-row hint string for `resolve_run_id`'s
    multiple-runs error message. Combines run_id, derived status,
    started_at, and a last-activity time so the user can tell which
    run is live without an extra `pila --list` invocation.

    Reads run.json from disk for `_derive_run_status` (same source
    `pila --list` consults). Falls back gracefully when sidecar or
    state.json is unreadable — disambiguation is best-effort UX, not
    a correctness boundary."""
    run_id = run["run_id"]
    started = run.get("started_at") or "?"
    # Derived status — uses run.json sidecar if present, falls back to
    # state.json fields. Same pattern as list_runs().
    run_dir = pila_root / "runs" / run_id
    run_json: dict | None = None
    sidecar = run_dir / "run.json"
    if sidecar.is_file():
        try:
            parsed = json.loads(sidecar.read_text())
            if isinstance(parsed, dict):
                run_json = parsed
        except (OSError, ValueError):
            pass
    status = _derive_run_status(run_json, run)
    # Last-activity: mtime of state.json formatted as the elapsed
    # duration from now. A live run shows seconds-to-minutes; a hung
    # or abandoned run shows hours-to-days.
    last_activity = "?"
    state_path = run.get("path")
    if state_path:
        try:
            mtime = os.path.getmtime(state_path)
            last_activity = _format_age(datetime.now(timezone.utc).timestamp()
                                        - mtime)
        except (OSError, ValueError, OverflowError):
            # OSError: state.json deleted between discover_runs and now.
            # ValueError/OverflowError: pathological mtime (NaN, inf) that
            # _format_age's int() would reject. Both are extremely unlikely
            # in practice; this is defense-in-depth so a one-in-a-million
            # filesystem quirk can't crash --resume startup.
            pass
    return (f"{run_id}  status={status}  started={started}  "
            f"last-activity={last_activity}")


def _format_age(seconds: float) -> str:
    """Render a duration in seconds as a short human-friendly age:
    "5s", "3m", "47m", "2h12m", "1d4h", "5d". Used by the --resume
    disambiguation hint to show how stale each in-flight run is."""
    if seconds < 0:
        seconds = 0
    s = int(seconds)
    if s < 60:
        return f"{s}s ago"
    if s < 3600:
        return f"{s // 60}m ago"
    if s < 86400:
        h, m = divmod(s, 3600)
        m //= 60
        return f"{h}h{m:02d}m ago" if m else f"{h}h ago"
    d, h = divmod(s, 86400)
    h //= 3600
    return f"{d}d{h}h ago" if h else f"{d}d ago"


# --- run status (consumed by `pila --list`) -------------------------

# The seven derived statuses returned by `_derive_run_status`. Status is
# *derived* from run.json + state.json fields, not stored, so the value
# rendered by --list is always consistent with the actual on-disk state.
RUN_STATUSES = (
    "corrupt-sidecar",
    "in-progress",
    "done-local",
    "done-pushed-no-pr",
    "done-pushed-pr",
    "push-failed",
    "pr-failed",
    "paused-remote",
)


def _derive_run_status(run_json: dict | None, state_json: dict | None) -> str:
    """Pure function: derive a run's status from run.json + state.json.

    Order of checks matters — earlier checks fire first:
      1. run.json invariant-invalid → `corrupt-sidecar`.
      2. push_error set            → `push-failed`.
      3. pr_error set              → `pr-failed`.
      4. pr_url set                → `done-pushed-pr`.
      5. pushed_at set             → `done-pushed-no-pr`.
      6. finished_at set           → `done-local` (run completed, --no-push).
      7. paused_at set             → `paused-remote` (remote pause-on-failure).
      8. otherwise                 → `in-progress`.

    Precedence note: push/PR errors fire before the paused-remote check
    because a finalize that failed mid-write should surface as the error
    it actually is, not as a pause. The invariant
    (paused_at xor pushed_at) means a paused run can't have pushed_at,
    but it can in principle have a stale push_error from a prior attempt
    — checking errors first makes the rendered status match the action
    the user needs to take.

    state_json is currently unused in the derivation but accepted for
    forward-compat: future statuses (e.g., 'blocked') may consult
    state.json["blocked"]."""
    rj = run_json or {}
    if rj:
        try:
            _validate_run_json(rj)
        except ValueError:
            return "corrupt-sidecar"
    if rj.get("push_error"):
        return "push-failed"
    if rj.get("pr_error"):
        return "pr-failed"
    if rj.get("pr_url"):
        return "done-pushed-pr"
    if rj.get("pushed_at"):
        return "done-pushed-no-pr"
    if rj.get("finished_at"):
        return "done-local"
    if rj.get("paused_at"):
        return "paused-remote"
    return "in-progress"


def _collect_run_rows(pila_root: Path) -> list[tuple[str, str, str, str]]:
    """Build (run_id, started_at, status, branch) rows for every run
    under `pila_root/runs/`. Pure data-gathering; rendering is the
    caller's concern."""
    runs = discover_runs(pila_root)
    rows: list[tuple[str, str, str, str]] = []
    for state in runs:
        run_id = state["run_id"]
        run_dir = pila_root / "runs" / run_id
        run_json: dict | None = None
        sidecar = run_dir / "run.json"
        if sidecar.is_file():
            try:
                parsed = json.loads(sidecar.read_text())
                if isinstance(parsed, dict):
                    run_json = parsed
            except (OSError, ValueError):
                run_json = None
        status = _derive_run_status(run_json, state)
        started_at = state.get("started_at") or "—"
        branch = (run_json or {}).get("branch") or compute_run_branch(run_id)
        rows.append((run_id[:50], started_at, status, branch))
    return rows


def _render_run_table(rows: list[tuple[str, str, str, str]]) -> None:
    """Print rows as a columnar table with auto-sized columns."""
    w_id = max(len("run_id"), *(len(r[0]) for r in rows))
    w_st = max(len("started_at"), *(len(r[1]) for r in rows))
    w_status = max(len("status"), *(len(r[2]) for r in rows))
    w_br = max(len("branch"), *(len(r[3]) for r in rows))
    fmt = f"{{:<{w_id}}}  {{:<{w_st}}}  {{:<{w_status}}}  {{:<{w_br}}}"
    print(fmt.format("run_id", "started_at", "status", "branch"))
    print(fmt.format("-" * w_id, "-" * w_st, "-" * w_status, "-" * w_br))
    for r in rows:
        print(fmt.format(*r))


def list_runs(pila_root: Path) -> None:
    """Render a sortable columnar table of runs to stdout. Used by
    `pila --list`. Reads run.json sidecar (commit 4) for status
    derivation; falls back to state.json fields for runs without a
    sidecar (e.g., extremely-early failures before the rename, though
    discover_runs filters those out)."""
    rows = _collect_run_rows(pila_root)
    if not rows:
        print("no runs under .pila/runs/")
        return
    _render_run_table(rows)


def list_paused_runs(pila_root: Path) -> None:
    """Filter the run table to paused-remote entries. Used by
    `pila --list-paused`. Reads the same sidecar source as `list_runs`;
    the filter is on the derived status, not on `paused_at` directly,
    so that the precedence rules in `_derive_run_status` apply (a
    paused run that also has `push_error` renders as `push-failed`,
    not `paused-remote`)."""
    rows = [r for r in _collect_run_rows(pila_root) if r[2] == "paused-remote"]
    if not rows:
        print("no paused remote runs")
        return
    _render_run_table(rows)


def _read_toml_key(path: Path, key: str) -> str | None:
    """Read a single `key = value` from a flat pila.toml. Returns
    None when the file does not exist or the key is absent. Strips
    matched surrounding double or single quotes from the value. Used
    by both source-of-truth and model resolvers — keeping one parser
    means a fix benefits both."""
    if not path.exists():
        return None
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        if k.strip() != key:
            continue
        return v.strip().strip('"').strip("'")
    return None


TASK_FILE_SUFFIXES = (".txt", ".md")


def resolve_task_argument(raw: str) -> str:
    """Resolve the positional `task` argument to the task string.

    If `raw` points at an existing .txt or .md file, return its contents
    (stripped). Otherwise return `raw` unchanged.

    Suffix is restricted (rather than reading any existing file) so a
    literal task string that happens to match a filename in cwd is not
    silently swallowed.
    """
    p = Path(raw)
    # A long literal task is one path component over NAME_MAX (255 bytes
    # on macOS/Linux), which makes stat() raise ENAMETOOLONG instead of
    # returning a "not found" result that is_file() would surface as
    # False. Any stat failure means we cannot confirm a file, so treat
    # `raw` as the literal task — same outcome as the missing-file path.
    try:
        is_task_file = (p.is_file()
                        and p.suffix.lower() in TASK_FILE_SUFFIXES)
    except OSError:
        is_task_file = False
    if is_task_file:
        contents = p.read_text().strip()
        if not contents:
            die(f"task file {raw!r} is empty")
        return contents
    return raw


def resolve_source_of_truth(repo_root: Path,
                            cli_value: str | None = None) -> str:
    """Resolve the source-of-truth preference. Order:
    --source-of-truth CLI flag → PILA_SOURCE_OF_TRUTH env var →
    pila.toml → default 'both'. argparse validates `cli_value` via
    choices=, so it is trusted when set. env and file values are
    rejected via die() if not in SOURCE_OF_TRUTH_VALUES — a bad
    config is caught at startup, not during a planner run."""
    if cli_value:
        return cli_value
    env = os.environ.get(SOURCE_OF_TRUTH_ENV, "").strip()
    if env:
        if env not in SOURCE_OF_TRUTH_VALUES:
            die(f"{SOURCE_OF_TRUTH_ENV}={env!r} is not one of "
                f"{SOURCE_OF_TRUTH_VALUES}")
        return env
    cfg = repo_root / SOURCE_OF_TRUTH_FILE
    file_val = _read_toml_key(cfg, "source_of_truth")
    if file_val is not None:
        if file_val not in SOURCE_OF_TRUTH_VALUES:
            die(f"{cfg}: source_of_truth={file_val!r} is not one of "
                f"{SOURCE_OF_TRUTH_VALUES}")
        return file_val
    return "both"


def resolve_runtime(repo_root: Path,
                    cli_value: str | None = None) -> str:
    """Resolve the runtime mode. Order:
    --runtime CLI flag → PILA_RUNTIME env var → pila.toml → default 'local'.
    argparse validates `cli_value` via choices=, so it is trusted when set.
    env and file values are rejected via die() if not in RUNTIME_VALUES — a
    bad config is caught at startup, not during a worker run."""
    if cli_value:
        return cli_value
    env = os.environ.get(RUNTIME_ENV, "").strip()
    if env:
        if env not in RUNTIME_VALUES:
            die(f"{RUNTIME_ENV}={env!r} is not one of "
                f"{RUNTIME_VALUES}")
        return env
    cfg = repo_root / RUNTIME_FILE
    file_val = _read_toml_key(cfg, "runtime")
    if file_val is not None:
        if file_val not in RUNTIME_VALUES:
            die(f"{cfg}: runtime={file_val!r} is not one of "
                f"{RUNTIME_VALUES}")
        return file_val
    return "local"


def resolve_pr_template(repo_root: Path,
                        cli_value: str | None = None) -> str | None:
    """Resolve the --pr-template selector. Order:
    --pr-template CLI flag → PILA_PR_TEMPLATE env → pila.toml → None.
    Returns the basename of the desired template inside a
    PULL_REQUEST_TEMPLATE/ directory (case preserved, .md optional).
    No validation against MODEL_VALUES-style enum since the choice is
    free-form (depends on the target repo's directory contents); the
    template-discovery helper validates existence later."""
    if cli_value:
        return cli_value
    env = os.environ.get(PR_TEMPLATE_ENV, "").strip()
    if env:
        return env
    cfg = repo_root / PR_TEMPLATE_FILE
    file_val = _read_toml_key(cfg, "pr_template")
    if file_val is not None:
        return file_val
    return None


def resolve_confidence_rounds(repo_root: Path,
                              cli_value: int | None = None) -> int:
    """Resolve the confidence-rounds cap. Order:
    --confidence-rounds CLI flag → PILA_CONFIDENCE_ROUNDS env var →
    pila.toml → DEFAULT_CAPS["confidence_rounds"]. argparse validates
    `cli_value` is a positive int via `type=`, so it is trusted when set.
    env and file values are rejected via die() when not a positive int —
    bad config caught at startup, not during a planner run."""
    if cli_value is not None:
        return cli_value
    env = os.environ.get(CONFIDENCE_ROUNDS_ENV, "").strip()
    if env:
        try:
            n = int(env)
        except ValueError:
            die(f"{CONFIDENCE_ROUNDS_ENV}={env!r} is not a positive integer")
        if n < 1:
            die(f"{CONFIDENCE_ROUNDS_ENV}={env!r} is not a positive integer")
        return n
    cfg = repo_root / CONFIDENCE_ROUNDS_FILE
    file_val = _read_toml_key(cfg, "confidence_rounds")
    if file_val is not None:
        try:
            n = int(file_val)
        except ValueError:
            die(f"{cfg}: confidence_rounds={file_val!r} is not a positive integer")
        if n < 1:
            die(f"{cfg}: confidence_rounds={file_val!r} is not a positive integer")
        return n
    return DEFAULT_CAPS["confidence_rounds"]


def resolve_max_workers(repo_root: Path,
                        cli_value: int | None = None) -> int:
    """Resolve the max-workers cap. Order:
    --max-workers CLI flag → PILA_MAX_WORKERS env var → pila.toml →
    DEFAULT_CAPS["max_total_workers"]. argparse validates `cli_value` is an
    int via `type=int` so it is trusted when set. env and file values are
    rejected via die() when not a positive int — bad config caught at
    startup, not mid-run."""
    if cli_value is not None:
        return cli_value
    env = os.environ.get(MAX_WORKERS_ENV, "").strip()
    if env:
        try:
            n = int(env)
        except ValueError:
            die(f"{MAX_WORKERS_ENV}={env!r} is not a positive integer")
        if n < 1:
            die(f"{MAX_WORKERS_ENV}={env!r} is not a positive integer")
        return n
    cfg = repo_root / MAX_WORKERS_FILE
    file_val = _read_toml_key(cfg, "max_workers")
    if file_val is not None:
        try:
            n = int(file_val)
        except ValueError:
            die(f"{cfg}: max_workers={file_val!r} is not a positive integer")
        if n < 1:
            die(f"{cfg}: max_workers={file_val!r} is not a positive integer")
        return n
    return DEFAULT_CAPS["max_total_workers"]


_MEMORY_SUFFIX_MULTIPLIER = {
    "": 1, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4,
}


def _parse_memory_size(value: str, context: str) -> int:
    """Parse a memory size string like "4G", "512M", "1024" into bytes.

    Accepts an optional case-insensitive IEC binary suffix (K/M/G/T).
    No suffix means bytes. Rejects negative, zero, fractional, and
    garbage values via die() with `context` in the error message so the
    user knows which knob produced the bad value."""
    v = value.strip()
    if not v:
        die(f"{context}: memory size cannot be empty")
    suffix = v[-1:].upper()
    if suffix in _MEMORY_SUFFIX_MULTIPLIER and suffix != "":
        numeric = v[:-1]
        mult = _MEMORY_SUFFIX_MULTIPLIER[suffix]
    else:
        numeric = v
        mult = 1
    try:
        n = int(numeric)
    except ValueError:
        die(f"{context}: {value!r} is not a valid memory size "
            f"(expected like '4G', '512M', '1024')")
    if n <= 0:
        die(f"{context}: {value!r} must be a positive memory size")
    return n * mult


def _auto_worker_memory_max(max_parallel: int) -> int:
    """Auto-derive a per-worker memory cap from /proc/meminfo.

    The goal: distribute the VM's RAM across `max_parallel + 1` slots
    so one slot remains for the orchestrator + system processes
    (sshd, lima-guestagent, etc.) outside any worker cgroup. Capped at
    4 GiB per worker — beyond that, a single tool subtree shouldn't
    legitimately need more, and an uncapped 8+ GiB cgroup defeats
    the containment purpose.

    Falls back to 2 GiB if /proc/meminfo is unreadable (non-Linux,
    sandboxed test, etc.). The cgroup write itself will detect a
    nonsensical limit and the probe will skip wrapping."""
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    kb = int(line.split()[1])
                    total = kb * 1024
                    break
            else:
                return 2 * 1024**3
    except (FileNotFoundError, PermissionError, ValueError):
        return 2 * 1024**3
    per_worker = total // (max_parallel + 1)
    return min(per_worker, 4 * 1024**3)


def resolve_worker_memory_max(repo_root: Path,
                              max_parallel: int,
                              cli_value: str | None = None) -> int:
    """Resolve the per-worker cgroup memory cap (bytes). Order:
    --worker-memory-max CLI flag → PILA_WORKER_MEMORY_MAX env →
    pila.toml `worker_memory_max` → auto-derive from /proc/meminfo.

    All sources accept the same format ("4G", "512M", "1024") and are
    validated by _parse_memory_size, which die()s on bad input — bad
    config is caught at startup, not during a worker spawn."""
    if cli_value is not None:
        return _parse_memory_size(cli_value, "--worker-memory-max")
    env = os.environ.get(WORKER_MEMORY_MAX_ENV, "").strip()
    if env:
        return _parse_memory_size(env, WORKER_MEMORY_MAX_ENV)
    cfg = repo_root / WORKER_MEMORY_MAX_FILE
    file_val = _read_toml_key(cfg, "worker_memory_max")
    if file_val is not None:
        return _parse_memory_size(file_val,
                                  f"{cfg}: worker_memory_max")
    return _auto_worker_memory_max(max_parallel)


def resolve_inspect_dirs(repo_root: Path,
                         cli_values: list[str] | None = None) -> list[str]:
    """Resolve the extra inspection directories for classifier/planner/
    reconciler/provision. Order: --inspect-dir CLI flags (one or more, repeatable) →
    PILA_INSPECT_DIRS env var (colon-separated) → inspect_dirs in
    pila.toml (comma-separated string) → []. Paths are expanded
    (~ → $HOME) and resolved to absolute form so a relative path in TOML
    still works after the orchestrator changes cwd. Non-existent paths
    are accepted at resolve time — the CLI surfaces a clearer error if
    --add-dir gets a bad path, and we want startup to fail fast at the
    use site rather than rejecting a typo before classify even runs."""
    out: list[str] = []
    seen: set[str] = set()

    def _add(raw: str) -> None:
        p = raw.strip()
        if not p:
            return
        abs_p = str(Path(p).expanduser().resolve())
        if abs_p not in seen:
            seen.add(abs_p)
            out.append(abs_p)

    if cli_values:
        for p in cli_values:
            _add(p)
        return out
    env = os.environ.get(INSPECT_DIRS_ENV, "").strip()
    if env:
        for p in env.split(":"):
            _add(p)
        return out
    cfg = repo_root / INSPECT_DIRS_FILE
    file_val = _read_toml_key(cfg, "inspect_dirs")
    if file_val is not None:
        for p in file_val.split(","):
            _add(p)
        return out
    return out


def _parse_bool_envtoml(value: str) -> bool | None:
    """Parse a boolean from an env var or TOML scalar. Returns True/False
    for the conventional spellings; None for the empty string / unset.
    Raises ValueError on any other input so the caller can die() with a
    helpful message rather than silently treating typos as False."""
    v = value.strip().lower()
    if v == "":
        return None
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    raise ValueError(value)


def _resolve_bool_pref(repo_root: Path, cli_value: bool, *,
                       env_var: str, file_key: str, file_name: str) -> bool:
    """Shared resolution for `store_true` CLI flags that also have an
    env-var and per-repo TOML mirror (see DESIGN §11 / §6 patterns).
    Order: CLI True wins → env → file → False. Bad env or file values
    `die()` at startup, not mid-run. Used by `resolve_no_push` and
    `resolve_clarify`; keep one shape so they cannot drift."""
    if cli_value:
        return True
    env = os.environ.get(env_var, "").strip()
    if env:
        try:
            parsed = _parse_bool_envtoml(env)
        except ValueError:
            die(f"{env_var}={env!r} is not a boolean "
                "(use 1/0, true/false, yes/no, on/off)")
        if parsed is not None:
            return parsed
    cfg = repo_root / file_name
    file_val = _read_toml_key(cfg, file_key)
    if file_val is not None:
        try:
            parsed = _parse_bool_envtoml(file_val)
        except ValueError:
            die(f"{cfg}: {file_key}={file_val!r} is not a boolean")
        if parsed is not None:
            return parsed
    return False


def resolve_no_push(repo_root: Path, cli_value: bool) -> bool:
    """Resolve the --no-push preference. Order:
    --no-push CLI flag (action='store_true', so True if passed) →
    PILA_NO_PUSH env var → no_push in pila.toml → False.
    `--no-verify` has no env/TOML mirror (see NO_PUSH_ENV comment)."""
    return _resolve_bool_pref(
        repo_root, cli_value,
        env_var=NO_PUSH_ENV, file_key="no_push", file_name=NO_PUSH_FILE)


def resolve_clarify(repo_root: Path, cli_value: bool) -> bool:
    """Resolve the --clarify preference. Order:
    --clarify CLI flag (action='store_true', so True if passed) →
    PILA_CLARIFY env var → clarify in pila.toml → False.
    See DESIGN §11 for the clarification semantics."""
    return _resolve_bool_pref(
        repo_root, cli_value,
        env_var=CLARIFY_ENV, file_key="clarify", file_name=CLARIFY_FILE)


def resolve_dangerously_skip_permissions(
        repo_root: Path, cli_value: bool) -> bool:
    """Resolve the --dangerously-skip-permissions preference. Order:
    --dangerously-skip-permissions CLI flag (action='store_true') →
    PILA_DANGEROUSLY_SKIP_PERMISSIONS env var →
    dangerously_skip_permissions in pila.toml → False.

    When True, EVERY claude -p worker — including the judgment workers
    (classifier, planner, reconciler, provision) that run in the real
    repo cwd, not an isolated worktree — is invoked with
    --dangerously-skip-permissions. This waives the DESIGN §12
    mechanical enforcement that planners stay read-only; trust shifts
    onto the prompts. Off by default; users opting in are making one
    all-or-nothing trust decision. See DESIGN §12 (last paragraph) and
    IMPLEMENTATION.md §2 "Permission override (dangerous)"."""
    return _resolve_bool_pref(
        repo_root, cli_value,
        env_var=DANGEROUS_SKIP_PERMS_ENV,
        file_key="dangerously_skip_permissions",
        file_name=DANGEROUS_SKIP_PERMS_FILE)


def _positive_int(s: str) -> int:
    """argparse `type=` helper. Rejects non-positive integers with the
    standard argparse error message. Used by --confidence-rounds."""
    try:
        n = int(s)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{s!r} is not an integer")
    if n < 1:
        raise argparse.ArgumentTypeError(f"{s!r} is not a positive integer")
    return n


def resolve_verbosity(repo_root: Path,
                      cli_value: str | None = None) -> str:
    """Resolve the verbosity level. Order:
    --verbosity CLI flag → PILA_VERBOSITY env var → pila.toml →
    VERBOSITY_DEFAULT. argparse validates `cli_value` via choices=, so
    it is trusted when set. env and file values are rejected via die()
    if not in VERBOSITY_VALUES — a bad config is caught at startup,
    not during a worker run.

    The -v/-vv/-q/-qq shortcuts are resolved separately in main()
    BEFORE this function is called — they map to one of VERBOSITY_VALUES
    and pass through as cli_value. The shortcut→level mapping is anchored
    to `normal` (the pre-streaming behavior), not to VERBOSITY_DEFAULT,
    so -v means "show me the streaming feature" rather than "bump above
    the default by one"."""
    if cli_value:
        return cli_value
    env = os.environ.get(VERBOSITY_ENV, "").strip()
    if env:
        if env not in VERBOSITY_VALUES:
            die(f"{VERBOSITY_ENV}={env!r} is not one of "
                f"{VERBOSITY_VALUES}")
        return env
    cfg = repo_root / VERBOSITY_FILE
    file_val = _read_toml_key(cfg, "verbosity")
    if file_val is not None:
        if file_val not in VERBOSITY_VALUES:
            die(f"{cfg}: verbosity={file_val!r} is not one of "
                f"{VERBOSITY_VALUES}")
        return file_val
    return VERBOSITY_DEFAULT


def verbosity_from_shortcuts(verbose: int, quiet: int) -> str | None:
    """Map argparse -v/-vv/-q/-qq counts to a verbosity level.

    Anchors to `normal` (NOT to VERBOSITY_DEFAULT), so -v always means
    "show me the streaming feature" and -q always means "back to the
    pre-streaming terse output", independent of what env-var / TOML
    defaults are set to. Matches the cargo / kubectl idiom of treating
    shortcuts as relative-to-baseline rather than relative-to-resolved.

    Returns None when neither shortcut was used (caller falls through
    to resolve_verbosity / env / TOML / default). Returns a value from
    VERBOSITY_VALUES when a shortcut was used. Stacking past -vv / -qq
    saturates at the endpoints rather than wrapping or raising — a
    user typing -vvvv gets debug, not an error."""
    if quiet:
        return "quiet" if quiet > 1 else "normal"
    if verbose:
        return "debug" if verbose > 1 else "stream"
    return None


def resolve_models(repo_root: Path, args) -> dict[str, str]:
    """Resolve the model alias for each worker type. Per-worker
    precedence (highest first):
      1. --model-<worker> CLI flag
      2. --model CLI flag (global default for this run)
      3. PILA_MODEL_<WORKER> env var
      4. PILA_MODEL env var
      5. model_<worker> in pila.toml
      6. model in pila.toml
      7. MODEL_DEFAULT_PER_WORKER[<worker>] (e.g., implementer → sonnet)
      8. MODEL_DEFAULT (opus)
    `args` is the parsed argparse.Namespace (CLI values are already
    validated by argparse choices=). env and file values are rejected
    via die() when not in MODEL_VALUES."""
    cfg = repo_root / MODEL_FILE

    def from_env(name: str) -> str | None:
        v = os.environ.get(name, "").strip()
        if not v:
            return None
        if v not in MODEL_VALUES:
            die(f"{name}={v!r} is not one of {MODEL_VALUES}")
        return v

    def from_file(key: str) -> str | None:
        v = _read_toml_key(cfg, key)
        if v is None:
            return None
        if v not in MODEL_VALUES:
            die(f"{cfg}: {key}={v!r} is not one of {MODEL_VALUES}")
        return v

    global_cli = getattr(args, "model", None)
    global_env = from_env(MODEL_ENV)
    global_file = from_file("model")

    models: dict[str, str] = {}
    for worker in WORKER_TYPES:
        # argparse converts --model-foo to args.model_foo
        per_cli = getattr(args, f"model_{worker}", None)
        per_env = from_env(f"{MODEL_ENV}_{worker.upper()}")
        per_file = from_file(f"model_{worker}")
        # Per-worker default kicks in only when no user override applies.
        # Implementer falls through to "sonnet"; everything else falls
        # through to MODEL_DEFAULT ("opus").
        per_worker_default = MODEL_DEFAULT_PER_WORKER.get(worker, MODEL_DEFAULT)
        models[worker] = (per_cli or global_cli or per_env or global_env
                          or per_file or global_file or per_worker_default)
    # Judge and heal use dedicated flags (--judge-model / --heal-model) and
    # dedicated env vars (PILA_MODEL_JUDGE / PILA_MODEL_HEAL) rather
    # than the --model-<W> pattern — they're post-run skill workers that don't
    # participate in the --model global-default resolution path. They still
    # fall back to the global override so `--model sonnet` applies everywhere.
    judge_cli = getattr(args, "judge_model", None)
    judge_env = from_env(MODEL_JUDGE_ENV)
    judge_file = from_file("model_judge")
    models["judge"] = (judge_cli or judge_env or global_cli or global_env
                       or judge_file or global_file
                       or MODEL_DEFAULT_PER_WORKER.get("judge", MODEL_DEFAULT))
    heal_cli = getattr(args, "heal_model", None)
    heal_env = from_env(MODEL_HEAL_ENV)
    heal_file = from_file("model_heal")
    models["heal"] = (heal_cli or heal_env or global_cli or global_env
                      or heal_file or global_file
                      or MODEL_DEFAULT_PER_WORKER.get("heal", MODEL_DEFAULT))
    pr_writer_cli = getattr(args, "pr_writer_model", None)
    pr_writer_env = from_env(MODEL_PR_WRITER_ENV)
    pr_writer_file = from_file("model_pr_writer")
    models["pr_writer"] = (pr_writer_cli or pr_writer_env
                           or global_cli or global_env
                           or pr_writer_file or global_file
                           or MODEL_DEFAULT_PER_WORKER.get(
                               "pr_writer", MODEL_DEFAULT))
    return models


def resolve_efforts(repo_root: Path, args) -> dict[str, str | None]:
    """Resolve the --effort value for each worker type. Mirrors
    resolve_models() rung-for-rung. Per-worker precedence (highest first):
      1. --effort-<worker> CLI flag
      2. --effort CLI flag (global default for this run)
      3. PILA_EFFORT_<WORKER> env var
      4. PILA_EFFORT env var
      5. effort_<worker> in pila.toml
      6. effort in pila.toml
      7. EFFORT_DEFAULT_PER_WORKER[<worker>] (e.g., planner → "high")
      8. EFFORT_DEFAULT (None — flag omitted from CLI invocation)
    A None value means "do not pass --effort"; claude_p's build() omits
    the flag entirely so the worker inherits Claude's default. CLI values
    are pre-validated by argparse choices=; env and file values are
    rejected via die() when not in EFFORT_VALUES."""
    cfg = repo_root / MODEL_FILE

    def from_env(name: str) -> str | None:
        v = os.environ.get(name, "").strip()
        if not v:
            return None
        if v not in EFFORT_VALUES:
            die(f"{name}={v!r} is not one of {EFFORT_VALUES}")
        return v

    def from_file(key: str) -> str | None:
        v = _read_toml_key(cfg, key)
        if v is None:
            return None
        if v not in EFFORT_VALUES:
            die(f"{cfg}: {key}={v!r} is not one of {EFFORT_VALUES}")
        return v

    global_cli = getattr(args, "effort", None)
    global_env = from_env(EFFORT_ENV)
    global_file = from_file("effort")

    efforts: dict[str, str | None] = {}
    for worker in WORKER_TYPES:
        per_cli = getattr(args, f"effort_{worker}", None)
        per_env = from_env(f"{EFFORT_ENV}_{worker.upper()}")
        per_file = from_file(f"effort_{worker}")
        per_worker_default = EFFORT_DEFAULT_PER_WORKER.get(worker, EFFORT_DEFAULT)
        # Explicit-None chain: every rung is either str or None, so we can
        # collapse with `or` — None falls through to the next rung; the
        # final fallback is EFFORT_DEFAULT (None), meaning "omit --effort".
        efforts[worker] = (per_cli or global_cli or per_env or global_env
                           or per_file or global_file or per_worker_default)
    # Post-run skill workers (judge, heal) are not in WORKER_TYPES so they
    # don't get per-worker --effort-<W> flags, but they still honor the
    # global override so `--effort high` applies everywhere.
    efforts["judge"] = (global_cli or global_env or global_file
                        or EFFORT_DEFAULT_PER_WORKER.get("judge", EFFORT_DEFAULT))
    efforts["heal"] = (global_cli or global_env or global_file
                       or EFFORT_DEFAULT_PER_WORKER.get("heal", EFFORT_DEFAULT))
    efforts["pr_writer"] = (global_cli or global_env or global_file
                            or EFFORT_DEFAULT_PER_WORKER.get(
                                "pr_writer", EFFORT_DEFAULT))
    return efforts


def resolve_telemetry_enabled(repo_root: Path,
                              cli_value: bool | None = None) -> bool:
    """Resolve the telemetry enabled/disabled preference. Order:
    --telemetry/--no-telemetry CLI flag → PILA_TELEMETRY env var →
    telemetry in pila.toml → TELEMETRY_DEFAULT (True). cli_value is
    True when --telemetry was passed, False when --no-telemetry was passed,
    None when neither was passed (argparse store_true/store_false pair with
    default None). env and file values are rejected via die() if not parseable
    as a boolean — a bad config is caught at startup."""
    if cli_value is not None:
        return cli_value
    env = os.environ.get(TELEMETRY_ENV, "").strip()
    if env:
        try:
            parsed = _parse_bool_envtoml(env)
        except ValueError:
            die(f"{TELEMETRY_ENV}={env!r} is not a boolean "
                "(use 1/0, true/false, yes/no, on/off)")
        if parsed is not None:
            return parsed
    cfg = repo_root / TELEMETRY_FILE
    file_val = _read_toml_key(cfg, "telemetry")
    if file_val is not None:
        try:
            parsed = _parse_bool_envtoml(file_val)
        except ValueError:
            die(f"{cfg}: telemetry={file_val!r} is not a boolean")
        if parsed is not None:
            return parsed
    return TELEMETRY_DEFAULT


def resolve_telemetry_subdir(repo_root: Path,
                             cli_value: str | None = None) -> str:
    """Resolve the telemetry event subdirectory name. Order:
    --telemetry-dir CLI flag → PILA_TELEMETRY_DIR env var →
    telemetry_dir in pila.toml → TELEMETRY_SUBDIR_DEFAULT ("events").
    The value is a plain directory name (or relative path) appended to
    the run dir — not validated against the filesystem at resolve time."""
    if cli_value and cli_value.strip():
        return cli_value.strip()
    env = os.environ.get(TELEMETRY_SUBDIR_ENV, "").strip()
    if env:
        return env
    cfg = repo_root / TELEMETRY_SUBDIR_FILE
    file_val = _read_toml_key(cfg, "telemetry_dir")
    if file_val is not None and file_val.strip():
        return file_val.strip()
    return TELEMETRY_SUBDIR_DEFAULT


def resolve_judge_dir(repo_root: Path, cli_value: str | None = None) -> str:
    """Resolve the judge output directory name. Order:
    --judge-dir CLI flag → PILA_JUDGE_DIR env var →
    judge_dir in pila.toml → JUDGE_DIR_DEFAULT ("judge-out").
    The value is a plain directory name (or relative path) appended to
    the run dir — not validated against the filesystem at resolve time."""
    if cli_value and cli_value.strip():
        return cli_value.strip()
    env = os.environ.get(JUDGE_DIR_ENV, "").strip()
    if env:
        return env
    cfg = repo_root / JUDGE_DIR_FILE
    file_val = _read_toml_key(cfg, "judge_dir")
    if file_val is not None and file_val.strip():
        return file_val.strip()
    return JUDGE_DIR_DEFAULT


def resolve_heal_dir(repo_root: Path, cli_value: str | None = None) -> str:
    """Resolve the heal output directory name. Order:
    --heal-dir CLI flag → PILA_HEAL_DIR env var →
    heal_dir in pila.toml → HEAL_DIR_DEFAULT ("heal-out").
    The value is a plain directory name (or relative path) appended to
    the run dir — not validated against the filesystem at resolve time."""
    if cli_value and cli_value.strip():
        return cli_value.strip()
    env = os.environ.get(HEAL_DIR_ENV, "").strip()
    if env:
        return env
    cfg = repo_root / HEAL_DIR_FILE
    file_val = _read_toml_key(cfg, "heal_dir")
    if file_val is not None and file_val.strip():
        return file_val.strip()
    return HEAL_DIR_DEFAULT


def resolve_heal_max_rounds(repo_root: Path, cli_value: int | None = None) -> int:
    """Resolve the heal-loop max-iterations cap. Order:
    --heal-max-rounds CLI flag → PILA_HEAL_MAX_ROUNDS env var →
    heal_max_rounds in pila.toml → HEAL_MAX_ROUNDS_DEFAULT (10).
    An invalid (non-positive) value in env or file is rejected via die()."""
    if cli_value is not None:
        return cli_value
    env = os.environ.get(HEAL_MAX_ROUNDS_ENV, "").strip()
    if env:
        try:
            v = int(env)
        except ValueError:
            die(f"{HEAL_MAX_ROUNDS_ENV}={env!r} is not a positive integer")
        if v <= 0:
            die(f"{HEAL_MAX_ROUNDS_ENV}={env!r} must be a positive integer")
        return v
    cfg = repo_root / HEAL_MAX_ROUNDS_FILE
    file_val = _read_toml_key(cfg, "heal_max_rounds")
    if file_val is not None:
        try:
            v = int(file_val)
        except ValueError:
            die(f"{cfg}: heal_max_rounds={file_val!r} is not a positive integer")
        if v <= 0:
            die(f"{cfg}: heal_max_rounds={file_val!r} must be a positive integer")
        return v
    return HEAL_MAX_ROUNDS_DEFAULT


def resolve_heal_success_threshold(repo_root: Path,
                                   cli_value: float | None = None) -> float:
    """Resolve the heal-loop success pass-rate threshold. Order:
    --heal-success-threshold CLI flag → PILA_HEAL_SUCCESS_THRESHOLD env var →
    heal_success_threshold in pila.toml → HEAL_SUCCESS_THRESHOLD_DEFAULT (0.9).
    Value must be in (0, 1]; invalid values in env or file are rejected via die()."""
    if cli_value is not None:
        return cli_value
    env = os.environ.get(HEAL_SUCCESS_THRESHOLD_ENV, "").strip()
    if env:
        try:
            v = float(env)
        except ValueError:
            die(f"{HEAL_SUCCESS_THRESHOLD_ENV}={env!r} is not a float")
        if not (0.0 < v <= 1.0):
            die(f"{HEAL_SUCCESS_THRESHOLD_ENV}={env!r} must be in (0, 1]")
        return v
    cfg = repo_root / HEAL_SUCCESS_THRESHOLD_FILE
    file_val = _read_toml_key(cfg, "heal_success_threshold")
    if file_val is not None:
        try:
            v = float(file_val)
        except ValueError:
            die(f"{cfg}: heal_success_threshold={file_val!r} is not a float")
        if not (0.0 < v <= 1.0):
            die(f"{cfg}: heal_success_threshold={file_val!r} must be in (0, 1]")
        return v
    return HEAL_SUCCESS_THRESHOLD_DEFAULT


async def run_proc(cmd: list[str], *, cwd: str | None = None,
                   timeout: float | None = None) -> subprocess.CompletedProcess:
    """Async equivalent of `subprocess.run(cmd, capture_output=True, text=True)`.
    On timeout, kills the process and raises `subprocess.TimeoutExpired` — same
    semantics callers already handle. One helper everywhere keeps the asyncio
    boilerplate out of the call sites.

    `start_new_session=True` isolates the child into its own POSIX
    session/process group, distinct from pila's own. This is what lets
    `_terminate_proc_tree` send `os.killpg(proc.pid, ...)` on the
    cleanup path without accidentally signaling the orchestrator's own
    group. The flag is a no-op on Windows."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    try:
        if timeout is None:
            stdout, stderr = await proc.communicate()
        else:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        await _terminate_proc_tree(proc)
        raise subprocess.TimeoutExpired(cmd, timeout)
    except BaseException:
        # Any other exception (CancelledError from a parent abort, an unexpected
        # OSError/BrokenPipeError from the PIPE, etc.) must still leave no
        # orphan subtree. Terminate the process group then re-raise.
        await _terminate_proc_tree(proc)
        raise
    # Success path needs no descendant sweep: `run_proc` is used for short
    # synchronous commands (git, smoke tests, cleanup helpers) that do not
    # background tool calls the way `claude -p` workers do via Claude Code's
    # Bash tool. The detached-session leak class addressed by
    # `_DescendantTracker` is specific to `_invoke`, not here.
    return subprocess.CompletedProcess(
        cmd,
        proc.returncode if proc.returncode is not None else 0,
        stdout.decode(errors="replace") if stdout else "",
        stderr.decode(errors="replace") if stderr else "",
    )


async def run_streaming(
    cmd: list[str],
    *,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    timeout: float | None = None,
    log_path: Path | None = None,
    label: str | None = None,
    verbosity: str = "stream",
    line_prefix: str = "  | ",
    tail_lines: int = 40,
) -> tuple[int, str]:
    """Run a subprocess with stdout+stderr streamed live, persisted to a
    log file, and tailed for error reporting. Use this instead of
    `run_proc` for *long-running* commands where:

      - the user should see progress in real time (no silent multi-minute
        hangs while a buffered pipe fills),
      - the full output should land on disk regardless of verbosity, and
      - the last N lines should be available in any exception we raise.

    Returns `(returncode, tail)`. On timeout raises
    `subprocess.TimeoutExpired` (same shape `run_proc` raises) with
    `output` populated with the captured tail so callers can include it
    in their error message. On any other exception, terminates the
    process tree via `_terminate_proc_tree` (same exception-safety
    contract as `run_proc`).

    `verbosity`:
      - "quiet": no stdout echo; log file still gets every line.
      - anything else ("normal", "stream", "debug"): echo each line
        through `log()` with `line_prefix`.

    `label` is appended to the persistent log's section header — useful
    when multiple commands write to the same log file (provision.log
    accumulates `mise install`, `.pila-setup.sh`, etc.).

    The DRY counterpart to `run_proc`: identical contract for the
    process-group/exception-safety story, different I/O shape. Pick
    `run_proc` for short captures (git plumbing, smoke tests) where
    the synchronous-collect shape is what the caller wants; pick
    `run_streaming` for anything that might run long enough that a
    silent terminal would mislead the user.
    """
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=cwd,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,
    )

    tail: deque[str] = deque(maxlen=tail_lines)
    log_fh = None
    if log_path is not None:
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_fh = log_path.open("a", buffering=1)  # line-buffered
            header = f"=== {label or ' '.join(cmd)} (cwd={cwd or '.'}) ==="
            log_fh.write(header + "\n")
        except OSError:
            log_fh = None

    echo = verbosity != "quiet"

    async def read_loop() -> None:
        # proc.stdout is guaranteed non-None because we passed
        # stdout=asyncio.subprocess.PIPE above; no runtime check needed.
        async for raw in proc.stdout:
            line = raw.decode(errors="replace").rstrip("\r\n")
            tail.append(line)
            if log_fh is not None:
                try:
                    log_fh.write(line + "\n")
                except OSError:
                    pass
            if echo:
                log(f"{line_prefix}{line}")

    try:
        if timeout is None:
            await asyncio.gather(read_loop(), proc.wait())
        else:
            await asyncio.wait_for(
                asyncio.gather(read_loop(), proc.wait()),
                timeout=timeout,
            )
    except asyncio.TimeoutError:
        if log_fh is not None:
            try:
                log_fh.write(f"=== TIMEOUT after {timeout}s ===\n")
            except OSError:
                pass
        await _terminate_proc_tree(proc)
        captured = "\n".join(tail)
        exc = subprocess.TimeoutExpired(cmd, timeout)
        # Standard TimeoutExpired exposes `output` and `stderr`; populate
        # `output` with the merged tail so callers can include it in
        # their die() message without re-reading the log file.
        exc.output = captured
        if log_fh is not None:
            try:
                log_fh.close()
            except OSError:
                pass
        raise exc
    except BaseException:
        await _terminate_proc_tree(proc)
        if log_fh is not None:
            try:
                log_fh.close()
            except OSError:
                pass
        raise

    if log_fh is not None:
        try:
            log_fh.close()
        except OSError:
            pass

    rc = proc.returncode if proc.returncode is not None else 0
    return rc, "\n".join(tail)


async def gather_or_cancel(*aws):
    """Like asyncio.gather, but on the first exception cancel every other
    in-flight task and await its finalization before re-raising. Paired with
    run_proc's child-killing exception handler, this terminates in-flight
    `claude -p` subprocesses immediately on a failed-run abort instead of
    letting them burn the worker budget for up to worker_timeout_sec."""
    tasks = [asyncio.ensure_future(a) for a in aws]
    try:
        return await asyncio.gather(*tasks)
    except BaseException:
        for t in tasks:
            if not t.done():
                t.cancel()
        try:
            await asyncio.gather(*tasks, return_exceptions=True)
        except BaseException:
            # If the cleanup itself is cancelled or errors, drop that
            # secondary exception so the bare `raise` below re-raises the
            # original — the user wants the real failure cause, not noise
            # from the cleanup phase.
            pass
        raise


async def run_script(name: str, *args: str) -> subprocess.CompletedProcess:
    """Run one of the bundled git worktree scripts in the target repo."""
    return await run_proc(["bash", str(SCRIPTS / name), *args], cwd=os.getcwd())


# =========================================================================
# deterministic enforcement — no LLM involvement
#
# Prompts are advisory; code enforces. Every rule that can be checked
# mechanically lives here, not in a prompt. The LLM handles: understanding
# intent, writing code, decomposing tasks, resolving semantic conflicts.
# Code handles: counting, hashing, graph invariants, file existence, running
# test suites, enforcing structural rules.
# =========================================================================

async def preflight(pila_dir: Path, verbosity: str = VERBOSITY_DEFAULT,
                    skip_smoke: bool = False, no_push: bool = False) -> None:
    """Hard checks before any LLM work. Fails fast rather than wasting workers."""

    # 1. git user identity — missing config causes implementer commits to fail
    for key in ("user.email", "user.name"):
        r = await run_proc(["git", "config", key])
        if r.returncode != 0 or not r.stdout.strip():
            die(f"git {key} is not configured. "
                f"Run: git config --global {key} \"<value>\"")

    # 2. working tree must be clean — a dirty tree produces ambiguous diffs
    r = await run_proc(["git", "status", "--porcelain"])
    dirty = [l for l in r.stdout.splitlines() if not l.startswith("??")]
    if dirty:
        die(f"working tree has {len(dirty)} modified/staged file(s). "
            "Commit or stash before running pila.")

    # 3. (removed in per-run refactor) The global pila/* branch and
    #    .pila/worktrees/* checks used to fail a second concurrent
    #    run; they no longer apply now that each run namespaces its
    #    branches as pila/runs/<run-id> (and subtask branches as
    #    pila/subtasks/<run-id>/<sid>) and its worktrees under the
    #    per-run dir. A run_id collision is detected separately at
    #    State.rename_to() (filesystem side) and during setup-run.sh
    #    (git side). See DESIGN.md §6 and §14 ("single-clone parallelism").

    # 4. claude CLI version is recent enough for `--json-schema` in -p mode.
    #    Runs even when --skip-smoke is set: --skip-smoke is for skipping the
    #    *live* model call (auth + a turn), not for skipping local CLI sanity
    #    checks. Without this, a stale CLI fails the smoke test with a cryptic
    #    'unknown option' that tells the user nothing actionable.
    _check_claude_cli_version()

    # 5. gh CLI preflight moved to the host launcher (DESIGN §6
    #    *Finalization*). The launcher checks `gh auth status` + origin
    #    remote presence before spinning up this container; if they
    #    fail, the container never starts.

    # 6. live smoke-test: auth + --output-format stream-json + --json-schema inline.
    #    Catches auth failures before a 40-worker run starts. Streams so a slow
    #    Opus / heavy-context startup is visible — the previous max_turns=1
    #    failure was invisible in the non-streaming mode until exit.
    if not skip_smoke:
        log("preflight: smoke-testing claude -p…")
        cmd = ["claude", "-p", "respond with the single word ok",
               "--output-format", "stream-json",
               "--verbose",
               "--json-schema", '{"type":"object"}']
        try:
            envelope = await _invoke(cmd, cwd=os.getcwd(), timeout=90,
                                     sid="smoke",
                                     pila_dir=pila_dir,
                                     verbosity=verbosity)
        except subprocess.TimeoutExpired:
            die("claude -p smoke test timed out — auth issue or network problem")
        except WorkerError as e:
            die(f"claude -p smoke test failed: {e}")
        if envelope.get("is_error"):
            die(f"claude -p smoke test returned an error: "
                f"{envelope.get('api_error_status') or envelope.get('result')}")
        log("preflight: ok")


_ID_PREFIXES = frozenset(f"{v}-" for v in CATEGORY_ABBREV.values())


_VALID_EXTENTS = frozenset({"in_plan", "external"})


def validate_plan(subtasks: dict) -> None:
    """Structural validation of the merged plan — pure Python set operations.

    `requires` entries are objects `{tag, extent, reason?}` per DESIGN §5
    `requires.extent`. The JSON schema (`_REQUIRES_ITEM`) enforces the
    shape; this function enforces the conditional invariants that
    vanilla JSON Schema cannot express, and verifies the in-plan
    producer-side of cross-domain dependencies. `extent: external`
    entries are deliberately *not* checked for a provider — they are
    declared out-of-graph by the planner and surface in `plan.json`'s
    `preconditions` section."""
    errors: list[str] = []

    # all provides tags across every subtask — used for requires resolution
    all_provides: set[str] = set()
    for s in subtasks.values():
        all_provides.update(s.get("provides", []))

    all_ids = set(subtasks.keys())
    for sid, s in subtasks.items():
        if not any(sid.startswith(p) for p in _ID_PREFIXES):
            errors.append(f"{sid}: id must start with one of "
                          f"{sorted(_ID_PREFIXES)} — cross-domain collisions "
                          "and audit-trail ambiguity otherwise")
        if s.get("size", "").lower() == "large":
            errors.append(f"{sid}: size='large' — planner must split it further")
        if not (s.get("success_criteria_seed") or "").strip():
            errors.append(f"{sid}: success_criteria_seed is empty — "
                          "implementer has no starting point for criteria")
        for dep in s.get("depends_on", []):
            if dep not in all_ids:
                errors.append(f"{sid}: depends_on '{dep}' which does not exist "
                              "— scheduler will silently drop this edge")
        for entry in s.get("requires", []):
            # Defensive: the JSON schema rejects bare strings before this
            # function runs, but the planner output gets mutated downstream
            # (rename / promotion) so re-check shape here.
            if not isinstance(entry, dict):
                errors.append(f"{sid}: requires entry must be an object "
                              f"{{tag, extent, reason?}}, got {entry!r}")
                continue
            tag = entry.get("tag", "")
            extent = entry.get("extent", "")
            reason = (entry.get("reason") or "").strip()
            if not tag or not isinstance(tag, str):
                errors.append(f"{sid}: requires entry has empty or non-string "
                              f"tag: {entry!r}")
                continue
            if extent not in _VALID_EXTENTS:
                errors.append(f"{sid}: requires '{tag}' has unknown extent "
                              f"{extent!r} — must be one of "
                              f"{sorted(_VALID_EXTENTS)}")
                continue
            if extent == "external" and not reason:
                errors.append(f"{sid}: requires '{tag}' with extent=external "
                              "must include a non-empty `reason` naming the "
                              "owner (other repo, ops runbook, manual step) "
                              "and why no in-repo subtask could produce it")
                continue
            if extent == "in_plan" and tag not in all_provides:
                errors.append(f"{sid}: requires '{tag}' but nothing provides it — "
                              "dependency is unresolvable and will be silently dropped")

    if errors:
        bullet = "\n".join(f"  • {e}" for e in errors)
        die(f"plan validation failed ({len(errors)} issue(s)):\n{bullet}")
    log(f"plan validation: {len(subtasks)} subtasks ok")


def warn_cross_planner_file_overlap(plans: list[dict]) -> None:
    """Log a warning when subtasks from different planner outputs both list
    the same path in `files_likely_touched`. Two planners decomposing the
    same surface produces contradictory criteria the integrator can't
    reconcile — surface that risk at plan-validation time so the user can
    re-frame the task before workers start.

    Empirically (n=3 historical runs in May 2026): a successful run had 0
    cross-planner overlaps; two failed runs had 9 and 10 respectively. The
    naive cross-prefix overlap signal had zero false positives in that
    data. This is a warning, not a hard fail — same-file overlap is
    sometimes legitimate (one planner adds scaffolding the other consumes)
    and the integrator is still the backstop. The future-work item is to
    extend the reconciler to resolve overlaps automatically (DESIGN §5)."""
    file_owners: dict[str, list[tuple[str, str]]] = {}
    for plan in plans:
        domain = plan.get("domain") or "?"
        for s in plan.get("subtasks", []):
            sid = s.get("id", "?")
            for f in s.get("files_likely_touched", []):
                file_owners.setdefault(f, []).append((domain, sid))
    overlaps = {f: owners for f, owners in file_owners.items()
                if len({d for d, _ in owners}) > 1}
    if not overlaps:
        return
    log(f"⚠  cross-planner file overlap: {len(overlaps)} file(s) claimed by "
        "multiple planners. Two planners decomposing the same surface "
        "produces contradictory subtask criteria the integrator may not "
        "be able to reconcile. Review the plan before proceeding.")
    for f, owners in sorted(overlaps.items()):
        per = ", ".join(f"{d}({sid})" for d, sid in sorted(owners))
        log(f"     {f}: {per}")


def _resolves_under(path_str: str, root: Path) -> bool:
    """True iff `path_str` (relative or absolute) resolves under `root`.
    Resolves symlinks so a planner cannot sneak a path through with a
    symlinked decoy. Returns False on any OSError or ValueError —
    treated by the caller as "not under root," which fails the check
    and triggers a drop (the safe direction)."""
    try:
        candidate = Path(path_str)
        if not candidate.is_absolute():
            candidate = root / candidate
        return candidate.resolve().is_relative_to(root.resolve())
    except (OSError, ValueError):
        return False


def filter_offtree_subtasks(plans: list[dict], repo_root: Path,
                            inspect_dirs: list[str], st: "State") -> None:
    """Mutate `plans` in place: drop any subtask whose `files_likely_touched`
    contains a path that does not resolve under `repo_root`. Record drops
    in `st.data["dropped_subtasks"]` and log a per-subtask warning. Soft
    drop — the run continues with the surviving subtasks, `schedule()`
    runs after this and sees a clean plan.

    Motivation: cross-repo `--inspect-dir` runs let the planner read
    files in mounts like `/inspect/api/...`, and the planner sometimes
    names those paths in `files_likely_touched` for an implementer that
    can only modify the run's primary worktree. The implementer either
    fails outright or clones the inspected repo into `/tmp` and edits
    there — those edits never reach the subtask branch, and
    `check_branch_has_commits` correctly fails the subtask.

    Why a soft drop and not `die()`: a hard fail here is unrecoverable
    via `--resume`. The resume branch in `_run_phases` does not re-run
    `phase_plan` or this filter, and `state.json["waves"]` is only
    written by `write_plan` which runs after `schedule()`. Soft drop
    matches the existing `warn_cross_planner_file_overlap` pattern at
    the same pre-schedule layer."""
    inspect_roots = [Path(d).resolve() for d in (inspect_dirs or [])]
    dropped: dict[str, dict] = {}
    for plan in plans:
        survivors = []
        for s in plan.get("subtasks", []):
            sid = s.get("id", "?")
            offtree_paths = [
                f for f in (s.get("files_likely_touched") or [])
                if not _resolves_under(f, repo_root)
            ]
            if not offtree_paths:
                survivors.append(s)
                continue
            reasons = []
            for f in offtree_paths:
                leaked = next((str(r) for r in inspect_roots
                               if _resolves_under(f, r)), None)
                if leaked:
                    reasons.append(
                        f"{f!r} resolves under inspect-dir {leaked!r} "
                        "(read-only; implementer cannot modify)")
                else:
                    reasons.append(
                        f"{f!r} does not resolve under repo root "
                        f"{str(repo_root)!r}")
            dropped[sid] = {"reasons": reasons, "files": offtree_paths}
        plan["subtasks"] = survivors
    if not dropped:
        return
    log(f"⚠  filter_offtree_subtasks: dropped {len(dropped)} subtask(s) "
        "with off-tree files_likely_touched:")
    for sid, info in sorted(dropped.items()):
        for r in info["reasons"]:
            log(f"     {sid}: {r}")
    st.data.setdefault("dropped_subtasks", {}).update(dropped)
    st.save()


# --- per-repo dependency provisioning ----------------------------------------
# See DESIGN.md §6½ "Per-repo dependency provisioning" and IMPLEMENTATION.md
# §6½ for the layered design (.pila-setup.sh → mise install → table → LLM
# fallback → worktree replay).

# argv[0] allowlist for any provision command — both table-emitted commands
# and the LLM-fallback recipe. Validated by validate_provision_recipe().
# Anything outside this set is rejected; the §12 carve-out (the LLM
# fallback worker) is mechanically contained by this list.
_PROVISION_ARGV0_ALLOW = frozenset({
    "pnpm", "npm", "yarn", "pip", "pip3", "uv", "poetry", "pipenv",
    "go", "cargo", "bundle", "gem", "mvn", "gradle", "gradlew", "make",
})

# Shell metacharacters that must not appear anywhere in a command argv.
# A command emitted as a true argv list cannot legitimately need any of
# these — the executor invokes it directly with no shell, and any
# metacharacter is a sign the recipe was malformed or smuggling shell
# semantics through the validator.
_PROVISION_SHELL_METACHARS = frozenset(set("|&;$`><\n\r"))


def _lockfile_table_entries(repo_root: Path) -> list[dict]:
    """The deterministic lockfile → install-command table. Returns a list of
    recipe entries (possibly empty) — polyglot repos like Rails-with-frontend
    emit ALL matching commands, not first-match-wins. See IMPLEMENTATION.md
    §6½ for the full table.

    Each entry is the minimal recipe shape: {kind, command, working_dir,
    timeout_s}. Callers compose them into a full recipe and persist to
    st.data["provision"]["recipe"].
    """
    entries: list[dict] = []

    # --- Node.js: pnpm > yarn > npm precedence ---
    # The precedence is documented at the pnpm and yarn sites: a repo that
    # commits multiple lockfiles is rare, but when it happens the most-
    # specific one wins. pnpm-lock.yaml means the team has chosen pnpm even
    # if package-lock.json was left behind from a prior tool.
    has_pnpm = (repo_root / "pnpm-lock.yaml").is_file()
    has_yarn = (repo_root / "yarn.lock").is_file()
    has_npm = (repo_root / "package-lock.json").is_file()
    if has_pnpm:
        entries.append({
            "kind": "install",
            "command": ["pnpm", "install", "--frozen-lockfile"],
            "working_dir": ".",
            "timeout_s": 1800,
        })
    elif has_yarn:
        entries.append({
            "kind": "install",
            "command": ["yarn", "install", "--frozen-lockfile"],
            "working_dir": ".",
            "timeout_s": 1800,
        })
    elif has_npm:
        entries.append({
            "kind": "install",
            "command": ["npm", "ci"],
            "working_dir": ".",
            "timeout_s": 1800,
        })

    # --- Python: uv > poetry > pipenv. Bare requirements.txt and bare
    # pyproject.toml (without a lockfile) deliberately do NOT match — they
    # are the ambiguous tail that goes to the LLM fallback (verified
    # against Django, which uses `pip install -e .`).
    if (repo_root / "uv.lock").is_file():
        entries.append({
            "kind": "install",
            "command": ["uv", "sync"],
            "working_dir": ".",
            "timeout_s": 1800,
        })
    elif (repo_root / "poetry.lock").is_file():
        entries.append({
            "kind": "install",
            "command": ["poetry", "install"],
            "working_dir": ".",
            "timeout_s": 1800,
        })
    elif (repo_root / "Pipfile.lock").is_file():
        entries.append({
            "kind": "install",
            "command": ["pipenv", "install"],
            "working_dir": ".",
            "timeout_s": 1800,
        })

    # --- Go ---
    if (repo_root / "go.mod").is_file() and (repo_root / "go.sum").is_file():
        entries.append({
            "kind": "install",
            "command": ["go", "mod", "download"],
            "working_dir": ".",
            "timeout_s": 1800,
        })

    # --- Rust ---
    if (repo_root / "Cargo.lock").is_file():
        entries.append({
            "kind": "install",
            "command": ["cargo", "fetch"],
            "working_dir": ".",
            "timeout_s": 1800,
        })

    # --- Ruby ---
    if (repo_root / "Gemfile.lock").is_file():
        entries.append({
            "kind": "install",
            "command": ["bundle", "install"],
            "working_dir": ".",
            "timeout_s": 1800,
        })

    return entries


def detect_recipe_from_lockfiles(repo_root: Path) -> list[dict]:
    """Public entry point for the deterministic detection layer. Returns
    a list of recipe entries (possibly empty). An empty list means the
    table abstained and the caller should fall back to the LLM worker.
    """
    return _lockfile_table_entries(repo_root)


def validate_provision_recipe(recipe: list[dict]) -> None:
    """Mechanically bound the provision recipe. Raises ValueError on any
    violation. Called for BOTH the table-emitted recipe and the LLM-
    fallback recipe — the §12 carve-out for the LLM worker is contained
    here, not in the prompt.

    Invariants enforced:
      - command is a non-empty argv list (no shell strings).
      - command[0] is in _PROVISION_ARGV0_ALLOW (or the entry is kind: none).
      - No shell metacharacters anywhere in the argv (no piping, no
        redirection, no command substitution).
      - No `sudo` anywhere.
      - working_dir is "." or a relative path with no ".." segments and
        no leading "/" (worker cannot reach outside the repo).
      - kind is one of {install, build, none}.
    """
    if not isinstance(recipe, list):
        raise ValueError(f"recipe must be a list, got {type(recipe).__name__}")
    for i, entry in enumerate(recipe):
        if not isinstance(entry, dict):
            raise ValueError(f"recipe[{i}] is not a dict: {entry!r}")
        kind = entry.get("kind")
        if kind not in ("install", "build", "none"):
            raise ValueError(
                f"recipe[{i}].kind={kind!r} must be one of install|build|none")
        if kind == "none":
            # `none` entries are bypass markers; no command required.
            continue
        cmd = entry.get("command")
        if not isinstance(cmd, list) or not cmd:
            raise ValueError(
                f"recipe[{i}].command must be a non-empty argv list")
        if any(not isinstance(a, str) for a in cmd):
            raise ValueError(
                f"recipe[{i}].command must be a list of strings")
        if cmd[0] not in _PROVISION_ARGV0_ALLOW:
            raise ValueError(
                f"recipe[{i}].command[0]={cmd[0]!r} is not in the allowed "
                f"package-manager set {sorted(_PROVISION_ARGV0_ALLOW)}")
        for j, arg in enumerate(cmd):
            if arg == "sudo":
                raise ValueError(
                    f"recipe[{i}].command contains 'sudo' at position {j}")
            bad = _PROVISION_SHELL_METACHARS & set(arg)
            if bad:
                raise ValueError(
                    f"recipe[{i}].command[{j}]={arg!r} contains shell "
                    f"metacharacters {sorted(bad)}")
        wd = entry.get("working_dir")
        if not isinstance(wd, str) or not wd:
            raise ValueError(
                f"recipe[{i}].working_dir must be a non-empty string")
        if wd.startswith("/"):
            raise ValueError(
                f"recipe[{i}].working_dir={wd!r} must be relative, not absolute")
        # ".." anywhere in the path — split on both `/` and `\` so a
        # Windows-style smuggling attempt is also caught.
        parts = wd.replace("\\", "/").split("/")
        if ".." in parts:
            raise ValueError(
                f"recipe[{i}].working_dir={wd!r} contains '..' "
                "(must not traverse outside the repo)")


# Section-header regex for the README extractor. Matches install/setup-
# adjacent words. Verified against 15 real OSS READMEs (DESIGN §6½ + the
# verification corpus in tests/test_readme_extractor.py): catches 13/15.
# The two known misses (Supabase, esbuild) are marketing-style READMEs
# that delegate install to external docs — those repos route through
# .pila-setup.sh.
_README_SECTION_RE = re.compile(
    r"(?i)\b("
    r"install"
    r"|getting[\s-]?started"
    r"|quick[\s-]?start"
    r"|setup"
    r"|usage"
    r"|\brun\b"
    r"|develop"
    r"|build(ing)?( from source| instructions)?"
    r"|compil(e|ing)( from source)?"
    r"|download"
    r"|from source"
    r"|requirements"
    r"|prerequisites"
    r"|dependenc(y|ies)"
    r")\b"
)

# Strip leading markdown-decoration glyphs (emoji, bullets, punctuation)
# from a header line before keyword matching. Handles `## 🚀 Getting
# Started` and `## • Install` without losing the keyword. The character
# class is intentionally permissive — emoji span several Unicode blocks,
# so we whitelist ASCII word characters / spaces instead and strip
# everything else from the left.
_HEADER_DECOR_RE = re.compile(r"^[^\w]+", flags=re.UNICODE)

# Code-fence content heuristics for the fallback layer. Used when no
# header matches: keep code fences that contain recognizable install
# commands so the LLM still sees the project's documented invocation.
_INSTALL_CMD_HINT_RE = re.compile(
    r"\b(pip|pip3|npm|pnpm|yarn|uv|poetry|cargo|brew|apt|apt-get|dnf|"
    r"yum|pacman|go install|make|bundle install|gem install|mise install)\b"
)


def _split_readme_headers(text: str) -> list[tuple[int, str, str]]:
    """Return [(line_index, header_text, body_until_next_header), ...] for
    text. Supports three header styles:
      - ATX: lines starting with `#`, `##`, etc.
      - Setext: a line followed by `===` or `---` of equal length.
      - Asciidoc: lines starting with `==`, `===`, etc. (no `#`).

    Returns sections in document order. The first section's header text
    is "" if the file does not start with a header (the "intro").
    """
    lines = text.split("\n")
    n = len(lines)
    # Find header line indices first.
    headers: list[tuple[int, str]] = []  # (line_index, header_text)
    i = 0
    while i < n:
        line = lines[i]
        stripped = line.strip()
        # ATX (`# Foo`, `## Foo`, etc.). Asciidoc level markers (`==
        # Foo`) are picked up too — the leading `=` group reads as
        # header decoration once we strip it for keyword matching.
        if stripped.startswith("#") or stripped.startswith("=="):
            headers.append((i, stripped))
            i += 1
            continue
        # Setext: `Foo\n=====` (h1) or `Foo\n-----` (h2). The underline
        # must be at least 3 chars of `=` or `-` and roughly the length
        # of the line above (RST conventions are looser than Markdown's
        # but we accept any underline ≥3 chars).
        if i + 1 < n and stripped:
            nxt = lines[i + 1].strip()
            if len(nxt) >= 3 and (set(nxt) == {"="} or set(nxt) == {"-"}):
                headers.append((i, stripped))
                i += 2
                continue
        i += 1

    if not headers:
        return [(0, "", text)]

    sections: list[tuple[int, str, str]] = []
    # Intro before the first header.
    if headers[0][0] > 0:
        intro_body = "\n".join(lines[: headers[0][0]])
        sections.append((0, "", intro_body))
    for k, (start, hdr) in enumerate(headers):
        end = headers[k + 1][0] if k + 1 < len(headers) else n
        body = "\n".join(lines[start: end])
        sections.append((start, hdr, body))
    return sections


def _is_install_section(header: str) -> bool:
    """True if a header (after decoration-strip) matches the section
    regex. Empty header (the intro) is not an install section by
    definition."""
    if not header:
        return False
    cleaned = _HEADER_DECOR_RE.sub("", header)
    return bool(_README_SECTION_RE.search(cleaned))


def _slice_code_fences_with_install_hints(text: str, ctx_lines: int = 10) -> str:
    """Fallback layer: scan for fenced code blocks containing recognized
    install commands and return them with ±ctx_lines of surrounding
    context. Used when the header-aware extractor finds no install
    section."""
    lines = text.split("\n")
    n = len(lines)
    in_fence = False
    fence_start = -1
    fence_marker = ""
    kept_ranges: list[tuple[int, int]] = []  # inclusive [start, end] line indices
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if not in_fence:
            if stripped.startswith("```") or stripped.startswith("~~~"):
                in_fence = True
                fence_start = i
                fence_marker = stripped[:3]
        else:
            if stripped.startswith(fence_marker):
                # Fence closed at line i.
                fence_text = "\n".join(lines[fence_start: i + 1])
                if _INSTALL_CMD_HINT_RE.search(fence_text):
                    lo = max(0, fence_start - ctx_lines)
                    hi = min(n - 1, i + ctx_lines)
                    kept_ranges.append((lo, hi))
                in_fence = False
                fence_start = -1
                fence_marker = ""
    if not kept_ranges:
        return ""
    # Merge overlapping ranges in order.
    merged: list[tuple[int, int]] = []
    for lo, hi in sorted(kept_ranges):
        if merged and lo <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], hi))
        else:
            merged.append((lo, hi))
    pieces = ["\n".join(lines[lo: hi + 1]) for lo, hi in merged]
    return "\n\n…\n\n".join(pieces)


# Per-extract budgets, in bytes. README ≤1KB intro + matched sections
# under an 8KB total cap. The fixture set as a whole is capped at 24KB
# by gather_provision_fixtures().
_README_INTRO_BUDGET = 1024
_README_EXTRACT_BUDGET = 8192
_README_FALLBACK_BUDGET = 6144  # final top-of-file fallback
_FIXTURE_TOTAL_BUDGET = 24576   # 24KB hard ceiling per repo


def extract_readme_sections(text: str) -> str:
    """Extract the install/setup-relevant slice of a README.

    Fallback chain (DESIGN §6½):
      1. Header-aware: ≤1KB intro + sections whose header matches
         _README_SECTION_RE, under an 8KB total cap.
      2. Code-fence hint: if no section header matches, scan for code
         fences containing install commands (pip/npm/cargo/etc.) and
         keep them with ±10 lines of surrounding context.
      3. Final: top-6KB of the README verbatim.

    Returns the extracted text (≤8KB). Empty input → empty output.
    """
    if not text:
        return ""
    sections = _split_readme_headers(text)
    out_parts: list[str] = []
    used = 0

    # Intro budget: first section body, whether labeled or not. A README
    # that starts with `# Project\n\nElevator pitch.\n\n## Install` has
    # its first section named "Project" (not ""), but the elevator pitch
    # is still the intro from the user's point of view. Including the
    # first section's body (up to the intro budget) keeps that signal
    # without making the whole top-level section count as an install
    # section.
    if sections:
        first_body = sections[0][2][:_README_INTRO_BUDGET]
        if first_body.strip():
            out_parts.append(first_body)
            used += len(first_body)

    matched_any = False
    for _idx, hdr, body in sections:
        if not _is_install_section(hdr):
            continue
        matched_any = True
        if used >= _README_EXTRACT_BUDGET:
            break
        room = _README_EXTRACT_BUDGET - used
        out_parts.append(body[:room])
        used += min(len(body), room)

    if matched_any:
        return "\n\n".join(out_parts)

    # Fallback 2: code-fence install-hint slicer.
    fence_slice = _slice_code_fences_with_install_hints(text)
    if fence_slice:
        intro_part = out_parts[0] if out_parts else ""
        fence_room = max(0, _README_EXTRACT_BUDGET - len(intro_part))
        fence_part = fence_slice[:fence_room]
        if intro_part:
            return intro_part + "\n\n" + fence_part
        return fence_part

    # Fallback 3: top-6KB.
    return text[:_README_FALLBACK_BUDGET]


def _read_file_safely(path: Path, budget: int) -> str:
    """Read a file with a byte ceiling, swallowing missing-file and
    decode errors. Used by gather_provision_fixtures to assemble the
    fixture dict from optional repo files."""
    try:
        return path.read_text(errors="replace")[:budget]
    except (OSError, UnicodeError):
        return ""


# Manifest file groups for the fixture gatherer.
_PROVISION_ROOT_MANIFESTS = (
    "package.json", "pyproject.toml", "go.mod", "Cargo.toml",
    "Gemfile", "Makefile", "pom.xml",
    "build.gradle", "build.gradle.kts",
)
_PROVISION_WORKFLOW_PREFERRED_RE = re.compile(r"(?i)\b(ci|test|build|release)\b")
_PROVISION_WORKFLOW_SKIP_RE = re.compile(r"(?i)\b(codeql|stale|dependabot)\b")


def _sample_workspace_manifests(repo_root: Path, pkg_json_text: str,
                                 per_file_budget: int,
                                 max_files: int) -> list[tuple[str, str]]:
    """For a monorepo whose root package.json declares `workspaces`,
    return up to `max_files` sampled child manifests as (rel_path, text)
    pairs. Returns [] if no workspaces are declared or no children are
    found."""
    try:
        pkg = json.loads(pkg_json_text)
    except (ValueError, TypeError):
        return []
    workspaces = pkg.get("workspaces")
    if isinstance(workspaces, dict):
        # npm/yarn shape: {"packages": [...]}
        workspaces = workspaces.get("packages")
    if not isinstance(workspaces, list) or not workspaces:
        return []

    sampled: list[tuple[str, str]] = []
    seen: set[Path] = set()
    for pattern in workspaces:
        if len(sampled) >= max_files:
            break
        if not isinstance(pattern, str):
            continue
        # glob via Path.glob (handles `packages/*` style).
        try:
            for child in sorted(repo_root.glob(pattern + "/package.json")):
                if len(sampled) >= max_files:
                    break
                if child in seen:
                    continue
                seen.add(child)
                rel = child.relative_to(repo_root).as_posix()
                sampled.append((rel, _read_file_safely(child, per_file_budget)))
        except (OSError, ValueError):
            continue
    return sampled


def gather_provision_fixtures(repo_root: Path) -> dict:
    """Assemble the LLM-fallback worker's input set. Returns a dict with
    keys:
      - readme: header-aware extract (≤8KB)
      - manifests: dict[rel_path -> text] of root manifest files present
      - workspace_manifests: list[(rel_path, text)] sampled child
        manifests for monorepos (≤3 files, 1KB each)
      - workflows: list[(filename, text)] up to 2 GitHub Actions files
        preferring ci/test/build/release names
      - contributing: text of CONTRIBUTING.md or docs/DEVELOPMENT.md
        (≤4KB) or empty
      - total_bytes: int — actual size after assembly
      - hit_ceiling: bool — True if any section was truncated by the
        24KB total budget

    See DESIGN.md §6½ "Provision-worker input fixtures."
    """
    out: dict = {
        "readme": "",
        "manifests": {},
        "workspace_manifests": [],
        "workflows": [],
        "contributing": "",
        "total_bytes": 0,
        "hit_ceiling": False,
    }

    def add_bytes(n: int) -> bool:
        """Return True if we have budget for `n` more bytes; flip
        hit_ceiling otherwise."""
        if out["total_bytes"] + n > _FIXTURE_TOTAL_BUDGET:
            out["hit_ceiling"] = True
            return False
        out["total_bytes"] += n
        return True

    # --- README ---
    readme_paths = [
        repo_root / "README.md",
        repo_root / "README.rst",
        repo_root / "README",
        repo_root / "README.txt",
        repo_root / "README.adoc",
    ]
    for rp in readme_paths:
        if rp.is_file():
            raw = _read_file_safely(rp, _README_EXTRACT_BUDGET * 4)
            extract = extract_readme_sections(raw)
            if add_bytes(len(extract)):
                out["readme"] = extract
            break

    # --- Root manifests ---
    pkg_json_text = ""
    for name in _PROVISION_ROOT_MANIFESTS:
        if out["hit_ceiling"]:
            break
        p = repo_root / name
        if not p.is_file():
            continue
        text = _read_file_safely(p, 8192)
        if not add_bytes(len(text)):
            break
        out["manifests"][name] = text
        if name == "package.json":
            pkg_json_text = text

    # --- Workspace child manifests (monorepo) ---
    if pkg_json_text and not out["hit_ceiling"]:
        children = _sample_workspace_manifests(
            repo_root, pkg_json_text, per_file_budget=1024, max_files=3)
        for rel, text in children:
            if not add_bytes(len(text)):
                break
            out["workspace_manifests"].append((rel, text))

    # --- Workflows ---
    if not out["hit_ceiling"]:
        wf_dir = repo_root / ".github" / "workflows"
        if wf_dir.is_dir():
            try:
                candidates = [p for p in sorted(wf_dir.iterdir())
                              if p.suffix in (".yml", ".yaml") and p.is_file()
                              and not _PROVISION_WORKFLOW_SKIP_RE.search(p.name)]
            except OSError:
                candidates = []
            # Prefer files whose names match ci/test/build/release.
            preferred = [p for p in candidates
                         if _PROVISION_WORKFLOW_PREFERRED_RE.search(p.name)]
            others = [p for p in candidates if p not in preferred]
            ordered = preferred + others
            for p in ordered[:2]:
                text = _read_file_safely(p, 4096)
                if not add_bytes(len(text)):
                    break
                out["workflows"].append((p.name, text))

    # --- CONTRIBUTING / DEVELOPMENT ---
    if not out["hit_ceiling"]:
        for cand in (repo_root / "CONTRIBUTING.md",
                     repo_root / "docs" / "DEVELOPMENT.md",
                     repo_root / "DEVELOPMENT.md"):
            if cand.is_file():
                text = _read_file_safely(cand, 4096)
                if add_bytes(len(text)):
                    out["contributing"] = text
                break

    return out


# --- checkpoint validation ---------------------------------------------------

_CHECKPOINT_SECTIONS = [
    "## Frozen success criteria",
    "## Current status",
    "## Files touched",
    "## Decisions made",
    "## Evidence gate status",
    "## Next action",
    "## Open unknowns",
]

# Sections where "nothing to report" is a legitimate answer — a worker that
# made no decisions yet, or has no open unknowns, should be able to say so.
# Every other section must carry real content for the successor to pick up.
_CHECKPOINT_SECTIONS_ALLOW_NONE = {"## Decisions made", "## Open unknowns"}

# Single-token substitutes for content. A required section that contains
# only one of these is not a checkpoint, it's a placeholder — the
# successor would learn nothing from it. `_normalize_for_noise()` strips
# trailing punctuation and collapses repeated `?` before the membership
# check, so `None.`, `TBD!`, and `???` are caught alongside the bare
# tokens.
_NOISE_TOKENS = {
    "none", "n/a", "na", "tbd",
    "nothing", "unknown", "todo", "pending",
    "—", "--", "-", "?",
}


def _split_checkpoint_sections(content: str) -> dict[str, list[str]]:
    """Split a checkpoint file by `## ` headers into {header: lines}.
    Lines are stripped; blanks dropped. Returns one bucket per header
    found, in the order they appeared."""
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for raw in content.splitlines():
        if raw.startswith("## "):
            current = raw.rstrip()
            sections.setdefault(current, [])
            continue
        if current is None:
            continue
        stripped = raw.strip()
        if stripped:
            sections[current].append(stripped)
    return sections


def validate_checkpoint(path: str,
                        worktree_root: Path | None = None) -> str | None:
    """Return an error description if the checkpoint is structurally incomplete,
    None if it looks good. A missing section produces a confused successor;
    so does a section that contains only a placeholder.

    `worktree_root`, when supplied, enables the freshness check on
    `## Files touched`: every path listed there must either exist in the
    worktree or carry a `[deleted]` annotation in its bullet line. Skip the
    freshness check when the worktree is gone (e.g. cleaned up already)."""
    p = Path(path)
    if not p.exists():
        return f"checkpoint file does not exist: {path}"
    content = p.read_text()

    missing = [s for s in _CHECKPOINT_SECTIONS if s not in content]
    if missing:
        return (f"missing {len(missing)} required section(s): "
                f"{', '.join(missing)}")

    sections = _split_checkpoint_sections(content)
    for header in _CHECKPOINT_SECTIONS:
        lines = sections.get(header, [])
        if not lines:
            return f"section '{header}' has no content"
        # Reject single-token noise placeholders in the sections that MUST
        # carry real handoff context. Allow them only in the two sections
        # where "nothing to report" is a legitimate answer.
        if header in _CHECKPOINT_SECTIONS_ALLOW_NONE:
            continue
        if all(_normalize_for_noise(l) in _NOISE_TOKENS for l in lines):
            return (f"section '{header}' contains only placeholder tokens "
                    f"({lines!r}) — successor cannot resume from this")

    # Freshness check: paths under `## Files touched` must still exist in
    # the worktree (or be explicitly marked [deleted]). A stale checkpoint
    # naming a file the successor cannot find produces wasted re-discovery.
    if worktree_root is not None and worktree_root.exists():
        for line in sections.get("## Files touched", []):
            path_str, is_deleted = _parse_touched_file_line(line)
            if path_str is None:
                continue  # not a path-shaped line; skip narration
            if is_deleted:
                continue
            if not (worktree_root / path_str).exists():
                return (f"`## Files touched` lists '{path_str}' but the file "
                        "does not exist in the worktree and is not flagged "
                        "[deleted] — checkpoint is stale")

    return None


def _normalize_for_noise(line: str) -> str:
    """Reduce a checkpoint line to its comparison key for `_NOISE_TOKENS`.
    Strips the bullet marker, lowercases, collapses a pure run of `?` to a
    single `?`, then peels off trailing `.`/`!`/`…` — so `None.`, `TBD!`,
    and `???` all match their bare-token forms. The `?`-collapse runs
    before the trailing-punctuation strip; otherwise `???` would be
    eaten down to the empty string and miss the `?` token entirely."""
    s = _strip_bullet(line).lower().strip()
    if s and set(s) == {"?"}:
        s = "?"
    while s and s[-1] in ".!…":
        s = s[:-1].rstrip()
    return s


def _strip_bullet(line: str) -> str:
    """Strip leading markdown bullet markers (`-`, `*`, `1.`) before noise
    comparison. `- none` should be rejected the same as bare `none`."""
    stripped = line.lstrip()
    for prefix in ("- ", "* ", "+ "):
        if stripped.startswith(prefix):
            return stripped[len(prefix):].strip()
    # numbered list: `1. `, `2. `, …
    if len(stripped) >= 3 and stripped[0].isdigit():
        i = 1
        while i < len(stripped) and stripped[i].isdigit():
            i += 1
        if stripped[i:i+2] == ". ":
            return stripped[i+2:].strip()
    return stripped


def _parse_touched_file_line(line: str) -> tuple[str | None, bool]:
    """Extract a file path from a `## Files touched` line and detect the
    `[deleted]` annotation. Returns (path, is_deleted) or (None, False)
    if the line doesn't look like a path entry. Conservative: only treats
    a line as path-shaped if its first whitespace-delimited token looks
    like a relative path (contains `/`, `.`, or ends with a common code
    extension). Narration lines without a path token are skipped."""
    body = _strip_bullet(line)
    if not body:
        return (None, False)
    is_deleted = "[deleted]" in body.lower()
    # The first token is the candidate path; strip backticks and trailing
    # punctuation that often surrounds paths in markdown.
    first = body.split()[0].strip("`,:;()[]")
    if not first or first.startswith("#"):
        return (None, False)
    # Only treat as a path if it has a separator or a dot — a bare word
    # like "refactored" is narration, not a path.
    if "/" in first or "." in first:
        return (first, is_deleted)
    return (None, False)


# --- result cross-field validation -------------------------------------------

def validate_result(result: dict) -> str | None:
    """Cross-field invariant checks that JSON Schema cannot express.
    Returns an error string if the result is self-contradictory, None if ok.

    Per DESIGN §8, the §8 confidence gate is the only load-bearing
    discipline; the criteria file is informational (DESIGN §9). A
    `complete` status is accepted regardless of what `criteria_results`
    carries — empty, missing, or with `met:false` entries are all
    valid. The unmet entries are recorded on the result for telemetry
    and surface as conformance warnings, but do not affect terminal
    status. The other branches (handoff, blocked, failed, clarification)
    still enforce the mechanical-precondition fields their next-step
    consumers require."""
    status = result.get("status")
    if status == "incomplete-handoff":
        cp = result.get("checkpoint_path")
        if not cp:
            return "status='incomplete-handoff' but checkpoint_path is null"
        if not Path(cp).exists():
            return f"checkpoint_path '{cp}' does not exist on disk"
    elif status == "blocked":
        if not (result.get("blocker") or "").strip():
            return "status='blocked' but blocker field is empty"
    elif status == "failed":
        # A `failed` result without a summary is a worker contract violation:
        # the prompt requires a diagnosis, and `_retryable_failure` needs real
        # text to classify against. Without it the run drops a canned string
        # ("worker reported failure") into the retry classifier — terminal,
        # but with no actionable record of what went wrong.
        if not (result.get("summary") or "").strip():
            return "status='failed' but summary is empty — no diagnosis provided"
    elif status == "needs-clarification":
        # DESIGN §11 mid-execution clarification: the question and the
        # work-in-progress checkpoint MUST both be present. The question
        # is what gets surfaced to the user; the checkpoint is what
        # carries the partial work forward to the re-spawned implementer.
        # The why_underivable field inside the question is required by
        # the schema and re-checked here as a content (not just shape)
        # gate against the worker drifting toward "ask instead of
        # research."
        cq = result.get("clarification_question")
        if not cq:
            return ("status='needs-clarification' but clarification_question "
                    "is null — see DESIGN §11")
        for field in ("id", "question", "why_underivable"):
            if not (cq.get(field) or "").strip():
                return (f"status='needs-clarification' but "
                        f"clarification_question.{field} is empty — "
                        "see DESIGN §11")
        cp = result.get("checkpoint_path")
        if not cp:
            return ("status='needs-clarification' but checkpoint_path is "
                    "null — the work-in-progress must survive the question")
        if not Path(cp).exists():
            return (f"status='needs-clarification' but checkpoint_path "
                    f"'{cp}' does not exist on disk")
    return None


# --- post-implementation diff scope check ------------------------------------

async def check_diff_scope(sid: str, worktree: str, subtask: dict,
                           st: State) -> str | None:
    """Check the implementer's diff for violations.
    Returns a fatal error string if protected paths were touched.
    Logs a non-fatal warning for unexpected scope. Returns None when clean.

    The diff is computed against the run branch (`pila/runs/<run-id>`)
    — the base every subtask branched off of. Hardcoding `pila/staging`
    here used to silently disable the check after the per-run refactor
    (the branch doesn't exist), so the protected-path enforcement was off."""
    run_branch = compute_run_branch(st.run_id)
    r = await run_proc(
        ["git", "diff", "--name-only", f"{run_branch}..HEAD"],
        cwd=worktree,
    )
    if r.returncode != 0:
        return None
    touched = [f for f in r.stdout.strip().splitlines() if f]
    if not touched:
        return None

    # fatal: any changes to protected meta-directories are out of bounds.
    # `.claude/{agents,commands,skills}/` are exempt (documented Claude
    # Code user-deliverable locations); top-level `.claude/` files
    # (settings.json, settings.local.json) stay protected. See
    # is_protected_path() for the rule.
    protected = [f for f in touched if is_protected_path(f)]
    if protected:
        return (f"{sid}: diff touches protected path(s): {protected} — "
                "implementers must not modify meta-directories")

    # non-fatal: log a warning for radically unexpected scope
    expected = subtask.get("files_likely_touched", [])
    over_ratio = bool(expected) and len(touched) > max(len(expected) * 3, 5)
    over_volume = len(touched) > 15
    if over_ratio or over_volume:
        reason = f"touched {len(touched)} files, expected ~{len(expected)}"
        log(f"  ⚠  scope warning {sid}: {reason}")
        st.data.setdefault("scope_warnings", {})[sid] = {
            "touched": touched, "expected": expected, "reason": reason,
        }
        st.save()

    return None


# --- post-integrator commit check --------------------------------------------

async def check_merge_committed(staging: Path) -> str | None:
    """Return an error if the staging worktree is still mid-merge.

    An integrator that returns status 'resolved' must have completed the merge
    commit. If `MERGE_HEAD` still exists, the merge was never concluded — the
    worker claimed success while leaving the worktree in a broken mid-merge
    state. This is the integrator-side analogue of `check_branch_has_commits`:
    it catches a worker lying about having finished."""
    r = await run_proc(
        ["git", "rev-parse", "--verify", "--quiet", "MERGE_HEAD"],
        cwd=str(staging),
    )
    if r.returncode == 0:
        return ("the staging worktree is still mid-merge (MERGE_HEAD exists) — "
                "the integrator did not complete the merge commit")
    # also reject a worktree left with staged-but-uncommitted conflict edits
    s = await run_proc(
        ["git", "diff", "--cached", "--name-only"],
        cwd=str(staging),
    )
    if s.returncode == 0 and s.stdout.strip():
        return ("the staging worktree has staged but uncommitted changes — "
                "the integrator did not complete the merge commit")
    return None


async def check_integrator_commit(staging: Path) -> str | None:
    """Return an error if the integrator's merge commit touched .pila/ files.
    The integrator should only touch project files, never coordination artifacts."""
    r = await run_proc(
        ["git", "show", "--name-only", "--format=", "HEAD"],
        cwd=str(staging),
    )
    if r.returncode != 0:
        return None
    bad = [f for f in r.stdout.strip().splitlines()
           if f and f.startswith(".pila/")]
    if bad:
        return f"integrator commit touched coordination files: {bad}"
    return None


# --- branch-has-commits verification -----------------------------------------

async def check_branch_has_commits(sid: str, worktree: str,
                                   parent_branch: str) -> str | None:
    """Return error if the implementer's subtask branch has no commits
    ahead of the run branch (`parent_branch` — typically
    `pila/runs/<run-id>`). An empty diff means the worker produced
    schema-valid JSON claiming success while doing nothing — a silent
    no-op that wastes an integration attempt."""
    if not Path(worktree).exists():
        return None  # worktree gone — can't determine, don't block
    try:
        r = await run_proc(
            ["git", "log", f"{parent_branch}..HEAD", "--oneline"],
            cwd=worktree,
        )
    except OSError:
        return None
    if r.returncode != 0:
        return None
    if not r.stdout.strip():
        return (f"subtask branch for {sid} has no commits ahead of the run "
                f"branch ({parent_branch}) — implementer claimed complete "
                "without making any changes")
    return None


# --- conflict marker scan post-integration -----------------------------------

async def scan_conflict_markers(staging: Path) -> str | None:
    """Return error if unresolved conflict markers remain in the staging tree.
    git grep exit 0 = matches found (bad); exit 1 = clean (good)."""
    if not staging.exists():
        return None
    try:
        r = await run_proc(
            ["git", "grep", "-l", "^<<<<<<< ", "HEAD"],
            cwd=str(staging),
        )
    except OSError:
        return None
    if r.returncode == 0:
        files = [f for f in r.stdout.strip().splitlines() if f]
        sample = files[:5]
        tail = "…" if len(files) > 5 else ""
        return (f"conflict markers in {len(files)} file(s) after integration: "
                f"{sample}{tail}")
    return None


# --- resume state integrity check --------------------------------------------

def validate_resume_state(data: dict) -> None:
    """Assert the structure of a loaded state.json before resuming. A corrupt
    or hand-edited file produces wrong behavior; fail fast rather than run
    silently. Only `task` is strictly required here — a run interrupted before
    scheduling has no `waves` yet, and main() handles that case separately with
    a clearer message."""
    if "task" not in data or not str(data.get("task", "")).strip():
        die("state.json has no usable 'task' — cannot resume. "
            "Inspect the run's state.json manually "
            "(under .pila/runs/<run-id>/).")

    # waves is optional (absent if interrupted before scheduling); if present
    # it must be well-formed, and completed_waves must be in range.
    if "waves" in data:
        waves = data["waves"]
        if not isinstance(waves, list) or not all(isinstance(w, list)
                                                  for w in waves):
            die("state.json: 'waves' must be a list of lists")
        completed = data.get("completed_waves", 0)
        if not isinstance(completed, int) or not (0 <= completed <= len(waves)):
            die(f"state.json: 'completed_waves' ({completed!r}) is out of range "
                f"(expected 0..{len(waves)})")

    # subtask_status, if present, must be a dict
    if "subtask_status" in data and not isinstance(data["subtask_status"], dict):
        die("state.json: 'subtask_status' must be an object")


# =========================================================================
# the single point where LLM work happens: a `claude -p` invocation
# =========================================================================
class WorkerError(RuntimeError):
    pass


def _is_auth_or_quota_failure(envelope: dict) -> bool:
    """True if the `claude -p` envelope looks like a 401/429/auth-message
    rejection from the Anthropic gateway.

    These need backoff, not the immediate corrective retry that the
    schema-error path uses — the request was rejected before reaching
    a model and a fresh request will be rejected too until the user's
    Claude Code subscription window clears. The auth/quota retry loop
    in claude_p() consults this classifier; non-matching envelopes
    fall through to the existing 2-attempt schema loop unchanged.
    """
    status = envelope.get("api_error_status")
    if status in (401, 429, "401", "429"):
        return True
    msg = str(envelope.get("result") or "").lower()
    return ("invalid authentication" in msg
            or "rate limit" in msg
            or "rate-limit" in msg)


def _extract_tool_result_text(block: dict) -> str:
    """Tool-result `content` is either a string or a list of content
    blocks (`{type: "text", text: "..."}`). Normalize to a plain
    string so summaries / file output don't have to branch."""
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for c in content:
            if isinstance(c, dict) and c.get("type") == "text":
                parts.append(c.get("text") or "")
        return " ".join(parts)
    return ""


def _tag_each_line(prefix: str, content: str) -> str:
    """Prefix the first non-empty line of `content` with `prefix`;
    subsequent lines get a width-matched continuation prefix that
    preserves the `[<sid>` worker-attribution segment but drops the
    event-kind token.

    Used for tool_result summaries whose content can be multi-line
    (a Read of a source file, a Grep result, a stack trace, a
    compound command's stdout). Lines 2+ must stay attributable to
    this worker — in a parallel run with max_parallel=4, untagged
    continuation lines would be indistinguishable from another
    worker's output. The kind token (`tool-fail`, `tool-ok`)
    repeated on every line, however, is per-tool-call information
    that obscures the actual content when repeated.

    For single-line content the result is `f'{prefix} {content}'`.
    For empty content it returns the empty string so the caller's
    truthiness check naturally drops it. If `prefix` doesn't match
    the expected `<indent>[<sid> <kind>]` shape (defensive — every
    current caller does), the helper falls back to repeating
    `prefix` on every line."""
    lines = [ln for ln in content.splitlines() if ln]
    if not lines:
        return ""
    open_b = prefix.find("[")
    close_b = prefix.rfind("]")
    cont = prefix
    if open_b >= 0 and close_b > open_b:
        inside = prefix[open_b + 1 : close_b]
        sid, _, kind = inside.partition(" ")
        if sid and kind:
            keep = prefix[: open_b + 1] + sid
            pad = " " * (close_b - len(keep))
            cont = keep + pad + "]"
    return "\n".join([f"{prefix} {lines[0]}",
                      *(f"{cont} {ln}" for ln in lines[1:])])


def _summarize_tool_use(sid: str, block: dict, verbosity: str) -> str:
    """Map one `tool_use` content block to a one-line inline summary.
    `verbosity` is "stream" or "debug" by the time this is called;
    debug allows wider truncation limits."""
    name = block.get("name", "?")
    inp = block.get("input", {}) or {}
    if name == "Read":
        return f"  [{sid} read] {inp.get('file_path', '?')}"
    if name == "Grep":
        path = inp.get("path", "")
        suffix = f" in {path}" if path else ""
        return f"  [{sid} grep] {inp.get('pattern', '?')}{suffix}"
    if name == "Glob":
        return f"  [{sid} glob] {inp.get('pattern', '?')}"
    if name == "Bash":
        cmd_lines = (inp.get("command") or "").splitlines()
        cmd = cmd_lines[0] if cmd_lines else ""
        # No truncation: a mid-cut shell command loses the part you
        # actually need to read (the operands at the end of a pipeline).
        # Multi-line scripts still show only the first line; the
        # per-worker .log file has the full command.
        return f"  [{sid} bash] {cmd}"
    if name in ("Write", "Edit", "NotebookEdit"):
        return f"  [{sid} {name.lower()}] {inp.get('file_path', '?')}"
    if name == "WebFetch":
        return f"  [{sid} fetch] {inp.get('url', '?')}"
    if name == "WebSearch":
        return f"  [{sid} search] {inp.get('query', '?')}"
    if name == "StructuredOutput":
        # `input` is the worker's full structured payload. Only surface
        # at debug — at stream this is noise since the `done` line
        # follows immediately. Per-worker file has it whole regardless.
        if verbosity == "debug":
            return f"  [{sid}] finalizing output {str(inp)}"
        return None
    # Unknown / MCP tool — dump the full repr of the input. The
    # tail of an MCP-tool input (a Supabase query operand, a Stripe
    # API parameter) is where the useful detail lives; mid-cut
    # loses it. Per-worker .log file matches.
    return f"  [{sid} {name}] {str(inp)}"


def _summarize_stream_event(sid: str, event: dict, verbosity: str) -> str | None:
    """Return the one-line inline-log summary for one stream event, or
    None to drop the event from the inline log. The per-worker file
    always gets the raw event regardless of verbosity — this function
    only governs what surfaces inline.

    Levels in increasing detail: quiet, normal, stream, debug. At
    quiet/normal, individual events are dropped (pila's existing
    phase / subtask-status log lines stand alone), with the one
    exception of result-with-error which surfaces at every level
    (clig.dev "errors emit at every level")."""
    t = event.get("type")
    sub = event.get("subtype")

    # quiet/normal: drop everything except worker-level errors.
    if verbosity in ("quiet", "normal"):
        if t == "result" and event.get("is_error"):
            n = event.get("num_turns", "?")
            return f"  [{sid}] worker failed ({sub}, turns={n})"
        return None

    # stream and debug: per-event summaries.
    if t == "system":
        if sub == "init":
            model = event.get("model", "?")
            return f"  [{sid}] starting (model={model})"
        # hook_started / hook_response are noisy (every SessionStart
        # hook fires once each); surface only at debug.
        if verbosity == "debug" and sub in ("hook_started", "hook_response"):
            hook_name = event.get("hook_name", "?")
            return f"  [{sid} hook] {sub} {hook_name}"
        return None

    if t == "assistant":
        msg = event.get("message", {})
        blocks = msg.get("content", []) or []
        lines = []
        for b in blocks:
            bt = b.get("type")
            if bt == "text":
                # First, inspect the text for a Claude Code session-limit /
                # rate-limit message. Detection here is load-bearing for
                # the text path of the limit: when the subscription limit
                # is hit, claude -p returns the session-limit string as
                # assistant text and then closes the session with
                # subtype="success" — without this check, validate_result
                # would see a synthesized incomplete-handoff and the
                # `_retryable_failure` safety net would be the only
                # remaining defense. (The protocol-level path is handled
                # below in the rate_limit_event branch.) See DESIGN §6
                # *Cleanup on abnormal exit* for the auto-resume contract.
                text = b.get("text") or ""
                if (exc := detect_session_limit(text)):
                    raise exc
                # Emit every non-empty line of the assistant's text as
                # its own [<sid> text] entry, full-width (no
                # truncation). Mid-cut sentences in earlier versions
                # ate the part the user actually wanted to read. The
                # per-worker .log file has the same content; this just
                # surfaces it inline too.
                for ln in text.splitlines():
                    ln = ln.strip()
                    if ln:
                        lines.append(f"  [{sid} text] {ln}")
            elif bt == "tool_use":
                tool_summary = _summarize_tool_use(sid, b, verbosity)
                if tool_summary is not None:
                    lines.append(tool_summary)
        return "\n".join(lines) if lines else None

    if t == "user":
        msg = event.get("message", {})
        for b in msg.get("content", []) or []:
            if b.get("type") != "tool_result":
                continue
            content_txt = _extract_tool_result_text(b).strip()
            if b.get("is_error"):
                # No truncation: a schema-validation failure or other
                # tool error names exactly the missing fields / the
                # rejection reason — the diagnostic information a user
                # needs to act. Mid-cut error messages drop the useful
                # detail. Multi-line errors (rare but possible) get the
                # `tool-fail` tag on line 1 and a width-matched
                # continuation prefix (keeping the sid) on lines 2+
                # so attribution survives parallel runs without
                # repeating the kind token on every line — see
                # _tag_each_line.
                return _tag_each_line(f"  [{sid} tool-fail]", content_txt)
            # Successful tool results are file-only at stream; debug
            # gets the FULL content. The user opting into debug is
            # explicitly asking for raw worker output; truncating
            # defeats the level. A worker reading a large file will
            # flood the orchestrator log at debug — that's the
            # accepted trade-off. Multi-line content (a Read of a
            # source file, a Grep of code) gets the `tool-ok` tag
            # on line 1 and a width-matched continuation prefix
            # (keeping the sid) on lines 2+ so attribution survives
            # parallel runs without repeating the kind token on
            # every line.
            if verbosity == "debug":
                return _tag_each_line(f"  [{sid} tool-ok]", content_txt)
        return None

    if t == "rate_limit_event":
        info = event.get("rate_limit_info", {}) or {}
        # The actual Claude Code stream-json schema (verified from
        # captured worker logs 2026-05-27): the payload carries
        # `status` (observed values: "allowed", "allowed_warning"),
        # `resetsAt` (Unix timestamp seconds), `rateLimitType`,
        # `utilization` (float 0..1, present on warning events),
        # `surpassedThreshold` (the *threshold value crossed*, e.g.
        # 0.9 — NOT a boolean flag), `overageStatus`,
        # `overageDisabledReason`, `isUsingOverage`. The terminal
        # status value (when the limit is actually hit) is
        # Anthropic-internal and unobserved by us; we treat any
        # status not in the known-allowed set as terminal —
        # defensive against future status strings ("exceeded",
        # "denied", "blocked", etc.) without hardcoding a guess.
        status = info.get("status")
        if status is not None and status not in _RATE_LIMIT_ALLOWED_STATUSES:
            reset_at: datetime | None = None
            resets_at_ts = info.get("resetsAt")
            rate_limit_type = info.get("rateLimitType", "?")
            if isinstance(resets_at_ts, (int, float)):
                try:
                    reset_at = datetime.fromtimestamp(
                        resets_at_ts, tz=timezone.utc)
                except (OSError, ValueError, OverflowError):
                    reset_at = None
            raw = (f"rate_limit_event status={status} "
                   f"rateLimitType={rate_limit_type} "
                   f"resetsAt={resets_at_ts}")
            raise RateLimitedExit(reset_at=reset_at, raw_message=raw)
        # Surface threshold-crossings at stream; everything at debug.
        # The boolean-or-numeric `surpassedThreshold` field is a
        # threshold value (e.g. 0.9), not a boolean flag — truthy when
        # present and non-zero, which is the right "this is a
        # threshold-crossing event" signal for surfacing the warning.
        if info.get("surpassedThreshold") or verbosity == "debug":
            util_frac = float(info.get("utilization") or 0)
            util = int(util_frac * 100)
            return f"  [{sid}] rate-limit {status or '?'} (util={util}%)"
        return None

    if t == "result":
        n = event.get("num_turns", "?")
        if sub == "success":
            cost = float(event.get("total_cost_usd") or 0)
            return f"  [{sid}] done (turns={n}, cost=${cost:.4f})"
        return f"  [{sid}] failed ({sub}, turns={n})"

    # Unknown event type — surface only at debug; otherwise drop.
    if verbosity == "debug":
        return f"  [{sid} ?] {t}/{sub}"
    return None


def _get_progress(st: "State") -> tuple[int, int] | None:
    """Return (done, total) subtask counts for the inline progress prefix.

    Only meaningful once waves are scheduled — returns None before that so
    classifier/planner workers emit no prefix. Terminal statuses are complete,
    failed, and blocked; in-progress subtasks don't count toward done."""
    waves = st.data.get("waves")
    if not waves:
        return None
    total = sum(len(w) for w in waves)
    if total == 0:
        return None
    done = sum(1 for v in st.data.get("subtask_status", {}).values()
               if v in _TERMINAL_STATUSES)
    return done, total


# --- cgroup v2 containment for worker subtrees ---------------------------
# Each `claude -p` worker (and every descendant it forks: bash children,
# vitest pools, webpack workers, tsc, etc.) is enrolled in its own child
# cgroup at /sys/fs/cgroup/pila-w-<sid>/. The cgroup's memory.max and
# pids.max bound how much RAM / how many PIDs the worker subtree may
# consume. When the worker subtree exceeds memory.max, the kernel OOM-
# kills inside that cgroup — sshd / pid 1 / sibling workers are not
# eligible victims. This is the fix for the cascade documented in
# DESIGN §6 Worker subtree termination — Memory containment.
#
# Delegation is purely file-permission based on cgroup v2; no
# CAP_SYS_ADMIN required. The launcher mounts /sys/fs/cgroup writable
# into the container (see `pila` launcher: --mount type=bind,source=
# /sys/fs/cgroup,target=/sys/fs/cgroup,bind-propagation=rshared). If
# the mount is not writable (older launcher, host kernel <5.x, custom
# container shape), the probe degrades the path to no-op with a
# warn-once log line — pila must never die because the cap can't be
# applied.

_CGROUP_ROOT = Path("/sys/fs/cgroup")
_CGROUP_PROBE_RESULT: bool | None = None


def _cgroup_probe() -> bool:
    """Once-per-run probe: can we create cgroups under /sys/fs/cgroup/?

    Memoized in `_CGROUP_PROBE_RESULT`. Returns True on success, False
    on any failure (RO mount, missing dir, missing controllers, etc.).
    A failure logs one warn-once line so the user sees that the
    containment is degraded; subsequent worker spawns silently skip the
    cgroup work.

    The probe creates a child dir and immediately rmdir's it. On real
    cgroupfs, rmdir of a cgroup dir succeeds even though it appears
    "non-empty" — the kernel removes the cgroup atomically. On a
    regular filesystem (test environment), the child dir IS empty
    after mkdir so rmdir also succeeds. We deliberately do NOT touch
    memory.max during the probe: writing a kernel-controller file
    would leave files in the dir on a non-cgroupfs path, breaking
    rmdir cleanup."""
    global _CGROUP_PROBE_RESULT
    if _CGROUP_PROBE_RESULT is not None:
        return _CGROUP_PROBE_RESULT
    probe_dir = _CGROUP_ROOT / "pila-probe"
    try:
        probe_dir.mkdir(exist_ok=True)
        probe_dir.rmdir()
        _CGROUP_PROBE_RESULT = True
    except OSError as e:
        log(f"  cgroup probe failed ({e.strerror or e}); worker memory "
            f"containment is OFF for this run. The launcher may need "
            f"the --mount type=bind,source=/sys/fs/cgroup,... flag.")
        with contextlib.suppress(OSError):
            probe_dir.rmdir()
        _CGROUP_PROBE_RESULT = False
    return _CGROUP_PROBE_RESULT


def _cgroup_create(sid: str, memory_max_bytes: int,
                   pids_max: int) -> Path | None:
    """Create a child cgroup for a worker and set its caps. Returns the
    cgroup path on success, None on any failure. Idempotent on the
    mkdir — re-spawning a worker with the same sid (handoff,
    continuation) reuses the existing cgroup. The caps are re-written
    so a config change between spawns takes effect."""
    if not _cgroup_probe():
        return None
    path = _CGROUP_ROOT / f"pila-w-{sid}"
    try:
        path.mkdir(exist_ok=True)
        (path / "memory.max").write_text(str(memory_max_bytes))
        (path / "pids.max").write_text(str(pids_max))
        # memory.swap.max = 0 so the kernel doesn't swap-out the
        # worker pages to delay an inevitable OOM. The Colima VM has
        # 4 GB swap; letting workers eat it just means slow death
        # instead of fast death.
        with contextlib.suppress(OSError):
            (path / "memory.swap.max").write_text("0")
    except OSError as e:
        log(f"  [{sid}] cgroup create failed ({e.strerror or e}); "
            f"worker runs uncapped")
        return None
    return path


def _cgroup_enroll(cgroup_path: Path, pid: int) -> bool:
    """Move `pid` into the cgroup. Called immediately after the worker
    subprocess spawns. Returns True on success. Failure logs but does
    not abort the worker — the worker will simply run in the parent
    cgroup (uncapped) which is the pre-fix behavior."""
    try:
        (cgroup_path / "cgroup.procs").write_text(str(pid))
        return True
    except OSError as e:
        log(f"  cgroup enroll failed for pid={pid}: "
            f"{e.strerror or e}")
        return False


def _cgroup_destroy(cgroup_path: Path | None) -> None:
    """Tear down the worker's cgroup. Best-effort:
    - cgroup.kill (kernel ≥5.14) atomically kills all members of the
      cgroup. Catches any lingering grandchild process the
      _DescendantTracker / proc-walk may have missed.
    - rmdir removes the empty cgroup.
    Both are swallowed on ENOENT (already cleaned). Called from
    `_invoke`'s cleanup path on every exit (success, timeout, abort)."""
    if cgroup_path is None:
        return
    with contextlib.suppress(OSError):
        (cgroup_path / "cgroup.kill").write_text("1")
    with contextlib.suppress(OSError):
        cgroup_path.rmdir()


async def _invoke(cmd: list[str], cwd: str, timeout: int,
                  sid: str, pila_dir: Path, verbosity: str,
                  progress: tuple[int, int] | None = None,
                  idle_warn_sec: float | None = None,
                  worker_memory_max_bytes: int | None = None,
                  worker_pids_max: int | None = None) -> dict:
    """Run a `claude -p` command, streaming events as they arrive.

    The CLI is invoked with `--output-format stream-json --verbose`; each
    line of stdout is one JSON event. The final `type: "result"` event
    is the envelope (same shape as the non-streaming `--output-format
    json` path produces). All events are appended to
    `.pila/logs/<sid>.log` regardless of verbosity. Inline summaries
    surface to the orchestrator log according to `verbosity` (see
    `_summarize_stream_event`).

    `cmd` must already contain `--output-format stream-json --verbose`
    — `claude_p` adds those.

    Errors / cancellation follow `run_proc`'s contract: timeout raises
    `subprocess.TimeoutExpired`, cancellation kills the child and
    re-raises. A worker that exits without emitting any `result` event
    raises `WorkerError` — same error class callers already handle."""
    log_path = pila_dir / "logs" / f"{sid}.log"
    # `limit=10MB` overrides asyncio's StreamReader 64KB-per-line default.
    # A single `claude -p` event can plausibly exceed 64KB: the
    # implementer's `structured_output` tool_use carries the full
    # worker payload, and a long assistant text block is one event
    # too. Without this, a large event would crash `_read_stream`:
    # readline() (under the `async for proc.stdout` iterator) calls
    # readuntil() which raises LimitOverrunError, which readline()
    # wraps and re-raises as ValueError("Separator is not found,
    # and chunk exceed the limit"). Either name in `except` works —
    # but the user-visible exception is ValueError.
    # PILA_WORKER_DEBUG=1 injects DEBUG=* and ANTHROPIC_LOG=debug into the
    # worker's environment. The point: if a worker hangs before emitting
    # any stdout event, rerunning with this env var makes the CLI emit its
    # internal state to stderr (which the watchdog below surfaces), so an
    # otherwise-invisible silent stall becomes diagnosable without
    # redeploying pila. The variable is opt-in because verbose CLI logging
    # is noisy on healthy runs.
    worker_env = None
    if os.environ.get("PILA_WORKER_DEBUG"):
        worker_env = os.environ.copy()
        worker_env["DEBUG"] = "*"
        worker_env["ANTHROPIC_LOG"] = "debug"

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=cwd,
        # stdin=DEVNULL: workers receive their full prompt + schema via
        # argv and never read terminal input. Without this the worker
        # inherits the orchestrator's stdin, which inside a `nerdctl run
        # -it` container is /dev/pts/0 — a real TTY. A CLI that branches
        # on isatty() (e.g. to prompt for permission) would hang
        # invisibly waiting for input that never arrives. Closing stdin
        # eliminates that whole class of hang.
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=10 * 1024 * 1024,
        # Session/PG leader so `_terminate_proc_tree` can reap the tool-call
        # grandchildren `claude -p` spawns (vitest, dev servers, etc.).
        start_new_session=True,
        env=worker_env,
    )
    # Spawn heartbeat: surfaces *that* the worker was launched before the
    # await blocks on its first event. Without this line, the user sees
    # the phase header and then silence until the first stream-json event
    # arrives (or the 90-min `worker_timeout_sec` fires) — a silent worker
    # was the failure class that motivated the watchdog below.
    #
    # Suppressed at `quiet`: per the verbosity contract, quiet emits
    # phase boundaries + errors only. The watchdog warning below IS
    # error-class and fires at every verbosity; this spawn line is
    # operational chatter and stays gated.
    if verbosity != "quiet":
        log(f"  [{sid}] spawned (pid={proc.pid})")
    # cgroup v2 containment: enroll the worker (and every descendant it
    # forks — `set_new_session=True` above means the kernel propagates
    # cgroup membership down the process tree by default on v2). On
    # systems without writable cgroupfs the helpers return None / False
    # silently and the worker runs uncapped. Failure modes from the
    # cgroup path NEVER abort the worker — telemetry that crashes its
    # host is worse than no telemetry (same principle as _memory_sampler).
    cgroup_path: Path | None = None
    if (worker_memory_max_bytes is not None
            and worker_pids_max is not None):
        cgroup_path = _cgroup_create(sid, worker_memory_max_bytes,
                                     worker_pids_max)
        if cgroup_path is not None:
            _cgroup_enroll(cgroup_path, proc.pid)
    # Track every descendant PID that ever appears under this worker. Claude
    # Code's Bash tool uses `run_in_background: true` to fire-and-forget
    # long-running commands (test runners, builds, dev servers); those
    # subprocesses outlive `claude -p`'s exit and are orphaned to init by the
    # time pila could PPID-walk post-exit. The tracker observes them while
    # the chain is still intact, then SIGKILLs the accumulated set at the
    # end. See DESIGN §6 *Cleanup on abnormal exit*.
    descendant_tracker = _DescendantTracker(proc.pid)
    descendant_tracker.start()
    envelope: dict | None = None
    stderr_chunks: list[bytes] = []
    # Watchdog state: last_event_at is updated by _read_stream on every
    # successfully-parsed stream-json event. The _idle_watchdog coroutine
    # below observes this clock and warns when no events arrive for
    # `worker_idle_warn_sec` seconds.
    last_event_at = time.monotonic()

    async def _read_stream():
        nonlocal envelope, last_event_at
        # `buffering=1` is line-buffered: every newline flushes to disk.
        # Without this Python text-mode files are fully buffered when not
        # connected to a TTY, so `tail -f .pila/logs/<sid>.log` would
        # show nothing until the file closed at worker end — defeating
        # the entire live-progress property of the streaming feature.
        with log_path.open("a", buffering=1) as log_file:
            try:
                async for raw in proc.stdout:
                    if not raw:
                        continue
                    # Any bytes from the worker count as liveness — refresh
                    # the watchdog clock before parsing, so a stream of
                    # non-JSON lines (which are logged and skipped below)
                    # still counts as activity.
                    last_event_at = time.monotonic()
                    line = raw.decode(errors="replace").rstrip("\n")
                    # File: always record the raw event with a timestamp
                    # header. The header lets `tail -f` users see
                    # structure without parsing JSON.
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        log_file.write(
                            f"[{now()}] non-json-line\n{line}\n\n")
                        continue
                    t = event.get("type", "?")
                    sub = event.get("subtype")
                    header = f"{t}/{sub}" if sub else t
                    log_file.write(f"[{now()}] {header}\n{line}\n\n")
                    # Inline summary (verbosity-gated). Multi-line
                    # summaries (multi-block events, multi-line text)
                    # are emitted one log() call per line so each
                    # line gets its own [pila HH:MM:SS] prefix —
                    # otherwise the timestamp only renders on line 1
                    # and lines 2+ visually disconnect from the
                    # orchestrator's timestamped log stream.
                    summary = _summarize_stream_event(sid, event, verbosity)
                    if summary:
                        prog_prefix = (f"[{progress[0]}/{progress[1]}] "
                                       if progress else "")
                        for ln in summary.splitlines():
                            if ln:
                                log(prog_prefix + ln)
                    # Capture the final result envelope
                    if t == "result":
                        envelope = event
            except ValueError as e:
                # asyncio's StreamReader raises ValueError("Separator
                # is not found, and chunk exceed the limit") when a
                # single line exceeds the 10 MiB limit (see
                # create_subprocess_exec above). Without this catch
                # the ValueError would propagate through claude_p's
                # retry loop unhandled and surface as a Python
                # traceback. Convert to WorkerError so callers see a
                # pila-shaped error and the retry path treats it
                # as a worker fault.
                raise WorkerError(
                    "claude -p emitted a line exceeding the 10 MiB "
                    "buffer limit — likely a runaway structured_output "
                    f"or text block: {e}") from e

    async def _drain_stderr():
        # Stream stderr live to the per-sid log file with a `[ts] stderr`
        # header so it's distinguishable from stream-json events, and
        # echo selectively to the orchestrator log at stream/debug
        # verbosity. Continues to buffer raw bytes into stderr_chunks
        # so the existing exit-time error path (line ~4195) and the
        # idle-watchdog stderr-tail flush still work unchanged.
        #
        # Solves the failure class where a worker emits a fatal message
        # to stderr (e.g. "Claude configuration file not found" from
        # the claude-code recovery-loop bug) but pila doesn't surface
        # it until the 300s watchdog fires (or never, if the recovery
        # loop spins indefinitely with no exit).
        nonlocal last_event_at  # stderr activity counts as liveness
        with log_path.open("a", buffering=1) as log_file:
            try:
                async for raw in proc.stderr:
                    if not raw:
                        continue
                    last_event_at = time.monotonic()
                    stderr_chunks.append(raw)
                    line = raw.decode(errors="replace").rstrip("\n")
                    log_file.write(f"[{now()}] stderr\n{line}\n\n")
                    if verbosity in ("stream", "debug"):
                        log(f"  [{sid}] stderr: {line}")
            except ValueError as e:
                # Mirror _read_stream's overlong-line protection
                # (line ~4082): asyncio's StreamReader raises
                # ValueError when a single line exceeds the 10 MiB
                # buffer limit. Convert to WorkerError so callers see
                # a pila-shaped error consistent with the stdout path.
                raise WorkerError(
                    "claude -p stderr emitted a line exceeding the "
                    f"10 MiB buffer limit: {e}") from e

    async def _idle_watchdog():
        # Observation-only stall detector. Wakes every `warn_sec` seconds
        # and warns if the worker has emitted no stdout bytes for that
        # long. Never kills the worker — the 90-min `worker_timeout_sec`
        # remains the only kill. Surfaces the silent-hang failure class
        # that motivated this watchdog: a `claude -p` worker that gets
        # stuck before its first `system/init` event would otherwise
        # leave the user with zero feedback for up to 90 minutes.
        #
        # When the worker exits normally (success or timeout), the
        # surrounding try/finally cancels this task; CancelledError is
        # suppressed by the awaiting caller.
        # `idle_warn_sec` carries the resolved per-run cap from
        # `claude_p` (which built `caps = dict(DEFAULT_CAPS)` and then
        # honored any CLI / env / TOML override). Direct `_invoke`
        # callers — preflight smoke-test, replay paths, tests — don't
        # plumb caps and pass `None`; we fall back to `DEFAULT_CAPS`
        # so the watchdog still functions for them without forcing
        # every call site to thread the full caps dict.
        warn_sec = (idle_warn_sec if idle_warn_sec is not None
                    else DEFAULT_CAPS["worker_idle_warn_sec"])
        while True:
            await asyncio.sleep(warn_sec)
            gap = time.monotonic() - last_event_at
            # Compare in floats — truncating to int here would make a
            # sub-second `warn_sec` (e.g. in tests) compare against 0
            # and never warn.
            if gap < warn_sec:
                continue
            # Stderr tail: if the CLI is logging to stderr (e.g. when
            # PILA_WORKER_DEBUG=1), surface the most recent bytes
            # alongside the silence warning so the user has something
            # actionable. Truncated to the last 400 chars to keep the
            # orchestrator log readable.
            tail = b"".join(stderr_chunks[-10:]).decode(
                errors="replace").strip()
            tail_note = (f" — stderr tail: {tail[-400:]!r}"
                         if tail else "")
            log(f"  [{sid}] no stdout events in {int(gap)}s "
                f"(pid={proc.pid}, hard kill at "
                f"{timeout}s){tail_note}")

    watchdog_task = asyncio.create_task(_idle_watchdog())
    try:
        try:
            await asyncio.wait_for(
                asyncio.gather(_read_stream(), _drain_stderr(),
                               proc.wait()),
                timeout=timeout)
        except asyncio.TimeoutError:
            # Cancel the watchdog BEFORE the termination awaits so it
            # cannot wake during them and log a stale "no stdout events"
            # warning against a worker that's already being killed. The
            # `finally:` below still collects the task; cancel() is
            # idempotent.
            watchdog_task.cancel()
            await _terminate_proc_tree(proc)
            await descendant_tracker.stop_and_reap()
            raise subprocess.TimeoutExpired(cmd, timeout)
        except BaseException:
            # Same race-closing cancel as above. Then terminate the
            # worker's whole subtree (claude -p + its tool-call
            # grandchildren via PPID walk) and reap any backgrounded
            # subprocesses the tracker observed during the run. Then
            # re-raise. Pila's gather_or_cancel relies on this for clean
            # aborts.
            watchdog_task.cancel()
            await _terminate_proc_tree(proc)
            await descendant_tracker.stop_and_reap()
            raise
    finally:
        # The watchdog runs for the whole worker lifetime and must be
        # cancelled on every exit path (success, timeout, abort) so it
        # doesn't outlive the worker and fire spuriously against a stale
        # `last_event_at`. The contextlib.suppress is the standard
        # asyncio pattern for awaiting a cancelled task without
        # propagating the CancelledError.
        watchdog_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watchdog_task
        # cgroup teardown. cgroup.kill (kernel ≥5.14) atomically reaps
        # any worker-tree process that survived _terminate_proc_tree /
        # descendant_tracker.stop_and_reap above — a backstop for the
        # backgrounded grandchild class. Then rmdir the cgroup so we
        # don't accumulate /sys/fs/cgroup/pila-w-* entries across a
        # long-running orchestrator. Best-effort: ENOENT etc. are
        # swallowed inside _cgroup_destroy.
        _cgroup_destroy(cgroup_path)
    # Success path: reap any backgrounded subprocesses the worker left
    # behind. `claude -p` workers use Claude Code's Bash tool with
    # `run_in_background: true` for long-running tasks (test runners,
    # builds, dev servers — DESIGN §6). Those subprocesses
    # are spawned in detached POSIX sessions, exit-reparent to PID 1, and
    # would otherwise outlive `claude -p`'s clean exit. The tracker has
    # accumulated their PIDs throughout the worker's life; stop_and_reap
    # SIGKILLs the union.
    leaked = await descendant_tracker.stop_and_reap()
    if leaked:
        log(f"  [{sid}] reaped {leaked} backgrounded subprocess(es) "
            f"that survived `claude -p` exit")

    if envelope is None:
        stderr_txt = b"".join(stderr_chunks).decode(errors="replace").strip()
        if proc.returncode and proc.returncode != 0:
            raise WorkerError(
                f"claude -p exited {proc.returncode}: "
                f"{stderr_txt or '(no stderr)'}")
        raise WorkerError(
            "claude -p produced no result event "
            f"(stderr: {stderr_txt or '(empty)'})")
    return envelope


def _capture_call(run_dir: Path, record: dict) -> None:
    """Append one NDJSON record to calls.ndjson with fsync-per-line durability.

    fsync ensures a hard-killed run leaves a clean, fully-written last line
    rather than a partial line that would break NDJSON parsers."""
    capture_path = run_dir / "calls.ndjson"
    line = json.dumps(record, separators=(",", ":")) + "\n"
    with capture_path.open("a") as f:
        f.write(line)
        f.flush()
        os.fsync(f.fileno())


def _collect_memory_sample(st: "State") -> dict:
    """Snapshot orchestrator RSS / current phase / worker count / open FDs /
    thread count. Stdlib only (no `psutil` dependency).

    The four axes give enough signal to distinguish "natural heavy run" from
    leak shape:
      - rss_kb grows linearly with no GC drops → RSS leak, escalate to
        `tracemalloc`.
      - open_fds grows with no decline → subprocess pipe / log handle leak,
        audit `_invoke`'s cleanup paths.
      - thread_count grows → leaked `_DescendantTracker` background threads
        not being joined.
      - phase / worker_count contextualize the other axes.

    `/proc/self/fd` is the canonical Linux FD source; pila's orchestrator
    runs as PID 1 inside the container (Linux), so the proc-fs path is
    valid. ru_maxrss is in KB on Linux (in bytes on macOS, but the
    orchestrator never runs on bare macOS — the launcher does, and we
    don't sample it). All probes are individually exception-guarded so a
    container without /proc still produces a partial sample rather than
    crashing the orchestrator over telemetry."""
    import resource
    import threading
    rss_kb = 0
    try:
        ru = resource.getrusage(resource.RUSAGE_SELF)
        rss_kb = int(ru.ru_maxrss)
    except Exception:
        pass

    open_fds = -1
    try:
        open_fds = len(os.listdir("/proc/self/fd"))
    except Exception:
        pass

    thread_count = -1
    try:
        thread_count = threading.active_count()
    except Exception:
        pass

    return {
        "ts": now(),
        "rss_kb": rss_kb,
        "phase": st.data.get("current_phase", "<unknown>"),
        "worker_count": st.data.get("worker_count", 0),
        "open_fds": open_fds,
        "thread_count": thread_count,
    }


async def _memory_sampler(st: "State",
                          interval_sec: float = 30.0) -> None:
    """Periodic orchestrator-memory sample for leak detection.

    Writes one ndjson line per `interval_sec` to `memory.ndjson` alongside
    `state.json`. Each line records RSS, current phase, worker count, open
    FDs, and thread count — enough to correlate growth with the phase and
    the worker concurrency in flight at that moment.

    Lifecycle: spawned as a fire-and-forget task by `orchestrate()`, cancelled
    in the `finally` block. On cancellation, one final sample is written
    before re-raising so the on-disk trail captures the orchestrator's
    end-of-run state.

    Never crashes the orchestrator: every probe is exception-guarded, the
    sample-write is exception-guarded, and an exception thrown anywhere
    inside the loop body is swallowed (telemetry that crashes the
    orchestrator is worse than no telemetry)."""
    # Re-resolve `st.run_dir` every tick — the orchestrator atomically
    # renames the run dir from `_bootstrap-<6hex>` to the final
    # `<run-id>` at the end of phase_classify (State.rename_to mutates
    # st.run_dir). Capturing the Path once would silently strand every
    # sample after the rename, since the bootstrap directory no longer
    # exists and `open("a")` would raise FileNotFoundError (swallowed
    # by the except below).
    while True:
        try:
            out = st.run_dir / "memory.ndjson"
            sample = _collect_memory_sample(st)
            with out.open("a", buffering=1) as f:
                f.write(json.dumps(sample, separators=(",", ":")) + "\n")
        except Exception:
            pass
        try:
            await asyncio.sleep(interval_sec)
        except asyncio.CancelledError:
            # Final sample before exit so the trail captures the
            # last-moment state. Best-effort: if the write fails (disk
            # full, run_dir gone), the existing samples on disk are
            # still useful; don't mask the CancelledError.
            try:
                out = st.run_dir / "memory.ndjson"
                sample = _collect_memory_sample(st)
                with out.open("a", buffering=1) as f:
                    f.write(json.dumps(sample, separators=(",", ":")) + "\n")
            except Exception:
                pass
            raise


async def claude_p(user_prompt: str, system_prompt: str, *, schema_key: str,
                   cwd: str, allowed_tools: str, max_turns: int, autonomous: bool,
                   caps: dict, st: "State", model: str, sid: str,
                   add_dirs: list[str] | None = None,
                   effort: str | None = None,
                   _suppress_capture: bool = False) -> dict:
    """Run one headless Claude Code worker and return its validated
    structured output.

    The worker's result is constrained with `--json-schema` (inline — a file
    path is silently ignored by the CLI). The CLI validates the worker's final
    output against the schema and exposes it as `structured_output` in the
    envelope. If that field is missing or the run reports an error, the worker
    is retried once with the failure noted, then declared failed.

    Worker activity streams as one JSON event per stdout line
    (`--output-format stream-json --verbose`). `_invoke` writes the raw
    events to `.pila/logs/<sid>.log` and emits per-event inline
    summaries gated by `st.data["verbosity"]`. The final `result` event
    is returned as the envelope — same shape as the pre-streaming
    single-result mode (`structured_output` present on schema success).

    `autonomous` workers skip permission prompts (they act on files inside an
    isolated worktree); non-autonomous workers get only read tools — unless
    `state.dangerously_skip_permissions` is set, in which case every worker
    is invoked with `--dangerously-skip-permissions`, waiving the §12
    mechanical read-only enforcement on judgment workers. See DESIGN §12
    and IMPLEMENTATION.md §2 "Permission override (dangerous)".

    `model` is a `claude --model` alias (`sonnet` / `opus` / `haiku`);
    resolved per worker-type by `resolve_models()` at startup.

    `effort` is a `claude --effort` level (`low` / `medium` / `high` /
    `xhigh` / `max`) or `None` to omit the flag entirely (worker
    inherits Claude's default). Resolved per worker-type by
    `resolve_efforts()` at startup. The CLI exposes no `--temperature`
    or `--seed`, so effort is the strongest determinism dial available
    — pinning it on judgment workers reduces cross-run variance in
    their structured output (IMPLEMENTATION.md §2 "Effort selection").

    `sid` is the worker identifier used in inline log tags and the
    per-worker log filename (e.g. `bugfix-001`, `classifier`,
    `planner-bug-fixing`, `integrator-feat-001`, `conformer-feat-003`).

    `add_dirs` are extra paths forwarded to the CLI as `--add-dir` entries.
    Used by the inspect bucket (classifier, planner, reconciler, provision)
    so the `Read`/`Grep`/`Glob` sandbox and the allowlisted `Bash` verbs can
    reach sibling repos referenced in the task description. Resolved by
    `resolve_inspect_dirs()` and persisted under `st.data["inspect_dirs"]`
    so `--resume` honors the original choice.
    """
    # Drift guard: typos in `schema_key` would write orphan rows into
    # calls.ndjson (judge/heal filter by call_type, so an orphan is
    # silently dropped). Fail fast at the call site instead. The
    # allowed set is WORKER_TYPES plus the two post-run skill schemas
    # (`judge`, `patch_generator`) that are not main-loop workers but
    # do invoke claude_p with their own schema.
    _allowed_schema_keys = set(WORKER_TYPES) | {
        "judge", "patch_generator", "pr_writer"}
    if schema_key not in _allowed_schema_keys:
        raise ValueError(
            f"claude_p called with unknown schema_key {schema_key!r}; "
            f"expected one of {sorted(_allowed_schema_keys)}"
        )
    schema = json.dumps(SCHEMAS[schema_key], separators=(",", ":"))
    pila_dir = st.path.parent
    verbosity = st.data.get("verbosity", VERBOSITY_DEFAULT)

    def build(extra_user: str = "") -> list[str]:
        cmd = [
            "claude", "-p", user_prompt + extra_user,
            "--append-system-prompt", system_prompt,
            "--output-format", "stream-json",
            "--verbose",
            "--json-schema", schema,
            "--allowedTools", allowed_tools,
            "--max-turns", str(max_turns),
            "--model", model,
        ]
        # IMPLEMENTATION.md §2 "Effort selection". When effort is None
        # (unset for this worker, the default for acting workers) the
        # CLI invocation is byte-identical to the pre-feature behavior;
        # only opted-in workers carry the flag.
        if effort is not None:
            cmd.extend(["--effort", effort])
        for d in (add_dirs or ()):
            cmd.extend(["--add-dir", d])
        skip_perms = autonomous or bool(
            st.data.get("dangerously_skip_permissions", False))
        if skip_perms:
            # Acting workers (autonomous=True) run inside an isolated
            # worktree; skipping prompts is what makes the run unattended,
            # blast radius bounded by the worktree. When the user passes
            # the top-level --dangerously-skip-permissions escape hatch
            # (DESIGN §12 last paragraph), judgment workers in the real
            # repo cwd also get the flag — §12 mechanical enforcement
            # waived, trust shifts onto the prompts.
            cmd.append("--dangerously-skip-permissions")
        return cmd

    timeout = caps["worker_timeout_sec"]

    async def _spawn(retry_note: str) -> dict:
        """One `_invoke` + telemetry + NDJSON capture + non-clean-exit
        warnings. Factored out so the auth/quota backoff loop below can
        re-invoke the worker without duplicating the capture/telemetry
        bookkeeping — every retry, success or failure, still produces
        one calls.ndjson row so the audit trail is complete."""
        _t0 = time.monotonic()
        envelope = await _invoke(build(retry_note), cwd, timeout,
                                 sid, pila_dir, verbosity,
                                 progress=_get_progress(st),
                                 idle_warn_sec=caps.get(
                                     "worker_idle_warn_sec",
                                     DEFAULT_CAPS["worker_idle_warn_sec"]),
                                 worker_memory_max_bytes=caps.get(
                                     "worker_memory_max_bytes"),
                                 worker_pids_max=caps.get(
                                     "worker_pids_max",
                                     DEFAULT_CAPS["worker_pids_max"]))
        _latency_ms = int((time.monotonic() - _t0) * 1000)

        # record run-weight telemetry
        st.add_telemetry(envelope)

        # capture NDJSON record — written on every attempt (success and failure)
        # so a hard-killed run leaves a complete audit trail.
        # Skipped when _suppress_capture=True (replay mode) so replays
        # never pollute the captures stream.
        if not _suppress_capture:
            _usage = envelope.get("usage") or {}
            _parsed_ok = envelope.get("structured_output") is not None
            _success = not envelope.get("is_error") and _parsed_ok
            # cgroup_applied: whether the per-worker cgroup containment
            # was active for this spawn. Useful when post-mortem
            # inspecting a calls.ndjson — a run with cgroup_applied
            # consistently False means the launcher's writable
            # /sys/fs/cgroup mount didn't propagate, and the OOM-
            # cascade safety net was off.
            _cgroup_applied = _CGROUP_PROBE_RESULT is True
            _capture_call(st.run_dir, {
                "call_id": str(uuid.uuid4()),
                "run_id": st.run_id,
                "call_type": schema_key,
                "model": model,
                "system_prompt": system_prompt,
                "user_content": user_prompt + retry_note,
                "response_content": str(envelope.get("result") or ""),
                "parsed_ok": _parsed_ok,
                "input_tokens": int(_usage.get("input_tokens") or 0),
                "output_tokens": int(_usage.get("output_tokens") or 0),
                "latency_ms": _latency_ms,
                "success": _success,
                "cgroup_applied": _cgroup_applied,
                "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            })

        # surface non-clean exits — a worker that hit --max-turns exits 0 and
        # can still produce structured_output, but stopped mid-work
        term = envelope.get("terminal_reason", "")
        turns = envelope.get("num_turns", -1)
        if term and term != "completed":
            log(f"  ⚠  worker exited with terminal_reason='{term}' "
                f"(num_turns={turns}) — output may be incomplete")
        # Context-decay proxy: a worker that returned at or above 80% of its
        # turn budget likely produced its final result against a degraded
        # context window. The schema only checks structure, not the quality
        # of reasoning underneath it. Surface the proxy so a 9.x confidence
        # score from a near-cap worker is read with the right scepticism.
        # `elif`: this branch only fires when the worker stopped cleanly —
        # if terminal_reason was set, the warning above already named
        # num_turns, so we avoid double-warning the same condition.
        elif turns >= 0 and turns >= int(0.8 * max_turns):
            log(f"  ⚠  worker returned at {turns}/{max_turns} turns "
                f"(≥80% of cap) — output may have been produced against a "
                "degraded context window")

        return envelope

    auth_retry_max_sec = caps.get(
        "auth_retry_max_sec", DEFAULT_CAPS["auth_retry_max_sec"])
    last_problem = ""
    for attempt in (1, 2):
        retry_note = ("" if attempt == 1 else
                      f"\n\nYOUR PREVIOUS ATTEMPT FAILED: {last_problem} "
                      "Return output that conforms exactly to the required schema.")
        envelope = await _spawn(retry_note)

        # Auth/quota backoff: 401/429/auth-message envelopes need waiting,
        # not the immediate corrective retry below. The gateway has already
        # rejected the request and a fresh request will be rejected too
        # until the user's Claude Code subscription window clears. Run
        # tenacity's exponential-backoff-with-jitter loop, capped at
        # `auth_retry_max_sec` cumulative seconds. The loop exits when an
        # invocation returns a non-auth envelope (success or a different
        # error class) or when the budget is exhausted.
        if _is_auth_or_quota_failure(envelope):
            def _log_before_sleep(rs: RetryCallState) -> None:
                env = rs.outcome.result()
                marker = (env.get("api_error_status") or "auth/quota")
                log(f"  worker hit {marker} — retrying in "
                    f"{rs.next_action.sleep:.0f}s "
                    f"(elapsed {rs.seconds_since_start:.0f}s of "
                    f"{auth_retry_max_sec}s budget)")

            # Use tenacity's __call__ (decorator) form rather than the
            # iterator form: the iterator form's AttemptManager.__exit__
            # unconditionally overwrites retry_state's result with None
            # on clean exit, defeating retry_if_result. __call__ sets
            # the result correctly inside its own loop and surfaces the
            # last attempt via RetryError.last_attempt on stop-fire.
            try:
                envelope = await AsyncRetrying(
                    wait=wait_exponential_jitter(
                        initial=15, max=120, jitter=5),
                    stop=stop_after_delay(auth_retry_max_sec),
                    retry=retry_if_result(_is_auth_or_quota_failure),
                    reraise=False,
                    before_sleep=_log_before_sleep,
                )(_spawn, retry_note)
            except RetryError as e:
                # Budget exhausted with the envelope still auth/quota.
                # Surface the last attempt's envelope so the
                # subscription-cap WorkerError below fires with
                # accurate context. retry_if_result only filters
                # results (not exceptions), so last_attempt holds a
                # result Future — .result() returns the envelope.
                envelope = e.last_attempt.result()

            if _is_auth_or_quota_failure(envelope):
                raise WorkerError(
                    "Claude API returned auth/quota error after "
                    f"~{auth_retry_max_sec}s of retries — your Claude "
                    "Code subscription likely hit its rolling usage "
                    "cap. Run --resume once the window clears.")

        if envelope.get("is_error"):
            last_problem = str(envelope.get("api_error_status")
                               or envelope.get("result") or "worker reported an error")
            continue
        structured = envelope.get("structured_output")
        if structured is None:
            last_problem = ("the run produced no structured_output — the final "
                            "output did not satisfy the JSON schema")
            continue
        return structured

    raise WorkerError(f"worker failed schema-valid output twice: {last_problem}")


async def replay_capture(record: dict, *,
                         override_system_prompt: str | None = None,
                         cwd: str | None = None) -> tuple[dict, dict]:
    """Replay one captured call from a calls.ndjson record.

    Given a single NDJSON record (as a dict), reconstructs the `claude_p()`
    invocation with the captured `system_prompt`, `user_content`, `call_type`
    (mapped to `schema_key`), `model`, and any other reproducible parameters.
    Returns `(envelope, structured_output)` from the new invocation.

    `override_system_prompt` lets the heal loop replay with a patched prompt
    instead of the originally captured one.

    Replays use a throw-away in-memory state and `_suppress_capture=True` so
    they never pollute the original run's calls.ndjson — the capture stream is
    the ground truth; replay is ephemeral analysis.

    `cwd` defaults to the current working directory. The replay worker runs
    non-autonomous (read-only tools) by default, matching the behaviour most
    call types actually use; callers may not need write access for scoring.

    The returned structured_output is the parsed object from the new envelope.
    A WorkerError is raised if the replay call fails schema validation twice,
    same as a live call.
    """
    call_type = record["call_type"]
    system_prompt = override_system_prompt or record["system_prompt"]
    user_prompt = record["user_content"]
    model = record.get("model", MODEL_DEFAULT)

    # Minimal in-memory state: no run dir needed because capture is suppressed.
    # _suppress_capture=True prevents _capture_call from writing anywhere;
    # add_telemetry is called but state.save() writes to a tempdir that is
    # discarded after replay.
    import tempfile
    with tempfile.TemporaryDirectory() as _tmpdir:
        tmp_run_dir = Path(_tmpdir) / "replay-run"
        tmp_run_dir.mkdir()
        tmp_state_path = tmp_run_dir / "state.json"
        tmp_state_path.write_text("{}")

        replay_st = _ReplayState(tmp_run_dir, tmp_state_path)
        caps = dict(DEFAULT_CAPS)

        # Replay deliberately omits `effort=`: captured records don't
        # store the original `--effort` level, so a faithful replay
        # would have to guess. Falling through to claude_p's None
        # default keeps replays shaped like every other
        # "no-effort-pinned" call.
        structured = await claude_p(
            user_prompt=user_prompt,
            system_prompt=system_prompt,
            schema_key=call_type,
            cwd=cwd or os.getcwd(),
            allowed_tools=INSPECT_TOOLS,
            max_turns=40,
            autonomous=False,
            caps=caps,
            st=replay_st,
            model=model,
            sid=f"replay-{call_type}",
            _suppress_capture=True,
        )
    envelope = replay_st.last_envelope
    return (envelope, structured)


class _ReplayState:
    """Minimal State-alike for replay_capture: no persistent writes.

    Satisfies the interface claude_p() calls on the state object (bump_workers,
    add_telemetry, .data, .run_id, .run_dir, .path) without touching .pila/.
    All save() calls are no-ops. last_envelope captures the envelope returned
    by _invoke so replay_capture can return (envelope, structured_output).
    """

    def __init__(self, run_dir: Path, state_path: Path) -> None:
        self.run_dir = run_dir
        self.path = state_path
        self.run_id = "replay"
        self.data: dict = {
            "telemetry": {"calls": 0, "cost_usd": 0.0,
                          "input_tokens": 0, "output_tokens": 0},
            "verbosity": "quiet",
        }
        self.last_envelope: dict = {}

    def save(self) -> None:
        pass  # replay writes nothing

    def bump_workers(self, caps: dict) -> None:
        pass  # no budget tracking during replay

    def add_telemetry(self, envelope: dict) -> None:
        self.last_envelope = envelope
        t = self.data.setdefault("telemetry", {"calls": 0, "cost_usd": 0.0,
                                               "input_tokens": 0,
                                               "output_tokens": 0})
        t["calls"] += 1
        t["cost_usd"] += float(envelope.get("total_cost_usd") or 0.0)
        usage = envelope.get("usage") or {}
        t["input_tokens"] += int(usage.get("input_tokens") or 0)
        t["output_tokens"] += int(usage.get("output_tokens") or 0)


# =========================================================================
# run state — persisted so a run is observable and resumable
# =========================================================================
class State:
    """In-memory run state with atomic on-disk persistence.

    No lock: every mutator runs on the single asyncio event loop, so reads and
    writes are not preempted mid-statement. Concurrent `claude -p` workers
    spawned via `asyncio.gather` interleave only at `await` points, which never
    fall inside a `st.data[k] = v; st.save()` pair.

    Per-run scope: every State instance is anchored at
    `pila_root / "runs" / run_id / state.json`. Two State instances with
    different run_ids share no on-disk state. See DESIGN.md §6 and §10."""

    def __init__(self, pila_root: Path, run_id: str):
        self.pila_root = pila_root
        self.run_id = run_id
        self.run_dir = pila_root / "runs" / run_id
        self.path = self.run_dir / "state.json"
        self.data: dict = {}

    def load(self) -> bool:
        if self.path.exists():
            self.data = json.loads(self.path.read_text())
            return True
        return False

    def save(self) -> None:
        """Atomic write via temp-file rename."""
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2))
        tmp.replace(self.path)   # atomic on POSIX; best-effort on Windows

    def rename_to(self, new_run_id: str) -> None:
        """Atomically rename the run dir to a new run_id. Used by
        orchestrate() after phase_classify to promote the bootstrap dir
        (`_bootstrap-<6hex>`) to the final run_id derived from the
        classifier category. Fails closed if the target directory
        already exists — that would mean two runs with the same
        microsecond `started_at` (extraordinarily unlikely, but caught
        as a hard error rather than silently overwritten)."""
        new_dir = self.pila_root / "runs" / new_run_id
        if new_dir.exists():
            die(
                f"run_id collision: .pila/runs/{new_run_id}/ already exists. "
                "This is extraordinarily unlikely; rerun, or "
                f"`--resume --run-id {new_run_id}` to continue the existing run."
            )
        os.rename(self.run_dir, new_dir)
        self.run_id = new_run_id
        self.run_dir = new_dir
        self.path = new_dir / "state.json"

    def bump_workers(self, caps: dict) -> None:
        self.data["worker_count"] = self.data.get("worker_count", 0) + 1
        count = self.data["worker_count"]
        self.save()
        if count > caps["max_total_workers"]:
            raise WorkerError(
                f"worker budget exhausted ({caps['max_total_workers']}). "
                "State saved; re-run with --resume after raising --max-workers."
            )

    def add_telemetry(self, envelope: dict) -> None:
        """Accumulate run-weight signals from a worker envelope. On a
        subscription the dollar figure is not billed, but it and the token
        counts are a useful proxy for how heavy the run is."""
        t = self.data.setdefault("telemetry", {"calls": 0, "cost_usd": 0.0,
                                               "input_tokens": 0,
                                               "output_tokens": 0})
        t["calls"] += 1
        t["cost_usd"] += float(envelope.get("total_cost_usd") or 0.0)
        usage = envelope.get("usage") or {}
        t["input_tokens"] += int(usage.get("input_tokens") or 0)
        t["output_tokens"] += int(usage.get("output_tokens") or 0)
        self.save()


# =========================================================================
# judge phase — LLM-scored review of captured call records
# =========================================================================

async def judge_capture(record: dict, models: dict[str, str],
                        efforts: dict[str, str | None],
                        caps: dict, st: "State") -> dict:
    """Run a judge worker against one captured call record.

    The judge evaluates the record's response_content on three dimensions:
    schema adherence, factual accuracy, and hallucination-freeness. Uses
    claude_p() with schema_key="judge" and a deterministic sid derived from
    the call_type and call_id so the per-worker log file is locatable.

    Returns the structured judge output dict (validated against
    SCHEMAS["judge"]).
    """
    call_type = record.get("call_type", "unknown")
    call_id = record.get("call_id", "unknown")
    sys_prompt = load_prompt("judge")
    user_prompt = (
        "CALL RECORD TO JUDGE:\n"
        f"call_type: {call_type}\n"
        f"call_id: {call_id}\n"
        f"model: {record.get('model', '')}\n\n"
        "SYSTEM PROMPT (the instructions the worker was given):\n"
        f"{record.get('system_prompt', '')}\n\n"
        "USER CONTENT (the input the worker received):\n"
        f"{record.get('user_content', '')}\n\n"
        "RESPONSE CONTENT (what the worker produced):\n"
        f"{record.get('response_content', '')}\n\n"
        f"parsed_ok: {record.get('parsed_ok', False)}\n"
        f"success: {record.get('success', False)}\n\n"
        "Judge this call on the three dimensions and return your verdict."
    )
    # Judge workers are stateless observers — read-only tools only.
    model = models.get("judge", MODEL_DEFAULT)
    effort = efforts.get("judge")
    st.bump_workers(caps)
    return await claude_p(
        user_prompt=user_prompt,
        system_prompt=sys_prompt,
        schema_key="judge",
        cwd=os.getcwd(),
        allowed_tools=INSPECT_TOOLS,
        max_turns=40,
        autonomous=False,
        caps=caps,
        st=st,
        model=model,
        effort=effort,
        sid=f"judge-{call_type}-{call_id[:8]}",
    )


async def phase_judge(run_dir: Path, judge_out_dir: Path,
                      caps: dict, st: "State",
                      models: dict[str, str],
                      efforts: dict[str, str | None],
                      judge_call_types: list[str] | None = None) -> dict:
    """Judge all captured call records in run_dir/calls.ndjson.

    Reads each line of calls.ndjson, optionally filters by call_type when
    `judge_call_types` is provided, then runs judge_capture() in parallel
    under the existing asyncio.Semaphore(max_parallel) bound.

    Each verdict is written to judge_out_dir/<call_id>.json. After all
    judgments complete, an INDEX.json is written to judge_out_dir/ listing
    every judged call with its call_id, call_type, and passed status.

    Returns a dict with keys "judged" (count) and "index" (list of index
    entries).
    """
    capture_path = run_dir / "calls.ndjson"
    if not capture_path.exists():
        log("phase_judge: no calls.ndjson found — nothing to judge")
        return {"judged": 0, "index": []}

    records: list[dict] = []
    for line in capture_path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            log(f"  phase_judge: skipping malformed NDJSON line: {line[:80]!r}")
            continue
        if judge_call_types and rec.get("call_type") not in judge_call_types:
            continue
        records.append(rec)

    if not records:
        log("phase_judge: no records to judge after filtering")
        return {"judged": 0, "index": []}

    judge_out_dir.mkdir(parents=True, exist_ok=True)
    log(f"phase_judge: judging {len(records)} record(s)")

    sem = asyncio.Semaphore(caps["max_parallel"])
    index: list[dict] = []

    async def judge_one(rec: dict) -> None:
        async with sem:
            call_id = rec.get("call_id", "unknown")
            call_type = rec.get("call_type", "unknown")
            verdict = await judge_capture(rec, models, efforts, caps, st)
            verdict_path = judge_out_dir / f"{call_id}.json"
            verdict_path.write_text(json.dumps(verdict, indent=2))
            index.append({
                "call_id": call_id,
                "call_type": call_type,
                "passed": verdict.get("passed", False),
            })
            status = "pass" if verdict.get("passed") else "FAIL"
            log(f"  judge-{call_type}-{call_id[:8]}: {status}")

    await gather_or_cancel(*(judge_one(r) for r in records))

    # Sort index by call_id for stable output across parallel orderings.
    index.sort(key=lambda e: e["call_id"])
    (judge_out_dir / "INDEX.json").write_text(json.dumps(index, indent=2))
    log(f"phase_judge: wrote {len(index)} verdict(s) to {judge_out_dir}")
    return {"judged": len(index), "index": index}


# =========================================================================
# heal-loop — persistent state and three phase functions
# =========================================================================

class HealState:
    """Persistent state for one heal-loop run scoped to a single call_type.

    Layout on disk: <heal_dir>/<call_type>/state.json

    Fields written to state.json:
      failing_samples  — list of capture records the heal loop is working on
      baseline         — {call_id: {"pass_rate": float, "verdicts": list}}
                         noise-floor measured by heal_baseline
      history          — list of iteration records appended by heal_replay_patched
      best_so_far      — {pass_rate: float, iter_n: int} tracking the best arm
    """

    def __init__(self, heal_dir: Path, call_type: str) -> None:
        self.heal_dir = heal_dir
        self.call_type = call_type
        self.state_dir = heal_dir / call_type
        self.path = self.state_dir / "state.json"
        self.failing_samples: list[dict] = []
        self.baseline: dict = {}
        self.history: list[dict] = []
        self.best_so_far: dict = {}

    def save(self) -> None:
        """Atomic write via temp-file rename."""
        self.state_dir.mkdir(parents=True, exist_ok=True)
        data = {
            "failing_samples": self.failing_samples,
            "baseline": self.baseline,
            "history": self.history,
            "best_so_far": self.best_so_far,
        }
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(self.path)

    def load(self) -> bool:
        """Load state from disk. Returns True if file existed and was loaded."""
        if not self.path.exists():
            return False
        data = json.loads(self.path.read_text())
        self.failing_samples = data.get("failing_samples", [])
        self.baseline = data.get("baseline", {})
        self.history = data.get("history", [])
        self.best_so_far = data.get("best_so_far", {})
        return True


async def heal_baseline(call_type: str, failing_records: list[dict], n: int,
                        heal_dir: Path, caps: dict, st: "State",
                        models: dict[str, str],
                        efforts: dict[str, str | None]) -> HealState:
    """Run n unpatched replays per failing capture to establish a noise-floor.

    For each record in failing_records, runs n replay_capture() calls with the
    original system prompt (no override), judges each replay via judge_capture(),
    and persists the per-sample pass rates + verdict list to
    <heal_dir>/<call_type>/baseline/verdicts/.

    Returns a HealState with failing_samples, baseline, and best_so_far set.
    Replays run in parallel under asyncio.Semaphore(max_parallel).
    """
    hs = HealState(heal_dir, call_type)
    hs.failing_samples = list(failing_records)

    verdicts_dir = heal_dir / call_type / "baseline" / "verdicts"
    verdicts_dir.mkdir(parents=True, exist_ok=True)

    sem = asyncio.Semaphore(caps["max_parallel"])
    baseline: dict = {}

    async def _run_one(record: dict, replay_idx: int) -> dict:
        """Run one replay+judge pair; return verdict dict."""
        async with sem:
            call_id = record["call_id"]
            # Replay with original system prompt (no patch).
            try:
                envelope, _ = await replay_capture(record)
            except Exception:
                envelope = {}
            # Build a synthetic record for the judge using the replayed output.
            judge_record = dict(record)
            judge_record["response_content"] = (
                envelope.get("result") or record.get("response_content", "")
            )
            judge_record["parsed_ok"] = not envelope.get("is_error", True)
            judge_record["success"] = not envelope.get("is_error", True)
            verdict = await judge_capture(judge_record, models, efforts, caps, st)
            # Write verdict file.
            call_id = record["call_id"]
            verdict_path = verdicts_dir / f"{call_id}-{replay_idx}.json"
            verdict_path.write_text(json.dumps(verdict, indent=2))
            return verdict

    # Gather all (record, replay_idx) pairs.
    tasks = []
    for record in failing_records:
        for idx in range(n):
            tasks.append((record, idx))

    results: list[tuple[dict, dict]] = []
    coros = [_run_one(rec, idx) for rec, idx in tasks]
    verdicts_flat = await gather_or_cancel(*coros)

    # Aggregate per-sample pass rates.
    task_idx = 0
    for record in failing_records:
        call_id = record["call_id"]
        sample_verdicts = []
        for idx in range(n):
            sample_verdicts.append(verdicts_flat[task_idx])
            task_idx += 1
        passes = sum(1 for v in sample_verdicts if v.get("passed", False))
        baseline[call_id] = {
            "pass_rate": passes / n if n > 0 else 0.0,
            "verdicts": sample_verdicts,
        }

    hs.baseline = baseline
    overall_pass_rate = (
        sum(v["pass_rate"] for v in baseline.values()) / len(baseline)
        if baseline else 0.0
    )
    hs.best_so_far = {"pass_rate": overall_pass_rate, "iter_n": 0}
    hs.save()
    log(f"heal_baseline: {call_type}: {len(failing_records)} sample(s), "
        f"n={n}, baseline pass_rate={overall_pass_rate:.2%}")
    return hs


def heal_apply_patch(call_type: str, iter_n: int, patch_text: str,
                     anchor_match: str, heal_dir: Path,
                     failing_records: list[dict]) -> list[Path]:
    """Materialise per-sample patched prompts under iter-<N>/patched-prompts/.

    For each record in failing_records, replaces the first occurrence of
    `anchor_match` in the original system_prompt with `patch_text`, and writes
    the result to <heal_dir>/<call_type>/iter-<N>/patched-prompts/<call_id>.txt.

    Returns the list of written paths.
    """
    out_dir = heal_dir / call_type / f"iter-{iter_n}" / "patched-prompts"
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for record in failing_records:
        call_id = record["call_id"]
        original = record.get("system_prompt", "")
        patched = original.replace(anchor_match, patch_text, 1)
        dest = out_dir / f"{call_id}.txt"
        dest.write_text(patched)
        written.append(dest)
    log(f"heal_apply_patch: {call_type} iter-{iter_n}: "
        f"wrote {len(written)} patched prompt(s)")
    return written


async def heal_replay_patched(call_type: str, iter_n: int, n: int,
                              heal_dir: Path, caps: dict, st: "State",
                              models: dict[str, str],
                              efforts: dict[str, str | None]) -> HealState:
    """Run n patched replays per failing capture and append an iteration record.

    Reads HealState from disk. For each failing sample, loads the patched
    prompt from iter-<iter_n>/patched-prompts/<call_id>.txt, runs n
    replay_capture() calls with that prompt override, judges each via
    judge_capture(), computes the pass rate, and appends an iteration record
    to hs.history. Updates hs.best_so_far if the pass rate improved.

    Replays run in parallel under asyncio.Semaphore(max_parallel).
    Returns the updated HealState.
    """
    hs = HealState(heal_dir, call_type)
    if not hs.load():
        raise FileNotFoundError(
            f"HealState not found at {hs.path} — run heal_baseline first"
        )

    patched_dir = heal_dir / call_type / f"iter-{iter_n}" / "patched-prompts"
    verdicts_dir = heal_dir / call_type / f"iter-{iter_n}" / "verdicts"
    verdicts_dir.mkdir(parents=True, exist_ok=True)

    sem = asyncio.Semaphore(caps["max_parallel"])

    async def _run_one(record: dict, replay_idx: int,
                       patched_prompt: str) -> dict:
        async with sem:
            try:
                envelope, _ = await replay_capture(
                    record, override_system_prompt=patched_prompt
                )
            except Exception:
                envelope = {}
            judge_record = dict(record)
            judge_record["response_content"] = (
                envelope.get("result") or record.get("response_content", "")
            )
            judge_record["parsed_ok"] = not envelope.get("is_error", True)
            judge_record["success"] = not envelope.get("is_error", True)
            verdict = await judge_capture(judge_record, models, efforts, caps, st)
            call_id = record["call_id"]
            verdict_path = verdicts_dir / f"{call_id}-{replay_idx}.json"
            verdict_path.write_text(json.dumps(verdict, indent=2))
            return verdict

    # Build tasks: (record, patched_prompt, replay_idx).
    tasks: list[tuple[dict, str, int]] = []
    for record in hs.failing_samples:
        call_id = record["call_id"]
        prompt_path = patched_dir / f"{call_id}.txt"
        if not prompt_path.exists():
            log(f"  heal_replay_patched: missing patched prompt for {call_id}, "
                f"skipping")
            continue
        patched_prompt = prompt_path.read_text()
        for idx in range(n):
            tasks.append((record, patched_prompt, idx))

    coros = [_run_one(rec, idx, prompt) for rec, prompt, idx in tasks]
    verdicts_flat: list[dict] = await gather_or_cancel(*coros)

    # Aggregate per-sample pass rates for this iteration.
    iter_scores: dict = {}
    task_offset = 0
    records_with_prompts = [
        r for r in hs.failing_samples
        if (patched_dir / f"{r['call_id']}.txt").exists()
    ]
    for record in records_with_prompts:
        call_id = record["call_id"]
        sample_verdicts = verdicts_flat[task_offset:task_offset + n]
        task_offset += n
        passes = sum(1 for v in sample_verdicts if v.get("passed", False))
        iter_scores[call_id] = {
            "pass_rate": passes / n if n > 0 else 0.0,
            "verdicts": sample_verdicts,
        }

    overall_pass_rate = (
        sum(v["pass_rate"] for v in iter_scores.values()) / len(iter_scores)
        if iter_scores else 0.0
    )

    iter_record = {
        "iter_n": iter_n,
        "pass_rate": overall_pass_rate,
        "scores": iter_scores,
    }
    hs.history.append(iter_record)

    if overall_pass_rate > hs.best_so_far.get("pass_rate", 0.0):
        hs.best_so_far = {"pass_rate": overall_pass_rate, "iter_n": iter_n}

    hs.save()
    log(f"heal_replay_patched: {call_type} iter-{iter_n}: "
        f"pass_rate={overall_pass_rate:.2%}")
    return hs


def check_convergence(state: HealState, config: dict) -> str:
    """Evaluate whether the heal loop has converged.

    Returns one of:
      SUCCESS          — best pass_rate >= config["success_threshold"]
      TIMEOUT          — iterations exhausted (len(history) >= max_iterations)
      BUDGET_EXHAUSTED — worker_count reached max_total_workers
      PLATEAUED        — last plateau_window iterations all have |delta| < plateau_delta
      REGRESSED        — every history entry's pass_rate is below the baseline
      CONTINUE         — none of the above; keep iterating

    `config` keys (all required):
      success_threshold   float  — e.g. 0.9
      max_iterations      int    — e.g. 10
      plateau_window      int    — e.g. 3
      plateau_delta       float  — e.g. 0.03
      worker_count        int    — current worker invocation count
      max_total_workers   int    — cap from caps dict

    The convergence check is deterministic (DESIGN §12): it operates entirely
    on measurements in HealState.history and best_so_far, with no model judgment.
    """
    history = state.history
    best_pass_rate = state.best_so_far.get("pass_rate", 0.0)
    success_threshold = config["success_threshold"]
    max_iterations = config["max_iterations"]
    plateau_window = config["plateau_window"]
    plateau_delta = config["plateau_delta"]
    worker_count = config.get("worker_count", 0)
    max_total_workers = config.get("max_total_workers", DEFAULT_CAPS["max_total_workers"])

    # SUCCESS: best arm already meets the target.
    if best_pass_rate >= success_threshold:
        return "SUCCESS"

    # BUDGET_EXHAUSTED: worker cap reached before convergence.
    if worker_count >= max_total_workers:
        return "BUDGET_EXHAUSTED"

    # TIMEOUT: iteration cap hit.
    if len(history) >= max_iterations:
        return "TIMEOUT"

    # REGRESSED: every iteration was worse than baseline.
    if history:
        baseline_rate = (
            sum(v["pass_rate"] for v in state.baseline.values()) / len(state.baseline)
            if state.baseline else 0.0
        )
        if all(entry.get("pass_rate", 0.0) < baseline_rate for entry in history):
            return "REGRESSED"

    # PLATEAUED: the last plateau_window iterations all changed by less than plateau_delta.
    if len(history) >= plateau_window:
        recent = history[-plateau_window:]
        rates = [entry.get("pass_rate", 0.0) for entry in recent]
        deltas = [abs(rates[i] - rates[i - 1]) for i in range(1, len(rates))]
        if all(d < plateau_delta for d in deltas):
            return "PLATEAUED"

    return "CONTINUE"


def write_heal_report(call_type: str, state: HealState,
                      best_patch_text: str = "") -> Path:
    """Render a markdown heal report to <heal_dir>/<call_type>/healing-<call_type>.md.

    The report includes:
    - The best patch text (or 'none' when no patch improved on baseline)
    - The number of iterations run
    - A per-iteration history table with pass rates
    - The baseline pass rate

    Returns the path of the written file.
    """
    report_dir = state.state_dir
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"healing-{call_type}.md"

    baseline_rate = (
        sum(v["pass_rate"] for v in state.baseline.values()) / len(state.baseline)
        if state.baseline else 0.0
    )
    best = state.best_so_far
    best_rate = best.get("pass_rate", 0.0)
    best_iter = best.get("iter_n", 0)
    n_iterations = len(state.history)

    lines = [
        f"# Heal report: {call_type}",
        "",
        f"**Iterations run:** {n_iterations}  ",
        f"**Baseline pass rate:** {baseline_rate:.1%}  ",
        f"**Best pass rate:** {best_rate:.1%} (iter {best_iter})  ",
        "",
        "## Best patch",
        "",
        "```",
        best_patch_text if best_patch_text else "(no patch improved on baseline)",
        "```",
        "",
        "## Iteration history",
        "",
        "| iter | pass_rate |",
        "|------|-----------|",
    ]
    for entry in state.history:
        lines.append(f"| {entry.get('iter_n', '?')} | {entry.get('pass_rate', 0.0):.1%} |")

    if not state.history:
        lines.append("| — | — |")

    lines.append("")
    report_path.write_text("\n".join(lines))
    log(f"write_heal_report: {call_type}: wrote {report_path}")
    return report_path


async def request_patch(state: HealState, iter_n: int,
                        st: "State", caps: dict,
                        models: dict[str, str],
                        efforts: dict[str, str | None]) -> tuple[str, str]:
    """Invoke the patch-generator worker to propose a minimal prompt edit.

    Builds a user_prompt containing:
    - The current prompt body (resolved via resolve_prompt)
    - The failing samples (response_content from each)
    - The prior iteration history for context

    Calls claude_p() with schema_key="patch_generator" and sid
    `heal-patch-<call_type>-iter<N>`.

    After the worker responds, validates that the returned `anchor` is a
    literal substring of the resolved prompt body. If not, raises ValueError
    — the heal loop must not apply a patch that cannot be cleanly located in
    the prompt (per the prompts-are-advisory-code-enforces principle: this
    check lives in code, not in the prompt).

    Returns (anchor_match, patch_text) on success.
    """
    call_type = state.call_type
    _, prompt_body, _ = resolve_prompt(call_type)
    sys_prompt = load_prompt("patch_generator")

    # Build the failing samples section: only response_content is needed
    # for the patch-generator to understand what went wrong.
    sample_lines = []
    for rec in state.failing_samples:
        cid = rec.get("call_id", "?")
        resp = rec.get("response_content", "")
        sample_lines.append(f"call_id: {cid}\nresponse_content:\n{resp}")
    samples_block = "\n---\n".join(sample_lines) if sample_lines else "(none)"

    # Prior history: anchor/replacement/strategy/pass_rate for each iteration.
    history_lines = []
    for entry in state.history:
        n = entry.get("iter_n", "?")
        pr = entry.get("pass_rate", 0.0)
        # patch text is not stored in history; only pass_rate and scores are.
        history_lines.append(f"iter {n}: pass_rate={pr:.2%}")
    history_block = "\n".join(history_lines) if history_lines else "(no prior iterations)"

    user_prompt = (
        f"CALL TYPE: {call_type}\n"
        f"ITERATION: {iter_n}\n\n"
        "CURRENT SYSTEM PROMPT:\n"
        f"{prompt_body}\n\n"
        "FAILING SAMPLES:\n"
        f"{samples_block}\n\n"
        "PRIOR ITERATION HISTORY:\n"
        f"{history_block}\n\n"
        "Propose a minimal patch to the system prompt that addresses the "
        "failure mode. Return anchor, replacement, strategy, and pivot_reason."
    )

    model = models.get("heal", MODEL_DEFAULT_PER_WORKER.get("heal", MODEL_DEFAULT))
    effort = efforts.get("heal")
    st.bump_workers(caps)
    result = await claude_p(
        user_prompt=user_prompt,
        system_prompt=sys_prompt,
        schema_key="patch_generator",
        cwd=os.getcwd(),
        allowed_tools=INSPECT_TOOLS,
        max_turns=40,
        autonomous=False,
        caps=caps,
        st=st,
        model=model,
        effort=effort,
        sid=f"heal-patch-{call_type}-iter{iter_n}",
    )

    anchor = result.get("anchor", "")
    replacement = result.get("replacement", "")

    # Code-enforced: anchor must be a literal substring of the prompt body.
    # A patch that cannot be located would corrupt the prompt silently —
    # the prompt is advisory but this application check is mechanical.
    if anchor not in prompt_body:
        raise ValueError(
            f"request_patch: anchor {anchor!r} not found in resolved prompt "
            f"for call_type={call_type!r} — cannot apply patch safely"
        )

    return anchor, replacement


async def phase_heal(call_type: str, failing_records: list[dict],
                     heal_dir: Path, caps: dict,
                     st: "State", models: dict[str, str],
                     efforts: dict[str, str | None],
                     request_patch_fn=None,
                     n: int = HEAL_N_REPLAYS_DEFAULT,
                     config: dict | None = None) -> str:
    """Drive the full heal loop for one call_type.

    Phases (per iteration):
      1. Baseline (once): run n unpatched replays per record to measure noise-floor.
      2. Loop:
         a. request_patch_fn(state, iter_n) → (anchor_match, patch_text)
         b. heal_apply_patch — materialise patched prompts
         c. heal_replay_patched — run n replays with the patched prompt + judge
         d. check_convergence — returns SUCCESS/PLATEAUED/TIMEOUT/BUDGET_EXHAUSTED/
            REGRESSED/CONTINUE
      3. write_heal_report — always written, even if the loop terminates early.

    `request_patch_fn` is a callable taking (state: HealState, iter_n: int) and
    returning (anchor_match: str, patch_text: str). When None (the default), the
    real `request_patch` worker is used. Injecting a stub keeps this function
    independently testable.

    Note: the injected callable may be sync (for tests) or async. If it is a
    sync stub with 2 arguments (state, iter_n), it is called directly. If it is
    None, the real async `request_patch(state, iter_n, st, caps, models)` is
    awaited — this is the production path.

    Returns the terminal verdict string.
    """
    converge_config = dict({
        "success_threshold": HEAL_SUCCESS_THRESHOLD_DEFAULT,
        "max_iterations": HEAL_MAX_ROUNDS_DEFAULT,
        "plateau_window": HEAL_PLATEAU_WINDOW_DEFAULT,
        "plateau_delta": HEAL_PLATEAU_DELTA_DEFAULT,
    }, **(config or {}))

    # Merge in caps-derived fields so check_convergence has budget visibility.
    converge_config.setdefault("worker_count", st.data.get("worker_count", 0))
    converge_config.setdefault("max_total_workers",
                               caps.get("max_total_workers",
                                        DEFAULT_CAPS["max_total_workers"]))

    log(f"phase_heal: {call_type}: starting heal loop "
        f"(max_iter={converge_config['max_iterations']}, "
        f"threshold={converge_config['success_threshold']:.0%}, "
        f"n={n})")

    hs = await heal_baseline(call_type, failing_records, n, heal_dir, caps, st,
                             models, efforts)

    best_patch_text: str = ""
    verdict = "CONTINUE"
    iter_n = 0

    while verdict == "CONTINUE":
        iter_n += 1
        # Update worker_count snapshot before convergence check each iteration.
        converge_config["worker_count"] = st.data.get("worker_count", 0)

        # Invoke the patch generator: real worker (default) or injected stub.
        if request_patch_fn is None:
            anchor_match, patch_text = await request_patch(
                hs, iter_n, st, caps, models, efforts)
        elif asyncio.iscoroutinefunction(request_patch_fn):
            anchor_match, patch_text = await request_patch_fn(hs, iter_n)
        else:
            anchor_match, patch_text = request_patch_fn(hs, iter_n)

        heal_apply_patch(call_type, iter_n, patch_text, anchor_match,
                         heal_dir, hs.failing_samples)
        hs = await heal_replay_patched(call_type, iter_n, n, heal_dir,
                                       caps, st, models, efforts)
        converge_config["worker_count"] = st.data.get("worker_count", 0)
        verdict = check_convergence(hs, converge_config)

        # Track the patch text that produced the best result so far.
        if hs.best_so_far.get("iter_n", 0) == iter_n:
            best_patch_text = patch_text

        log(f"phase_heal: {call_type} iter-{iter_n}: verdict={verdict}")

    write_heal_report(call_type, hs, best_patch_text)
    log(f"phase_heal: {call_type}: terminated with {verdict}")
    return verdict


# =========================================================================
# per-repo dependency provisioning helpers (DESIGN §6½)
# =========================================================================

# Categories that touch only documentation / non-code surfaces. If classify
# returned *only* these, phase_provision short-circuits to kind:none — no
# point detecting an install recipe for a docs-only run (workers wouldn't
# need it anyway, and skipping the lockfile-table / LLM-fallback work
# trims the run time). The check is "are all returned categories in this
# set?" so a feature+docs task still produces a recipe.
_DOCS_ONLY_CATEGORIES = frozenset({"documentation"})


async def run_setup_hook(repo_root: Path, log_dir: Path,
                          st: "State") -> None:
    """Execute `<repo>/.pila-setup.sh` if present. Idempotent via
    `st.data["provision"]["sh_hook_ran"]` — re-entering this function
    after the hook has already run is a no-op.

    The script runs as the `pila` container user (non-root), in the
    repo root, with the same environment workers will see. Output
    streams to `<log_dir>/setup-hook.log`. Nonzero exit → `die()`.

    **What the hook CAN do** (runs unprivileged):
    - `mise install <lang>@<version>` to add a language runtime mise
      supports beyond the image-baked LTS bake (Ruby, Java, Rust, etc.).
    - Install user-space CLI tools into `~/.local/bin` or any other
      user-writable location.
    - Pre-populate fixtures the workers need (sample data, config).
    - Set per-run environment variables via `~/.bashrc` (note: the
      orchestrator does not source bashrc by default; the hook would
      need to write its own activation).

    **What the hook CANNOT do** (no root, no sudo):
    - `apt-get install` or any package-manager invocation requiring
      root. The container intentionally does NOT ship sudo.
    - Write to `/usr/*`, `/etc/*`, or any other system directory.
    - Install system services.

    If a repo needs a system package the language layer can't provide,
    the documented workaround is to maintain a fork of the pila
    Dockerfile that installs it at image-build time and override
    `IMAGE_TAG`. Out of scope for pila to automate.
    """
    prov = st.data.setdefault("provision", {})
    if prov.get("sh_hook_ran"):
        return
    hook = repo_root / ".pila-setup.sh"
    if not hook.exists():
        return
    # A path at .pila-setup.sh that isn't a regular file (most likely a
    # directory committed by mistake) is silent-failure-shaped: workers
    # would later die with confusing "command not found" messages from
    # the unrun setup. Surface the misshape here with a clear message.
    if not hook.is_file():
        die(
            f".pila-setup.sh at {hook} exists but is not a regular "
            "file (it's a directory or special file). Remove the "
            "misnamed entry or replace it with an executable script."
        )

    log("phase 1½: running .pila-setup.sh")
    st.data["current_phase"] = "phase 1½: setup-hook"
    st.save()
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "setup-hook.log"
    verbosity = st.data.get("verbosity", VERBOSITY_DEFAULT)
    try:
        rc, tail = await run_streaming(
            ["bash", str(hook)],
            cwd=str(repo_root),
            timeout=600,  # 10 minutes
            log_path=log_path,
            label=".pila-setup.sh",
            verbosity=verbosity,
        )
    except subprocess.TimeoutExpired as exc:
        die(f".pila-setup.sh did not complete within 10 minutes\n"
            f"(see {log_path})\n{exc.output or ''}")
    if rc != 0:
        die(f".pila-setup.sh exited {rc}\n(see {log_path})\n{tail}")
    prov["sh_hook_ran"] = True
    st.save()


# Regex for extracting the `go 1.X[.Y]` directive from a go.mod file.
# Matches the canonical form documented at https://go.dev/ref/mod#go-mod-file-go .
_GO_MOD_VERSION_RE = re.compile(r"^\s*go\s+(\d+(?:\.\d+){0,2})\s*$", re.MULTILINE)


def _existing_mise_toml_path(repo_root: Path) -> Path | None:
    """Return the path to whichever of `mise.toml` or `.mise.toml`
    exists in the repo root, or None if neither does. Prefers the
    non-dotted form when both exist — matches mise's documented
    discovery precedence
    (https://mise.jdx.dev/configuration.html: "Paths which start
    with `mise` can be dotfiles, e.g.: `.mise.toml`").
    """
    for name in ("mise.toml", ".mise.toml"):
        p = repo_root / name
        if p.is_file():
            return p
    return None


def _go_already_pinned(repo_root: Path) -> bool:
    """Return True if the repo already specifies a Go version mise would
    pick up — via `.go-version`, a `go` entry in `.tool-versions`, or a
    `[tools] go = "..."` in `mise.toml`/`.mise.toml`. In any of these
    cases pila should NOT synthesize an override; the existing pin wins.
    """
    if (repo_root / ".go-version").is_file():
        return True
    tv = repo_root / ".tool-versions"
    if tv.is_file():
        try:
            for line in tv.read_text(errors="replace").splitlines():
                stripped = line.strip()
                if stripped.startswith("#") or not stripped:
                    continue
                # `.tool-versions` is whitespace-separated: `tool version`.
                parts = stripped.split()
                if parts and parts[0].lower() == "go":
                    return True
        except OSError:
            pass
    mt = _existing_mise_toml_path(repo_root)
    if mt is not None:
        try:
            content = mt.read_text(errors="replace")
            # Cheap text-level check — TOML parsing here would pull in
            # tomllib but the heuristic is sufficient: any `go =` line
            # under a [tools] section indicates a pin.
            if re.search(r"(?m)^\s*go\s*=", content):
                return True
        except OSError:
            pass
    return False


# Strip leading `v` or `V` (any number) from `.nvmrc`/`.node-version`
# values; mise expects bare semver. Compiled once at module load.
_LEADING_V_RE = re.compile(r"^[vV]+")


# Idiomatic version files mise reads natively *when discovery walks
# the repo* — but NOT when `MISE_OVERRIDE_CONFIG_FILENAMES` is set
# (verified against mise discussions #6598 / #7058). When the override
# fires (because pila synthesized a go pin), every idiomatic file the
# repo committed for some OTHER language is silently dropped — workers
# end up running on the image-baked LTS instead of the pinned version.
# So when the override fires, pila scans these files and injects their
# pins into the override's `[tools]` section.
_IDIOMATIC_VERSION_FILES = (
    # (filename, mise tool key, value transformer)
    (".nvmrc", "node", lambda s: _LEADING_V_RE.sub("", s)),
    (".node-version", "node", lambda s: _LEADING_V_RE.sub("", s)),
    (".python-version", "python", lambda s: s),
    (".ruby-version", "ruby", lambda s: s),
)


# asdf-compatible names (used by `.tool-versions`) that mise treats as
# aliases for its canonical tool names. Without this map, a repo with
# `.nvmrc` (injects `node`) plus `.tool-versions` carrying
# `nodejs 20.11.0` would end up with both `node` and `nodejs` in the
# override — mise treats both as the same tool, producing ambiguous
# resolution. Normalize asdf names BEFORE checking already_pinned.
_ASDF_TOOL_ALIASES = {
    "nodejs": "node",
    "python3": "python",
}


def _existing_mise_toml_tool_keys(text: str | None) -> set[str]:
    """Return the set of tool keys pinned by a `[tools]` section in the
    given mise.toml text. Used by `synth_mise_go_override` to avoid
    re-pinning a tool the repo already wired up explicitly. Heuristic
    line-level scan — full TOML parsing would pull in tomllib and the
    set of forms we care about (top-level `[tools]` table, simple
    `<key> =` lines) is tiny.
    """
    if not text:
        return set()
    keys: set[str] = set()
    in_tools = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_tools = (stripped == "[tools]")
            continue
        if not in_tools:
            continue
        m = re.match(r"\s*([A-Za-z_][A-Za-z0-9_-]*)\s*=", line)
        if m:
            keys.add(m.group(1))
    return keys


def _read_idiomatic_pins(repo_root: Path,
                          already_pinned: set[str]) -> list[tuple[str, str]]:
    """Return [(tool, version), ...] for every idiomatic version file in
    `repo_root` whose tool is NOT already in `already_pinned`.

    Used to bridge the `MISE_OVERRIDE_CONFIG_FILENAMES` semantic: when
    the override is set, mise reads ONLY the listed files; idiomatic
    discovery is suppressed. Pila must therefore copy the pins forward
    explicitly. See DESIGN §6½ "Per-repo dependency provisioning."

    `.tool-versions` is parsed line-by-line (asdf-compatible format:
    `<tool> <version>` per line, comments with `#`).

    `rust-toolchain.toml` is intentionally out of scope — the file's
    `[toolchain] channel = "..."` shape needs more care than a regex
    sweep, and the rare repo that committed only rust-toolchain.toml
    can add a `mise.toml` or commit `.tool-versions` instead.
    """
    pins: list[tuple[str, str]] = []
    for filename, tool, transform in _IDIOMATIC_VERSION_FILES:
        if tool in already_pinned:
            continue
        path = repo_root / filename
        if not path.is_file():
            continue
        try:
            raw = path.read_text(errors="replace").strip()
        except OSError:
            continue
        if not raw:
            continue
        version = transform(raw.splitlines()[0].strip())
        if not version:
            continue
        pins.append((tool, version))
        already_pinned.add(tool)
    # .tool-versions: asdf-compatible, multiple tools per file.
    # asdf and mise sometimes disagree on tool names (asdf calls Node
    # `nodejs`, mise calls it `node`). Normalize via _ASDF_TOOL_ALIASES
    # before the dedup check so we don't pin both `node` AND `nodejs`
    # (which mise treats as the same tool — ambiguous resolution).
    tv = repo_root / ".tool-versions"
    if tv.is_file():
        try:
            content = tv.read_text(errors="replace")
        except OSError:
            content = ""
        for line in content.splitlines():
            stripped = line.split("#", 1)[0].strip()
            if not stripped:
                continue
            parts = stripped.split()
            if len(parts) < 2:
                continue
            raw_tool, version = parts[0], parts[1]
            tool = _ASDF_TOOL_ALIASES.get(raw_tool, raw_tool)
            if tool in already_pinned:
                continue
            pins.append((tool, version))
            already_pinned.add(tool)
    return pins


def synth_mise_go_override(repo_root: Path, run_dir: Path) -> Path | None:
    """If `go.mod` exists and no other Go pin is in place, write a mise
    override file that pins the Go version mise should install. Returns
    the absolute path to the override file (so the caller can export
    `MISE_OVERRIDE_CONFIG_FILENAMES` before invoking `mise install`),
    or None if no synthesis was needed.

    `MISE_OVERRIDE_CONFIG_FILENAMES` REPLACES the default config
    discovery rather than merging with it (verified against mise docs
    and discussions #4136 / #8510 / #6598 / #7058). When the override
    is set, mise reads ONLY the listed files — both `mise.toml` AND
    idiomatic files (`.nvmrc`, `.python-version`, etc.) are otherwise
    silently dropped. This helper preserves both:

      - Existing `mise.toml` content is read and the `go = "X"` pin
        is inserted into its `[tools]` section (no duplicate header).
      - Idiomatic version files in the repo root (`.nvmrc`,
        `.node-version`, `.python-version`, `.ruby-version`,
        `.tool-versions`) are read; any tool NOT already pinned in
        `mise.toml` gets its pin copied into the override's `[tools]`
        section alongside the synthesized go pin.

    Without the idiomatic-file copy, a polyglot Go+Node repo with
    `go.mod` + `.nvmrc: 20.11.0` and no `mise.toml` would silently
    install only Go; the Node version pinned in `.nvmrc` would drop
    to the image-baked LTS, defeating the runtime-version guarantee
    the entire mise layer was built to provide.

    **Known limits of the existing-mise-config scanner:**
    `_existing_mise_toml_tool_keys` reads tool pins from the canonical
    `[tools]` section with bare keys (`node = "20"`). Two valid TOML
    forms are NOT detected — when present, pila will re-inject pins
    from idiomatic files alongside the existing ones, producing
    duplicate keys that mise rejects mid-run:

      - Inline-table form: `tools = { node = "20.11.0" }`
      - Quoted keys: `[tools]\n"node" = "20.11.0"`

    Both are rare in practice (the canonical form is what mise's
    docs and `mise use` write). Repos that hit these can switch to
    the canonical form. A proper fix would need `tomllib` (3.11+
    stdlib) — pila's minimum is 3.10 — or a hand-written inline-
    table parser; neither is justified by current usage.

    See DESIGN §6½ and IMPLEMENTATION §6½ step 3.
    """
    gomod = repo_root / "go.mod"
    if not gomod.is_file():
        return None
    if _go_already_pinned(repo_root):
        return None
    try:
        text = gomod.read_text(errors="replace")
    except OSError:
        return None
    m = _GO_MOD_VERSION_RE.search(text)
    if not m:
        return None
    version = m.group(1)

    run_dir.mkdir(parents=True, exist_ok=True)
    override_path = run_dir / "mise-overrides.toml"

    header_comment = (
        "# Synthesized by pila from go.mod (DESIGN §6½).\n"
        "# mise's go plugin does not parse go.mod itself; idiomatic\n"
        "# version files (.nvmrc, .python-version, etc.) are copied in\n"
        "# because MISE_OVERRIDE_CONFIG_FILENAMES suppresses discovery.\n"
    )

    existing = _existing_mise_toml_path(repo_root)
    existing_text: str | None = None
    if existing is not None:
        try:
            existing_text = existing.read_text(errors="replace")
        except OSError:
            existing_text = None

    # Build the full set of new pins (go + every idiomatic-file tool
    # that the existing mise.toml doesn't already pin).
    already_pinned = _existing_mise_toml_tool_keys(existing_text)
    already_pinned.add("go")  # we're adding it ourselves below
    idiomatic_pins = _read_idiomatic_pins(repo_root, already_pinned)
    new_pin_lines = [f'go = "{version}"'] + [
        f'{tool} = "{ver}"' for tool, ver in idiomatic_pins
    ]

    if existing_text is None:
        # No existing mise.toml — emit a minimal override carrying just
        # the new pins.
        body = "[tools]\n" + "\n".join(new_pin_lines) + "\n"
        override_path.write_text(header_comment + body)
        return override_path

    # Insert new pin lines into the existing [tools] section if one
    # exists; otherwise append a fresh [tools] section. We avoid
    # emitting a duplicate [tools] header (TOML 1.0 §6.5 — "Defining a
    # table more than once is invalid").
    lines = existing_text.rstrip("\n").split("\n")
    out_lines: list[str] = []
    inserted = False
    in_tools = False
    for line in lines:
        out_lines.append(line)
        stripped = line.strip()
        # Detect entering the [tools] table. A subsequent table header
        # (`[other]`) exits it. Subtables (`[tools.something]`) are also
        # valid TOML but are out of scope for this synthesis — pila only
        # cares about adding scalar keys to the top-level [tools].
        if not in_tools and stripped == "[tools]":
            in_tools = True
            # Insert immediately after the header, before any existing
            # keys. This is the safe minimal change.
            out_lines.extend(new_pin_lines)
            inserted = True
            in_tools = False  # done — keys after are preserved as-is
            continue
    if not inserted:
        # No [tools] section in the existing file — append one.
        if out_lines and out_lines[-1].strip() != "":
            out_lines.append("")
        out_lines.append("[tools]")
        out_lines.extend(new_pin_lines)

    override_path.write_text(
        header_comment + "\n".join(out_lines) + "\n")
    return override_path


# Filenames that signal "this repo pins a runtime version mise should
# install." Used by `_repo_has_version_signal` to decide whether to
# invoke `mise install` at all — an unversioned repo runs on the
# image-baked LTS without bothering mise.
_MISE_SIGNAL_FILES = (
    "mise.toml", ".mise.toml",
    ".tool-versions",
    ".nvmrc", ".node-version",
    ".python-version",
    ".ruby-version",
    "rust-toolchain.toml",
    ".go-version",
)


def _repo_has_version_signal(repo_root: Path,
                              override_file: Path | None) -> bool:
    """Return True if the repo declares any runtime version pin mise
    can act on, OR if pila already synthesized an override file. False
    means there's nothing for `mise install` to do; the LTS fallback
    in the image is the right answer."""
    if override_file is not None:
        return True
    for name in _MISE_SIGNAL_FILES:
        if (repo_root / name).is_file():
            return True
    return False


async def run_mise_install(repo_root: Path, log_dir: Path,
                            st: "State",
                            override_file: Path | None = None) -> None:
    """Invoke `mise install` at the repo root. If `override_file` is
    provided, exports `MISE_OVERRIDE_CONFIG_FILENAMES` so mise reads
    pila's synthesized config instead of the default discovery walk.

    Captures resolved versions via `mise ls --current --json` and stores
    the raw blob at `st.data["provision"]["mise_versions"]` — callers can
    reduce `tools[name][0].version` for display.

    Failures propagate to `die()` with the failing tool/version and the
    last 40 lines of mise output.

    No-signals short-circuit: if the repo has zero version pins (no
    `mise.toml`, `.tool-versions`, idiomatic file, or pila-synthesized
    override), this function is a logged no-op. The image-baked LTS
    Node and Python on PATH then become the workers' runtime. Without
    this guard, mise's exact behavior for `mise install` with no
    declared tools is implementation-dependent and could `die()` the
    whole run with a confusing "no tools to install" message — exactly
    the case the LTS-fallback story was supposed to handle smoothly.
    """
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "provision.log"

    if not _repo_has_version_signal(repo_root, override_file):
        log("  no version pins detected — workers run on image-baked LTS")
        prov = st.data.setdefault("provision", {})
        prov["mise_versions"] = {}
        st.save()
        return

    env = os.environ.copy()
    if override_file is not None:
        env["MISE_OVERRIDE_CONFIG_FILENAMES"] = str(override_file)

    # `mise install` with no tool args reads the active config and
    # installs every declared tool. We let the resolver figure out the
    # set from the repo's .tool-versions / .nvmrc / .python-version /
    # rust-toolchain.toml / .go-version (the last either committed or
    # synthesized by synth_mise_go_override).
    #
    # Stream output: a first-run install of Python 3.12 / Ruby 3.2 /
    # Rust can take minutes; without streaming the user sees a silent
    # `mise install` line and nothing else until it finishes (or hits
    # whatever container-level wall-clock the user gives up at).
    verbosity = st.data.get("verbosity", VERBOSITY_DEFAULT)
    try:
        rc, tail = await run_streaming(
            ["mise", "install"],
            cwd=str(repo_root),
            env=env,
            log_path=log_path,
            label="mise install",
            verbosity=verbosity,
        )
    except subprocess.TimeoutExpired as exc:
        die(f"mise install timed out\n(see {log_path})\n{exc.output or ''}")
    if rc != 0:
        die(f"mise install failed (exit {rc})\n(see {log_path})\n{tail}")

    # Capture resolved versions. `mise ls --current --json` is the
    # documented machine-readable view; `mise current --json` does NOT
    # exist (verified against mise.usage.kdl).
    proc = await asyncio.create_subprocess_exec(
        "mise", "ls", "--current", "--json",
        cwd=str(repo_root),
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        # Not fatal — workers run their own install commands via prompt
        # injection (DESIGN §6½ "Worker-driven install"); they don't
        # need the resolved-versions blob to do so. Log and move on.
        log(f"mise ls --current --json failed (exit {proc.returncode}); "
            "skipping version capture")
        return
    try:
        versions = json.loads(stdout.decode(errors="replace") if stdout else "{}")
    except (ValueError, TypeError):
        versions = {}
    prov = st.data.setdefault("provision", {})
    prov["mise_versions"] = versions
    st.save()


# =========================================================================
# phases
# =========================================================================
async def phase_classify(task: str, st: State, caps: dict, clarify: bool,
                         models: dict[str, str],
                         efforts: dict[str, str | None]) -> dict:
    """Phase 1 (classify), which also produces the Phase 0 clarification
    questions: classify the task and surface only genuinely underivable
    (intent-level) questions."""
    log("phase 1: classifying task")
    st.data["current_phase"] = "phase 1: classify"
    st.save()
    sys_prompt = load_prompt("classifier")
    st.bump_workers(caps)
    result = await claude_p(
        user_prompt=f"TASK:\n{task}\n\nClassify it and apply the clarification filter.",
        system_prompt=sys_prompt, schema_key="classifier", cwd=os.getcwd(),
        allowed_tools=INSPECT_TOOLS, max_turns=60, autonomous=False,
        caps=caps, st=st, model=models["classifier"],
        effort=efforts["classifier"], sid="classifier",
        add_dirs=st.data.get("inspect_dirs") or None,
    )
    cats = [c for c in result.get("categories", []) if c in CATEGORIES]
    if not cats:
        die("classifier returned no recognized categories")
    questions = result.get("questions", []) if clarify else []
    st.data["categories"] = cats
    st.data["classifier_questions"] = questions
    st.data["needs_source_of_truth"] = bool(result.get("source_of_truth_question"))
    st.save()
    log(f"categories: {', '.join(cats)}")
    return result


def gather_answers(st: State, supplied: dict | None) -> dict:
    """Collect clarification answers — from --answers, from the resolved
    source-of-truth preference, from a TTY prompt, or (no TTY, no answers)
    defer by writing pending-questions.json and exiting."""
    questions = st.data.get("classifier_questions", [])
    need_sot = st.data.get("needs_source_of_truth", False)
    sot_pref = st.data.get("source_of_truth_pref", "both")
    answers: dict = dict(supplied or {})

    provided_sot = answers.get("source_of_truth")
    if provided_sot is not None and provided_sot not in SOURCE_OF_TRUTH_VALUES:
        die(f"source_of_truth={provided_sot!r} is not one of "
            f"{SOURCE_OF_TRUTH_VALUES}")

    # Satisfy source_of_truth non-interactively from the resolved
    # preference (DESIGN §11). The preference always holds a real value
    # — `codebase`, `research`, or `both` (default) — so this never
    # blocks for an interactive answer.
    if need_sot and "source_of_truth" not in answers:
        answers["source_of_truth"] = sot_pref

    pending = [q for q in questions if q.get("id") not in answers]

    if not pending:
        st.data["answers"] = answers
        st.save()
        return answers

    if not sys.stdin.isatty():
        # launched non-interactively (e.g. via the plugin skill): defer.
        pila_dir = st.path.parent
        (pila_dir / "pending-questions.json").write_text(json.dumps({
            "questions": pending,
        }, indent=2))
        log("clarification needed; wrote .pila/pending-questions.json")
        sys.exit(EXIT_NEEDS_ANSWERS)

    for q in pending:
        print(f"\n? {q['question']}")
        if q.get("why_underivable"):
            print(f"  (underivable: {q['why_underivable']})")
        answers[q["id"]] = input("  > ").strip()

    st.data["answers"] = answers
    st.save()
    return answers


def absorb_supplied_answers(args, st: State, pila_dir: Path) -> None:
    """Merge --answers FILE into st.data['answers'] and propagate the
    update to existing subtask spec files. Safe to call on both initial
    runs and on --resume; a no-op when --answers is not set.

    The reason this is its own helper, separate from `gather_answers`,
    is that the latter runs the classifier-question collection flow
    (asking the user / writing pending-questions.json / exiting non-zero)
    which is appropriate on the initial run but not on resume. On
    resume we just want the merge half — the user has already produced
    an answers file in response to a prior EXIT_NEEDS_ANSWERS exit
    (either pending-questions.json from gather_answers, or
    pending-clarifications.json from surface_clarification), and the
    job here is to get those answers into state and onto disk so the
    next worker invocation sees them.

    The subtask-spec rewrite mirrors pila.py around the
    needs-clarification branch of settle_subtask: every existing spec
    file gets its `_clarification_answers` field overwritten with the
    current st.data['answers']. This is intentionally aggressive — a
    subtask that doesn't read the new keys ignores them; a subtask
    that does, sees them on its next invocation."""
    if not args.answers:
        return
    supplied_path = Path(args.answers)
    if not supplied_path.exists():
        die(f"--answers file does not exist: {args.answers}")
    try:
        supplied = json.loads(supplied_path.read_text())
    except json.JSONDecodeError as e:
        die(f"--answers file is not valid JSON: {args.answers}: {e}")
    if not isinstance(supplied, dict):
        die(f"--answers file must contain a JSON object, got "
            f"{type(supplied).__name__}")

    # Validate source_of_truth if present — same validation gate as
    # gather_answers uses, so a bad value fails at startup not mid-run.
    provided_sot = supplied.get("source_of_truth")
    if provided_sot is not None and provided_sot not in SOURCE_OF_TRUTH_VALUES:
        die(f"source_of_truth={provided_sot!r} is not one of "
            f"{SOURCE_OF_TRUTH_VALUES}")

    answers = st.data.setdefault("answers", {})
    # Supplied keys override anything already in state — a re-run with
    # an answer to a previously-deferred question is the whole point.
    answers.update(supplied)
    st.data["answers"] = answers
    st.save()

    # Propagate the new answers to every existing subtask spec file so
    # implementers spawned (or re-spawned) after this point see them in
    # their `_clarification_answers`. Specs are written once at
    # phase_plan time with the then-current answers; later answers must
    # be flushed through.
    sub_dir = pila_dir / "subtasks"
    if sub_dir.exists():
        for spec_path in sub_dir.glob("*.json"):
            try:
                spec = json.loads(spec_path.read_text())
            except json.JSONDecodeError:
                continue  # corrupted spec; let the implementer surface it
            spec["_clarification_answers"] = answers
            spec_path.write_text(json.dumps(spec, indent=2))


def surface_clarification(sid: str, question: dict, checkpoint_path: str,
                          st: State) -> bool:
    """Surface a mid-execution clarification question to the user
    (DESIGN §11). Mirrors `gather_answers`'s TTY-vs-non-TTY split:

      - Interactive (TTY): prompt right here, store the answer in
        st.data['answers'][question.id], and return True so the caller
        re-spawns the implementer as a CONTINUATION.
      - Non-interactive: write .pila/pending-clarifications.json
        with the question, the subtask id, and the checkpoint path,
        then sys.exit(EXIT_NEEDS_ANSWERS) so the calling layer can
        collect the answer and resume.

    Returning True signals "answer captured, re-spawn the worker."
    Non-interactive callers never reach the return — sys.exit fires
    first. The caller is responsible for bumping the
    subtask_continuations counter before treating this as the
    continuation step."""
    pila_dir = st.path.parent
    answers = st.data.setdefault("answers", {})

    if not sys.stdin.isatty():
        # Persist enough state for the surrounding layer to resume.
        # The question id keys the answer; the checkpoint path is
        # what the re-spawned worker will read.
        (pila_dir / "pending-clarifications.json").write_text(
            json.dumps({
                "subtask_id": sid,
                "question": question,
                "checkpoint_path": checkpoint_path,
            }, indent=2))
        log(f"  {sid}: clarification needed; wrote "
            ".pila/pending-clarifications.json")
        # Save state so the answer the user supplies on the re-run
        # lands in a state.json that already knows about this subtask's
        # progress so far.
        st.save()
        sys.exit(EXIT_NEEDS_ANSWERS)

    qid = question["id"]
    print(f"\n? [{sid}] {question['question']}")
    print(f"  (underivable: {question.get('why_underivable', '')})")
    answers[qid] = input("  > ").strip()
    st.data["answers"] = answers
    st.save()
    return True


def _format_provision_user_prompt(fixtures: dict, task: str) -> str:
    """Compose the LLM-fallback user prompt from the assembled fixture
    set. Mirrors the layout the worker prompt expects."""
    parts: list[str] = [
        f"TASK CONTEXT:\n{task}",
        "",
        "Below are the repo signals you have to decide how to install "
        "its dependencies. Emit a recipe that uses the project's own "
        "documented commands. Reject any command outside the allowlist."
        " Emit `kind: none` if no install is needed (pure docs repo).",
        "",
    ]
    if fixtures["readme"]:
        parts += ["=== README (install-relevant slice) ===",
                  fixtures["readme"], ""]
    for name, text in fixtures["manifests"].items():
        parts += [f"=== {name} ===", text, ""]
    for rel, text in fixtures["workspace_manifests"]:
        parts += [f"=== {rel} (workspace child) ===", text, ""]
    for name, text in fixtures["workflows"]:
        parts += [f"=== .github/workflows/{name} ===", text, ""]
    if fixtures["contributing"]:
        parts += ["=== CONTRIBUTING / DEVELOPMENT docs ===",
                  fixtures["contributing"], ""]
    if fixtures["hit_ceiling"]:
        parts.append(
            "[fixture set was truncated to the 24KB budget — some "
            "files may be incomplete]")
    return "\n".join(parts)


async def phase_provision(repo_root: Path, st: State, caps: dict,
                           models: dict[str, str],
                           efforts: dict[str, str | None]) -> None:
    """Phase 1½: per-repo dependency *detection*.

    Runs after classify so a docs-only run can short-circuit. Five
    ordered steps (DESIGN §6½):

      1. Docs-only short-circuit. If classify returned only
         documentation categories, persist `kind: none` and return.
      2. `.pila-setup.sh` hook if present.
      3. Synthesize a mise go-override from `go.mod` if needed.
      4. `mise install` at the repo root (reads .tool-versions natively
         and .nvmrc / .python-version / .ruby-version /
         rust-toolchain.toml via the image-set
         MISE_IDIOMATIC_VERSION_FILE_ENABLE_TOOLS env). Capture
         resolved versions via `mise ls --current --json`.
      5. Detect install commands: deterministic lockfile table first
         (emits all matches for polyglot repos), LLM fallback if the
         table abstains. Validate the recipe and persist it to
         st.data["provision"]["recipe"] for downstream workers to
         consult via prompt injection.

    Phase 1½ deliberately does NOT execute the install recipe at
    repo_root. The repo is bind-mounted from the host; writing
    `node_modules/` / `.venv/` / `target/` into it would clobber the
    host's checkout with linux-built artifacts on darwin hosts. Each
    worker runs installs in its own worktree against the shared
    cache instead (DESIGN §6½ "Worker-driven install").

    Naturally skipped on `--resume` because the entire fresh-run
    else-branch in `orchestrate()` is skipped.
    """
    log("phase 1½: detecting per-repo deps")
    st.data["current_phase"] = "phase 1½: provision"
    st.save()
    prov = st.data.setdefault("provision", {})
    log_dir = st.run_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    # 1. Docs-only short-circuit.
    cats = set(st.data.get("categories") or [])
    if cats and cats <= _DOCS_ONLY_CATEGORIES:
        log("  docs-only task: skipping dep detection")
        prov["source"] = "skipped-docs-only"
        prov["recipe"] = [{"kind": "none", "command": [],
                           "working_dir": ".", "timeout_s": 0}]
        st.save()
        return

    # 2. Setup hook.
    await run_setup_hook(repo_root, log_dir, st)

    # 3. Synthesize a mise go override if go.mod lacks a sibling pin.
    override = synth_mise_go_override(repo_root, st.run_dir)
    if override is not None:
        log(f"  synthesized mise override at {override.name} "
            f"(go.mod → .go-version equivalent)")
    # Persist so a `--resume` after this point can re-export the env
    # var (orchestrate() re-reads provision state on resume).
    prov["override_file"] = str(override) if override is not None else None
    st.save()
    # Export MISE_OVERRIDE_CONFIG_FILENAMES into the orchestrator's
    # os.environ now so every downstream subprocess — `mise install`
    # below, the implementer/conformer `claude -p` workers (which
    # inherit os.environ via _invoke), and any `mise exec --` they
    # invoke from their worktrees — sees the synthesized go pin.
    # Without this, mise's discovery in the worktree wouldn't find the
    # synth (the override file lives under .pila/, which isn't in the
    # worktree's tracked-file set) and `mise exec -- go ...` would
    # fall through to system PATH where Go isn't installed.
    if override is not None:
        os.environ["MISE_OVERRIDE_CONFIG_FILENAMES"] = str(override)

    # 4. mise install + version capture.
    await run_mise_install(repo_root, log_dir, st, override_file=override)
    versions = prov.get("mise_versions") or {}
    if versions:
        version_summary = ", ".join(
            f"{tool} {entries[0].get('version', '?')}"
            for tool, entries in sorted(versions.items())
            if isinstance(entries, list) and entries
        )
        if version_summary:
            log(f"  resolved versions: {version_summary}")

    # 5a. Detect install commands — table first.
    recipe = detect_recipe_from_lockfiles(repo_root)
    if recipe:
        prov["source"] = "table"
        log(f"  table emitted {len(recipe)} install command(s)")
    else:
        # 5b. LLM fallback.
        log("  table abstained — invoking provision worker")
        fixtures = gather_provision_fixtures(repo_root)
        sys_prompt = load_prompt("provision")
        user_prompt = _format_provision_user_prompt(fixtures, st.data["task"])
        st.bump_workers(caps)
        result = await claude_p(
            user_prompt=user_prompt,
            system_prompt=sys_prompt,
            schema_key="provision",
            cwd=str(repo_root),
            allowed_tools=INSPECT_TOOLS,
            max_turns=30,
            autonomous=False,
            caps=caps, st=st,
            model=models.get("provision", MODEL_DEFAULT),
            effort=efforts.get("provision"),
            sid="provision",
            add_dirs=st.data.get("inspect_dirs") or None,
        )
        recipe = result.get("recipe") or []
        prov["source"] = "llm"

    # Validate the recipe shape. §12 carve-out: any drift from the
    # schema or allowlist is rejected here, not in the prompt — even
    # though we no longer execute the recipe ourselves, workers will
    # see it via prompt injection and we don't want to ship them
    # malformed entries.
    try:
        validate_provision_recipe(recipe)
    except ValueError as e:
        die(f"provision recipe failed validation: {e}")

    prov["recipe"] = recipe
    st.save()

    log(f"  recipe detected ({len(recipe)} command(s), "
        f"source={prov['source']}) — workers will run installs in their worktrees")


async def phase_plan(task: str, st: State, caps: dict,
                     models: dict[str, str],
                     efforts: dict[str, str | None]) -> list[dict]:
    """Phase 2: one planner per category, run in parallel (bounded by
    max_parallel). Each returns a JSON plan of granular subtasks."""
    log("phase 2: planning")
    st.data["current_phase"] = "phase 2: planning"
    st.save()
    cats = st.data["categories"]
    answers = st.data.get("answers", {})
    sot = answers.get("source_of_truth", "codebase")
    sys_prompt = load_prompt("planner")
    # confidence_rounds is the worker-internal evidence-gate bound (DESIGN
    # §8 planner gate). The orchestrator does not enforce it — the planner
    # bounds itself — but passing it in the context blob is what makes the
    # user-visible knob real.
    ctx = json.dumps({"task": task, "source_of_truth": sot,
                      "clarification_answers": answers,
                      "confidence_rounds": caps["confidence_rounds"]},
                     indent=2)

    sem = asyncio.Semaphore(caps["max_parallel"])

    async def plan_one(category: str) -> dict:
        async with sem:
            st.bump_workers(caps)
            prefix = f"{CATEGORY_ABBREV[category]}-"
            up = (f"DOMAIN: {category}\nID_PREFIX: {prefix}\n\n"
                  f"CONTEXT:\n{ctx}\n\n"
                  f"Decompose the {category} aspect of this task into a JSON plan "
                  "per your instructions. Every subtask id MUST start with "
                  f"`{prefix}` (e.g., `{prefix}001`).")
            return await claude_p(user_prompt=up, system_prompt=sys_prompt,
                                  schema_key="planner", cwd=os.getcwd(),
                                  allowed_tools=INSPECT_TOOLS, max_turns=100,
                                  autonomous=False, caps=caps, st=st,
                                  model=models["planner"],
                                  effort=efforts["planner"],
                                  sid=f"planner-{category}",
                                  add_dirs=st.data.get("inspect_dirs") or None)

    plans = await gather_or_cancel(*(plan_one(c) for c in cats))
    for category, plan in zip(cats, plans):
        n = len(plan.get("subtasks", []))
        status = plan.get("status", "ready")
        if status == "blocked":
            gap = (plan.get("confidence", {}) or {}).get("gap_to_close", {})
            log(f"  {category}: BLOCKED (planner gate) — {n} subtask(s); "
                f"gap: {gap}")
        else:
            log(f"  {category}: {n} subtask(s)")
    return list(plans)


def _promote_external_collisions(plans: list[dict]) -> int:
    """In-place: for every `requires` entry with `extent: external` whose
    tag is in some plan's `provides`, rewrite the entry to `extent:
    in_plan`. The in-plan producer wins so a planner cannot unilaterally
    bypass a real producer in another domain — DESIGN §5 `requires.extent`
    collision rule.

    Returns the count of promoted entries (for logging). Mutates the
    plans list in place; `reason` is preserved on the promoted entry
    for telemetry but is no longer load-bearing once `extent` is
    `in_plan`."""
    all_provides: set[str] = set()
    for plan in plans:
        for s in plan.get("subtasks", []):
            all_provides.update(s.get("provides", []))
    promoted = 0
    for plan in plans:
        for s in plan.get("subtasks", []):
            for entry in s.get("requires", []):
                if (isinstance(entry, dict)
                        and entry.get("extent") == "external"
                        and entry.get("tag") in all_provides):
                    entry["extent"] = "in_plan"
                    promoted += 1
    return promoted


def _collect_external_preconditions(plans: list[dict]) -> list[dict]:
    """Walk plans and return the deduped list of planner-declared
    `extent: external` requires entries — the `preconditions` surface
    persisted in `plan.json` (DESIGN §5 `requires.extent`).

    Run AFTER `_promote_external_collisions` so any entry that had an
    in-plan producer has already been demoted out of the external set.
    Each output entry is `{tag, reasons: [{sid, reason}, …],
    originating_subtasks: [sid, …]}`, deduped by tag and stable-sorted
    for deterministic output."""
    by_tag: dict[str, dict] = {}
    for plan in plans:
        for s in plan.get("subtasks", []):
            sid = s.get("id", "")
            for entry in s.get("requires", []):
                if (not isinstance(entry, dict)
                        or entry.get("extent") != "external"):
                    continue
                tag = entry.get("tag", "")
                reason = (entry.get("reason") or "").strip()
                if not tag:
                    continue
                bucket = by_tag.setdefault(tag, {
                    "tag": tag,
                    "reasons": [],
                    "originating_subtasks": [],
                })
                if sid not in bucket["originating_subtasks"]:
                    bucket["originating_subtasks"].append(sid)
                bucket["reasons"].append({"sid": sid, "reason": reason})
    return [by_tag[t] for t in sorted(by_tag)]


def _compute_unresolved_requires(plans: list[dict]) -> list[dict]:
    """Pure-Python lookup: every (sid, tag, domain) where a subtask
    `requires` a capability tag that no subtask in the merged plan
    `provides`. Mirrors the set logic in validate_plan() but emits the
    data rather than raising. Used by phase_reconcile to assemble the
    reconciler worker's input and (after the worker applies its
    resolutions) to verify the output actually closed every gap.

    Only `extent: in_plan` entries are checked — `extent: external` is
    a planner-declared out-of-graph prerequisite (DESIGN §5
    `requires.extent`) and is collected separately by
    `_collect_external_preconditions`. Caller is expected to have run
    `_promote_external_collisions` first so any external entry with an
    in-plan producer has already been demoted.

    `domain` names the producing planner-domain of `sid` — surfaced in
    the abort message so the user can see which planner held the
    dangling dependency. Reconciler input is read-or-ignore on the
    field; it's there for the orchestrator's own rendering."""
    all_provides: set[str] = set()
    sid_domain: dict[str, str] = {}
    for plan in plans:
        for s in plan.get("subtasks", []):
            all_provides.update(s.get("provides", []))
            sid_domain[s["id"]] = plan.get("domain", "<unknown>")
    unresolved: list[dict] = []
    for plan in plans:
        for s in plan.get("subtasks", []):
            for entry in s.get("requires", []):
                if not isinstance(entry, dict):
                    continue
                if entry.get("extent") != "in_plan":
                    continue
                tag = entry.get("tag", "")
                if tag and tag not in all_provides:
                    unresolved.append({
                        "sid": s["id"], "tag": tag,
                        "domain": sid_domain[s["id"]],
                    })
    return unresolved


def _apply_reconciler_output(plans: list[dict], output: dict) -> list[dict]:
    """Mutate `plans` per the reconciler's output. On success, returns
    the same `plans` list (with in-place edits on existing subtasks
    plus an appended `_reconciler` pseudo-plan for any added_subtasks).
    On an id-collision in `added_subtasks` (either with an existing
    subtask or within added_subtasks itself), calls `die()` — the
    pseudo-plan is never appended and `plans` is left unmutated.

    Renames rewrite a single `requires` entry on the named subtask.
    Added_provides append a tag to the named subtask's `provides`.
    Added_subtasks become a new domain="_reconciler" plan appended to
    the list — schedule() flattens by id, so domain only affects the
    per-domain log line. Each added subtask carries
    `_added_by_reconciler: true` for downstream traceability.

    The `unresolvable` array is not consumed here — phase_reconcile()
    inspects it directly before calling this helper."""
    # Index subtasks by id for O(1) mutation. Modifying the subtask
    # dict mutates the underlying plan because dicts are shared by
    # reference; no need to write the plan back.
    by_id: dict[str, dict] = {}
    for plan in plans:
        for s in plan.get("subtasks", []):
            by_id[s["id"]] = s

    for r in output.get("renames", []):
        s = by_id.get(r["sid"])
        if s is None:
            continue  # reconciler named a sid that doesn't exist; ignore
        # `requires` entries are objects `{tag, extent, reason?}`
        # (DESIGN §5 `requires.extent`); rewrite the `tag` field on the
        # entry whose tag matches `from`, preserve extent/reason. The
        # `extent: in_plan` guard makes the architectural invariant
        # load-bearing: the reconciler only ever reasons about in_plan
        # tags (externals are filtered out before its input is built),
        # so a rename must not mutate an external entry even if its tag
        # happens to collide.
        for entry in s.get("requires", []) or []:
            if (isinstance(entry, dict)
                    and entry.get("extent") == "in_plan"
                    and entry.get("tag") == r["from"]):
                entry["tag"] = r["to"]

    for ap in output.get("added_provides", []):
        s = by_id.get(ap["sid"])
        if s is None:
            continue
        provs = s.setdefault("provides", [])
        if ap["tag"] not in provs:
            provs.append(ap["tag"])

    added = output.get("added_subtasks", [])
    if added:
        # Fail loud on id collisions. schedule() merges all subtasks
        # into a single dict keyed by id (pila.py: see `schedule`),
        # so a duplicate id would silently overwrite a real subtask and
        # vanish its requires/provides/depends_on from the DAG. The
        # reconciler's prompt warns against this, but prompts are
        # advisory per CLAUDE.md "The central principle" — the
        # mechanical guarantee lives here. Two failure modes to cover:
        #   1. existing-vs-added: an added_subtask id collides with a
        #      subtask the planners already produced.
        #   2. added-vs-added: the reconciler emitted the same id twice
        #      within added_subtasks itself. Both halves get silently
        #      collapsed by schedule()'s dict-flatten if not caught here.
        existing_ids = {s["id"] for s in by_id.values()}
        ext_collisions = sorted({s["id"] for s in added if s["id"] in existing_ids})
        seen: set[str] = set()
        self_collisions: list[str] = []
        for s in added:
            sid = s["id"]
            if sid in seen and sid not in self_collisions:
                self_collisions.append(sid)
            seen.add(sid)
        if ext_collisions or self_collisions:
            parts = []
            if ext_collisions:
                parts.append("collide with existing subtasks: "
                             + ", ".join(ext_collisions))
            if self_collisions:
                parts.append("are duplicated within added_subtasks: "
                             + ", ".join(sorted(self_collisions)))
            die(
                "reconciler proposed added_subtasks whose id(s) "
                + "; ".join(parts)
                + ". The scheduler merges by id; an unchecked collision "
                "would silently drop one of the subtasks from the DAG. "
                "Refine the task or re-run."
            )
        plans.append({
            "domain": "_reconciler",
            "status": "ready",
            "subtasks": added,
        })

    return plans


async def phase_reconcile(plans: list[dict], task: str, st: State,
                          caps: dict, models: dict[str, str],
                          efforts: dict[str, str | None]) -> list[dict]:
    """Phase 2½: reconcile cross-domain capability-tag drift between
    parallel planners (DESIGN §5, §14). Short-circuits when planners
    agreed; otherwise runs one reconciler worker whose output is applied
    mechanically. Genuinely unresolvable gaps die.

    Returns the (possibly mutated) `plans` list, ready for `schedule()`."""
    # Pre-condition: subtask ids are globally unique across plans. The
    # planner prompt tells each domain to scope ids to itself with a
    # domain-prefix, and the 8 CATEGORIES map to distinct prefixes
    # (pila.py: CATEGORIES / _ID_PREFIXES), so in practice this
    # invariant holds. But prompts are advisory per CLAUDE.md; if a
    # planner ignores the rule, schedule()'s dict-flatten (line ~2997:
    # `subtasks[s["id"]] = s`) would silently overwrite, vanishing the
    # loser's requires/provides/depends_on from the DAG — the same
    # silent-data-loss failure class as the reconciler-output collisions
    # caught downstream. Catch it here, before any reconciler mutation
    # and before the short-circuit (a collision that doesn't manifest as
    # an unresolved `requires` would otherwise slip through).
    id_owners: dict[str, list[str]] = {}
    for plan in plans:
        domain = plan.get("domain", "<unknown>")
        for s in plan.get("subtasks", []):
            id_owners.setdefault(s["id"], []).append(domain)
    cross_collisions = {sid: owners for sid, owners in id_owners.items()
                        if len(owners) > 1}
    if cross_collisions:
        bullets = "\n".join(
            f"  • {sid!r} emitted by: {', '.join(owners)}"
            for sid, owners in sorted(cross_collisions.items())
        )
        die(
            "planner-vs-planner subtask id collision(s):\n"
            f"{bullets}\n"
            "Planners must emit globally unique subtask ids — by "
            "convention, each domain prefixes its ids with the domain "
            "(feat-, test-, bugfix-, …). schedule()'s by-id merge "
            "would otherwise silently drop one of the subtasks from "
            "the DAG. Refine the task or re-run."
        )

    # Apply the DESIGN §5 `requires.extent` mechanical passes BEFORE
    # computing the unresolved set:
    #   1. Promote `external` entries whose tag is in some plan's
    #      `provides` to `in_plan` — the real producer wins.
    #   2. Collect remaining `external` entries into the preconditions
    #      list, persisted via st so write_plan can surface it in
    #      plan.json. Externals never enter the reconciler's queue.
    promoted = _promote_external_collisions(plans)
    if promoted:
        log(f"phase 2½: promoted {promoted} external requires entry/entries "
            "to in_plan (an in-plan provider exists)")
    preconditions = _collect_external_preconditions(plans)
    st.data["external_preconditions"] = preconditions
    st.save()
    if preconditions:
        log(f"phase 2½: collected {len(preconditions)} external precondition(s) "
            "(planner-declared out-of-graph requirements — will surface in "
            "plan.json's `preconditions` section)")

    unresolved = _compute_unresolved_requires(plans)
    if not unresolved:
        # Common-case short-circuit: every `requires` already has a
        # producer. No worker call needed.
        return plans

    log(f"phase 2½: reconciling {len(unresolved)} cross-domain "
        f"capability-tag mismatch(es)")
    st.data["current_phase"] = "phase 2½: reconcile"
    st.save()

    # Build the reconciler's input. The worker sees the task, the
    # categories that contributed subtasks, every subtask's id/title/
    # intent/provides/requires (omit other fields to keep context small),
    # and the precomputed unresolved set.
    #
    # `requires` is flattened to bare tag strings here, dropping any
    # `extent: external` entries entirely. The reconciler reasons
    # purely about graph edges (DESIGN §5); externals are out-of-graph
    # by planner declaration and surface via `preconditions` in
    # plan.json, not through the reconciler. Keeping the view simple
    # also matches the worked example in prompts/reconciler.md (bare
    # strings).
    categories: list[str] = []
    subtask_views: list[dict] = []
    for plan in plans:
        domain = plan.get("domain")
        if domain and domain not in categories and domain != "_reconciler":
            categories.append(domain)
        for s in plan.get("subtasks", []):
            in_plan_tags = [
                e.get("tag", "") for e in (s.get("requires") or [])
                if isinstance(e, dict) and e.get("extent") == "in_plan"
                and e.get("tag")
            ]
            subtask_views.append({
                "id": s.get("id", ""),
                "title": s.get("title", ""),
                "intent": s.get("intent", ""),
                "provides": list(s.get("provides", []) or []),
                "requires": in_plan_tags,
            })
    payload = {
        "task": task,
        "categories": categories,
        "subtasks": subtask_views,
        "unresolved_requires": unresolved,
    }

    sys_prompt = load_prompt("reconciler")
    user_prompt = (
        "RECONCILER INPUT:\n" + json.dumps(payload, indent=2) +
        "\n\nResolve every unresolved_requires entry per your "
        "instructions and emit the four-array JSON output."
    )

    st.bump_workers(caps)
    output = await claude_p(
        user_prompt=user_prompt, system_prompt=sys_prompt,
        schema_key="reconciler", cwd=os.getcwd(),
        allowed_tools=INSPECT_TOOLS, max_turns=30,
        autonomous=False, caps=caps, st=st,
        model=models["reconciler"], effort=efforts["reconciler"],
        sid="reconciler",
        add_dirs=st.data.get("inspect_dirs") or None,
    )

    # Fail closed on unresolvable BEFORE mutating anything — the user
    # gets the worker's diagnosis without phantom mutations on disk.
    unresolvable = output.get("unresolvable", []) or []
    if unresolvable:
        # Reconciler output is {sid, tag, reason} — no domain field —
        # so the orchestrator joins the producing planner-domain back
        # in from the pre-reconcile unresolved list for rendering.
        sid_domain = {u["sid"]: u["domain"] for u in unresolved}
        bullets = "\n".join(
            f"  • {sid_domain.get(u['sid'], '<unknown>')}/{u['sid']} "
            f"requires '{u['tag']}': {u['reason']}"
            for u in unresolvable
        )
        die(
            f"reconciler could not resolve {len(unresolvable)} "
            f"capability-tag dependency/dependencies:\n{bullets}\n"
            "Each dependency is a planner-coverage gap: the consuming "
            "planner-domain emitted `requires` for a capability no "
            "other planner's domain produced. A common cause is a "
            "scope disagreement — two planners reading the task "
            "differently. To unblock:\n"
            "  • Refine the task description to make the disputed "
            "scope explicit (e.g., name the missing capability or the "
            "surface it lives on), and re-run.\n"
            "  • Or narrow scope with `--source-of-truth codebase` so "
            "planners reading repo docs stop treating them as a "
            "feature checklist."
        )

    _apply_reconciler_output(plans, output)

    # Re-run the DESIGN §5 `requires.extent` mechanical passes against the
    # post-reconciler plan tree so any `extent: external` entries on
    # reconciler-added connector subtasks flow through the same machinery
    # as planner-declared externals. Without this, an added_subtask
    # carrying an external prerequisite would be silently dropped: not
    # collected as a precondition, not surfaced in plan.json, not
    # promoted even if an in-plan producer exists in another plan. The
    # collector returns the full deduped set, so replacing (not
    # appending to) st.data["external_preconditions"] keeps the
    # re-run idempotent.
    #
    # The count can move in either direction:
    #   - GROWS when a reconciler added_subtask declares a new external
    #     requirement (the common forward case).
    #   - SHRINKS when a reconciler `added_provides` (or an added_subtask
    #     that provides a tag) absorbs a planner-declared external —
    #     the second-pass `_promote_external_collisions` demotes the
    #     external entry to in_plan because a provider now exists. This
    #     is correct behavior: the reconciler discovered that the
    #     external prerequisite is actually in-plan after all.
    promoted_after = _promote_external_collisions(plans)
    if promoted_after:
        log(f"phase 2½: promoted {promoted_after} external requires "
            "entry/entries from reconciler added_subtasks to in_plan")
    preconditions_after = _collect_external_preconditions(plans)
    if len(preconditions_after) != len(preconditions):
        log(f"phase 2½: preconditions count changed from "
            f"{len(preconditions)} to {len(preconditions_after)} "
            "after reconciler output")
    st.data["external_preconditions"] = preconditions_after
    st.save()

    # Second-pass check: an `added_subtask` may itself have an unresolved
    # `requires`. If so, the reconciler's output didn't actually close
    # every gap — fail loud rather than progress to schedule() with a
    # still-broken graph.
    still_unresolved = _compute_unresolved_requires(plans)
    if still_unresolved:
        bullets = "\n".join(
            f"  • {u['domain']}/{u['sid']} requires '{u['tag']}'"
            for u in still_unresolved
        )
        die(
            "reconciler output left "
            f"{len(still_unresolved)} cross-domain dependency/dependencies "
            f"still unresolved after applying its renames / "
            f"added_provides / added_subtasks:\n{bullets}\n"
            "This usually means an added_subtask itself requires a "
            "capability that no other subtask provides. Refine the task "
            "description and re-run."
        )

    log(f"phase 2½: reconciled "
        f"({len(output.get('renames', []))} rename(s), "
        f"{len(output.get('added_provides', []))} added_provides, "
        f"{len(output.get('added_subtasks', []))} new subtask(s))")
    return plans


def schedule(plans: list[dict]) -> tuple[dict, list[list[str]]]:
    """Phase 3 (pure Python): merge plans, resolve intra- and cross-domain
    dependencies, topologically sort into waves. Deterministic."""
    log("phase 3: scheduling")
    subtasks: dict[str, dict] = {}
    blocked_domains: list[str] = []
    for plan in plans:
        for s in plan.get("subtasks", []):
            subtasks[s["id"]] = s
        if plan.get("status") == "blocked":
            blocked_domains.append(plan.get("domain", "<unknown>"))
    if not subtasks:
        if blocked_domains:
            die("planners produced no subtasks — all relevant domains exited "
                f"blocked at the evidence gate: {', '.join(blocked_domains)}. "
                "See each planner's confidence.gap_to_close for what evidence "
                "would unblock; raise --confidence-rounds or supply the "
                "missing information and re-run.")
        die("planners produced no subtasks")
    if blocked_domains:
        # Partial block: some domains succeeded, others exited blocked.
        # The earlier phase_plan log line carried each blocked domain's
        # gap, but by the time the user is reading scheduling output that
        # signal is several phases back. Surface it again here so a
        # silently-dropped domain is not invisible in the run summary.
        log(f"WARNING: {len(blocked_domains)} domain(s) exited blocked at "
            f"the planner evidence gate and contributed no subtasks: "
            f"{', '.join(blocked_domains)}. Proceeding with the ready "
            "domains; see the per-category log lines above for each "
            "blocked planner's gap_to_close.")

    # provides -> [subtask ids] for cross-domain edge resolution
    providers: dict[str, list[str]] = {}
    for sid, s in subtasks.items():
        for cap in s.get("provides", []):
            providers.setdefault(cap, []).append(sid)

    # build edges: predecessors of each subtask. `requires` entries are
    # objects `{tag, extent, reason?}` (DESIGN §5 `requires.extent`);
    # only `extent: in_plan` entries become graph edges — `external`
    # entries are out-of-graph by planner declaration and are surfaced
    # as preconditions in plan.json instead.
    preds: dict[str, set[str]] = {sid: set() for sid in subtasks}
    for sid, s in subtasks.items():
        for dep in s.get("depends_on", []):
            if dep in subtasks:
                preds[sid].add(dep)
        for entry in s.get("requires", []):
            if not isinstance(entry, dict) or entry.get("extent") != "in_plan":
                continue
            cap = entry.get("tag", "")
            for provider in providers.get(cap, []):
                if provider != sid:
                    preds[sid].add(provider)

    # Kahn's algorithm -> waves
    waves: list[list[str]] = []
    done: set[str] = set()
    remaining = set(subtasks)
    while remaining:
        wave = sorted(sid for sid in remaining if preds[sid] <= done)
        if not wave:
            cyc = ", ".join(sorted(remaining))
            die(f"dependency cycle among subtasks: {cyc}")
        waves.append(wave)
        done |= set(wave)
        remaining -= set(wave)

    log(f"  {len(subtasks)} subtasks across {len(waves)} wave(s)")
    return subtasks, waves


def write_plan(pila_dir: Path, task: str, st: State,
               subtasks: dict, waves: list[list[str]]) -> None:
    """Persist the merged plan and per-subtask spec files the implementers read."""
    answers = st.data.get("answers", {})
    sot = answers.get("source_of_truth", "codebase")
    # External preconditions are the planner-declared out-of-graph
    # requires entries collected during phase_reconcile (DESIGN §5
    # `requires.extent`). Surfacing them in plan.json gives the
    # launcher / integrator / human a deploy-notes section without
    # treating them as build-graph edges. Empty list when no planner
    # declared any `extent: external` entry — common case.
    preconditions = st.data.get("external_preconditions", []) or []
    (pila_dir / "plan.json").write_text(json.dumps(
        {"task": task, "waves": waves, "subtasks": subtasks,
         "preconditions": preconditions}, indent=2))
    sub_dir = pila_dir / "subtasks"
    for sid, s in subtasks.items():
        spec = dict(s)
        spec["_task"] = task
        spec["_source_of_truth"] = sot
        spec["_clarification_answers"] = answers
        (sub_dir / f"{sid}.json").write_text(json.dumps(spec, indent=2))
    st.data["waves"] = waves
    st.data["completed_waves"] = st.data.get("completed_waves", 0)
    st.data["subtask_status"] = st.data.get("subtask_status", {})
    st.save()


def _format_provision_recipe_section(recipe: list[dict],
                                      *, audience: str) -> str | None:
    """Render the persisted provision recipe as a prompt section, or
    return None if the recipe is empty / all-`none`.

    `audience` controls the framing:
      - "implementer": "decide whether your subtask needs them"
      - "conformer": "ensure deps are installed before BUILD/LINT/TEST"

    The recipe is detected in phase_provision but executed by workers
    in their own worktrees (DESIGN §6½ "Worker-driven install"). This
    function is the prompt-injection helper that hands the recipe to
    workers verbatim — no per-worker variation, same string in every
    prompt.
    """
    install_entries = [e for e in recipe
                       if e.get("kind") in ("install", "build")
                       and e.get("command")]
    if not install_entries:
        return None

    lines = ["", "PROVISION_RECIPE:"]
    if audience == "implementer":
        lines.append(
            "  The orchestrator detected the following install (and "
            "follow-on build) commands for this repo. Your worktree "
            "starts with NO installed dependencies and no build "
            "outputs. Decide whether your subtask needs them — if yes, "
            "run them via Bash in the order shown. The package-manager "
            "caches (pnpm store, pip wheel cache, go module cache, cargo "
            "registry) are warm and shared across worktrees, so "
            "re-running these is fast. These are advisory: skip them if "
            "your subtask is purely documentation, config, or otherwise "
            "doesn't touch buildable code."
        )
    elif audience == "conformer":
        lines.append(
            "  Your worktree starts with NO installed dependencies "
            "(or only those the implementer chose to install) and no "
            "build outputs. Before running BUILD_CMD / LINT_CMD / "
            "TEST_CMD, ensure deps and any required build artifacts are "
            "present — either run the install (and follow-on build) "
            "command(s) yourself first, in the order shown, or react to "
            "a failing test/build that diagnoses missing deps and run "
            "them then. The caches are warm so re-running is fast."
        )
    else:
        raise ValueError(f"unknown audience {audience!r}")
    for i, e in enumerate(install_entries, 1):
        cmd_str = " ".join(e["command"])
        wd = e.get("working_dir", ".")
        timeout = e.get("timeout_s") or 1800
        lines.append(f"  {i}. {cmd_str}   (cwd: {wd}, timeout: {timeout}s)")
    return "\n".join(lines)


async def run_implementer(sid: str, pila_dir: Path, caps: dict, st: State,
                          models: dict[str, str],
                          efforts: dict[str, str | None],
                          continuation: bool = False, note: str = "") -> dict:
    """Spawn one implementer for one subtask in its own worktree. Handles
    both kinds of continuation up to the shared `subtask_continuations`
    cap: context-exhaustion handoffs and DESIGN §11 mid-execution
    clarifications."""
    sys_prompt = load_prompt("implementer")
    proc = await run_script("new-worktree.sh", sid, st.run_id)
    if proc.returncode != 0:
        raise WorkerError(f"worktree creation failed for {sid}: {proc.stderr.strip()}")
    worktree = proc.stdout.strip().splitlines()[-1]
    # The fresh worktree has NO installed deps. The implementer runs
    # installs itself in its own worktree against the shared
    # package-manager caches (DESIGN §6½ "Worker-driven install"); the
    # recipe to follow is injected into its prompt below. We don't
    # pre-install here because (a) it would clobber the host's
    # repo_root checkout that this worktree shares the package cache
    # with, and (b) workers correctly skip install when their subtask
    # is config-only / docs-only.

    # DESIGN §11 mid-execution clarification: the worker may exit with
    # `needs-clarification` only when --clarify is in effect. Without
    # --clarify (the default) the user has not opted into questions, so
    # the worker must run the same codebase→research probe and make a
    # documented best-effort decision instead of interrupting.
    can_ask_user = st.data.get("clarify", False)

    up = [f"Execute subtask `{sid}`.",
          f"PILA_DIR is {pila_dir} (absolute).",
          f"Read your spec at {pila_dir}/subtasks/{sid}.json.",
          "Your current working directory IS your isolated worktree — make and "
          "commit all code changes here.",
          # DESIGN §8 + §13: evidence-gate bound, prompt-governed.
          f"CONFIDENCE_ROUNDS: {caps['confidence_rounds']} (the maximum "
          "number of evidence-gate iterations before you exit blocked).",
          # DESIGN §11 mid-execution clarification gate.
          f"CAN_ASK_USER: {str(can_ask_user).lower()} (when true, you may "
          "exit `needs-clarification` for a genuine intent question that "
          "neither the codebase nor research can resolve; when false, you "
          "must make a best-effort decision and proceed)."]
    recipe_section = _format_provision_recipe_section(
        (st.data.get("provision") or {}).get("recipe") or [],
        audience="implementer")
    if recipe_section is not None:
        up.append(recipe_section)
    if continuation:
        up.append(f"This is a CONTINUATION. Read the checkpoint at "
                  f"{pila_dir}/checkpoints/{sid}.md, validate it against the "
                  f"actual repo state, then continue.")
    if note:
        up.append(f"NOTE FROM ORCHESTRATOR: {note}")

    st.bump_workers(caps)
    try:
        return await claude_p(user_prompt="\n".join(up), system_prompt=sys_prompt,
                              schema_key="implementer", cwd=worktree,
                              allowed_tools=ACT_TOOLS, max_turns=120,
                              autonomous=True, caps=caps, st=st,
                              model=models["implementer"],
                              effort=efforts["implementer"], sid=sid)
    except WorkerError as e:
        # worker could not return schema-valid output even after a retry
        # (e.g. it hit --max-turns mid-task) -> treat as a handoff so a fresh
        # implementer can continue from whatever checkpoint exists.
        return {"subtask_id": sid, "status": "incomplete-handoff",
                "checkpoint_path": str(pila_dir / "checkpoints" / f"{sid}.md"),
                "summary": f"worker produced no schema-valid result: {e}"}
    except subprocess.TimeoutExpired:
        # worker hit the per-process wall-clock cap (`worker_timeout_sec`,
        # default 5400s / 90 min). _invoke killed the claude -p child
        # and re-raised TimeoutExpired. Without this catch the
        # exception would escape settle_subtask → gather_or_cancel →
        # phase_execute → orchestrate → main()'s catch-all and dump a
        # 50KB traceback (with the entire claude -p command line) to
        # the user's terminal. Same treatment as the WorkerError
        # arm — a fresh implementer can continue from any partial
        # checkpoint. If no checkpoint was written, the line-2314
        # arm of _retryable_failure catches the empty-handoff and
        # allows one retry; the failed_retries cap then bounds the
        # chain.
        #
        # Why retry rather than terminal: pila's typical usage is
        # unattended (overnight runs), so a transient hang has real
        # value in recovering on a fresh process. The worst case —
        # one extra 90-min worker invocation bounded by failed_retries
        # — is an acceptable trade for that recovery chance. An
        # operator-supervised mode that wanted fail-fast semantics
        # would need a separate cap (not currently in scope).
        timeout = caps.get("worker_timeout_sec", "?")
        return {"subtask_id": sid, "status": "incomplete-handoff",
                "checkpoint_path": str(pila_dir / "checkpoints" / f"{sid}.md"),
                "summary": (f"worker timed out after {timeout}s "
                            "(worker_timeout_sec cap) — fresh implementer "
                            "can continue from any partial checkpoint")}


def _retryable_failure(reason: str) -> bool:
    """The retry policy, in one place.

    A failure is retried only if a corrective note to a fresh worker can
    plausibly fix it — e.g. "you forgot to commit" or "your worktree was
    dirty." A failure that means the worker is broken or dishonest is NOT
    retried: re-running it burns a worker invocation against the budget for no
    expected gain, and (for a bad-handoff case) a cold restart discards the
    partial work the checkpoint pointed at.

    Retryable (corrective note can fix it):
      - branch had no commits ahead of the run branch
      - worktree left dirty (uncommitted changes)
      - `incomplete-handoff` worker produced no checkpoint on disk
        (the `validate_result` line-2314-style message). Two known
        triggers: (a) the Claude Code session-limit / rate-limit case
        where the worker did nothing because the subscription was
        capped — primarily caught by `detect_session_limit()` upstream
        but this is the safety net for a message-format change; (b) a
        worker that hit `--max-turns` with no checkpoint written, which
        `run_implementer` synthesizes into the same envelope shape (see
        the WorkerError catch at the end of `run_implementer`). Both
        are corrective-note cases — a fresh worker can plausibly do
        better — not lies about status.

    Terminal (worker is broken/dishonest — terminate immediately, no retry):
      - cross-field invariant violation (worker lied about its own status)
      - diff touched a protected path (.pila/, .git/, or any top-level
        .claude/ file; the .claude/{agents,commands,skills}/ subtrees are
        exempt per is_protected_path())
      - any worker-level error surfaced as a failure
    """
    retryable_markers = ("no commits ahead of the run",
                         "uncommitted change")
    if any(m in reason for m in retryable_markers):
        return True
    # Prefix-match — not pair-match on "checkpoint_path" + "does not
    # exist on disk" — because validate_result's needs-clarification
    # check emits a *different* message that also contains both
    # substrings (`status='needs-clarification' but checkpoint_path
    # '...' does not exist on disk`) but represents a genuinely-broken
    # worker that must stay non-retryable. Only the
    # incomplete-handoff variant (`checkpoint_path '...' does not exist
    # on disk`) is the session-limit-no-op signature.
    if reason.startswith("checkpoint_path '"):
        return True
    return False


# --- post-work conformance phase (DESIGN §9 *Post-work conformance*) -------
# Runs after the implementer's success-path settlement checks pass, before
# `settle_subtask` returns. The phase is advisory: nothing it does or fails
# to do can produce a `failed` / `blocked` subtask status. The code-enforced
# guarantees are narrow — rule-file discovery is deterministic, the worker's
# output is schema-validated, and the same diff-scope check that gates the
# implementer is re-applied to the conformer's commits. Everything else
# (which rule was violated, whether build/lint/tests passed, whether docs
# are actually stale) is the worker's judgment, surfaced as warnings.

# Fixed, capped allowlist of rule-file paths the discovery function checks.
# Order is the priority order the conformer reads in. Adding to this list
# is a design change (DESIGN §9) — the worker is told these are
# authoritative and only these.
_RULES_FILE_CANDIDATES = (
    "CLAUDE.md", "AGENTS.md", ".agent.md",
    ".cursorrules", ".windsurfrules",
    "docs/CLAUDE.md", "docs/AGENTS.md",
    "docs/CONVENTIONS.md", "docs/STYLE.md",
    "README.md", "CONTRIBUTING.md",
    "docs/DESIGN.md", "docs/IMPLEMENTATION.md",
)


def discover_rules_files(repo_root: Path) -> list[Path]:
    """Return existing rule-file paths from `_RULES_FILE_CANDIDATES`, in
    declaration order, capped at the candidate-list length. Never raises;
    never recurses; returns [] cleanly when nothing matches."""
    out: list[Path] = []
    for rel in _RULES_FILE_CANDIDATES:
        p = repo_root / rel
        try:
            if p.is_file():
                out.append(p)
        except OSError:
            continue
    return out


def _infer_build_lint_test(repo_root: Path) -> dict[str, str]:
    """Best-effort guess at the repo's build / lint / test commands. Returns
    a dict with keys 'build', 'lint', 'test' — empty string when no command
    could be inferred for that axis. The conformer is told an empty string
    means "not applicable; report ran=false." This is a *suggestion* the
    worker may override based on what it sees in the repo."""
    out = {"build": "", "lint": "", "test": ""}
    if (repo_root / "Makefile").is_file():
        # Don't assume specific targets — the conformer reads the Makefile
        # and picks. We just signal "a Makefile exists."
        out["build"] = "make"
    if (repo_root / "package.json").is_file():
        # npm has both build and test conventions; lint varies. The
        # conformer reads scripts and picks.
        out["build"] = out["build"] or "npm run build"
        out["test"] = out["test"] or "npm test"
    if (repo_root / "pyproject.toml").is_file() or \
       (repo_root / "pytest.ini").is_file() or \
       (repo_root / "setup.cfg").is_file():
        out["test"] = out["test"] or "pytest"
    if (repo_root / "Cargo.toml").is_file():
        out["build"] = out["build"] or "cargo build"
        out["test"] = out["test"] or "cargo test"
    if (repo_root / "go.mod").is_file():
        out["build"] = out["build"] or "go build ./..."
        out["test"] = out["test"] or "go test ./..."
    if (repo_root / ".eslintrc").is_file() or \
       (repo_root / ".eslintrc.json").is_file() or \
       (repo_root / ".eslintrc.js").is_file() or \
       (repo_root / ".eslintrc.cjs").is_file() or \
       (repo_root / ".eslintrc.yaml").is_file() or \
       (repo_root / ".eslintrc.yml").is_file():
        out["lint"] = out["lint"] or "npx eslint ."
    if (repo_root / ".ruff.toml").is_file() or \
       (repo_root / "ruff.toml").is_file():
        out["lint"] = out["lint"] or "ruff check ."
    return out


def validate_conformance_result(result: dict, worktree: str) -> str | None:
    """Cross-field invariants for the conformer's structured output.
    Returns None when valid, else a one-line error string.

    The JSON schema already enforces the *shape* (required fields, their
    types). This function enforces the cross-field rules the schema can't:
    residuals require a non-empty `rules_files_read`, every fixed violation
    cites a non-empty `rule`, every docs/tests update cites a path that
    actually exists in the worktree.

    Per DESIGN §12 this is the code-enforced honesty check; the worker's
    own judgment is not second-guessed beyond these structural minimums."""
    if not isinstance(result, dict):
        return "conformer result is not an object"

    files_read = result.get("rules_files_read") or []
    residuals = result.get("rule_violations_residual") or []
    if residuals and not files_read:
        return ("rule_violations_residual non-empty but rules_files_read "
                "is empty — a violation cannot exist without a rule")

    fixed = result.get("rule_violations_fixed") or []
    for i, item in enumerate(fixed):
        if not (item.get("rule") or "").strip():
            return f"rule_violations_fixed[{i}] has empty 'rule'"

    # Resolve the worktree once for path-traversal checking. Paths must
    # both exist AND resolve inside the worktree — a `path` like
    # "../../etc/passwd" or "/etc/passwd" is an honesty failure (the
    # conformer claims to have updated a doc inside the subtask, but the
    # path it cites escapes the worktree).
    try:
        wt_resolved = Path(worktree).resolve()
    except OSError:
        return f"worktree path {worktree!r} could not be resolved"
    for kind in ("docs_updates", "tests_updates"):
        for i, item in enumerate(result.get(kind) or []):
            rel = (item.get("path") or "").strip()
            if not rel:
                return f"{kind}[{i}] has empty 'path'"
            try:
                resolved = (wt_resolved / rel).resolve()
            except OSError:
                return (f"{kind}[{i}] path {rel!r} could not be resolved")
            # Path.is_relative_to was added in 3.9; we target 3.10+ (see
            # CLAUDE.md tech-stack note) so this is safe.
            if not resolved.is_relative_to(wt_resolved):
                return (f"{kind}[{i}] path {rel!r} escapes the worktree "
                        f"(resolves to {resolved}); paths must stay inside "
                        f"the subtask's worktree")
            if not resolved.exists():
                return (f"{kind}[{i}] cites path {rel!r} which does not "
                        f"exist in the worktree")
    return None


async def _branch_head_sha(worktree: str) -> str:
    """HEAD sha in the worktree, or empty string on failure. Used as the
    rollback target before the conformer adds commits."""
    r = await run_proc(["git", "rev-parse", "HEAD"], cwd=worktree)
    if r.returncode != 0:
        return ""
    return r.stdout.strip()


async def rollback_conformer_commits(worktree: str, before_sha: str) -> None:
    """Hard-reset the subtask branch back to `before_sha`. Used when the
    conformer wrote to a protected path — the implementer's commits
    are preserved, the conformer's are dropped. Safe to call when no
    new commits were made: it's a no-op reset.

    Note: `git reset --hard` also discards uncommitted changes. Callers
    that want to warn about discarded scribbles should call
    `_uncommitted_paths` first."""
    if not before_sha:
        return
    await run_proc(["git", "reset", "--hard", before_sha], cwd=worktree)


async def _uncommitted_paths(worktree: str) -> list[str]:
    """Return tracked-file paths with uncommitted changes in the worktree,
    or [] if the check fails. Untracked files are excluded — the rollback
    only touches tracked state. Used as a pre-rollback observability
    helper: when the conformer leaves uncommitted scribbles alongside a
    commit that triggers rollback, those scribbles get silently discarded
    by `git reset --hard`. This lets the caller surface what was lost."""
    try:
        r = await run_proc(["git", "status", "--porcelain"], cwd=worktree)
    except OSError:
        return []
    if r.returncode != 0:
        return []
    return [line for line in r.stdout.splitlines()
            if line and not line.startswith("??")]


async def _unprefixed_conformer_commits(worktree: str, before_sha: str,
                                        prefix: str = "conformer:"
                                        ) -> list[str]:
    """Return subject lines of commits between before_sha..HEAD whose
    subjects do not start with `prefix`. Empty list when there are no new
    commits, when every new commit is correctly prefixed, or when the git
    invocation fails (the caller treats a missing answer as no warning).

    This is the code-side honesty check for the prompt-level rule
    "conformer commits must start with `conformer:`" (DESIGN §9
    Post-work conformance + §12 prompts-are-advisory). The check is
    *observability*, not enforcement — unprefixed commits surface as
    `conformance_warnings`, never trigger rollback."""
    if not before_sha:
        return []
    r = await run_proc(
        ["git", "log", "--format=%s", f"{before_sha}..HEAD"],
        cwd=worktree,
    )
    if r.returncode != 0:
        return []
    return [line for line in r.stdout.splitlines()
            if line and not line.startswith(prefix)]


async def run_conformer(sid: str, pila_dir: Path, worktree: str,
                        caps: dict, st: State, models: dict[str, str],
                        efforts: dict[str, str | None],
                        rules_files: list[Path],
                        blt_commands: dict[str, str],
                        diff_base: str) -> dict | None:
    """Spawn one conformer for one subtask in its existing worktree.
    Returns the worker's structured output, or None on WorkerError (which
    is recorded as a warning by the caller — DESIGN §9: the phase is
    advisory)."""
    sys_prompt = load_prompt("conformer")
    repo_root = st.pila_root.parent
    rules_paths_str = ", ".join(
        str(p.relative_to(repo_root)) if str(p).startswith(str(repo_root))
        else str(p)
        for p in rules_files
    ) or "(none)"
    up = [f"Run the post-work conformance phase for subtask `{sid}`.",
          f"PILA_DIR is {pila_dir} (absolute). Your subtask spec "
          f"is at {pila_dir}/subtasks/{sid}.json and the implementer's "
          f"success-criteria notes are at {pila_dir}/criteria/{sid}.md "
          "— both read-only inputs.",
          "Your current working directory IS the subtask's worktree. Make "
          "and commit any fixes here. Every commit subject must start "
          "with `conformer:`.",
          f"RULES_FILES: {rules_paths_str}",
          f"BUILD_CMD: {blt_commands.get('build') or '(none)'}",
          f"LINT_CMD: {blt_commands.get('lint') or '(none)'}",
          f"TEST_CMD: {blt_commands.get('test') or '(none)'}",
          f"DIFF_BASE: {diff_base} (compare with `git diff {diff_base}..HEAD`)"]
    recipe_section = _format_provision_recipe_section(
        (st.data.get("provision") or {}).get("recipe") or [],
        audience="conformer")
    if recipe_section is not None:
        up.append(recipe_section)

    # bump_workers is inside the try block on purpose: it raises
    # WorkerError when max_total_workers is exhausted, and the conformance
    # phase must NEVER escalate that into a failed/blocked subtask
    # (DESIGN §9 Post-work conformance — the phase is advisory only). The
    # implementer at run_implementer() places bump_workers outside its try
    # because for the implementer the budget-exhausted error IS meant to
    # abort the run.
    try:
        st.bump_workers(caps)
        return await claude_p(user_prompt="\n".join(up),
                              system_prompt=sys_prompt,
                              schema_key="conformer", cwd=worktree,
                              allowed_tools=ACT_TOOLS, max_turns=60,
                              autonomous=True, caps=caps, st=st,
                              model=models["conformer"],
                              effort=efforts["conformer"],
                              sid=f"{sid}-conformer")
    except WorkerError as e:
        log(f"  {sid}: conformer crashed: {e}")
        return None
    except subprocess.TimeoutExpired:
        # Same rationale as run_implementer's TimeoutExpired catch —
        # don't let the worker-timeout traceback escape. The conformer
        # phase is advisory; a timed-out conformer becomes one more
        # warning, not a run-killer.
        timeout = caps.get("worker_timeout_sec", "?")
        log(f"  {sid}: conformer timed out after {timeout}s")
        return None


def _summarize_residuals(conf_res: dict) -> list[str]:
    """One advisory string per residual / failing build-lint-test axis.
    Empty list when the conformer reports a fully clean pass."""
    out: list[str] = []
    for item in conf_res.get("rule_violations_residual") or []:
        rule = (item.get("rule") or "").strip()
        why = (item.get("why_not_fixed") or "").strip()
        out.append(f"rule-residual: {rule!r} not fixed — {why}")
    for axis in ("build", "lint", "tests"):
        a = conf_res.get(axis) or {}
        if a.get("ran") and not a.get("passed"):
            summary = (a.get("summary") or "").strip() or "(no summary)"
            out.append(f"{axis}-failed: {a.get('command', '')!r}: {summary}")
    return out


def _conformance_clean(conf_res: dict) -> bool:
    """True when the conformer reports no residuals and every axis is
    either passed or not applicable. Used to short-circuit the
    orchestrator-level conformer loop."""
    if conf_res.get("rule_violations_residual"):
        return False
    for axis in ("build", "lint", "tests"):
        a = conf_res.get(axis) or {}
        if a.get("ran") and not a.get("passed"):
            return False
    return True


async def _run_conformance_phase(sid: str, pila_dir: Path,
                                 worktree: str, subtask: dict, caps: dict,
                                 st: State, models: dict[str, str],
                                 efforts: dict[str, str | None]
                                 ) -> tuple[dict | None, list[str]]:
    """Drive the orchestrator-level conformer loop for one subtask.
    Returns `(last_conformer_result, warnings)`. Never raises a workflow
    error: all failure modes — malformed output, WorkerError, gate
    violations on conformer commits, exhausted rounds — surface as
    entries in `warnings`. The subtask still returns `complete`."""
    warnings: list[str] = []
    repo_root = st.pila_root.parent
    rules_files = discover_rules_files(repo_root)
    blt = _infer_build_lint_test(repo_root)
    run_branch = compute_run_branch(st.run_id)
    last_res: dict | None = None

    for c_round in range(caps["conformance_rounds"]):
        before_sha = await _branch_head_sha(worktree)
        last_res = await run_conformer(
            sid, pila_dir, worktree, caps, st, models, efforts,
            rules_files=rules_files, blt_commands=blt, diff_base=run_branch)

        if last_res is None:
            warnings.append(f"conformer round {c_round}: worker crashed; "
                            "phase surfaced as advisory")
            break

        err = validate_conformance_result(last_res, worktree)
        if err:
            warnings.append(f"conformer round {c_round}: malformed result: {err}")
            break

        # Re-apply the implementer gates against any new conformer commits.
        # Empty diff (worker added no commits) is fine and common: a
        # well-formed result with no fixes is a legitimate "nothing to do."
        # check_diff_scope returns a string ONLY for a protected-path
        # violation — .pila/, .git/, or top-level .claude/ files;
        # .claude/{agents,commands,skills}/ are exempt per
        # is_protected_path(). The scope-volume warning is logged
        # side-channel and does not surface here.
        scope_err = await check_diff_scope(sid, worktree, subtask, st)
        if scope_err:
            discarded = await _uncommitted_paths(worktree)
            if discarded:
                warnings.append(
                    f"conformer round {c_round}: discarding "
                    f"{len(discarded)} uncommitted file(s) during rollback: "
                    f"{[line[3:] for line in discarded]}")
            await rollback_conformer_commits(worktree, before_sha)
            warnings.append(f"conformer round {c_round}: protected-path "
                            f"violation reverted ({scope_err})")
            break

        # Dirty-worktree check: the conformer should commit, not leave
        # uncommitted changes that integration would lose.
        dirty = await _uncommitted_paths(worktree)
        if dirty:
            warnings.append(f"conformer round {c_round}: left "
                            f"{len(dirty)} uncommitted change(s) — not "
                            "rolled back, but surfaced as advisory")

        # Commit-prefix observability: surface (but don't roll back) any
        # conformer commits whose subject doesn't start with `conformer:`.
        # The prefix lets reviewers identify conformer commits in git log;
        # missing prefixes are a discipline lapse, not a correctness issue.
        unprefixed = await _unprefixed_conformer_commits(worktree, before_sha)
        for subject in unprefixed:
            warnings.append(f"conformer round {c_round}: commit subject "
                            f"missing `conformer:` prefix: {subject!r}")

        if _conformance_clean(last_res):
            break

    if last_res is not None:
        warnings.extend(_summarize_residuals(last_res))
    return last_res, warnings


async def settle_subtask(sid: str, pila_dir: Path, caps: dict, st: State,
                         models: dict[str, str],
                         efforts: dict[str, str | None]) -> dict:
    """Drive one subtask to a terminal state.

    Three bounded escalation paths, all code-enforced:
      - subtask continuations (cap: caps['subtask_continuations']) —
        consumed by both context-exhaustion handoffs and DESIGN §11
        mid-execution clarifications, sharing a single budget so a
        subtask cannot get extra re-spawns by mixing the two
      - corrective retries of a retryable failure (cap: caps['failed_retries'])

    A non-retryable failure (see `_retryable_failure`) terminates the subtask
    immediately with status 'failed' — no retry is attempted. Returns the final
    result."""
    continuations = 0
    retries = 0
    note = ""
    continuation = False
    worktree = str(pila_dir / "worktrees" / sid)
    subtask_path = pila_dir / "subtasks" / f"{sid}.json"
    subtask = json.loads(subtask_path.read_text()) if subtask_path.exists() else {}

    async def fail(reason: str) -> dict | None:
        """Record a failed attempt. Returns a terminal result dict if the
        subtask is done (non-retryable, or retry cap exhausted), or None if the
        caller should loop for one more corrective attempt.

        On a retryable failure that will loop, `_reset_subtask_worktree`
        clears the leftover worktree + branch so `new-worktree.sh`
        reaches its "fresh subtask" path on the next iteration. Without
        this reset, the retry hits `fatal: a branch ... already exists`
        and the WorkerError escapes to `gather_or_cancel`, killing the
        whole wave."""
        nonlocal retries, continuation, note
        res = {"subtask_id": sid, "status": "failed", "summary": reason}
        st.data.setdefault("subtask_status", {})[sid] = "failed"
        st.save()
        if not _retryable_failure(reason):
            log(f"  {sid}: non-retryable failure — terminating: {reason}")
            return res
        retries += 1
        if retries > caps["failed_retries"]:
            log(f"  {sid}: retry cap reached — terminating")
            return res
        await _reset_subtask_worktree(sid, pila_dir, st.run_id)
        continuation = False
        note = f"Previous attempt failed: {reason}"
        return None

    while True:
        res = await run_implementer(sid, pila_dir, caps, st, models, efforts,
                                    continuation=continuation, note=note)

        # cross-field invariant check — catches a worker that lied about
        # status. A self-contradictory result means the worker is malfunctioning
        # or dishonest: non-retryable by `_retryable_failure`.
        problem = validate_result(res)
        if problem:
            log(f"  result invariant violated for {sid}: {problem}")
            done = await fail(problem)
            if done is not None:
                return done
            continue

        status = res.get("status")
        st.data.setdefault("subtask_status", {})[sid] = status
        st.save()

        if status == "complete":
            # a 'complete' claim with no commits is a retryable mistake —
            # the worker may genuinely have work to commit and just forgot
            commit_err = await check_branch_has_commits(
                sid, worktree, compute_run_branch(st.run_id))
            if commit_err:
                log(f"  branch check failed for {sid}: {commit_err}")
                done = await fail(commit_err)
                if done is not None:
                    return done
                continue
            # uncommitted changes — retryable, same reasoning
            wt_status = await run_proc(
                ["git", "status", "--porcelain"], cwd=worktree)
            dirty = [l for l in wt_status.stdout.splitlines()
                     if l and not l.startswith("??")]
            if dirty:
                done = await fail(f"{sid}: worktree has {len(dirty)} uncommitted "
                                  f"change(s) — changes will be lost on integration")
                if done is not None:
                    return done
                continue
            # protected-path violation — the worker wrote to .git/ etc.: it is
            # broken, not merely careless. Non-retryable by `_retryable_failure`.
            scope_err = await check_diff_scope(sid, worktree, subtask, st)
            if scope_err:
                done = await fail(scope_err)
                if done is not None:
                    return done
                continue

            # DESIGN §9 *Post-work conformance*: advisory phase. Runs only on
            # the success path (every check above has passed), never produces
            # a `failed` / `blocked` status, attaches its result and any
            # warnings to `res` and to st.data["conformance"].
            #
            # The broad try/except is load-bearing: the phase is documented
            # as "Never raises a workflow error," but `_run_conformance_phase`
            # calls `run_proc` which calls `asyncio.create_subprocess_exec`,
            # which raises `FileNotFoundError` when `cwd` is missing. The
            # worktree could disappear mid-phase (operator action or a racy
            # external cleanup), and an unhandled exception would escalate
            # a `complete` subtask to a crash. Catching everything here
            # preserves the advisory framing: any failure mode reduces to a
            # warning. Specific exception types are logged.
            conf_res: dict | None = None
            conf_warnings: list[str] = []
            try:
                conf_res, conf_warnings = await _run_conformance_phase(
                    sid, pila_dir, worktree, subtask, caps, st, models, efforts)
            except Exception as e:
                conf_warnings.append(
                    f"conformance phase raised {type(e).__name__}: {e} — "
                    "surfaced as advisory, subtask still complete")
            if conf_res is not None:
                res["conformance"] = conf_res
            if conf_warnings:
                res["conformance_warnings"] = conf_warnings
                for w in conf_warnings:
                    log(f"  {sid}: conformance: {w}")
            st.data.setdefault("conformance", {})[sid] = {
                "result": conf_res,
                "warnings": conf_warnings,
            }
            st.save()
            return res

        if status == "incomplete-handoff":
            # Worktree convention from scripts/new-worktree.sh:
            # .pila/worktrees/<subtask-id>. The freshness check on
            # `## Files touched` validates paths against this directory;
            # if it no longer exists (e.g. cleanup ran early), the check
            # is skipped gracefully.
            wt_root = pila_dir / "worktrees" / sid
            cp_err = validate_checkpoint(res.get("checkpoint_path") or "",
                                         worktree_root=wt_root)
            if cp_err:
                log(f"  bad checkpoint for {sid}: {cp_err}")
                return {"subtask_id": sid, "status": "blocked",
                        "blocker": f"checkpoint invalid: {cp_err}",
                        "summary": cp_err}
            continuations += 1
            if continuations > caps["subtask_continuations"]:
                return {"subtask_id": sid, "status": "blocked",
                        "blocker": ("exceeded subtask continuation cap — "
                                    "subtask is mis-scoped and needs "
                                    "re-decomposition"),
                        "summary": "subtask continuation cap exceeded"}
            continuation, note = True, ""
            continue

        if status == "needs-clarification":
            # DESIGN §11 mid-execution clarification: same continuation
            # mechanism as `incomplete-handoff` (worker wrote a
            # checkpoint, orchestrator re-spawns with CONTINUATION),
            # plus a side trip through surface_clarification to capture
            # the user's answer. Consumes from the same
            # subtask_continuations budget — there is no extra "ask the
            # user" allowance.
            wt_root = pila_dir / "worktrees" / sid
            cp_err = validate_checkpoint(res.get("checkpoint_path") or "",
                                         worktree_root=wt_root)
            if cp_err:
                log(f"  bad checkpoint for {sid}: {cp_err}")
                return {"subtask_id": sid, "status": "blocked",
                        "blocker": f"checkpoint invalid: {cp_err}",
                        "summary": cp_err}
            continuations += 1
            if continuations > caps["subtask_continuations"]:
                return {"subtask_id": sid, "status": "blocked",
                        "blocker": ("exceeded subtask continuation cap — "
                                    "subtask is mis-scoped and needs "
                                    "re-decomposition"),
                        "summary": "subtask continuation cap exceeded"}
            # Surface the question; interactive prompt or non-interactive
            # exit with EXIT_NEEDS_ANSWERS. On interactive return, the
            # answer is already in st.data['answers'] so the re-spawned
            # worker reads it via _clarification_answers in its spec.
            surface_clarification(sid, res["clarification_question"],
                                  res.get("checkpoint_path") or "", st)
            # Rewrite this subtask's spec so the new answer is visible
            # to the next implementer — the spec was written once at
            # phase_plan time with the then-current answers; clarifications
            # captured later must be propagated.
            spec_path = pila_dir / "subtasks" / f"{sid}.json"
            if spec_path.exists():
                spec = json.loads(spec_path.read_text())
                spec["_clarification_answers"] = st.data.get("answers", {})
                spec_path.write_text(json.dumps(spec, indent=2))
            continuation, note = True, ""
            continue

        if status == "failed":
            # a worker that reported failure itself — treat its summary as the
            # reason and run it through the same retry policy
            done = await fail(res.get("summary") or "worker reported failure")
            if done is not None:
                return done
            continue

        # blocked, or anything unexpected
        return res


async def integrate_wave(wave: list[str], results: dict[str, dict],
                         pila_dir: Path, caps: dict, st: State,
                         models: dict[str, str],
                         efforts: dict[str, str | None]) -> list[str]:
    """Merge each completed subtask branch into staging (git merge, not
    cherry-pick); resolve conflicts with an integrator worker. Returns the
    list of integrated ids.

    If an integrator cannot resolve a conflict (status other than 'resolved'),
    the in-progress merge is aborted so the staging worktree is left clean, and
    the run is terminated with the integrator's diagnosis — an unresolved
    conflict must not silently proceed onto a corrupt staging tree."""
    integrated, integrated_so_far = [], []
    staging = (pila_dir / "worktrees" / "staging").resolve()
    for sid in wave:
        if results.get(sid, {}).get("status") != "complete":
            continue
        proc = await run_script("integrate.sh", sid, st.run_id)
        if proc.returncode == 0:
            integrated.append(sid)
            integrated_so_far.append(sid)
            continue
        if proc.returncode == 2:
            # exit 2 from integrate.sh is a precondition failure (staging
            # worktree or subtask branch missing) — not a merge conflict.
            # Spawning an integrator against a missing worktree fails in
            # confusing ways, so abort here with the script's own message.
            # Save state first (local convention — see the two neighboring
            # die() sites below) so `--resume` can pick up what was done.
            reason = (f"integrate.sh precondition failure: "
                      f"{proc.stderr.strip() or proc.stdout.strip() or 'no message'}")
            st.data.setdefault("blocked", {})[sid] = reason
            st.save()
            die(f"integrate.sh precondition failure for {sid}: "
                f"{proc.stderr.strip() or proc.stdout.strip() or 'no message'}")
        # exit 1 (conflict): staging worktree is mid-merge — hand to an integrator
        log(f"  conflict integrating {sid}; spawning integrator")
        sys_prompt = load_prompt("integrator")
        up = (f"Resolve the in-progress merge conflict in this worktree.\n"
              f"PILA_DIR is {pila_dir}.\n"
              f"Incoming subtask: {sid}\n"
              f"Already-integrated subtasks it may conflict with: "
              f"{', '.join(integrated_so_far) or 'none'}")
        st.bump_workers(caps)
        ires = await claude_p(user_prompt=up, system_prompt=sys_prompt,
                              schema_key="integrator", cwd=str(staging),
                              allowed_tools=ACT_TOOLS, max_turns=60,
                              autonomous=True, caps=caps, st=st,
                              model=models["integrator"],
                              effort=efforts["integrator"],
                              sid=f"integrator-{sid}")
        if ires.get("status") == "resolved":
            # the integrator must have actually committed the merge — a
            # 'resolved' claim with the worktree still mid-merge is a lie,
            # the integrator-side analogue of check_branch_has_commits.
            merge_err = await check_merge_committed(staging)
            if merge_err:
                await run_proc(["git", "merge", "--abort"], cwd=str(staging))
                die(f"integrator for {sid} returned 'resolved' but {merge_err}. "
                    f"The merge was aborted; {compute_run_branch(st.run_id)} "
                    "is clean. Resolve and re-run with --resume.")
            commit_err = await check_integrator_commit(staging)
            if commit_err:
                # non-fatal: log and record, but don't undo the integration
                log(f"  ⚠  integrator commit warning for {sid}: {commit_err}")
                st.data.setdefault("integrator_warnings", {})[sid] = commit_err
                st.save()
            integrated.append(sid)
            integrated_so_far.append(sid)
        else:
            # design-conflict or failed: the integrator could not produce a
            # correct merge. Abort the in-progress merge so staging is left
            # clean, then terminate — this must not proceed silently.
            diagnosis = (ires.get("diagnosis")
                         or ires.get("resolution_summary")
                         or "no diagnosis provided")
            log(f"  integrator could not resolve {sid}: "
                f"{ires.get('status')} — {diagnosis}")
            await run_proc(["git", "merge", "--abort"], cwd=str(staging))
            die(f"integrator could not integrate {sid} "
                f"({ires.get('status')}): {diagnosis}\n"
                f"The in-progress merge was aborted; "
                f"{compute_run_branch(st.run_id)} is intact at the last "
                f"good wave. Resolve the conflict between {sid} and "
                f"the already-integrated subtasks manually, then re-run with "
                f"--resume.")
    return integrated


async def phase_execute(pila_dir: Path, st: State, caps: dict,
                        models: dict[str, str],
                        efforts: dict[str, str | None]) -> None:
    """Phases 4-5: create staging, then run waves sequentially; within a wave,
    subtasks in parallel (bounded by max_parallel)."""
    log("phase 4: creating run-branch worktree")
    st.data["current_phase"] = "phase 4-5: implementing"
    st.save()
    proc = await run_script("setup-run.sh", st.run_id)
    if proc.returncode != 0:
        die(f"run setup failed: {proc.stderr.strip()}")

    sem = asyncio.Semaphore(caps["max_parallel"])

    async def settle_one(sid: str) -> tuple[str, dict]:
        async with sem:
            r = await settle_subtask(sid, pila_dir, caps, st, models, efforts)
            log(f"  {sid}: {r.get('status')}")
            return sid, r

    waves = st.data["waves"]
    start = st.data.get("completed_waves", 0)
    for wi in range(start, len(waves)):
        wave = waves[wi]
        log(f"phase 5: wave {wi + 1}/{len(waves)} — {len(wave)} subtask(s)")

        pairs = await gather_or_cancel(*(settle_one(sid) for sid in wave))
        results: dict[str, dict] = dict(pairs)

        blocked = [s for s, r in results.items()
                   if r.get("status") in ("blocked", "failed")]
        if blocked:
            st.data["blocked"] = {s: results[s].get("blocker")
                                  or results[s].get("summary") for s in blocked}
            st.save()
            die(f"wave {wi + 1} has unresolved subtasks: {', '.join(blocked)}. "
                f"See {st.path}; resolve and re-run with --resume.")

        await integrate_wave(wave, results, pila_dir, caps, st, models, efforts)

        # Deterministic post-integration safety net: an unresolved
        # conflict marker means integration broke the tree. Per-subtask
        # quality is the implementer's §8 confidence gate — there is no
        # LLM wave-level re-validation (see DESIGN §8, §9).
        staging_path = pila_dir / "worktrees" / "staging"
        marker_err = await scan_conflict_markers(staging_path)
        if marker_err:
            die(f"wave {wi + 1}: {marker_err}\n"
                f"Resolve manually in {staging_path}, commit, "
                "then re-run with --resume.")

        st.data["completed_waves"] = wi + 1
        st.save()


# `push_and_open_pr` was removed when finalize moved to the host
# launcher (DESIGN §6 *Finalization*). The launcher does `git push` +
# `gh pr create` in bash + jq after this container exits — auth state
# lives on the host where it works without forwarding.
#
# `compose_pr_body` is kept (above) as the canonical reference for the
# PR body shape; the launcher's bash composition is structurally
# equivalent. Keeping the Python version makes future audits cheap
# (one file to read).


# --- PR template discovery + LLM body composition -----------------------
# DESIGN §6 *Finalization* hands the PR title/body to a `claude -p`
# worker (pr_writer) so the body respects the target repo's PR template
# when one is present. The deterministic bash composition in the host
# launcher (and `compose_pr_body` above) remains the fail-open fallback.

# GitHub's canonical search order for a single top-level PR template,
# in the priority the GitHub web UI uses.
_PR_TEMPLATE_SINGLE_LOCATIONS = (
    ".github/pull_request_template.md",
    "pull_request_template.md",
    "docs/pull_request_template.md",
)
# Directories where GitHub looks for *multiple* templates. Any .md
# inside any of these counts; pila defaults to the alphabetically
# first basename, with --pr-template overriding the choice.
_PR_TEMPLATE_MULTI_DIRS = (
    ".github/PULL_REQUEST_TEMPLATE",
    "PULL_REQUEST_TEMPLATE",
    "docs/PULL_REQUEST_TEMPLATE",
)


def find_pr_template(repo_root: Path,
                     override: str | None = None) -> tuple[Path, str] | None:
    """Locate the PR template the worker should fill out, or None when
    the repo has no template.

    Returns `(absolute_path, relative_path_from_repo_root)` so the caller
    can both read the file and report which template was used (the
    relative path goes into run.json under `pr_template_used`).

    Discovery order:
      1. The four single-template locations in
         `_PR_TEMPLATE_SINGLE_LOCATIONS` (GitHub's canonical order).
      2. Any `PULL_REQUEST_TEMPLATE/` directory in
         `_PR_TEMPLATE_MULTI_DIRS`. When `override` matches a basename
         inside one of these directories (with or without `.md`), that
         template wins; otherwise the alphabetically first `.md` wins.

    Case sensitivity: file lookups use the literal paths above
    (lowercase `pull_request_template.md`, uppercase
    `PULL_REQUEST_TEMPLATE/`) — GitHub itself accepts both cases but
    pila normalizes on the canonical casing rather than scanning every
    case-variant.
    """
    for rel in _PR_TEMPLATE_SINGLE_LOCATIONS:
        candidate = repo_root / rel
        if candidate.is_file():
            return (candidate, rel)
    for rel_dir in _PR_TEMPLATE_MULTI_DIRS:
        d = repo_root / rel_dir
        if not d.is_dir():
            continue
        mds = sorted(p for p in d.iterdir()
                     if p.is_file() and p.suffix == ".md")
        if not mds:
            continue
        if override:
            wanted = override if override.endswith(".md") else f"{override}.md"
            for p in mds:
                if p.name == wanted:
                    return (p, f"{rel_dir}/{p.name}")
            # Override named, no match — fall through to the default rather
            # than die(), since a bad pr_template setting should not block
            # finalize. The fail-open path in _compose_pr_via_llm logs a
            # warning if pr_template was set but didn't resolve.
        return (mds[0], f"{rel_dir}/{mds[0].name}")
    return None


# Byte budgets for the pr_writer payload. The launcher passes the whole
# JSON-encoded payload as a single argv element to `claude -p`; Linux
# ARG_MAX in the pila container (Debian 12) is ~128 KB. These caps keep
# the largest fields well under that ceiling. The diff sample is line-
# capped instead of byte-capped because individual diff lines can be
# long but the worker reads them as hunks.
PR_WRITER_COMMIT_LOG_MAX_BYTES = 80_000
PR_WRITER_TEMPLATE_MAX_BYTES = 32_000
PR_WRITER_DIFF_SAMPLE_MAX_LINES = 500


def _cap_text(s: str, max_bytes: int, label: str) -> tuple[str, bool]:
    """Return (capped_text, was_truncated). Cap `s` at `max_bytes` of
    its UTF-8 encoding without splitting a multi-byte codepoint, then
    append a single-line sentinel marker so the worker sees in-band
    that the field was truncated. `label` names the field in the
    sentinel so the worker can attribute the truncation correctly.
    Empty / short strings pass through unchanged."""
    if not s:
        return (s, False)
    encoded = s.encode("utf-8")
    if len(encoded) <= max_bytes:
        return (s, False)
    # Trim to max_bytes and back off until the trailing bytes form a
    # complete UTF-8 codepoint. errors="ignore" on the final decode
    # makes this defensive against the rare case where the back-off
    # logic still lands inside a continuation.
    truncated = encoded[:max_bytes].decode("utf-8", errors="ignore")
    sentinel = (f"\n... [{label} truncated at ~{max_bytes // 1000} KB; "
                "remainder omitted — rely on the commit log] ...")
    return (truncated + sentinel, True)


# Matches "pila:" at the very start of a string, case-insensitive, with
# any whitespace that follows. Anchored so it can't fire mid-string
# (does not false-positive on "pilates", "pila is great", etc.).
_PILA_PREFIX_RE = re.compile(r"^pila:\s*", re.IGNORECASE)


def _strip_pila_prefix(title: str) -> str:
    """Strip a leading `pila:` from a worker-emitted PR title so the
    launcher's unconditional `pila: ` prepend cannot produce
    `pila: pila: ...`.

    The pr_writer prompt tells the worker not to emit the prefix, but
    DESIGN §12 *prompts are advisory, code enforces* — a guarantee
    that matters and can be checked mechanically must live in code.
    Without this guard, a single drift produces a user-visible defect
    on every PR until the prompt is patched."""
    return _PILA_PREFIX_RE.sub("", title)


def _truncate_diff_sample(diff_text: str, max_lines: int) -> tuple[str, bool]:
    """Return (truncated_text, was_truncated). Splits on newlines and
    keeps the first `max_lines`, appending a sentinel line when truncated
    so the worker can see in-band that the sample is incomplete.

    Line-based (not byte-based) because individual diff lines can be
    long and breaking one mid-line would render the surrounding hunk
    unreadable. Byte budgets for other fields go through `_cap_text`."""
    lines = diff_text.splitlines()
    if len(lines) <= max_lines:
        return (diff_text, False)
    kept = lines[:max_lines]
    kept.append(f"... [diff sample truncated at {max_lines} lines; "
                "remaining hunks omitted — rely on the commit log] ...")
    return ("\n".join(kept), True)


async def _compose_pr_via_llm(st: "State",
                              caps: dict,
                              models: dict[str, str],
                              efforts: dict[str, str | None],
                              repo_root: Path,
                              pr_template_override: str | None) -> None:
    """Run the pr_writer worker and persist its title/body to run.json.

    DESIGN §6 *Finalization*: the worker runs *inside* the orchestrator
    container (where `claude -p` is available) and writes its output to
    run.json — the existing container→host handoff channel. The host
    launcher then reads `pr_title` and `pr_body` from run.json and
    passes them to `gh pr create`.

    **Fail-open contract**: any error (subprocess failure, schema
    mismatch, timeout, git errors collecting context) is logged as a
    warning and swallowed. The launcher's bash fallback composition
    runs in that case, so a PR will still open — generating a richer
    body must never block finalize success.
    """
    try:
        # 1. Locate the template (may be None).
        tpl = find_pr_template(repo_root, pr_template_override)
        tpl_content = ""
        tpl_rel: str | None = None
        tpl_truncated = False
        if tpl is not None:
            tpl_path, tpl_rel = tpl
            try:
                raw = tpl_path.read_text()
                tpl_content, tpl_truncated = _cap_text(
                    raw, PR_WRITER_TEMPLATE_MAX_BYTES, "PR template")
            except OSError as e:
                log(f"pr_writer: failed to read template {tpl_rel}: {e} "
                    "(falling back to no-template mode)")
                tpl = None
                tpl_rel = None
        if pr_template_override and tpl_rel is None:
            log(f"pr_writer: --pr-template={pr_template_override!r} did "
                f"not match any template; using default discovery")

        # 2. Collect git context. Commits are the spine; diff is sampled.
        working_branch = st.data.get("working_branch") or "HEAD"
        run_branch = compute_run_branch(st.run_id)
        rev_range = f"{working_branch}..{run_branch}"

        async def _git(args: list[str]) -> str:
            # start_new_session=True per DESIGN §6 "Worker subtree
            # termination" — every subprocess in this module isolates
            # into its own POSIX session so cleanup can killpg without
            # signalling the orchestrator's own group. Static-enforced
            # by tests/test_signal_cleanup.py.
            proc = await asyncio.create_subprocess_exec(
                "git", "-C", str(repo_root), *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
            out, _err = await proc.communicate()
            return out.decode("utf-8", errors="replace")

        commit_log_raw = await _git([
            "log", "--no-merges", "--format=%h %s%n%b%n---", rev_range])
        commit_log, commit_log_truncated = _cap_text(
            commit_log_raw, PR_WRITER_COMMIT_LOG_MAX_BYTES, "commit log")
        diff_stat = await _git(["diff", "--stat", rev_range])
        dirstat = await _git(["diff", "--dirstat=files,5", rev_range])
        # Sample diff: cap at ~500 lines. A full diff of a 32-file run
        # easily blows the prompt budget; the commit log is the canonical
        # record. We pull the head of `git diff` (deterministic order:
        # alphabetical by path) so the sample at least covers some
        # concrete hunks instead of being a no-op stat.
        full_diff = await _git(["diff", rev_range])
        diff_sample, diff_truncated = _truncate_diff_sample(
            full_diff, PR_WRITER_DIFF_SAMPLE_MAX_LINES)

        # 3. Pull planner-written subtask titles from plan.json. The
        # planner writes its full plan there (write_plan above), and the
        # titles are the cleanest human-readable summary of each subtask's
        # intent. Empty list if plan.json is missing or malformed.
        subtask_titles: list[str] = []
        try:
            plan = json.loads(
                (st.run_dir / "plan.json").read_text())
            for sid, spec in (plan.get("subtasks") or {}).items():
                title = (spec or {}).get("title")
                if title:
                    subtask_titles.append(title)
        except (OSError, json.JSONDecodeError):
            pass

        categories = st.data.get("categories") or []
        answers = st.data.get("answers") or {}

        payload = {
            "task": st.data.get("task", ""),
            "categories": categories,
            "source_of_truth": answers.get("source_of_truth"),
            "working_branch": working_branch,
            "run_branch": run_branch,
            "wave_count": len(st.data.get("waves") or []),
            "subtask_count": sum(
                len(w) for w in (st.data.get("waves") or [])),
            "worker_count": st.data.get("worker_count"),
            "subtask_titles": subtask_titles,
            "template": (
                {"path": tpl_rel, "content": tpl_content,
                 "truncated": tpl_truncated}
                if tpl_rel else None
            ),
            "commit_log": commit_log,
            "commit_log_truncated": commit_log_truncated,
            "diff_stat": diff_stat,
            "dirstat": dirstat,
            "diff_sample": diff_sample,
            "diff_sample_truncated": diff_truncated,
        }

        sys_prompt = load_prompt("pr_writer")
        model = models.get("pr_writer", MODEL_DEFAULT_PER_WORKER.get(
            "pr_writer", MODEL_DEFAULT))
        effort = efforts.get("pr_writer")
        # Pre-check the worker budget. If the run already saturated
        # max_total_workers in execution, bump_workers() would raise
        # WorkerError — which the fail-open except below would catch
        # and log as "composition failed (WorkerError: ...)", which is
        # misleading (the worker never ran; the budget said no). Skip
        # cleanly and let the launcher's deterministic fallback run.
        wc = st.data.get("worker_count", 0)
        if wc >= caps["max_total_workers"]:
            log(f"pr_writer: skipped (worker budget exhausted at "
                f"{wc}/{caps['max_total_workers']}); deterministic "
                "fallback will run")
            return
        st.bump_workers(caps)
        result = await claude_p(
            user_prompt=json.dumps(payload, separators=(",", ":")),
            system_prompt=sys_prompt,
            schema_key="pr_writer",
            cwd=str(repo_root),
            allowed_tools=INSPECT_TOOLS,
            max_turns=20,
            autonomous=False,
            caps=caps,
            st=st,
            model=model,
            effort=effort,
            sid="pr-writer",
        )

        # Strip whitespace, then strip any leading `pila:` the worker
        # may have emitted despite the prompt telling it not to —
        # DESIGN §12 *prompts are advisory, code enforces*. The
        # launcher unconditionally prepends `pila: `, so leaving a
        # worker-emitted prefix in place would render `pila: pila: …`.
        title = _strip_pila_prefix((result.get("title") or "").strip()).strip()
        body = (result.get("body") or "").strip()
        if not title or not body:
            log("pr_writer: worker returned empty title or body; "
                "launcher will use deterministic fallback")
            return
        used = result.get("used_template")
        _write_run_json(
            st.run_dir,
            pr_title=title,
            pr_body=body,
            pr_template_used=used,
        )
        log(f"pr_writer: composed PR via {model}"
            + (f" (filled template {used})" if used else ""))
    except Exception as e:
        # Fail-open: any failure means the launcher uses its bash
        # fallback. Surface enough to debug but never raise.
        log(f"pr_writer: composition failed ({type(e).__name__}: {e}); "
            "launcher will use deterministic fallback")


async def phase_finalize(pila_dir: Path, st: State, no_push: bool,
                         no_verify: bool,
                         caps: dict | None = None,
                         models: dict[str, str] | None = None,
                         efforts: dict[str, str | None] | None = None,
                         pr_template_override: str | None = None) -> None:
    """Phase 6: verify the run branch and record finalize state.

    The push + PR step has moved to the host launcher (DESIGN §6
    *Finalization*); this phase no longer makes network calls. It runs
    `finalize.sh` to verify the run branch is non-empty, runs
    `cleanup.sh` to drop subtask branches, writes `finished_at` to
    state.json + run.json, and exits. The launcher polls run.json's
    `finished_at` sentinel and does `git push` + `gh pr create` on the
    host using the host's own auth state.

    `no_verify` is passed through into the run.json sidecar so the
    launcher knows whether to add `--no-verify` to its `git push`.

    When `caps`, `models`, and `efforts` are provided and `no_push` is
    False, the `pr_writer` worker runs after `finished_at` is recorded
    to compose an LLM-written title + body that respects any
    PR template in the target repo. Output lands in run.json's
    `pr_title` / `pr_body` / `pr_template_used` fields; the launcher
    reads these and falls back to the deterministic `compose_pr_body`
    shape if they are missing. The args default to None so legacy
    call sites (and tests) keep working with the old signature.
    """
    log("phase 6: finalizing")
    st.data["current_phase"] = "phase 6: finalize"
    st.save()
    proc = await run_script("finalize.sh", st.run_id)
    if proc.returncode != 0:
        die(f"finalize failed (run branch is intact): {proc.stderr.strip()}")
    await run_script("cleanup.sh", "--run-id", st.run_id, "--subtask-branches")

    wc = st.data.get("worker_count", 0)
    nsub = len(st.data.get("subtask_status", {}))
    tel = st.data.get("telemetry", {})
    st.data["finished_at"] = now()
    st.save()
    # Record finalize success in the run.json sidecar. The launcher
    # uses `finished_at` as the "ready for push" sentinel; `no_push`
    # and `no_verify` propagate intent the launcher needs.
    _write_run_json(
        st.run_dir,
        finished_at=st.data["finished_at"],
        no_push=no_push,
        no_verify=no_verify,
    )

    # LLM-composed PR title/body. Runs only when push will happen and
    # the caller threaded models/efforts/caps through. Fail-open: any
    # error is swallowed and the launcher uses its bash fallback.
    if not no_push and caps is not None and models is not None and efforts is not None:
        await _compose_pr_via_llm(
            st, caps, models, efforts,
            repo_root=Path(os.getcwd()),
            pr_template_override=pr_template_override,
        )

    if no_push:
        log(f"skipped push and PR (--no-push); the run branch "
            f"{compute_run_branch(st.run_id)} is local-only; "
            "your working branch is unchanged")
    else:
        log(f"work is on {compute_run_branch(st.run_id)}; the host "
            "launcher will push and open the PR after this container exits")

    pr_url = None  # the launcher writes pr_url to run.json after gh pr create
    pr_suffix = ""
    log(f"done — {nsub} subtasks, {len(st.data['waves'])} waves, "
        f"{wc} worker invocations.{pr_suffix} Work is on "
        f"{compute_run_branch(st.run_id)}; working branch unchanged.")
    if tel:
        log(f"run weight: {tel.get('calls', 0)} claude -p calls, "
            f"{tel.get('input_tokens', 0):,} in / "
            f"{tel.get('output_tokens', 0):,} out tokens "
            f"(see {st.path})")


# =========================================================================
# entry point
# =========================================================================
async def orchestrate(args, caps: dict, pila_dir: Path, st: State,
                      sot_pref: str, verbosity: str,
                      models: dict[str, str],
                      efforts: dict[str, str | None]) -> None:
    """The async portion of a run: every phase that spawns a `claude -p`
    worker. main() handles sync setup, then drives this with `asyncio.run`."""
    # Memory telemetry: a long-running coroutine that snapshots RSS / phase /
    # worker count / open FDs / thread count into memory.ndjson every 30s
    # so we can distinguish "natural heavy run" from "real orchestrator leak"
    # after the fact. Lifecycle is bounded by this function — cancelled in
    # the finally so it never outlives the run.
    sampler_task = asyncio.create_task(_memory_sampler(st))
    try:
        await _run_phases(args, caps, pila_dir, st, sot_pref, verbosity,
                          models, efforts)
    finally:
        sampler_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await sampler_task


async def _run_phases(args, caps: dict, pila_dir: Path, st: State,
                      sot_pref: str, verbosity: str,
                      models: dict[str, str],
                      efforts: dict[str, str | None]) -> None:
    """The phase sequence of one run. Split out from `orchestrate()`
    so the latter can wrap it with the memory-sampler try/finally
    without burying the phase calls behind extra indentation. Source-
    text coupling tests for the orchestrate call-sites parse this
    function's body — keep all phase calls here."""
    if args.resume:
        if not st.load():
            die(f"nothing to resume — no state.json at {st.path}")
        validate_resume_state(st.data)
        task = st.data["task"]
        log(f"resuming: {task!r} (worker count {st.data.get('worker_count', 0)})")
        log(f"per-worker logs: {st.run_dir / 'logs'}/")
        if "waves" not in st.data:
            die("cannot resume — run did not reach the scheduling phase")
        # Refresh the preferences in case env vars or pila.toml
        # changed since the original run started. Verbosity is
        # resolved fresh every run — the user can dial up or down on
        # resume without editing state.json.
        st.data["source_of_truth_pref"] = sot_pref
        st.data["verbosity"] = verbosity
        st.data["inspect_dirs"] = list(getattr(args, "inspect_dirs", []) or [])
        st.data["clarify"] = bool(args.clarify)
        st.data["dangerously_skip_permissions"] = bool(
            args.dangerously_skip_permissions)
        st.save()
        # Absorb --answers on resume too. The documented user flow for
        # a non-interactive deferred-question exit (Phase-1 or §11
        # mid-execution) is: get a pending-*.json, write an answers
        # file, re-run with --resume --answers <file>. Without this
        # call the answers file was silently dropped — the re-spawned
        # worker would re-ask the same question forever. See P5-1.
        absorb_supplied_answers(args, st, pila_dir)
        # Re-export the mise override env var if the original run
        # synthesized one. phase_provision (which set it on os.environ
        # the first time) is skipped on resume, but downstream
        # implementer/conformer subprocesses still need it to find the
        # synthesized go pin.
        override = (st.data.get("provision") or {}).get("override_file")
        if override:
            os.environ["MISE_OVERRIDE_CONFIG_FILENAMES"] = str(override)
    else:
        if not args.task:
            die("a task description is required (or use --resume)")
        task = resolve_task_argument(args.task)
        st.data = {"task": task, "started_at": now(), "worker_count": 0,
                   "source_of_truth_pref": sot_pref,
                   "verbosity": verbosity,
                   "inspect_dirs": list(getattr(args, "inspect_dirs", []) or []),
                   "clarify": bool(args.clarify),
                   "dangerously_skip_permissions": bool(
                       args.dangerously_skip_permissions)}
        st.save()
        await preflight(pila_dir, verbosity=verbosity,
                        skip_smoke=args.skip_smoke,
                        no_push=getattr(args, "no_push", False))
        supplied = (json.loads(Path(args.answers).read_text())
                    if args.answers else None)
        await phase_classify(task, st, caps, args.clarify, models, efforts)
        # Now that classification has chosen a category, promote the
        # bootstrap dir to its final per-run name (DESIGN §6 "The run
        # identifier"). The rename is atomic on POSIX same-filesystem;
        # state.save() opens-writes-closes per call so no long-lived
        # handle straddles it. POSIX file handles already opened inside
        # phase_classify's worker (the classifier log under logs/)
        # survive the rename because they reference inodes, not paths.
        if st.run_id.startswith("_bootstrap-"):
            final_run_id = compute_run_id(
                st.data.get("categories", []), task, st.data["started_at"])
            log(f"run id: {final_run_id}")
            st.rename_to(final_run_id)
            # All subsequent calls in this function pass the new dir;
            # phase_execute / phase_finalize internally re-derive their
            # working dir from st.path.parent, so they automatically
            # pick up the new location.
            pila_dir = st.run_dir
            # Initialize run.json with the immutable run-identity fields
            # (run_id, branch, working_branch, started_at, task) so
            # `pila --list` can enumerate this run from the moment
            # it has a stable identity — not only after finalize.
            # working_branch is HEAD-at-classify-time; setup-run.sh
            # records the same value to .pila/runs/<id>/working-branch
            # later, but we capture it here so a run that fails
            # before phase_execute still has a recoverable run.json.
            head_proc = await run_proc(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"])
            working_branch = (head_proc.stdout.strip()
                              if head_proc.returncode == 0 else "")
            _write_run_json(
                st.run_dir,
                run_id=final_run_id,
                branch=compute_run_branch(final_run_id),
                working_branch=working_branch,
                started_at=st.data["started_at"],
                task=task,
            )
        # Provision per-repo deps (DESIGN §6½). Runs after classify (so a
        # docs-only run can short-circuit) and after the run-id rename
        # (so state writes go to the final run dir). On `--resume` the
        # entire else-branch is skipped, so phase_provision never re-fires.
        await phase_provision(Path(os.getcwd()), st, caps, models, efforts)
        # gather_answers blocks on input(). That's fine here: no concurrent
        # tasks are scheduled yet, so blocking the loop blocks nothing. Kept
        # on the event loop deliberately — every State mutation runs on the
        # loop, which is why the lock-free State works.
        gather_answers(st, supplied)
        plans = await phase_plan(task, st, caps, models, efforts)
        # Bridge cross-domain capability-tag mismatches before the
        # scheduler builds its DAG. Short-circuits with no worker call
        # when planners agreed on vocabulary (the common case).
        plans = await phase_reconcile(plans, task, st, caps, models, efforts)
        # Surface cross-planner file-claim overlaps. Warning only — the
        # reconciler handles capability-tag drift but not file-claim
        # conflicts (yet); empirically these correlate strongly with
        # integrator design-conflict crashes downstream.
        warn_cross_planner_file_overlap(plans)
        # Drop subtasks whose files_likely_touched leak into inspect-dir
        # mounts (read-only) or other off-tree paths. Soft drop so the
        # surviving subtasks proceed; the drop is recorded in
        # state.data["dropped_subtasks"] for audit. Must run BEFORE
        # schedule() so the resulting waves do not reference dropped sids.
        filter_offtree_subtasks(plans, Path(os.getcwd()),
                                st.data.get("inspect_dirs") or [], st)
        st.data["current_phase"] = "phase 3: scheduling"
        st.save()
        subtasks, waves = schedule(plans)
        validate_plan(subtasks)
        write_plan(pila_dir, task, st, subtasks, waves)

    await phase_execute(pila_dir, st, caps, models, efforts)
    await phase_finalize(pila_dir, st,
                        no_push=getattr(args, "no_push", False),
                        no_verify=getattr(args, "no_verify", False),
                        caps=caps, models=models, efforts=efforts,
                        pr_template_override=getattr(
                            args, "pr_template", None))


def main() -> None:
    ap = argparse.ArgumentParser(prog="pila", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version",
                    version=f"pila {_read_version()}",
                    help="print the pila version and exit")
    ap.add_argument("task", nargs="?",
                    help="the task to execute (literal string, or path to "
                         "a .txt/.md file whose contents are the task)")
    ap.add_argument("--resume", action="store_true",
                    help="resume an interrupted run (auto-picks if exactly "
                         "one run exists under .pila/runs/). "
                         "Default: off (start a new run)")
    ap.add_argument("--run-id", metavar="ID",
                    help="select a specific run by id (for --resume when "
                         "multiple runs are in flight). See `--list` to "
                         "enumerate.")
    ap.add_argument("--list", action="store_true", dest="list_runs",
                    help="enumerate in-flight and completed runs in this "
                         "repository (run id, started, status, branch). "
                         "Exits without running orchestrate. Default: off")
    ap.add_argument("--list-paused", action="store_true", dest="list_paused",
                    help="like --list but filters to paused remote runs "
                         "(DESIGN §6 Remote pause-on-failure). Exits "
                         "without running orchestrate. Default: off")
    ap.add_argument("--answers", metavar="FILE",
                    help="JSON file of pre-supplied clarification answers")
    ap.add_argument("--clarify", action="store_true",
                    help="opt into surfacing intent questions to the user "
                         "(DESIGN §11). Without this flag (the default), the "
                         "classifier's filter still runs but surviving "
                         "questions are dropped and the implementer makes a "
                         f"best-effort decision. Also {CLARIFY_ENV} env var "
                         "or clarify=true in pila.toml.")
    ap.add_argument("--no-push", action="store_true",
                    help="skip the push and PR step at finalize. The run "
                         "completes with the run branch local-only; your "
                         "working branch is unchanged. Default: off (push "
                         f"and PR happen). Also {NO_PUSH_ENV} env var or "
                         "no_push in pila.toml.")
    ap.add_argument("--no-verify", action="store_true",
                    help="pass --no-verify to the finalize `git push` "
                         "(skips pre-push hooks). Worker commits inside "
                         "worktrees still run all hooks. Default: off "
                         "(hooks run). CLI flag only (no env/TOML mirror — "
                         "matches CLAUDE.md's explicit-user-request "
                         "principle for hook-skipping).")
    ap.add_argument("--pr-template", metavar="NAME",
                    help="when the target repo has multiple PR templates "
                         "in PULL_REQUEST_TEMPLATE/, pick this one by "
                         "basename (with or without .md). No effect "
                         "when the repo has a single top-level template "
                         "or none at all. Default: alphabetically first "
                         ".md. Also "
                         f"{PR_TEMPLATE_ENV} env var or pr_template in "
                         "pila.toml.")
    ap.add_argument("--dangerously-skip-permissions", action="store_true",
                    help="DANGEROUS: pass --dangerously-skip-permissions "
                         "to EVERY claude -p worker — including the "
                         "judgment workers (classifier, planner, "
                         "reconciler, provision) that run in the real "
                         "repo cwd, not an isolated worktree. Waives "
                         "the DESIGN §12 mechanical enforcement that "
                         "they stay read-only. Use only on repos you "
                         "would run `claude --dangerously-skip-permissions` "
                         "against directly. Default: off (judgment "
                         "workers narrow-allowlisted). Also "
                         f"{DANGEROUS_SKIP_PERMS_ENV} env var or "
                         "dangerously_skip_permissions=true in pila.toml.")
    ap.add_argument("--max-workers", type=_positive_int, metavar="N",
                    help=f"total worker-invocation budget "
                         f"(default {DEFAULT_CAPS['max_total_workers']}); "
                         f"also {MAX_WORKERS_ENV} and max_workers in "
                         "pila.toml")
    ap.add_argument("--max-parallel", type=int,
                    help=f"override concurrent workers per wave "
                         f"(default {DEFAULT_CAPS['max_parallel']})")
    ap.add_argument("--confidence-rounds", type=_positive_int, metavar="N",
                    help=f"how many evidence-gate rounds each planner / "
                         f"implementer may run before exiting blocked "
                         f"(default {DEFAULT_CAPS['confidence_rounds']}); "
                         f"also {CONFIDENCE_ROUNDS_ENV} and "
                         f"confidence_rounds in pila.toml")
    ap.add_argument("--worker-memory-max", metavar="SIZE",
                    help="per-worker cgroup memory cap (e.g. '4G', "
                         "'512M', '1024'). Bounds RAM available to each "
                         "claude -p worker subtree; an OOM stays inside "
                         "the worker cgroup rather than cascading to "
                         "sshd / orchestrator. Auto-derived from "
                         "/proc/meminfo when unset. Also "
                         f"{WORKER_MEMORY_MAX_ENV} env var or "
                         "worker_memory_max in pila.toml")
    ap.add_argument("--skip-smoke", action="store_true",
                    help="skip the live claude -p smoke test during preflight. "
                         "Default: off (smoke test runs)")
    ap.add_argument("--source-of-truth", choices=SOURCE_OF_TRUTH_VALUES,
                    metavar="VALUE",
                    help=f"source-of-truth preference "
                         f"({'|'.join(SOURCE_OF_TRUTH_VALUES)}, default both); "
                         f"overrides {SOURCE_OF_TRUTH_ENV} and pila.toml")
    ap.add_argument("--runtime", choices=RUNTIME_VALUES,
                    metavar="MODE",
                    help=f"execution runtime "
                         f"({'|'.join(RUNTIME_VALUES)}, default local); "
                         f"overrides {RUNTIME_ENV} and pila.toml")
    ap.add_argument("--inspect-dir", action="append", metavar="PATH",
                    dest="inspect_dir",
                    help="extra directory the inspect-bucket workers "
                         "(classifier, planner, reconciler, provision) may read. "
                         "Forwarded to `claude -p` as --add-dir. Repeatable. "
                         "Use for sibling repos referenced in the task that "
                         "live outside the current repo cwd. Default: none. "
                         f"Also {INSPECT_DIRS_ENV} (colon-separated) or "
                         "inspect_dirs in pila.toml (comma-separated).")
    ap.add_argument("--model", choices=MODEL_VALUES, metavar="ALIAS",
                    help=f"model alias for all workers "
                         f"({'|'.join(MODEL_VALUES)}); no global default — "
                         f"without an override, judgment workers default to "
                         f"{MODEL_DEFAULT} and the acting workers "
                         f"(implementer, conformer) default to "
                         f"{MODEL_DEFAULT_PER_WORKER['implementer']} "
                         "(IMPLEMENTATION.md §2). Per-worker "
                         "--model-<worker> flags override this, as do "
                         "PILA_MODEL[_*] env vars and pila.toml")
    for _w in WORKER_TYPES:
        _w_default = MODEL_DEFAULT_PER_WORKER.get(_w, MODEL_DEFAULT)
        ap.add_argument(f"--model-{_w}", choices=MODEL_VALUES, metavar="ALIAS",
                        help=f"model alias for the {_w} worker "
                             f"(default {_w_default}) — overrides "
                             f"--model, PILA_MODEL, and pila.toml")
    # Effort selection — see IMPLEMENTATION.md §2 "Effort selection".
    # Same shape as --model: a global --effort plus per-worker --effort-<W>
    # overrides. Acting workers (implementer, conformer) have no per-worker
    # default, so without an override they get no --effort flag at all and
    # inherit Claude's default — the previous behavior.
    _judgment_workers = ", ".join(sorted(EFFORT_DEFAULT_PER_WORKER))
    ap.add_argument("--effort", choices=EFFORT_VALUES, metavar="LEVEL",
                    help=f"reasoning-depth dial for all workers "
                         f"({'|'.join(EFFORT_VALUES)}); judgment workers "
                         f"({_judgment_workers}) default to "
                         f"{EFFORT_DEFAULT_PER_WORKER['planner']}, acting "
                         "workers (implementer, conformer) default to unset "
                         "(IMPLEMENTATION.md §2). Per-worker --effort-<worker> "
                         "flags override this, as do PILA_EFFORT[_*] env vars "
                         "and pila.toml")
    for _w in WORKER_TYPES:
        _e_default = EFFORT_DEFAULT_PER_WORKER.get(_w, "unset")
        ap.add_argument(f"--effort-{_w}", choices=EFFORT_VALUES, metavar="LEVEL",
                        help=f"reasoning depth for the {_w} worker "
                             f"(default {_e_default}) — overrides "
                             f"--effort, PILA_EFFORT, and pila.toml")
    ap.add_argument("--judge-model", choices=MODEL_VALUES, metavar="ALIAS",
                    help=f"model alias for the judge post-run worker "
                         f"(default {MODEL_DEFAULT_PER_WORKER['judge']}); "
                         f"also {MODEL_JUDGE_ENV} or model_judge in pila.toml")
    ap.add_argument("--heal-model", choices=MODEL_VALUES, metavar="ALIAS",
                    help=f"model alias for the heal post-run worker "
                         f"(default {MODEL_DEFAULT_PER_WORKER['heal']}); "
                         f"also {MODEL_HEAL_ENV} or model_heal in pila.toml")
    ap.add_argument("--pr-writer-model", choices=MODEL_VALUES, metavar="ALIAS",
                    help=f"model alias for the pr_writer finalize worker "
                         f"(default {MODEL_DEFAULT_PER_WORKER['pr_writer']}); "
                         f"also {MODEL_PR_WRITER_ENV} or model_pr_writer "
                         f"in pila.toml")
    ap.add_argument("--heal-max-rounds", type=int, metavar="N",
                    help=f"maximum heal-loop iterations per call_type "
                         f"(default {HEAL_MAX_ROUNDS_DEFAULT}); "
                         f"also {HEAL_MAX_ROUNDS_ENV} or heal_max_rounds in pila.toml")
    ap.add_argument("--heal-success-threshold", type=float, metavar="RATE",
                    help=f"pass-rate threshold for heal-loop SUCCESS verdict "
                         f"(default {HEAL_SUCCESS_THRESHOLD_DEFAULT}); "
                         f"also {HEAL_SUCCESS_THRESHOLD_ENV} or "
                         "heal_success_threshold in pila.toml")
    # Verbosity: explicit --verbosity wins; -v/-q stackable shortcuts
    # anchor to `normal` (the pre-streaming behavior). So `-v` = stream,
    # `-vv` = debug, `-q` = normal, `-qq` = quiet. See IMPLEMENTATION.md
    # §2 "Verbosity". When none are given, resolve_verbosity falls
    # through to env / TOML / VERBOSITY_DEFAULT.
    ap.add_argument("--verbosity", choices=VERBOSITY_VALUES, metavar="LEVEL",
                    help=f"output verbosity ({'/'.join(VERBOSITY_VALUES)}, "
                         f"default {VERBOSITY_DEFAULT}); overrides "
                         f"{VERBOSITY_ENV} and pila.toml")
    ap.add_argument("-v", "--verbose", action="count", default=0,
                    help="shortcut: -v=stream, -vv=debug. Default: 0 "
                         "(no -v; falls through to --verbosity)")
    ap.add_argument("-q", "--quiet", action="count", default=0,
                    help="shortcut: -q=normal (pre-streaming behavior), "
                         "-qq=quiet (errors and phase boundaries only). "
                         "Default: 0 (no -q; falls through to --verbosity)")
    # Telemetry knobs. --telemetry / --no-telemetry are a mutually exclusive
    # pair; default None means "neither was passed" so the resolver falls
    # through to env / TOML / TELEMETRY_DEFAULT.
    _tel_grp = ap.add_mutually_exclusive_group()
    _tel_grp.add_argument("--telemetry", dest="telemetry",
                          action="store_true", default=None,
                          help=f"enable telemetry (default on); also "
                               f"{TELEMETRY_ENV}=1 or telemetry=true in "
                               "pila.toml")
    _tel_grp.add_argument("--no-telemetry", dest="telemetry",
                          action="store_false",
                          help=f"disable telemetry event writing "
                               f"(default: telemetry is on); also "
                               f"{TELEMETRY_ENV}=0 or telemetry=false in "
                               "pila.toml")
    ap.add_argument("--telemetry-dir", metavar="DIR",
                    help=f"subdirectory name under the run dir for telemetry "
                         f"NDJSON events (default '{TELEMETRY_SUBDIR_DEFAULT}'); "
                         f"also {TELEMETRY_SUBDIR_ENV} or telemetry_dir in "
                         "pila.toml")
    ap.add_argument("--judge-dir", metavar="DIR",
                    help=f"subdirectory name under the run dir for LLM judge "
                         f"output (default '{JUDGE_DIR_DEFAULT}'); also "
                         f"{JUDGE_DIR_ENV} or judge_dir in pila.toml")
    ap.add_argument("--heal-dir", metavar="DIR",
                    help=f"subdirectory name under the run dir for LLM self-heal "
                         f"output (default '{HEAL_DIR_DEFAULT}'); also "
                         f"{HEAL_DIR_ENV} or heal_dir in pila.toml")
    ap.add_argument("--phase", choices=["judge", "heal"], metavar="PHASE",
                    help="run a post-run skill phase against an existing run's "
                         "captured LLM calls instead of starting a new run. "
                         "PHASE must be 'judge' or 'heal'. Requires an existing "
                         "run (use --run-id to select one when multiple runs "
                         "exist, or omit when exactly one run is in flight). "
                         "'judge' scores every captured call in calls.ndjson "
                         "using the 3-dimensional LLM judge rubric and writes "
                         "verdict files to <run-dir>/<judge-dir>/. "
                         "'heal' reads the judge index for failing call_types "
                         "and runs the self-heal loop for each, writing healing "
                         "reports to <run-dir>/<heal-dir>/.")
    args = ap.parse_args()

    # --list / --list-paused short-circuit everything else: read
    # .pila/runs/* and exit. No git/CLI checks needed; the user might
    # be inspecting runs from outside a git repo.
    if args.list_runs:
        pila_root = Path(".pila").resolve()
        list_runs(pila_root)
        return
    if args.list_paused:
        pila_root = Path(".pila").resolve()
        list_paused_runs(pila_root)
        return

    if not shutil.which("claude"):
        die("`claude` CLI not found on PATH. Install Claude Code (native, "
            "recommended): `curl -fsSL https://claude.ai/install.sh | bash`. "
            "Docs: https://docs.claude.com/en/docs/claude-code/setup")
    # The cwd-is-git-repo check moved to the host launcher (DESIGN §6).
    # If the launcher started us, we're already in a git repo by then.

    caps = dict(DEFAULT_CAPS)
    # Resolve max_total_workers across CLI / env / TOML / default. The
    # resolver die()s on a bad env or TOML value; argparse already rejected
    # a bad --max-workers via _positive_int.
    caps["max_total_workers"] = resolve_max_workers(
        Path(os.getcwd()), args.max_workers)
    if args.max_parallel:
        caps["max_parallel"] = args.max_parallel
    # Resolve confidence_rounds across CLI / env / TOML / default. The
    # resolver die()s on a bad env or TOML value; argparse already rejected
    # a bad --confidence-rounds via _positive_int.
    caps["confidence_rounds"] = resolve_confidence_rounds(
        Path(os.getcwd()), args.confidence_rounds)
    # Resolve per-worker cgroup memory cap. Auto-derives from
    # /proc/meminfo when unset; resolver die()s on a bad size string.
    # Reads `caps["max_parallel"]` already resolved above so the auto-
    # derived value is "VM ram split N+1 ways, capped at 4 GiB".
    caps["worker_memory_max_bytes"] = resolve_worker_memory_max(
        Path(os.getcwd()), caps["max_parallel"], args.worker_memory_max)

    # Resolve verbosity. Explicit --verbosity wins; else -v/-q
    # shortcuts (anchored to `normal`); else env / TOML / default.
    # See verbosity_from_shortcuts() for the shortcut-mapping rationale.
    verbosity = (args.verbosity
                 or verbosity_from_shortcuts(args.verbose, args.quiet)
                 or resolve_verbosity(Path(os.getcwd()), None))

    # The on-disk layout is per-run: every run gets its own subdirectory
    # `pila_root/runs/<run-id>/` (see DESIGN.md §6, §10). For a fresh
    # run we don't know the final run_id until phase_classify has chosen
    # a category, so state lives in `_bootstrap-<6hex>/` until then; the
    # rename to the final run_id happens in orchestrate() after classify.
    pila_root = Path(".pila").resolve()
    pila_root.mkdir(parents=True, exist_ok=True)
    (pila_root / "runs").mkdir(parents=True, exist_ok=True)
    if args.resume:
        # Auto-pick if exactly one run exists; die with the available list
        # if multiple are in flight unless --run-id picks one explicitly.
        run_id = resolve_run_id(pila_root, args.run_id)
    else:
        # Bootstrap directory: keyed on the current wall-clock time so two
        # concurrent invocations don't pick the same one. Renamed to the
        # final `<short_category>-<slug>-<6hex>` after classify.
        run_id = "_bootstrap-" + hashlib.sha1(now().encode()).hexdigest()[:6]
    st = State(pila_root, run_id)
    for sub in ("", "subtasks", "criteria", "checkpoints", "logs"):
        (st.run_dir / sub).mkdir(parents=True, exist_ok=True)

    # Resolve source-of-truth and per-worker model preferences once per run.
    # Both die() on a bad value so typos in pila.toml or env vars are
    # caught at startup, not mid-planner. argparse already rejected any bad
    # --source-of-truth / --model[-*] before we got here.
    repo_root = Path(os.getcwd())
    sot_pref = resolve_source_of_truth(repo_root, args.source_of_truth)
    args.runtime = resolve_runtime(repo_root, args.runtime)
    models = resolve_models(repo_root, args)
    log(f"models: " + ", ".join(f"{w}={models[w]}" for w in WORKER_TYPES))
    efforts = resolve_efforts(repo_root, args)
    # Log only workers with a resolved effort — an "unset" worker is
    # explicitly opting out of the --effort flag and showing it as
    # "effort=None" in the log would be noise.
    _e_pairs = [f"{w}={efforts[w]}" for w in WORKER_TYPES
                if efforts[w] is not None]
    if _e_pairs:
        log("efforts: " + ", ".join(_e_pairs))

    # Resolve --no-push: CLI flag → PILA_NO_PUSH env → no_push in
    # pila.toml → False. Re-attach to args so orchestrate() /
    # preflight() / phase_finalize() see the resolved value uniformly via
    # `args.no_push` regardless of where the choice came from.
    args.no_push = resolve_no_push(repo_root, args.no_push)

    # Resolve --clarify with the same shape as --no-push (DESIGN §11).
    # Re-attach to args so orchestrate() folds it into state.json under
    # the canonical "clarify" key.
    args.clarify = resolve_clarify(repo_root, args.clarify)

    # Resolve --dangerously-skip-permissions (DESIGN §12 escape hatch).
    # Same precedence shape as --no-push / --clarify. Re-attach to args
    # so orchestrate() folds it into state.json under the canonical
    # "dangerously_skip_permissions" key; claude_p reads it from there
    # on every invocation instead of threading another parameter.
    args.dangerously_skip_permissions = resolve_dangerously_skip_permissions(
        repo_root, args.dangerously_skip_permissions)
    if args.dangerously_skip_permissions:
        log("dangerously-skip-permissions: ON "
            "(judgment workers run with prompts disabled — "
            "§12 enforcement waived)")

    # Resolve --pr-template: free-form string (no enum). Re-attach to
    # args so phase_finalize sees the resolved value via
    # `args.pr_template`. None means "alphabetically first .md in
    # PULL_REQUEST_TEMPLATE/" (the discovery helper's default).
    args.pr_template = resolve_pr_template(
        repo_root, getattr(args, "pr_template", None))

    # Resolve --inspect-dir: CLI flags (repeatable) → PILA_INSPECT_DIRS
    # env (colon-separated) → inspect_dirs in pila.toml (comma-separated)
    # → []. Re-attached to args so orchestrate() can fold it into state.
    args.inspect_dirs = resolve_inspect_dirs(
        repo_root, getattr(args, "inspect_dir", None))

    # Resolve telemetry knobs. Re-attached to args so orchestrate() and any
    # telemetry writer can read them without re-resolving.
    args.telemetry = resolve_telemetry_enabled(repo_root, args.telemetry)
    args.telemetry_subdir = resolve_telemetry_subdir(
        repo_root, args.telemetry_dir)
    args.judge_dir = resolve_judge_dir(repo_root, args.judge_dir)
    args.heal_dir = resolve_heal_dir(repo_root, args.heal_dir)
    args.heal_max_rounds = resolve_heal_max_rounds(
        repo_root, getattr(args, "heal_max_rounds", None))
    args.heal_success_threshold = resolve_heal_success_threshold(
        repo_root, getattr(args, "heal_success_threshold", None))

    # --phase judge|heal: post-run skill phases. Short-circuit the normal
    # orchestrate() flow — just pick an existing run and run the skill.
    if args.phase:
        phase_run_id = resolve_run_id(pila_root, args.run_id)
        phase_st = State(pila_root, phase_run_id)
        if not phase_st.load():
            die(f"no state.json found for run {phase_run_id!r}; "
                f"the run may not have reached the execute phase yet")
        # Refresh the escape-hatch preference so a user invoking
        # `--phase judge|heal --dangerously-skip-permissions` gets the
        # flag flowed into the judge/patch_generator workers — without
        # this, claude_p reads the value the original run persisted and
        # the visible startup log would lie about whether the workers
        # actually see the override.
        phase_st.data["dangerously_skip_permissions"] = bool(
            args.dangerously_skip_permissions)
        phase_st.save()
        phase_run_dir = phase_st.run_dir
        judge_out_dir = phase_run_dir / args.judge_dir
        heal_out_dir = phase_run_dir / args.heal_dir
        if args.phase == "judge":
            asyncio.run(phase_judge(phase_run_dir, judge_out_dir, caps,
                                    phase_st, models, efforts))
        else:  # heal
            # Read judge INDEX.json to find failing call_types; if no index
            # exists yet, run phase_judge first so heal has verdicts to
            # act on.
            index_path = judge_out_dir / "INDEX.json"
            if not index_path.exists():
                log("--phase heal: no judge INDEX.json found; running judge first")
                asyncio.run(phase_judge(phase_run_dir, judge_out_dir, caps,
                                        phase_st, models))
            if index_path.exists():
                try:
                    index = json.loads(index_path.read_text())
                except (OSError, ValueError) as e:
                    die(f"--phase heal: could not read {index_path}: {e}")
            else:
                index = []
            # Collect failing call_ids grouped by call_type.
            failing_by_type: dict[str, list[str]] = {}
            for entry in index:
                if not entry.get("passed", True):
                    ct = entry.get("call_type", "unknown")
                    failing_by_type.setdefault(ct, []).append(
                        entry.get("call_id", ""))
            if not failing_by_type:
                log("--phase heal: no failing captures in judge index; nothing to heal")
                return
            # Load the original capture records from calls.ndjson.
            capture_path = phase_run_dir / "calls.ndjson"
            all_captures: dict[str, dict] = {}
            if capture_path.exists():
                for line in capture_path.read_text().splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                        all_captures[rec.get("call_id", "")] = rec
                    except (ValueError, AttributeError):
                        pass
            for call_type, failing_ids in sorted(failing_by_type.items()):
                failing_records = [all_captures[cid] for cid in failing_ids
                                   if cid in all_captures]
                if not failing_records:
                    log(f"--phase heal: {call_type}: no capture records found; skipping")
                    continue
                config = {
                    "max_iterations": args.heal_max_rounds,
                    "success_threshold": args.heal_success_threshold,
                }
                asyncio.run(phase_heal(call_type, failing_records, heal_out_dir,
                                       caps, phase_st, models, efforts,
                                       config=config))
        return

    # Signal handlers (DESIGN §6 / DESIGN §14): SIGTERM and SIGHUP raise
    # InterruptedBySignal so the same try/except machinery that catches
    # KeyboardInterrupt also handles process-level termination. The
    # cleanup logic chooses full purge vs. worktrees-only based on which
    # signal/exception fired.
    _install_signal_handlers()

    abnormal = False
    full_purge = False
    exit_code = 0
    exit_message: str | None = None
    try:
        asyncio.run(orchestrate(args, caps, st.run_dir, st,
                                sot_pref, verbosity, models, efforts))
    except WorkerError as e:
        abnormal = True
        full_purge = False
        st.save()
        exit_message = str(e)
        exit_code = 1
    except RateLimitedExit as e:
        # Claude Code session-limit / rate-limit hit mid-worker. See
        # DESIGN §6 *Cleanup on abnormal exit*. Two paths:
        #   - reset_at parsed cleanly → run worktree-only cleanup,
        #     sleep until the moment + 30s margin, then os.execvp the
        #     launcher with --resume for a fresh orchestrator process.
        #     The `worker_count` cap is NOT reset — it persists in
        #     state.json so a run repeatedly re-exec'ing through the
        #     rate-limit still respects the user's --max-workers cap.
        #   - reset_at is None (parse failed or protocol-level event
        #     carried no time) → run cleanup, print the manual
        #     --resume instruction, exit 75 (EX_TEMPFAIL).
        # Subprocess cleanup before sleep is handled by the existing
        # asyncio cancellation chain (DESIGN §6, IMPLEMENTATION §5
        # *Abnormal exit and rate-limit contract*): _invoke's
        # BaseException guard kills the rate-limited claude -p child;
        # sibling wave-tasks cancel through gather and each kills its
        # own child.
        abnormal = True
        full_purge = False
        st.save()
        log(f"rate-limited: {e.raw_message}")
        if e.reset_at is not None:
            # Run cleanup BEFORE computing wait_seconds, so the sleep
            # duration reflects time remaining after cleanup finishes
            # — not time-from-now-at-exception. With the worktree-
            # remove timeout raised to 240s per worktree, a wave with
            # several heavy worktrees can spend tens of minutes inside
            # cleanup; computing wait_seconds first would make the
            # logged "sleeping until X" line under-promise the actual
            # wake-up time by that much. State and branches preserved
            # by full_purge=False.
            try:
                _cleanup_on_abnormal_exit(st, full_purge=False)
            except BaseException as ce:
                log(f"  cleanup before sleep failed (non-fatal): {ce}")
            # Skip the finally-block cleanup — we just did it.
            abnormal = False
            wait_seconds = max(
                0,
                int((e.reset_at - datetime.now(e.reset_at.tzinfo))
                    .total_seconds())) + 30
            log(f"  sleeping until {e.reset_at.isoformat()} "
                f"(~{wait_seconds}s) then auto-resuming")
            interrupted_during_sleep = False
            try:
                time.sleep(wait_seconds)
            except KeyboardInterrupt:
                # User Ctrl-C'd during the sleep — they don't want to
                # wait for the auto-resume. State + branches are
                # already preserved (cleanup ran before the sleep)
                # so a manual --resume picks up cleanly. Emit the
                # same friendly message the top-level KeyboardInterrupt
                # arm would have printed, so the user doesn't see a
                # silent exit after `sleeping until ...`. Sets the
                # SIGINT exit code (130) the way the top-level arm
                # does — `main()` returns through the normal flow.
                log("interrupted by user (SIGINT) during rate-limit "
                    f"sleep — state preserved (resume with --resume "
                    f"--run-id {st.run_id})")
                interrupted_during_sleep = True
                exit_code = 130
            if not interrupted_during_sleep:
                launcher = str(ROOT / "pila")
                log(f"  auto-resuming: exec {launcher} "
                    f"--resume --run-id {st.run_id}")
                os.execvp(launcher,
                          ["pila", "--resume", "--run-id", st.run_id])
                # Unreachable: execvp replaces the process.
        else:
            log(f"  could not parse reset time; "
                f"resume manually: pila --resume --run-id {st.run_id}")
            exit_code = 75  # EX_TEMPFAIL
    except KeyboardInterrupt:
        # Ctrl-C → worktree cleanup only; state and branches preserved
        # so the user can --resume. The explicit "throw this away"
        # gesture is `scripts/cleanup.sh --run-id <id> --branches`,
        # not Ctrl-C. asyncio.run already cancelled pending tasks and
        # _invoke's / run_proc's BaseException handlers killed
        # in-flight child processes (DESIGN §6).
        abnormal = True
        full_purge = False
        st.save()
        log("interrupted by user (SIGINT) — worktree cleanup; "
            f"state preserved (resume with --resume --run-id {st.run_id})")
        exit_code = 130
    except InterruptedBySignal as e:
        # SIGTERM / SIGHUP → external orchestration (CI cancel, systemd
        # stop, terminal close). User likely wants to recover; preserve
        # state and run branch for --resume.
        abnormal = True
        full_purge = False
        st.save()
        log(f"interrupted by signal ({e}) — worktree cleanup; "
            f"state preserved (resume with --resume --run-id {st.run_id})")
        # 128 + signal number; SIGTERM=15 → 143, SIGHUP=1 → 129.
        signum = getattr(signal, str(e), None)
        exit_code = (128 + int(signum)) if signum else 1
    except SystemExit:
        # `die()` raises SystemExit. It's the *clean* exit mechanism for
        # known failure modes (preflight gh missing, classifier produced
        # no categories, integrator design-conflict, ...). Don't treat it
        # as an unhandled exception — die() already printed the right
        # message. Mark abnormal so the finally block can clean up any
        # worktrees the run did create (no-op when none exist, e.g.
        # preflight die() before setup-run.sh ran).
        abnormal = True
        full_purge = False
        raise
    except BaseException as e:
        # Anything else (genuinely unhandled exception in orchestrate,
        # asyncio cancellation chain, etc.). Save state, mark abnormal
        # so the finally block runs cleanup, then re-raise so the user
        # sees the traceback.
        abnormal = True
        full_purge = False
        st.save()
        log(f"unhandled exception: {type(e).__name__}: {e}")
        raise
    finally:
        if abnormal:
            try:
                _cleanup_on_abnormal_exit(st, full_purge=full_purge)
            except BaseException as cleanup_err:
                # Cleanup failure is non-fatal; the user can re-run
                # `scripts/cleanup.sh --run-id <id>` manually.
                log(f"cleanup failed (non-fatal): {cleanup_err}")
    if exit_message is not None:
        die(exit_message, code=exit_code)
    if exit_code != 0:
        sys.exit(exit_code)


if __name__ == "__main__":
    main()
