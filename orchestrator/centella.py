#!/usr/bin/env python3
"""
Centella — deterministic task orchestrator for Claude Code.

Runs entirely on the Claude Code CLI / subscription. Every unit of LLM work is
a `claude -p` headless invocation. This script owns ALL control flow — phase
sequencing, wave scheduling, caps, retries, integration — in real Python, so
the orchestration cannot drift the way an LLM-driven controller can.

Each worker is a separate `claude -p` process, so there is no subagent nesting
anywhere. The script is the orchestrator; each `claude -p` call is a leaf.

Usage:
    centella "<task description>"
    centella --resume
    centella "<task>" --answers answers.json
    centella "<task>" --no-clarify          # skip clarification entirely

Run it from the root of the target git repository.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent       # centella plugin/repo root
PROMPTS = ROOT / "prompts"
SCRIPTS = ROOT / "scripts"

# Minimum `claude` CLI version that supports `--json-schema` in `claude -p`
# mode. Anthropic CHANGELOG v2.1.22 (2026-01-28): "Fixed structured outputs
# for non-interactive (-p) mode." Earlier 2.1.x point releases may work but
# have no positive evidence in the release notes; v1.x and v2.0.x do not
# have the flag at all. Enforced at preflight by _check_claude_cli_version().
MIN_CLAUDE_CLI = (2, 1, 22)

# --- tunable caps --------------------------------------------------------
DEFAULT_CAPS = {
    "max_total_workers": 40,        # hard ceiling on claude -p invocations
    "max_parallel": 4,              # concurrent workers within a wave
    # Per-subtask re-spawn budget. Consumed by BOTH context-exhaustion
    # handoffs and DESIGN §11 mid-execution clarifications — a subtask
    # that mixes the two is still bounded by this single cap, so "ask
    # instead of research" cannot win extra budget. See DESIGN §11
    # mid-execution clarification subsection.
    "subtask_continuations": 3,
    "failed_retries": 1,            # re-spawns of a failed implementer
    "wave_revalidation_rounds": 5,  # staging re-validation attempts per wave
    "worker_timeout_sec": 5400,     # 90 minutes per worker process
    # Worker-internal evidence-gate iterations for planner and implementer
    # (DESIGN §8 + §13). User-tunable via --confidence-rounds /
    # CENTELLA_CONFIDENCE_ROUNDS / centella.toml; see IMPLEMENTATION.md §2
    # "Confidence rounds". The orchestrator does not count these iterations
    # — the cap is passed into each worker's prompt and the worker bounds
    # itself. Surfacing the knob is for tuning persistence, not for
    # promoting a prompt-governed limit to a code guarantee.
    "confidence_rounds": 8,
}

# Every key the orchestrator writes to `st.data`. Canonical alongside the
# `state.json` field table in IMPLEMENTATION.md §8 — drift in either
# direction is caught by tests/test_state_fields.py.
STATE_FIELDS = (
    "task", "started_at", "finished_at",
    "waves", "completed_waves", "subtask_status",
    "criteria_locks", "criteria_revisions",
    "blocked",
    "worker_count", "telemetry",
    "categories", "classifier_questions", "answers",
    "needs_source_of_truth", "source_of_truth_pref", "no_clarify",
    "verbosity",
    "test_runner",
    "integrator_failure", "integrator_warnings", "scope_warnings",
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
    "bug-fixing": "fix",
    "refactoring": "refactor",
    "performance-optimization": "perf",
    "testing": "test",
    "dependency-migration": "deps",
    "configuration-build": "config",
    "documentation": "docs",
}

READ_TOOLS = "Read,Grep,Glob,WebSearch,WebFetch"
ACT_TOOLS = "Read,Grep,Glob,WebSearch,WebFetch,Bash,Write,Edit"
# RUN_TOOLS adds Bash to the read set so the validator can execute criteria
# (pytest, shell checks) without gaining Write/Edit. Mechanical enforcement of
# VALIDATOR_SYSTEM's "you do not modify code" rule, per DESIGN §12.
RUN_TOOLS = "Read,Grep,Glob,WebSearch,WebFetch,Bash"

EXIT_NEEDS_ANSWERS = 10   # emitted when clarification is needed but no TTY

# Source-of-truth preference — see DESIGN.md §11. Resolution order:
# --source-of-truth CLI flag → CENTELLA_SOURCE_OF_TRUTH env var →
# per-repo centella.toml → 'ask'. CLI/env are session knobs, so they
# outrank the committed file default.
SOURCE_OF_TRUTH_VALUES = ("codebase", "research", "both", "ask")
SOURCE_OF_TRUTH_ANSWERS = ("codebase", "research", "both")  # 'ask' is never an answer
SOURCE_OF_TRUTH_ENV = "CENTELLA_SOURCE_OF_TRUTH"
SOURCE_OF_TRUTH_FILE = "centella.toml"

# Confidence-rounds preference — see IMPLEMENTATION.md §2 "Confidence
# rounds". Resolution order: --confidence-rounds CLI flag →
# CENTELLA_CONFIDENCE_ROUNDS env var → centella.toml → DEFAULT_CAPS
# fallback. The TOML file is shared with source-of-truth and model
# resolution.
CONFIDENCE_ROUNDS_ENV = "CENTELLA_CONFIDENCE_ROUNDS"
CONFIDENCE_ROUNDS_FILE = SOURCE_OF_TRUTH_FILE

# Verbosity — see IMPLEMENTATION.md §2 "Verbosity". Four levels with
# stackable -v/-q shortcuts following the clig.dev / cargo / kubectl
# convention. Default is `stream` because the user invoking centella
# is opening to watch; -q drops to centella's pre-streaming behavior;
# -qq goes fully quiet (errors still emit per clig.dev "errors emit at
# every level" anti-pattern guard).
VERBOSITY_VALUES = ("quiet", "normal", "stream", "debug")
VERBOSITY_DEFAULT = "stream"
VERBOSITY_ENV = "CENTELLA_VERBOSITY"
VERBOSITY_FILE = SOURCE_OF_TRUTH_FILE

# Subtask statuses that count as "done" for the progress counter.
_TERMINAL_STATUSES = frozenset({"complete", "failed", "blocked"})

# Model selection — see IMPLEMENTATION.md §2 "Model selection". Aliases
# are passed straight to `claude --model`; the CLI resolves them to the
# current version. Each worker type has independent CLI/env/TOML
# overrides; falls back through global CLI/env/TOML/MODEL_DEFAULT.
MODEL_VALUES = ("sonnet", "opus", "haiku")
MODEL_DEFAULT = "sonnet"
MODEL_ENV = "CENTELLA_MODEL"
MODEL_FILE = "centella.toml"
WORKER_TYPES = ("classifier", "planner", "implementer",
                "integrator", "validator")


def _source_of_truth_hint() -> str:
    """The one-line hint shown when the user is asked the source-of-truth
    question — interactive and non-interactive paths share this string."""
    return (f"Skip this question next time by passing "
            f"--source-of-truth codebase|research|both on the next "
            f"invocation, by setting {SOURCE_OF_TRUTH_ENV}=codebase|research|both, "
            f"or by adding source_of_truth=... to {SOURCE_OF_TRUTH_FILE} "
            f"at the repo root.")


VALIDATOR_SYSTEM = (
    "You verify whether an integrated set of changes satisfies a list of "
    "frozen success-criteria files. You run the criteria — execute the tests "
    "they describe, perform the documented checks — against the current "
    "working directory. You do not modify code. Your final result is delivered "
    "as structured output conforming to the JSON schema you were given: for "
    "each subtask, its id, whether all its criteria were met, and a list of "
    "any failing criteria with the reason each failed."
)

# --- worker output schemas -----------------------------------------------
# Passed to `claude -p` via --json-schema. The CLI validates the worker's
# final output against the schema AFTER the run and exposes the validated
# object as `structured_output` in the JSON envelope. NOTE: --json-schema
# only accepts an INLINE schema string; a file path is silently ignored
# (verified against Claude Code 2.1.143), so these are embedded here.
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
            # Defensive: the planner echoes back what it was given; downstream
            # code reads from answers["source_of_truth"] (validated in
            # gather_answers), not from this field. Kept as future-proofing in
            # case a consumer of the planner's output appears later.
            "source_of_truth": {
                "type": "string",
                "enum": ["codebase", "research", "both"],
            },
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
                        "requires": {"type": "array", "items": {"type": "string"}},
                        "provides": {"type": "array", "items": {"type": "string"}},
                        "success_criteria_seed": {"type": "string"},
                        "size": {"type": "string"},
                        "investigation_notes": {"type": "string"},
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
            # DESIGN §9: proposal-only revision channel. An implementer
            # that believes its criteria are wrong submits a proposal here;
            # the orchestrator (not the implementer) decides whether to
            # apply it. See _proposal_structurally_valid /
            # apply_criteria_revision / record_criteria_revision below.
            "criteria_revision_proposal": {
                "type": ["object", "null"],
                "properties": {
                    "proposed_text": {"type": "string"},
                    "evidence": {"type": "string"},
                },
                "required": ["proposed_text", "evidence"],
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
    "validator": {
        "type": "object",
        "required": ["results"],
        "properties": {
            "results": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["subtask_id", "all_criteria_met"],
                    "properties": {
                        "subtask_id": {"type": "string"},
                        "all_criteria_met": {"type": "boolean"},
                        "failing": {"type": "array", "items": {"type": "string"}},
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
    print(f"[centella {datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def die(msg: str, code: int = 1):
    print(f"centella: error: {msg}", file=sys.stderr, flush=True)
    sys.exit(code)


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
            f"claude CLI {'.'.join(map(str, found))} is too old; centella "
            f"requires >= {'.'.join(map(str, MIN_CLAUDE_CLI))} for "
            "--json-schema (introduced for `claude -p` in v2.1.22). "
            "Upgrade with the native installer: "
            "`curl -fsSL https://claude.ai/install.sh | bash`. "
            "(npm/pnpm installs are now an advanced/legacy option per the "
            "Claude Code docs.)"
        )


# --- run identifier (DESIGN §6 "The run identifier") --------------------
#
# A run_id namespaces a single centella invocation across its branch
# (`centella/<run-id>`), state directory (`.centella/runs/<run-id>/`),
# and PR title (`centella: <run-id>`). Built from three deterministic
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
    Trivial wrapper, but the single place to change branch-name shape
    later if needed (e.g., adding a `centella/runs/` prefix)."""
    return f"centella/{run_id}"


# --- run.json sidecar invariants (IMPLEMENTATION.md §8) -----------------

def _validate_run_json(data: dict) -> None:
    """Enforce the three logical invariants on a `run.json` sidecar.

    1. `pushed_at` and `push_error` are mutually exclusive (at most one
       is non-null).
    2. `pr_url` and `pr_error` are mutually exclusive.
    3. If `pr_url` is set, `pushed_at` must be set (cannot have a PR
       without a successful push).

    Raises ValueError on any violation. Caller (e.g., `centella --list`)
    decides whether to die, warn, or render as `status=corrupt-sidecar`."""
    if not isinstance(data, dict):
        raise ValueError("run.json must be a JSON object")
    pushed_at = data.get("pushed_at")
    push_error = data.get("push_error")
    pr_url = data.get("pr_url")
    pr_error = data.get("pr_error")
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


# --- PR body composition (DESIGN §6 "Finalization") ---------------------

def compose_pr_body(state: dict, run_id: str) -> str:
    """Generate the PR body from run state + run_id. Deterministic given
    the inputs; no I/O. Used by finalize.sh (commit 4) via a small
    JSON-stdin protocol to avoid passing 4kb of body as a shell argument.

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
        f"- Generated by centella on `{_or_na(working_branch)}`.\n"
        "\n"
        f"See `.centella/runs/{run_id}/state.json` for full run state.\n"
    )


# --- run discovery and resolution (DESIGN §6 multi-run resume) ----------

def discover_runs(centella_root: Path) -> list[dict]:
    """Enumerate `.centella/runs/*/state.json`, returning one summary
    dict per discovered run. Skip the `_bootstrap-*` directories silently
    (those are pre-classify, not real runs). Malformed state.json files
    are skipped with a logged warning, never raising.

    Returned dicts have at least: `run_id` (directory name), `path` (the
    state.json path), `task`, `started_at`, `finished_at`, `categories`.
    Other state.json fields are passed through unchanged. Sorted by
    `started_at` descending (newest first) for stable display in
    `centella --list`.

    Pure read; no writes. Returns [] if `centella_root/runs` doesn't
    exist."""
    runs_dir = centella_root / "runs"
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


def resolve_run_id(centella_root: Path, cli_run_id: str | None) -> str:
    """Pick the run_id to operate on. Used by `--resume` and `--list`.

    Policy (DESIGN §6 "the run branch is the resume contract"):
    - If `cli_run_id` is given, it must exactly match an existing run.
      Otherwise die with the available list (fails closed).
    - Elif exactly one run exists, use it. Preserves the common case
      where there's only one run in flight.
    - Else die: multiple runs and no `--run-id` is ambiguous.

    Never guesses across multiple runs. `--resume` against an ambiguous
    repo is a hard error, not a heuristic."""
    runs = discover_runs(centella_root)
    if cli_run_id is not None:
        for r in runs:
            if r["run_id"] == cli_run_id:
                return cli_run_id
        available = ", ".join(r["run_id"] for r in runs) or "(none)"
        die(
            f"--run-id {cli_run_id!r} does not match any known run. "
            f"Available: {available}. Use `centella --list` to enumerate."
        )
    if not runs:
        die(
            "no runs found under .centella/runs/. Start a new run with "
            "`./centella \"<task>\"`."
        )
    if len(runs) == 1:
        return runs[0]["run_id"]
    available = "\n  ".join(
        f"{r['run_id']}  (started {r.get('started_at', '?')})" for r in runs
    )
    die(
        "multiple runs present; pass --run-id <id> to disambiguate:\n  "
        f"{available}\nUse `centella --list` to see full details."
    )


def _read_toml_key(path: Path, key: str) -> str | None:
    """Read a single `key = value` from a flat centella.toml. Returns
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


def resolve_source_of_truth(repo_root: Path,
                            cli_value: str | None = None) -> str:
    """Resolve the source-of-truth preference. Order:
    --source-of-truth CLI flag → CENTELLA_SOURCE_OF_TRUTH env var →
    centella.toml → default 'ask'. argparse validates `cli_value` via
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
    return "ask"


def resolve_confidence_rounds(repo_root: Path,
                              cli_value: int | None = None) -> int:
    """Resolve the confidence-rounds cap. Order:
    --confidence-rounds CLI flag → CENTELLA_CONFIDENCE_ROUNDS env var →
    centella.toml → DEFAULT_CAPS["confidence_rounds"]. argparse validates
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
    --verbosity CLI flag → CENTELLA_VERBOSITY env var → centella.toml →
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
      3. CENTELLA_MODEL_<WORKER> env var
      4. CENTELLA_MODEL env var
      5. model_<worker> in centella.toml
      6. model in centella.toml
      7. MODEL_DEFAULT
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
        models[worker] = (per_cli or global_cli or per_env or global_env
                          or per_file or global_file or MODEL_DEFAULT)
    return models


async def run_proc(cmd: list[str], *, cwd: str | None = None,
                   timeout: float | None = None) -> subprocess.CompletedProcess:
    """Async equivalent of `subprocess.run(cmd, capture_output=True, text=True)`.
    On timeout, kills the process and raises `subprocess.TimeoutExpired` — same
    semantics callers already handle. One helper everywhere keeps the asyncio
    boilerplate out of the call sites."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        if timeout is None:
            stdout, stderr = await proc.communicate()
        else:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise subprocess.TimeoutExpired(cmd, timeout)
    except BaseException:
        # Any other exception (CancelledError from a parent abort, an unexpected
        # OSError/BrokenPipeError from the PIPE, etc.) must still leave no
        # orphan child. Reap then re-raise the original exception.
        proc.kill()
        try:
            await proc.wait()
        except BaseException:
            pass
        raise
    return subprocess.CompletedProcess(
        cmd,
        proc.returncode if proc.returncode is not None else 0,
        stdout.decode(errors="replace") if stdout else "",
        stderr.decode(errors="replace") if stderr else "",
    )


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

async def preflight(centella_dir: Path, verbosity: str = VERBOSITY_DEFAULT,
                    skip_smoke: bool = False) -> None:
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
            "Commit or stash before running centella.")

    # 3. (removed in per-run refactor) The global centella/* branch and
    #    .centella/worktrees/* checks used to fail a second concurrent
    #    run; they no longer apply now that each run namespaces its
    #    branches as centella/<run-id> and its worktrees under the
    #    per-run dir. A run_id collision is detected separately at
    #    State.rename_to() (filesystem side) and during setup-run.sh
    #    (git side). See DESIGN.md §6 and §14 ("single-clone parallelism").

    # 4. claude CLI version is recent enough for `--json-schema` in -p mode.
    #    Runs even when --skip-smoke is set: --skip-smoke is for skipping the
    #    *live* model call (auth + a turn), not for skipping local CLI sanity
    #    checks. Without this, a stale CLI fails the smoke test with a cryptic
    #    'unknown option' that tells the user nothing actionable.
    _check_claude_cli_version()

    # 5. live smoke-test: auth + --output-format stream-json + --json-schema inline.
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
                                     centella_dir=centella_dir,
                                     verbosity=verbosity)
        except subprocess.TimeoutExpired:
            die("claude -p smoke test timed out — auth issue or network problem")
        except WorkerError as e:
            die(f"claude -p smoke test failed: {e}")
        if envelope.get("is_error"):
            die(f"claude -p smoke test returned an error: "
                f"{envelope.get('api_error_status') or envelope.get('result')}")
        log("preflight: ok")


_ID_PREFIXES = frozenset({
    "bugfix-", "feat-", "refactor-", "perf-",
    "test-", "deps-", "config-", "docs-",
})


def validate_plan(subtasks: dict) -> None:
    """Structural validation of the merged plan — pure Python set operations."""
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
        for cap in s.get("requires", []):
            if cap not in all_provides:
                errors.append(f"{sid}: requires '{cap}' but nothing provides it — "
                              "dependency is unresolvable and will be silently dropped")

    if errors:
        bullet = "\n".join(f"  • {e}" for e in errors)
        die(f"plan validation failed ({len(errors)} issue(s)):\n{bullet}")
    log(f"plan validation: {len(subtasks)} subtasks ok")


def detect_test_runner() -> list[str] | None:
    """Scan cwd for a known deterministic test harness.
    Returns the command as a list, or None if nothing is recognisable."""
    cwd = Path.cwd()

    # Python: pytest
    pt = cwd / "pyproject.toml"
    if (cwd / "pytest.ini").exists() or (cwd / "setup.cfg").exists():
        return ["python", "-m", "pytest", "--tb=short", "-q"]
    if pt.exists() and "pytest" in pt.read_text():
        return ["python", "-m", "pytest", "--tb=short", "-q"]

    # JavaScript / TypeScript: npm test
    pkg = cwd / "package.json"
    if pkg.exists():
        try:
            scripts = json.loads(pkg.read_text()).get("scripts", {})
            if "test" in scripts:
                return ["npm", "test"]
        except (json.JSONDecodeError, OSError):
            pass

    # Go
    if (cwd / "go.mod").exists():
        return ["go", "test", "./..."]

    # Rust
    if (cwd / "Cargo.toml").exists():
        return ["cargo", "test", "--quiet"]

    # Make: test target
    mk = cwd / "Makefile"
    if mk.exists():
        content = mk.read_text()
        if "\ntest:" in content or content.startswith("test:"):
            return ["make", "test"]

    return None


# --- criteria locking --------------------------------------------------------

def _hash_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def lock_criteria(sid: str, centella_dir: Path, st: State) -> None:
    """Hash the criteria file and store it — called after first write.
    Idempotent: does nothing if no file exists or a lock is already stored."""
    path = centella_dir / "criteria" / f"{sid}.md"
    if not path.exists():
        return
    locks = st.data.setdefault("criteria_locks", {})
    if sid not in locks:
        locks[sid] = _hash_file(path)
        st.save()


def verify_criteria_lock(sid: str, centella_dir: Path, st: State) -> None:
    """Raise WorkerError if the criteria file changed after locking.
    A changed hash means the implementer silently lowered its own bar."""
    path = centella_dir / "criteria" / f"{sid}.md"
    locks = st.data.get("criteria_locks", {})
    if sid not in locks or not path.exists():
        return
    current = _hash_file(path)
    if current != locks[sid]:
        raise WorkerError(
            f"{sid}: criteria file was modified after being locked "
            f"(stored={locks[sid][:8]}, current={current[:8]}). "
            "Implementer may have lowered its own bar — escalating.")


# --- proposal-only criteria revision (DESIGN §9) -----------------------------
# The implementer cannot apply its own revision — the lock prevents it.
# Instead it returns a `criteria_revision_proposal` and the orchestrator
# decides. The decision is a structural-minimum check: code can verify a
# proposal is well-formed and points at real artifacts, but cannot judge
# its semantic merit (per DESIGN §12 — orchestrator does only what can be
# checked mechanically). Every proposal, approved or rejected, is logged.

def _proposal_structurally_valid(proposal: dict, worktree: str) -> str | None:
    """Return None if the proposal passes the structural minimum, else an
    error string. The minimum: both fields non-empty after strip, and the
    evidence references at least one path that actually exists in the
    worktree (file:line citations, test names that map to a real file).
    Cannot judge the proposal's semantic merit — see module-level comment."""
    proposed_text = (proposal.get("proposed_text") or "").strip()
    if not proposed_text:
        return "proposed_text is empty"
    evidence = (proposal.get("evidence") or "").strip()
    if not evidence:
        return "evidence is empty"
    # The evidence must reference at least one real artifact in the worktree.
    # Scan tokens that look like paths (contain "/") or look like file:line
    # citations, and require at least one to resolve to an existing path.
    candidates: list[str] = []
    for tok in evidence.replace(",", " ").split():
        # strip common surrounding punctuation
        cleaned = tok.strip("`'\"()[]{}.,;:")
        if not cleaned:
            continue
        # file:line → take the file part
        if ":" in cleaned:
            cleaned = cleaned.split(":", 1)[0]
        if "/" in cleaned or cleaned.endswith((".py", ".md", ".sh",
                                                ".js", ".ts", ".go",
                                                ".rs", ".java", ".rb",
                                                ".cpp", ".c", ".h",
                                                ".json", ".yaml", ".yml",
                                                ".toml")):
            candidates.append(cleaned)
    wt = Path(worktree)
    for c in candidates:
        # try both absolute and relative-to-worktree
        if Path(c).exists() or (wt / c).exists():
            return None
    return ("evidence cites no path that exists in the worktree — "
            f"checked candidates: {candidates[:5] or '(none found)'}")


def record_criteria_revision(sid: str, st: State, evidence: str, status: str,
                              old_hash: str | None, new_hash: str | None,
                              rejection_reason: str | None = None) -> None:
    """Append one entry to state.data['criteria_revisions']. Append-only;
    every proposal (approved or rejected) is logged for audit per DESIGN §9
    'every approved revision is logged with its justification' — extended to
    also log rejections so a reader can see what was tried."""
    entry = {
        "sid": sid, "timestamp": now(), "status": status,
        "evidence": evidence,
    }
    if old_hash is not None:
        entry["old_hash"] = old_hash
    if new_hash is not None:
        entry["new_hash"] = new_hash
    if rejection_reason is not None:
        entry["rejection_reason"] = rejection_reason
    st.data.setdefault("criteria_revisions", []).append(entry)
    st.save()


def apply_criteria_revision(sid: str, centella_dir: Path, st: State,
                             proposed_text: str) -> tuple[str, str]:
    """Write the new criteria file and update the lock hash to match.
    Returns (old_hash, new_hash). Caller must have already validated the
    proposal — this function does not re-check; it just commits the change."""
    path = centella_dir / "criteria" / f"{sid}.md"
    old_hash = _hash_file(path) if path.exists() else ""
    path.write_text(proposed_text)
    new_hash = _hash_file(path)
    # Update the lock to the new hash so verify_criteria_lock does not fire
    # on the next loop. Without this the orchestrator would immediately
    # reject the file *it just wrote*.
    st.data.setdefault("criteria_locks", {})[sid] = new_hash
    st.save()
    return old_hash, new_hash


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
# successor would learn nothing from it.
_NOISE_TOKENS = {"none", "n/a", "na", "tbd", "—", "--", "-", "?"}


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
        if all(_strip_bullet(l).lower() in _NOISE_TOKENS for l in lines):
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

def validate_result(result: dict,
                    centella_dir: Path | None = None) -> str | None:
    """Cross-field invariant checks that JSON Schema cannot express.
    Returns an error string if the result is self-contradictory, None if ok."""
    status = result.get("status")
    if status == "complete":
        cr = result.get("criteria_results") or []
        if not cr:
            return ("status='complete' but criteria_results is empty — "
                    "no verification evidence provided")
        failing = [c.get("criterion", "?") for c in cr
                   if not c.get("met", False)]
        if failing:
            n = len(failing)
            sample = failing[:3]
            return (f"status='complete' but {n} criterion/criteria unmet: "
                    f"{sample}{'…' if n > 3 else ''}")
        # criteria file must exist — a missing file means criteria_results
        # was fabricated without ever writing the lock-able file
        if centella_dir is not None:
            sid = result.get("subtask_id")
            if sid:
                cf = centella_dir / "criteria" / f"{sid}.md"
                if not cf.exists():
                    return (f"status='complete' but criteria file does not exist: "
                            f"{cf} — criteria_results may have been fabricated")
    elif status == "incomplete-handoff":
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

    The diff is computed against the run branch (centella/<run-id>) — the
    base every subtask branched off of. Hardcoding `centella/staging` here
    used to silently disable the check after the per-run refactor (the
    branch doesn't exist), so the protected-path enforcement was off."""
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

    # fatal: any changes to protected meta-directories are out of bounds
    _PROTECTED = (".centella/", ".git/", ".claude/")
    protected = [f for f in touched
                 if any(f.startswith(p) for p in _PROTECTED)]
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
    """Return an error if the integrator's merge commit touched .centella/ files.
    The integrator should only touch project files, never coordination artifacts."""
    r = await run_proc(
        ["git", "show", "--name-only", "--format=", "HEAD"],
        cwd=str(staging),
    )
    if r.returncode != 0:
        return None
    bad = [f for f in r.stdout.strip().splitlines()
           if f and f.startswith(".centella/")]
    if bad:
        return f"integrator commit touched coordination files: {bad}"
    return None


# --- branch-has-commits verification -----------------------------------------

async def check_branch_has_commits(sid: str, worktree: str,
                                   parent_branch: str) -> str | None:
    """Return error if the implementer's subtask branch has no commits
    ahead of the run branch (`parent_branch` — typically
    `centella/<run-id>`). An empty diff means the worker produced
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


# --- pre-validator criteria existence check ----------------------------------

def check_criteria_files_exist(wave: list[str],
                                centella_dir: Path) -> str | None:
    """Return error if any subtask in the wave is missing its criteria file.
    A missing file means validation would fail with no useful diagnosis —
    catch it before spending a worker invocation."""
    missing = [sid for sid in wave
               if not (centella_dir / "criteria" / f"{sid}.md").exists()]
    if missing:
        return (f"criteria files missing for: {', '.join(missing)} — "
                "validation cannot proceed without them")
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
            "(under .centella/runs/<run-id>/).")

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
    """Prefix every non-empty line of `content` with `prefix`. Used
    for tool_result summaries whose content can be multi-line (a
    Read of a source file, a Grep result, a multi-line schema
    error). Without this, the tag appears only on line 1 and
    subsequent lines are bare text — in a parallel run with
    max_parallel=4, untagged lines from one worker would be
    indistinguishable from another worker's output.

    For single-line content this is a no-op (returns the same
    string as `f'{prefix} {content}'`). For empty content it
    returns the empty string so the caller's truthiness check
    naturally drops it."""
    return "\n".join(f"{prefix} {ln}" for ln in content.splitlines() if ln)


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
    quiet/normal, individual events are dropped (centella's existing
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
                # Emit every non-empty line of the assistant's text as
                # its own [<sid> text] entry, full-width (no
                # truncation). Mid-cut sentences in earlier versions
                # ate the part the user actually wanted to read. The
                # per-worker .log file has the same content; this just
                # surfaces it inline too.
                for ln in (b.get("text") or "").splitlines():
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
                # detail. Multi-line errors (rare but possible) are
                # tagged per-line so lines 2+ stay attributable to
                # this worker; see _tag_each_line.
                return _tag_each_line(f"  [{sid} tool-fail]", content_txt)
            # Successful tool results are file-only at stream; debug
            # gets the FULL content. The user opting into debug is
            # explicitly asking for raw worker output; truncating
            # defeats the level. A worker reading a large file will
            # flood the orchestrator log at debug — that's the
            # accepted trade-off. Multi-line content (a Read of a
            # source file, a Grep of code) is tagged per-line so
            # every line is attributable to this worker.
            if verbosity == "debug":
                return _tag_each_line(f"  [{sid} tool-ok]", content_txt)
        return None

    if t == "rate_limit_event":
        info = event.get("rate_limit_info", {}) or {}
        # Surface threshold-crossings at stream; everything at debug.
        if info.get("surpassedThreshold") or verbosity == "debug":
            util = int(float(info.get("utilization") or 0) * 100)
            status = info.get("status", "?")
            return f"  [{sid}] rate-limit {status} (util={util}%)"
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


async def _invoke(cmd: list[str], cwd: str, timeout: int,
                  sid: str, centella_dir: Path, verbosity: str,
                  progress: tuple[int, int] | None = None) -> dict:
    """Run a `claude -p` command, streaming events as they arrive.

    The CLI is invoked with `--output-format stream-json --verbose`; each
    line of stdout is one JSON event. The final `type: "result"` event
    is the envelope (same shape as the non-streaming `--output-format
    json` path produces). All events are appended to
    `.centella/logs/<sid>.log` regardless of verbosity. Inline summaries
    surface to the orchestrator log according to `verbosity` (see
    `_summarize_stream_event`).

    `cmd` must already contain `--output-format stream-json --verbose`
    — `claude_p` adds those.

    Errors / cancellation follow `run_proc`'s contract: timeout raises
    `subprocess.TimeoutExpired`, cancellation kills the child and
    re-raises. A worker that exits without emitting any `result` event
    raises `WorkerError` — same error class callers already handle."""
    log_path = centella_dir / "logs" / f"{sid}.log"
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
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=10 * 1024 * 1024,
    )
    envelope: dict | None = None
    stderr_chunks: list[bytes] = []

    async def _read_stream():
        nonlocal envelope
        # `buffering=1` is line-buffered: every newline flushes to disk.
        # Without this Python text-mode files are fully buffered when not
        # connected to a TTY, so `tail -f .centella/logs/<sid>.log` would
        # show nothing until the file closed at worker end — defeating
        # the entire live-progress property of the streaming feature.
        with log_path.open("a", buffering=1) as log_file:
            try:
                async for raw in proc.stdout:
                    if not raw:
                        continue
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
                    # line gets its own [centella HH:MM:SS] prefix —
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
                # centella-shaped error and the retry path treats it
                # as a worker fault.
                raise WorkerError(
                    "claude -p emitted a line exceeding the 10 MiB "
                    "buffer limit — likely a runaway structured_output "
                    f"or text block: {e}") from e

    async def _drain_stderr():
        # Drain stderr concurrently so a chatty worker doesn't block on
        # a full pipe. stderr content surfaces only if the process
        # exits with no envelope (used in the error message).
        while True:
            chunk = await proc.stderr.read(4096)
            if not chunk:
                return
            stderr_chunks.append(chunk)

    try:
        await asyncio.wait_for(
            asyncio.gather(_read_stream(), _drain_stderr(), proc.wait()),
            timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        try:
            await proc.wait()
        except BaseException:
            pass
        raise subprocess.TimeoutExpired(cmd, timeout)
    except BaseException:
        # Same orphan-child guard as run_proc: kill + reap, then
        # re-raise. Centella's gather_or_cancel relies on this for
        # clean aborts.
        proc.kill()
        try:
            await proc.wait()
        except BaseException:
            pass
        raise

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


async def claude_p(user_prompt: str, system_prompt: str, *, schema_key: str,
                   cwd: str, allowed_tools: str, max_turns: int, autonomous: bool,
                   caps: dict, st: "State", model: str, sid: str) -> dict:
    """Run one headless Claude Code worker and return its validated
    structured output.

    The worker's result is constrained with `--json-schema` (inline — a file
    path is silently ignored by the CLI). The CLI validates the worker's final
    output against the schema and exposes it as `structured_output` in the
    envelope. If that field is missing or the run reports an error, the worker
    is retried once with the failure noted, then declared failed.

    Worker activity streams as one JSON event per stdout line
    (`--output-format stream-json --verbose`). `_invoke` writes the raw
    events to `.centella/logs/<sid>.log` and emits per-event inline
    summaries gated by `st.data["verbosity"]`. The final `result` event
    is returned as the envelope — same shape as the pre-streaming
    single-result mode (`structured_output` present on schema success).

    `autonomous` workers skip permission prompts (they act on files inside an
    isolated worktree); non-autonomous workers get only read tools.

    `model` is a `claude --model` alias (`sonnet` / `opus` / `haiku`);
    resolved per worker-type by `resolve_models()` at startup.

    `sid` is the worker identifier used in inline log tags and the
    per-worker log filename (e.g. `bugfix-001`, `classifier`,
    `planner-bug-fixing`, `integrator-feat-001`, `validator-wave-2`).
    """
    schema = json.dumps(SCHEMAS[schema_key], separators=(",", ":"))
    centella_dir = st.path.parent
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
        if autonomous:
            # acting workers run inside an isolated worktree; skipping prompts
            # is what makes the run unattended. Blast radius is the worktree.
            cmd.append("--dangerously-skip-permissions")
        return cmd

    timeout = caps["worker_timeout_sec"]
    last_problem = ""
    for attempt in (1, 2):
        retry_note = ("" if attempt == 1 else
                      f"\n\nYOUR PREVIOUS ATTEMPT FAILED: {last_problem} "
                      "Return output that conforms exactly to the required schema.")
        envelope = await _invoke(build(retry_note), cwd, timeout,
                                 sid, centella_dir, verbosity,
                                 progress=_get_progress(st))

        # record run-weight telemetry
        st.add_telemetry(envelope)

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
    `centella_root / "runs" / run_id / state.json`. Two State instances with
    different run_ids share no on-disk state. See DESIGN.md §6 and §10."""

    def __init__(self, centella_root: Path, run_id: str):
        self.centella_root = centella_root
        self.run_id = run_id
        self.run_dir = centella_root / "runs" / run_id
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
        new_dir = self.centella_root / "runs" / new_run_id
        if new_dir.exists():
            die(
                f"run_id collision: .centella/runs/{new_run_id}/ already exists. "
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
# phases
# =========================================================================
async def phase_classify(task: str, st: State, caps: dict, no_clarify: bool,
                         models: dict[str, str]) -> dict:
    """Phase 1 (classify), which also produces the Phase 0 clarification
    questions: classify the task and surface only genuinely underivable
    (intent-level) questions."""
    log("phase 1: classifying task")
    sys_prompt = (PROMPTS / "classifier.md").read_text()
    st.bump_workers(caps)
    result = await claude_p(
        user_prompt=f"TASK:\n{task}\n\nClassify it and apply the clarification filter.",
        system_prompt=sys_prompt, schema_key="classifier", cwd=os.getcwd(),
        allowed_tools=READ_TOOLS, max_turns=20, autonomous=False,
        caps=caps, st=st, model=models["classifier"], sid="classifier",
    )
    cats = [c for c in result.get("categories", []) if c in CATEGORIES]
    if not cats:
        die("classifier returned no recognized categories")
    questions = [] if no_clarify else result.get("questions", [])
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
    sot_pref = st.data.get("source_of_truth_pref", "ask")
    answers: dict = dict(supplied or {})

    provided_sot = answers.get("source_of_truth")
    if provided_sot is not None and provided_sot not in SOURCE_OF_TRUTH_ANSWERS:
        die(f"source_of_truth={provided_sot!r} is not one of "
            f"{SOURCE_OF_TRUTH_ANSWERS}")

    # If the preference is preset (not 'ask') and the classifier flagged a
    # feature task, satisfy source_of_truth from the preference without
    # asking. See DESIGN.md §11.
    if need_sot and "source_of_truth" not in answers and sot_pref != "ask":
        answers["source_of_truth"] = sot_pref

    # --no-clarify means "skip clarification entirely" per DESIGN §11. If
    # the source-of-truth is still missing at this point — preference is
    # 'ask' and no answer was pre-supplied — default to 'codebase' and warn
    # rather than block. DESIGN's rationale: the caller invoked --no-clarify
    # to guarantee the task is fully specified, so any remaining question
    # must be resolved by the orchestrator without user interaction.
    if (st.data.get("no_clarify") and need_sot
            and "source_of_truth" not in answers):
        answers["source_of_truth"] = "codebase"
        log("--no-clarify with no source-of-truth preference set; defaulting "
            f"to 'codebase' (pass --source-of-truth, set {SOURCE_OF_TRUTH_ENV}, "
            f"or set source_of_truth in {SOURCE_OF_TRUTH_FILE} to choose a "
            "different default)")

    pending = [q for q in questions if q.get("id") not in answers]
    sot_missing = need_sot and "source_of_truth" not in answers

    if not pending and not sot_missing:
        st.data["answers"] = answers
        st.save()
        return answers

    if not sys.stdin.isatty():
        # launched non-interactively (e.g. via the plugin skill): defer.
        centella_dir = st.path.parent
        (centella_dir / "pending-questions.json").write_text(json.dumps({
            "questions": pending,
            "source_of_truth": sot_missing,
            "source_of_truth_hint": _source_of_truth_hint() if sot_missing else None,
        }, indent=2))
        log("clarification needed; wrote .centella/pending-questions.json")
        sys.exit(EXIT_NEEDS_ANSWERS)

    for q in pending:
        print(f"\n? {q['question']}")
        if q.get("why_underivable"):
            print(f"  (underivable: {q['why_underivable']})")
        answers[q["id"]] = input("  > ").strip()
    if sot_missing:
        print("\n? Build feature work from the existing codebase's patterns, "
              "from researched best-practice standards, or both?")
        print(f"  ({_source_of_truth_hint()})")
        choice = input("  [codebase/research/both] > ").strip().lower()
        if choice.startswith("c"):
            answers["source_of_truth"] = "codebase"
        elif choice.startswith("r"):
            answers["source_of_truth"] = "research"
        elif choice.startswith("b"):
            answers["source_of_truth"] = "both"
        else:
            die("source-of-truth answer must be codebase, research, or both")

    st.data["answers"] = answers
    st.save()
    return answers


def absorb_supplied_answers(args, st: State, centella_dir: Path) -> None:
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

    The subtask-spec rewrite mirrors centella.py around the
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
    if provided_sot is not None and provided_sot not in SOURCE_OF_TRUTH_ANSWERS:
        die(f"source_of_truth={provided_sot!r} is not one of "
            f"{SOURCE_OF_TRUTH_ANSWERS}")

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
    sub_dir = centella_dir / "subtasks"
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
      - Non-interactive: write .centella/pending-clarifications.json
        with the question, the subtask id, and the checkpoint path,
        then sys.exit(EXIT_NEEDS_ANSWERS) so the calling layer can
        collect the answer and resume.

    Returning True signals "answer captured, re-spawn the worker."
    Non-interactive callers never reach the return — sys.exit fires
    first. The caller is responsible for bumping the
    subtask_continuations counter before treating this as the
    continuation step."""
    centella_dir = st.path.parent
    answers = st.data.setdefault("answers", {})

    if not sys.stdin.isatty():
        # Persist enough state for the surrounding layer to resume.
        # The question id keys the answer; the checkpoint path is
        # what the re-spawned worker will read.
        (centella_dir / "pending-clarifications.json").write_text(
            json.dumps({
                "subtask_id": sid,
                "question": question,
                "checkpoint_path": checkpoint_path,
            }, indent=2))
        log(f"  {sid}: clarification needed; wrote "
            ".centella/pending-clarifications.json")
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


async def phase_plan(task: str, st: State, caps: dict,
                     models: dict[str, str]) -> list[dict]:
    """Phase 2: one planner per category, run in parallel (bounded by
    max_parallel). Each returns a JSON plan of granular subtasks."""
    log("phase 2: planning")
    cats = st.data["categories"]
    answers = st.data.get("answers", {})
    sot = answers.get("source_of_truth", "codebase")
    sys_prompt = (PROMPTS / "planner.md").read_text()
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
            up = (f"DOMAIN: {category}\n\nCONTEXT:\n{ctx}\n\n"
                  f"Decompose the {category} aspect of this task into a JSON plan "
                  "per your instructions.")
            return await claude_p(user_prompt=up, system_prompt=sys_prompt,
                                  schema_key="planner", cwd=os.getcwd(),
                                  allowed_tools=READ_TOOLS, max_turns=40,
                                  autonomous=False, caps=caps, st=st,
                                  model=models["planner"],
                                  sid=f"planner-{category}")

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

    # build edges: predecessors of each subtask
    preds: dict[str, set[str]] = {sid: set() for sid in subtasks}
    for sid, s in subtasks.items():
        for dep in s.get("depends_on", []):
            if dep in subtasks:
                preds[sid].add(dep)
        for cap in s.get("requires", []):
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


def write_plan(centella_dir: Path, task: str, st: State,
               subtasks: dict, waves: list[list[str]]) -> None:
    """Persist the merged plan and per-subtask spec files the implementers read."""
    answers = st.data.get("answers", {})
    sot = answers.get("source_of_truth", "codebase")
    (centella_dir / "plan.json").write_text(json.dumps(
        {"task": task, "waves": waves, "subtasks": subtasks}, indent=2))
    sub_dir = centella_dir / "subtasks"
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


async def run_implementer(sid: str, centella_dir: Path, caps: dict, st: State,
                          models: dict[str, str],
                          continuation: bool = False, note: str = "") -> dict:
    """Spawn one implementer for one subtask in its own worktree. Handles
    both kinds of continuation up to the shared `subtask_continuations`
    cap: context-exhaustion handoffs and DESIGN §11 mid-execution
    clarifications."""
    sys_prompt = (PROMPTS / "implementer.md").read_text()
    proc = await run_script("new-worktree.sh", sid, st.run_id)
    if proc.returncode != 0:
        raise WorkerError(f"worktree creation failed for {sid}: {proc.stderr.strip()}")
    worktree = proc.stdout.strip().splitlines()[-1]

    # DESIGN §11 mid-execution clarification: the worker may exit with
    # `needs-clarification` only when --no-clarify is NOT in effect.
    # Under --no-clarify the user has asked centella not to interrupt
    # them, so the worker must make a best-effort decision and proceed
    # (same semantics as Phase-1 under --no-clarify, which defaults the
    # source-of-truth resolution instead of asking).
    can_ask_user = not st.data.get("no_clarify", False)

    up = [f"Execute subtask `{sid}`.",
          f"CENTELLA_DIR is {centella_dir} (absolute).",
          f"Read your spec at {centella_dir}/subtasks/{sid}.json.",
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
    if continuation:
        up.append(f"This is a CONTINUATION. Read the checkpoint at "
                  f"{centella_dir}/checkpoints/{sid}.md, validate it against the "
                  f"actual repo state, then continue.")
    if note:
        up.append(f"NOTE FROM ORCHESTRATOR: {note}")

    st.bump_workers(caps)
    try:
        return await claude_p(user_prompt="\n".join(up), system_prompt=sys_prompt,
                              schema_key="implementer", cwd=worktree,
                              allowed_tools=ACT_TOOLS, max_turns=120,
                              autonomous=True, caps=caps, st=st,
                              model=models["implementer"], sid=sid)
    except WorkerError as e:
        # worker could not return schema-valid output even after a retry
        # (e.g. it hit --max-turns mid-task) -> treat as a handoff so a fresh
        # implementer can continue from whatever checkpoint exists.
        return {"subtask_id": sid, "status": "incomplete-handoff",
                "checkpoint_path": str(centella_dir / "checkpoints" / f"{sid}.md"),
                "summary": f"worker produced no schema-valid result: {e}"}


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

    Terminal (worker is broken/dishonest — terminate immediately, no retry):
      - cross-field invariant violation (worker lied about its own status)
      - diff touched a protected path (.centella/, .git/, .claude/)
      - any worker-level error surfaced as a failure
    """
    retryable_markers = ("no commits ahead of the run",
                         "uncommitted change")
    return any(m in reason for m in retryable_markers)


async def settle_subtask(sid: str, centella_dir: Path, caps: dict, st: State,
                         models: dict[str, str]) -> dict:
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
    revision_retries = 0   # DESIGN §9: at most one revision-driven retry per subtask
    note = ""
    continuation = False
    worktree = str(centella_dir / "worktrees" / sid)
    subtask_path = centella_dir / "subtasks" / f"{sid}.json"
    subtask = json.loads(subtask_path.read_text()) if subtask_path.exists() else {}

    def fail(reason: str) -> dict | None:
        """Record a failed attempt. Returns a terminal result dict if the
        subtask is done (non-retryable, or retry cap exhausted), or None if the
        caller should loop for one more corrective attempt."""
        nonlocal retries, continuation, note
        res = {"subtask_id": sid, "status": "failed", "summary": reason}
        st.data.setdefault("subtask_status", {})[sid] = "failed"
        st.save()
        lock_criteria(sid, centella_dir, st)
        if not _retryable_failure(reason):
            log(f"  {sid}: non-retryable failure — terminating: {reason}")
            return res
        retries += 1
        if retries > caps["failed_retries"]:
            log(f"  {sid}: retry cap reached — terminating")
            return res
        continuation = False
        note = f"Previous attempt failed: {reason}"
        return None

    while True:
        # Before re-invoking the implementer (whether this is a corrective
        # retry or a subtask continuation — handoff or clarification),
        # verify the criteria file has not
        # been altered since it was locked. A retried implementer is a stuck
        # model — exactly the case the lock guards against. No-op on the first
        # iteration, when no lock exists yet.
        verify_criteria_lock(sid, centella_dir, st)

        res = await run_implementer(sid, centella_dir, caps, st, models,
                                    continuation=continuation, note=note)

        # cross-field invariant check — catches a worker that lied about
        # status. A self-contradictory result means the worker is malfunctioning
        # or dishonest: non-retryable by `_retryable_failure`.
        problem = validate_result(res, centella_dir)
        if problem:
            log(f"  result invariant violated for {sid}: {problem}")
            done = fail(problem)
            if done is not None:
                return done
            continue

        status = res.get("status")
        st.data.setdefault("subtask_status", {})[sid] = status
        st.save()

        # DESIGN §9: proposal-only criteria revision. If the implementer
        # included a proposal alongside its result, the orchestrator decides
        # whether to apply it (structural-minimum check) and logs every
        # decision. Approved proposals overwrite the criteria file and the
        # lock; if the implementer originally returned `failed` against the
        # old criteria, it gets one retry against the new ones.
        proposal = res.get("criteria_revision_proposal")
        if proposal:
            err = _proposal_structurally_valid(proposal, worktree)
            if err:
                record_criteria_revision(sid, st, proposal.get("evidence", ""),
                                         "rejected", None, None,
                                         rejection_reason=err)
                log(f"  {sid}: criteria revision rejected: {err}")
            else:
                old_hash, new_hash = apply_criteria_revision(
                    sid, centella_dir, st, proposal["proposed_text"])
                record_criteria_revision(sid, st, proposal["evidence"],
                                         "approved", old_hash, new_hash)
                log(f"  {sid}: criteria revision approved "
                    f"(old={old_hash[:8] or '(new file)'}, new={new_hash[:8]})")
                if status == "failed" and revision_retries == 0:
                    revision_retries += 1
                    log(f"  {sid}: retrying once against revised criteria")
                    continuation = False
                    note = ("Criteria were revised based on your proposal — "
                            "retry against the new criteria.")
                    continue

        if status == "complete":
            # a 'complete' claim with no commits is a retryable mistake —
            # the worker may genuinely have work to commit and just forgot
            commit_err = await check_branch_has_commits(
                sid, worktree, compute_run_branch(st.run_id))
            if commit_err:
                log(f"  branch check failed for {sid}: {commit_err}")
                done = fail(commit_err)
                if done is not None:
                    return done
                continue
            # uncommitted changes — retryable, same reasoning
            wt_status = await run_proc(
                ["git", "status", "--porcelain"], cwd=worktree)
            dirty = [l for l in wt_status.stdout.splitlines()
                     if l and not l.startswith("??")]
            if dirty:
                done = fail(f"{sid}: worktree has {len(dirty)} uncommitted "
                            f"change(s) — changes will be lost on integration")
                if done is not None:
                    return done
                continue
            lock_criteria(sid, centella_dir, st)
            # protected-path violation — the worker wrote to .git/ etc.: it is
            # broken, not merely careless. Non-retryable by `_retryable_failure`.
            scope_err = await check_diff_scope(sid, worktree, subtask, st)
            if scope_err:
                done = fail(scope_err)
                if done is not None:
                    return done
                continue
            return res

        if status == "incomplete-handoff":
            # Worktree convention from scripts/new-worktree.sh:
            # .centella/worktrees/<subtask-id>. The freshness check on
            # `## Files touched` validates paths against this directory;
            # if it no longer exists (e.g. cleanup ran early), the check
            # is skipped gracefully.
            wt_root = centella_dir / "worktrees" / sid
            cp_err = validate_checkpoint(res.get("checkpoint_path") or "",
                                         worktree_root=wt_root)
            if cp_err:
                log(f"  bad checkpoint for {sid}: {cp_err}")
                return {"subtask_id": sid, "status": "blocked",
                        "blocker": f"checkpoint invalid: {cp_err}",
                        "summary": cp_err}
            lock_criteria(sid, centella_dir, st)
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
            wt_root = centella_dir / "worktrees" / sid
            cp_err = validate_checkpoint(res.get("checkpoint_path") or "",
                                         worktree_root=wt_root)
            if cp_err:
                log(f"  bad checkpoint for {sid}: {cp_err}")
                return {"subtask_id": sid, "status": "blocked",
                        "blocker": f"checkpoint invalid: {cp_err}",
                        "summary": cp_err}
            lock_criteria(sid, centella_dir, st)
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
            spec_path = centella_dir / "subtasks" / f"{sid}.json"
            if spec_path.exists():
                spec = json.loads(spec_path.read_text())
                spec["_clarification_answers"] = st.data.get("answers", {})
                spec_path.write_text(json.dumps(spec, indent=2))
            continuation, note = True, ""
            continue

        if status == "failed":
            # a worker that reported failure itself — treat its summary as the
            # reason and run it through the same retry policy
            done = fail(res.get("summary") or "worker reported failure")
            if done is not None:
                return done
            continue

        # blocked, or anything unexpected
        return res


async def integrate_wave(wave: list[str], results: dict[str, dict],
                         centella_dir: Path, caps: dict, st: State,
                         models: dict[str, str]) -> list[str]:
    """Merge each completed subtask branch into staging (git merge, not
    cherry-pick); resolve conflicts with an integrator worker. Returns the
    list of integrated ids.

    If an integrator cannot resolve a conflict (status other than 'resolved'),
    the in-progress merge is aborted so the staging worktree is left clean, and
    the run is terminated with the integrator's diagnosis — an unresolved
    conflict must not silently proceed onto a corrupt staging tree."""
    integrated, integrated_so_far = [], []
    staging = (centella_dir / "worktrees" / "staging").resolve()
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
        sys_prompt = (PROMPTS / "integrator.md").read_text()
        up = (f"Resolve the in-progress merge conflict in this worktree.\n"
              f"CENTELLA_DIR is {centella_dir}.\n"
              f"Incoming subtask: {sid}\n"
              f"Already-integrated subtasks it may conflict with: "
              f"{', '.join(integrated_so_far) or 'none'}")
        st.bump_workers(caps)
        ires = await claude_p(user_prompt=up, system_prompt=sys_prompt,
                              schema_key="integrator", cwd=str(staging),
                              allowed_tools=ACT_TOOLS, max_turns=60,
                              autonomous=True, caps=caps, st=st,
                              model=models["integrator"],
                              sid=f"integrator-{sid}")
        if ires.get("status") == "resolved":
            # the integrator must have actually committed the merge — a
            # 'resolved' claim with the worktree still mid-merge is a lie,
            # the integrator-side analogue of check_branch_has_commits.
            merge_err = await check_merge_committed(staging)
            if merge_err:
                await run_proc(["git", "merge", "--abort"], cwd=str(staging))
                st.data["integrator_failure"] = {
                    "subtask": sid,
                    "reason": f"integrator claimed 'resolved' but {merge_err}"}
                st.save()
                die(f"integrator for {sid} returned 'resolved' but {merge_err}. "
                    f"The merge was aborted; {compute_run_branch(st.run_id)} "
                    "is clean. State saved — resolve and re-run with --resume.")
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
            st.data["integrator_failure"] = {
                "subtask": sid, "status": ires.get("status"),
                "diagnosis": diagnosis}
            st.save()
            die(f"integrator could not integrate {sid} "
                f"({ires.get('status')}): {diagnosis}\n"
                f"The in-progress merge was aborted; "
                f"{compute_run_branch(st.run_id)} is intact at the last "
                f"good wave. Resolve the conflict between {sid} and "
                f"the already-integrated subtasks manually, then re-run with "
                f"--resume.")
    return integrated


async def validate_wave(wave: list[str], centella_dir: Path, caps: dict,
                        st: State, models: dict[str, str],
                        wave_idx: int) -> dict:
    """Re-run every wave subtask's frozen criteria against integrated staging.
    Tries the deterministic test runner first; falls back to LLM only on
    failure or when no runner was detected. `wave_idx` is the 0-based
    index used in the worker's log file name (`validator-wave-N`)."""
    staging = (centella_dir / "worktrees" / "staging").resolve()

    # criteria files must exist before we spend any validation workers
    missing_err = check_criteria_files_exist(wave, centella_dir)
    if missing_err:
        die(f"pre-validation check failed: {missing_err}")

    # fast path: deterministic test suite — no worker invocation, no quota
    runner = st.data.get("test_runner")
    if runner:
        log(f"  running deterministic test suite: {' '.join(runner)}")
        try:
            r = await run_proc(runner, cwd=str(staging), timeout=600)
        except subprocess.TimeoutExpired:
            log("  deterministic test suite exceeded 600s — "
                "falling through to LLM validator for diagnosis")
        else:
            if r.returncode == 0:
                log("  staging tests pass — skipping LLM validator")
                return {"results": [
                    {"subtask_id": sid, "all_criteria_met": True, "failing": []}
                    for sid in wave
                ]}
            log(f"  tests failed (exit {r.returncode}) — "
                "falling through to LLM validator for diagnosis")

    # LLM validator: runs criteria that aren't captured by the test suite,
    # or diagnoses why the test suite failed
    criteria = [f"{centella_dir}/criteria/{sid}.md" for sid in wave]
    up = ("Verify the current working directory against these frozen "
          "success-criteria files. Run every criterion.\n" +
          "\n".join(f"- subtask {sid}: {path}"
                    for sid, path in zip(wave, criteria)))
    st.bump_workers(caps)
    return await claude_p(user_prompt=up, system_prompt=VALIDATOR_SYSTEM,
                          schema_key="validator", cwd=str(staging),
                          allowed_tools=RUN_TOOLS, max_turns=40,
                          autonomous=True, caps=caps, st=st,
                          model=models["validator"],
                          sid=f"validator-wave-{wave_idx + 1}")


async def phase_execute(centella_dir: Path, st: State, caps: dict,
                        models: dict[str, str]) -> None:
    """Phases 4-5: create staging, then run waves sequentially; within a wave,
    subtasks in parallel (bounded by max_parallel)."""
    log("phase 4: creating run-branch worktree")
    proc = await run_script("setup-run.sh", st.run_id)
    if proc.returncode != 0:
        die(f"run setup failed: {proc.stderr.strip()}")

    sem = asyncio.Semaphore(caps["max_parallel"])

    async def settle_one(sid: str) -> tuple[str, dict]:
        async with sem:
            r = await settle_subtask(sid, centella_dir, caps, st, models)
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

        await integrate_wave(wave, results, centella_dir, caps, st, models)

        # deterministic: scan staging for unresolved conflict markers before
        # spending any validation workers — a marker means integration is broken
        staging_path = centella_dir / "worktrees" / "staging"
        marker_err = await scan_conflict_markers(staging_path)
        if marker_err:
            die(f"wave {wi + 1}: {marker_err}\n"
                f"Resolve manually in {staging_path}, commit, "
                "then re-run with --resume.")

        # re-validate integrated staging; re-spawn failing implementers
        for attempt in range(caps["wave_revalidation_rounds"]):
            v = await validate_wave(wave, centella_dir, caps, st, models, wi)
            failing = [r["subtask_id"] for r in v.get("results", [])
                       if not r.get("all_criteria_met", False)]
            if not failing:
                break
            log(f"  staging re-validation failed for: {', '.join(failing)} "
                f"(round {attempt + 1})")
            if attempt == caps["wave_revalidation_rounds"] - 1:
                die(f"wave {wi + 1} fails staging validation after "
                    f"{caps['wave_revalidation_rounds']} rounds: {failing}")
            for sid in failing:
                await settle_subtask(sid, centella_dir, caps, st, models)
                await run_script("integrate.sh", sid, st.run_id)   # re-merge the delta

        st.data["completed_waves"] = wi + 1
        st.save()


async def phase_finalize(centella_dir: Path, st: State) -> None:
    log("phase 6: finalizing")
    proc = await run_script("finalize.sh", st.run_id)
    if proc.returncode != 0:
        die(f"finalize failed (run branch is intact): {proc.stderr.strip()}")
    await run_script("cleanup.sh")

    # verify the merge commit actually landed on the working branch
    r = await run_proc(
        ["git", "log", "--merges", "-1", "--format=%s", "HEAD"],
    )
    if r.returncode == 0 and "centella:" not in r.stdout:
        log("  ⚠  finalize warning: centella merge commit not found at HEAD — "
            "verify the working branch manually")

    # verify the run branch and the working branch are now identical — a
    # non-empty diff here means the merge silently dropped changes (data loss)
    run_branch = compute_run_branch(st.run_id)
    r = await run_proc(
        ["git", "diff", "--stat", f"{run_branch}..HEAD"],
    )
    if r.returncode == 0 and r.stdout.strip():
        log(f"  ⚠  finalize warning: working branch diverges from {run_branch} "
            f"after merge:\n"
            f"    {r.stdout.strip()}\n"
            "    Some changes may not have merged. Inspect manually.")
    wc = st.data.get("worker_count", 0)
    nsub = len(st.data.get("subtask_status", {}))
    tel = st.data.get("telemetry", {})
    st.data["finished_at"] = now()
    st.save()
    log(f"done — {nsub} subtasks, {len(st.data['waves'])} waves, "
        f"{wc} worker invocations. Merged into the working branch.")
    if tel:
        log(f"run weight: {tel.get('calls', 0)} claude -p calls, "
            f"{tel.get('input_tokens', 0):,} in / "
            f"{tel.get('output_tokens', 0):,} out tokens "
            f"(see {st.path})")


# =========================================================================
# entry point
# =========================================================================
async def orchestrate(args, caps: dict, centella_dir: Path, st: State,
                      sot_pref: str, verbosity: str,
                      models: dict[str, str]) -> None:
    """The async portion of a run: every phase that spawns a `claude -p`
    worker. main() handles sync setup, then drives this with `asyncio.run`."""
    if args.resume:
        if not st.load():
            die(f"nothing to resume — no state.json at {st.path}")
        validate_resume_state(st.data)
        task = st.data["task"]
        log(f"resuming: {task!r} (worker count {st.data.get('worker_count', 0)})")
        if "waves" not in st.data:
            die("cannot resume — run did not reach the scheduling phase")
        # Refresh the preferences in case env vars or centella.toml
        # changed since the original run started. Verbosity is
        # resolved fresh every run — the user can dial up or down on
        # resume without editing state.json.
        st.data["source_of_truth_pref"] = sot_pref
        st.data["verbosity"] = verbosity
        st.save()
        # Absorb --answers on resume too. The documented user flow for
        # a non-interactive deferred-question exit (Phase-1 or §11
        # mid-execution) is: get a pending-*.json, write an answers
        # file, re-run with --resume --answers <file>. Without this
        # call the answers file was silently dropped — the re-spawned
        # worker would re-ask the same question forever. See P5-1.
        absorb_supplied_answers(args, st, centella_dir)
    else:
        if not args.task:
            die("a task description is required (or use --resume)")
        task = args.task
        st.data = {"task": task, "started_at": now(), "worker_count": 0,
                   "source_of_truth_pref": sot_pref,
                   "verbosity": verbosity,
                   "no_clarify": bool(args.no_clarify)}
        st.save()
        await preflight(centella_dir, verbosity=verbosity,
                        skip_smoke=args.skip_smoke)
        supplied = (json.loads(Path(args.answers).read_text())
                    if args.answers else None)
        await phase_classify(task, st, caps, args.no_clarify, models)
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
            centella_dir = st.run_dir
        # gather_answers blocks on input(). That's fine here: no concurrent
        # tasks are scheduled yet, so blocking the loop blocks nothing. Kept
        # on the event loop deliberately — every State mutation runs on the
        # loop, which is why the lock-free State works.
        gather_answers(st, supplied)
        plans = await phase_plan(task, st, caps, models)
        subtasks, waves = schedule(plans)
        validate_plan(subtasks)
        runner = detect_test_runner()
        if runner:
            log(f"detected test runner: {' '.join(runner)}")
        st.data["test_runner"] = runner
        write_plan(centella_dir, task, st, subtasks, waves)

    await phase_execute(centella_dir, st, caps, models)
    await phase_finalize(centella_dir, st)


def main() -> None:
    ap = argparse.ArgumentParser(prog="centella", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("task", nargs="?", help="the task to execute")
    ap.add_argument("--resume", action="store_true",
                    help="resume an interrupted run (auto-picks if exactly "
                         "one run exists under .centella/runs/)")
    ap.add_argument("--answers", metavar="FILE",
                    help="JSON file of pre-supplied clarification answers")
    ap.add_argument("--no-clarify", action="store_true",
                    help="skip clarification entirely (DESIGN §11): drop "
                         "intent questions and satisfy the source-of-truth "
                         "from --source-of-truth / CENTELLA_SOURCE_OF_TRUTH / "
                         "centella.toml if set, otherwise default to 'codebase'")
    ap.add_argument("--max-workers", type=int,
                    help="override the total worker-invocation budget")
    ap.add_argument("--max-parallel", type=int,
                    help="override concurrent workers per wave")
    ap.add_argument("--confidence-rounds", type=_positive_int, metavar="N",
                    help=f"how many evidence-gate rounds each planner / "
                         f"implementer may run before exiting blocked "
                         f"(default {DEFAULT_CAPS['confidence_rounds']}); "
                         f"also {CONFIDENCE_ROUNDS_ENV} and "
                         f"confidence_rounds in centella.toml")
    ap.add_argument("--skip-smoke", action="store_true",
                    help="skip the live claude -p smoke test during preflight")
    ap.add_argument("--source-of-truth", choices=SOURCE_OF_TRUTH_VALUES,
                    metavar="VALUE",
                    help=f"source-of-truth preference "
                         f"({'|'.join(SOURCE_OF_TRUTH_VALUES)}); overrides "
                         f"{SOURCE_OF_TRUTH_ENV} and centella.toml")
    ap.add_argument("--model", choices=MODEL_VALUES, metavar="ALIAS",
                    help=f"model alias for all workers "
                         f"({'|'.join(MODEL_VALUES)}, default {MODEL_DEFAULT}); "
                         f"per-worker --model-<worker> flags override this, "
                         f"as do CENTELLA_MODEL[_*] env vars and centella.toml")
    for _w in WORKER_TYPES:
        ap.add_argument(f"--model-{_w}", choices=MODEL_VALUES, metavar="ALIAS",
                        help=f"model alias for the {_w} worker — overrides "
                             f"--model, CENTELLA_MODEL, and centella.toml")
    # Verbosity: explicit --verbosity wins; -v/-q stackable shortcuts
    # anchor to `normal` (the pre-streaming behavior). So `-v` = stream,
    # `-vv` = debug, `-q` = normal, `-qq` = quiet. See IMPLEMENTATION.md
    # §2 "Verbosity". When none are given, resolve_verbosity falls
    # through to env / TOML / VERBOSITY_DEFAULT.
    ap.add_argument("--verbosity", choices=VERBOSITY_VALUES, metavar="LEVEL",
                    help=f"output verbosity ({'/'.join(VERBOSITY_VALUES)}, "
                         f"default {VERBOSITY_DEFAULT}); overrides "
                         f"{VERBOSITY_ENV} and centella.toml")
    ap.add_argument("-v", "--verbose", action="count", default=0,
                    help="shortcut: -v=stream (default), -vv=debug")
    ap.add_argument("-q", "--quiet", action="count", default=0,
                    help="shortcut: -q=normal (pre-streaming behavior), "
                         "-qq=quiet (errors and phase boundaries only)")
    args = ap.parse_args()

    if not shutil.which("claude"):
        die("`claude` CLI not found on PATH. Install Claude Code (native, "
            "recommended): `curl -fsSL https://claude.ai/install.sh | bash`. "
            "Docs: https://docs.claude.com/en/docs/claude-code/setup")
    if subprocess.run(["git", "rev-parse", "--is-inside-work-tree"],
                      capture_output=True).returncode != 0:
        die("not inside a git repository")

    # Pre-per-run layout detection: a top-level .centella/state.json means
    # the user upgraded from a previous centella version. We can't safely
    # migrate (don't know the run_id retroactively), so refuse to run
    # until the user explicitly cleans up the legacy artifacts.
    if Path(".centella/state.json").exists():
        die(
            "legacy state layout detected at .centella/state.json. "
            "This version of centella uses per-run state under "
            ".centella/runs/<run-id>/. To migrate, run "
            "`scripts/cleanup.sh --legacy` (removes the old layout) and "
            "re-invoke centella."
        )

    caps = dict(DEFAULT_CAPS)
    if args.max_workers:
        caps["max_total_workers"] = args.max_workers
    if args.max_parallel:
        caps["max_parallel"] = args.max_parallel
    # Resolve confidence_rounds across CLI / env / TOML / default. The
    # resolver die()s on a bad env or TOML value; argparse already rejected
    # a bad --confidence-rounds via _positive_int.
    caps["confidence_rounds"] = resolve_confidence_rounds(
        Path(os.getcwd()), args.confidence_rounds)

    # Resolve verbosity. Explicit --verbosity wins; else -v/-q
    # shortcuts (anchored to `normal`); else env / TOML / default.
    # See verbosity_from_shortcuts() for the shortcut-mapping rationale.
    verbosity = (args.verbosity
                 or verbosity_from_shortcuts(args.verbose, args.quiet)
                 or resolve_verbosity(Path(os.getcwd()), None))

    # The on-disk layout is per-run: every run gets its own subdirectory
    # `centella_root/runs/<run-id>/` (see DESIGN.md §6, §10). For a fresh
    # run we don't know the final run_id until phase_classify has chosen
    # a category, so state lives in `_bootstrap-<6hex>/` until then; the
    # rename to the final run_id happens in orchestrate() after classify.
    centella_root = Path(".centella").resolve()
    centella_root.mkdir(parents=True, exist_ok=True)
    (centella_root / "runs").mkdir(parents=True, exist_ok=True)
    if args.resume:
        # Auto-pick if exactly one run exists; die with the available list
        # if multiple are in flight. (--run-id wiring lands in commit 5.)
        run_id = resolve_run_id(centella_root, getattr(args, "run_id", None))
    else:
        # Bootstrap directory: keyed on the current wall-clock time so two
        # concurrent invocations don't pick the same one. Renamed to the
        # final `<short_category>-<slug>-<6hex>` after classify.
        run_id = "_bootstrap-" + hashlib.sha1(now().encode()).hexdigest()[:6]
    st = State(centella_root, run_id)
    for sub in ("", "subtasks", "criteria", "checkpoints", "logs"):
        (st.run_dir / sub).mkdir(parents=True, exist_ok=True)

    # Resolve source-of-truth and per-worker model preferences once per run.
    # Both die() on a bad value so typos in centella.toml or env vars are
    # caught at startup, not mid-planner. argparse already rejected any bad
    # --source-of-truth / --model[-*] before we got here.
    repo_root = Path(os.getcwd())
    sot_pref = resolve_source_of_truth(repo_root, args.source_of_truth)
    models = resolve_models(repo_root, args)
    log(f"models: " + ", ".join(f"{w}={models[w]}" for w in WORKER_TYPES))

    try:
        asyncio.run(orchestrate(args, caps, st.run_dir, st,
                                sot_pref, verbosity, models))
    except WorkerError as e:
        st.save()
        die(str(e))
    except KeyboardInterrupt:
        # asyncio.run cancels pending tasks on KeyboardInterrupt; run_proc's
        # CancelledError handler kills any in-flight child processes.
        st.save()
        die("interrupted — state saved; re-run with --resume", code=130)


if __name__ == "__main__":
    main()
