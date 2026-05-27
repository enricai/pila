# Centella — Design Document

> Deterministic, headless task orchestrator for Claude Code. Classifies an
> engineering task, decomposes it into granular subtasks, schedules them into
> dependency-ordered waves, and executes each in an isolated git worktree under
> an evidence-gated implement/validate loop — with the fewest possible
> interruptions to the user.

**Scope of this document.** This is the *theory*: the architecture, the
constraints that forced it, and the reasoning behind each design decision. It
describes the intended system, not the current code. It stays correct across
any reimplementation that honors the same architecture — a line here goes stale
only if the *design* changes, never because a function was renamed or a
constant retuned. Mechanism — function names, cap values, file paths, schemas,
enforcement tables, install steps — lives in the companion `IMPLEMENTATION.md`,
which is true only against the current code. Where the two disagree, this
document defines what *should* be true and the code is the defect.

---

## 1. Purpose

Given one task description, Centella drives it to a validated, integrated result
without further human input — except where input is genuinely impossible to
derive. Every loop is bounded, every decision is made from the codebase or from
research, and state is kept on disk so a run is observable and resumable.

---

## 2. The two constraints that produced this architecture

The architecture is not a free choice. Two platform constraints eliminate the
obvious designs and leave essentially one.

**Constraint 1 — subagents cannot spawn subagents.** The original concept had
three levels of delegation: orchestrator → domain subagent → granular subagent.
Claude Code's documented rule is explicit: a subagent cannot spawn another
subagent; only the main thread can. A three-level delegation tree therefore has
no native implementation.

**Constraint 2 — a plugin slash-command body is advisory, not executable.** A
plugin command is a skill: its markdown is injected into a model's context as
instructions, not executed as deterministic code. For a long, capped,
multi-wave run, "the model will probably follow these steps" is not a strong
enough guarantee — control flow can drift, and the drift is silent.

Both constraints are resolved by the same move: **the orchestrator is an
ordinary program, not an in-session agent.** Every unit of LLM work is a
separate headless process. The program owns all control flow. Subagent nesting
is impossible because there are no subagents — only independent OS processes.
Control-flow drift is impossible because the orchestrator is real loops and
conditionals, not a model interpreting instructions.

**Why a headless CLI process, not an API library.** "The orchestrator is a
program" still admits two forms. One shells out to the headless CLI binary,
once per worker, and runs on the interactive Claude Code subscription with only
the CLI as a dependency. The other uses an agent library whose calls return
typed objects — less brittle, because there is no marshalling of CLI strings
and stdout — but it authenticates against the metered API rather than the
subscription. Running on the subscription rather than the API was a hard
requirement, so Centella takes the CLI-subprocess form. The brittleness that
choice accepts (parsing process output rather than typed objects) is contained
by two later mechanisms: worktree isolation limits the blast radius of a
misbehaving worker, and every worker result is validated against a schema
before the orchestrator acts on it.

---

## 3. Architecture

The orchestrator is a deterministic program. It runs six phases; each unit of
LLM work within a phase is a separate headless worker process with its own
context and a defined input/output contract.

```
Orchestrator (deterministic — owns all control flow, caps, state)
│
├─ Phase 1   Classify the task into 1..8 categories          → 1 worker
│              ↓ derive the run identifier from category + task + start time
├─ Phase 0   Clarify — intent-only questions, only if needed
├─ Phase 2   Plan — one planner per matched category         → N workers (parallel)
│              ↓ reconcile cross-domain capability tags       → 0 or 1 worker
├─ Phase 3   Schedule — merge plans, build global DAG, sort into waves
├─ Phase 4   Set up the run branch and worktree (per-run unique)
├─ Phase 5   For each wave, in sequence:
│   ├─ Implement — one implementer per subtask               → workers (parallel)
│   ├─ Integrate each result into the run branch; on conflict → 1 integrator worker
│   └─ Validate the integrated run branch result
└─ Phase 6   Verify the run branch; push it and open a PR against the
             working branch; clean up. (Working branch is not modified
             locally — the PR is the proposed integration.)
```

**Why classification precedes clarification.** Phase 1 runs before Phase 0
because Centella cannot know what to ask until it knows what kind of task this
is — the set of questions worth asking is a function of the classification.
Phase 0 is skipped entirely for fully-specified tasks.

**Why planners run before scheduling.** Decomposition (Phase 2) and scheduling
(Phase 3) are separate because decomposition needs LLM judgment about a domain
while scheduling is pure graph computation over the merged result. Keeping them
separate means the non-deterministic part produces data and the deterministic
part consumes it — the scheduler never has to trust a model's ordering.

**The division of labor.** Everything that requires understanding — classify,
decompose, write code, resolve a semantic merge conflict — is done by a worker.
Everything that can be checked mechanically — scheduling, caps, retries, state,
integration bookkeeping — is done by the orchestrator. This line is the single
most important idea in the system and recurs throughout: see §12.

**Invocation.** The orchestrator is invoked directly as a command-line program;
that terminal path is primary. A thin plugin skill is also provided as a
convenience entry point from inside Claude Code, but it is only a wrapper — it
launches the same orchestrator program and adds no logic of its own. All
control flow lives in the orchestrator regardless of how it was started.

**Observability.** Workers do their work inside a single `claude -p`
session that takes minutes; the orchestrator surfaces that activity as it
happens. Each worker's stream of tool calls, text, and intermediate
results is read line-by-line, written verbatim to a per-worker log file,
and summarized inline at a user-controllable verbosity level. The
default level shows one-line summary per worker event; the user can dial
down to centella's pre-streaming terse output (`-q`) or up to raw event
payloads (`-vv`). Errors emit at every level. The per-worker file is
the ground-truth audit trail; the inline view is the live feed.

---

## 4. The eight task categories

Every task is classified into one or more of:

1. **feature-implementation** — new functionality that did not exist
2. **bug-fixing** — correcting wrong behavior, including diagnosis
3. **refactoring** — restructuring without changing behavior
4. **performance-optimization** — faster, lighter, or cheaper; same behavior
5. **testing** — writing and maintaining automated tests
6. **dependency-migration** — upgrading libraries, moving frameworks or API versions
7. **configuration-build** — CI/CD, build scripts, infrastructure-as-code
8. **documentation** — docstrings, comments, READMEs, changelogs

A task commonly spans several categories. One planner is assigned per matched
category; the categories are domains of expertise, not mutually exclusive bins.

---

## 5. Decomposition, sizing, and the wave model

### The sizing target

Each planner decomposes its domain into subtasks. The decomposition target is
**the smallest independently verifiable unit of change** — explicitly *not*
"the smallest possible unit." This is a deliberate correction to the original
specification, which asked for "the most granular possible" decomposition.

Over-decomposition is not free. Every subtask runs as a fresh worker that must
re-establish its understanding of the codebase from cold context. Splitting one
coherent change into five trivial subtasks pays that cold-start cost five times
and adds four integration steps. The correct floor is the point below which a
subtask can no longer be verified on its own; below that, finer granularity
buys nothing and costs coordination overhead. The matching ceiling: a subtask
must be small enough that one worker can finish it within its context. A
subtask that would require reading or changing a large surface area is split
before execution begins.

Sizing is also the **primary defense against context exhaustion** (see §10): a
subtask scoped to fit inside one worker's context never needs a handoff.
Splitting a plan is cheap; handing off mid-implementation is not. Planner
decomposition quality is therefore the load-bearing assumption of the whole
system — if planners under-decompose, implementers degrade before they hand
off. It is the first place to look when a run goes wrong.

### Cross-domain dependencies

Planners run in parallel and cannot see each other's output. Yet dependencies
cross domains: a testing subtask may depend on the feature subtask it tests.
That coupling has to be reconciled somewhere, and it cannot be reconciled
inside a planner that cannot see the other planners.

It is reconciled by the orchestrator with three mechanisms:

- **Intra-domain ordering** — within its own domain a planner declares which
  subtasks must precede which, because it owns and can see those subtasks.
- **Cross-domain capability tags** — a planner cannot name another domain's
  subtasks, so it does not try. Instead each subtask declares the capabilities
  it *produces* and the capabilities it *requires*, as abstract tags. The
  orchestrator matches every "requires" against every domain's "provides" and
  adds a dependency edge from producer to consumer.
- **Reconciler worker** — capability tags are a shared vocabulary with no
  enforced dictionary. Two planners can name the same capability with
  different words (`slm-capture-shim` vs. `capture-slm-call-implemented`),
  and a literal-string match would miss the equivalence. After all planners
  finish, the orchestrator computes the set of `requires` tags that no
  `provides` claims, and if that set is non-empty, spawns a single
  *reconciler* worker. The reconciler reads the full task plus every
  subtask and emits one of four actions per unresolved tag: a *rename* (two
  tags mean the same thing — rewrite one to match the other), an
  *added provides* (an existing subtask actually produces the capability
  but didn't declare it — add the tag), an *added subtask* (a genuine gap —
  propose a new subtask to fill it), or *unresolvable* (no plausible
  resolution — abort the run with the reconciler's diagnosis). All judgment
  about tag equivalence lives in the reconciler worker; the orchestrator
  computes the unresolved set mechanically and applies the worker's output
  mechanically.

The result is a single global dependency graph spanning all domains. A
topological sort turns it into waves: subtasks within a wave are mutually
independent and run in parallel; waves run in sequence. A dependency cycle is
unsatisfiable and aborts the run rather than being silently broken.

Cross-domain dependencies are reconciled by the orchestrator from capability
tags (with the reconciler bridging vocabulary drift) and enforced as wave
ordering. Planners can therefore run in parallel without coordination: the
coupling between their outputs is recovered globally by the scheduler, and
vocabulary mismatches that would have produced silent missing-edges are
caught by the reconciler before they reach the scheduler.

### Why waves are sequential

Each wave's worktrees are branched from the integrated result of all prior
waves. A subtask therefore always sees the complete, validated output of
everything it depends on — never a half-finished intermediate state. Sequential
waves are what make "this subtask depends on that one" mean something concrete:
the dependency is satisfied in the filesystem the dependent subtask starts from.

---

## 6. Worktree and integration model

### Isolation

Parallel workers that write to a shared directory race. Centella gives each
implementer its own git worktree — an isolated checkout backed by the same
repository. Parallel writes land in separate working directories and never
collide. This is what makes "a wave of parallel implementers" safe even when
two of them touch the same file.

### The run identifier

Every run has a unique identifier `run_id`, derived deterministically from
three inputs known by the end of Phase 1:

- the first classified category (a short abbreviation — `feat`, `fix`,
  `refactor`, etc.),
- a sanitized kebab-case slug of the task description (≤30 chars, word-
  boundary truncated),
- a 6-character hex digest of the run's start timestamp (microsecond
  precision).

The result looks like `feat-add-telemetry-skills-a3f7c2`. It is the same
string in three places: the run branch name (`centella/runs/<run-id>`), the
per-run state directory (`.centella/runs/<run-id>/`), and the title of the
PR opened at finalize. A user looking at any of the three can grep for the
others.

A run identifier is *per-run*, not per-repository. Two concurrent invocations
in the same repository produce two different `run_id`s — their branches,
state directories, worktrees, and PRs are disjoint by construction. There
is no shared "staging" namespace that two runs could collide on.

### The run branch as an integration buffer

Integration does not happen on the user's working branch. Each run has its
own **run branch** (`centella/runs/<run-id>`) that receives every subtask's
work; the user's branch is untouched until the run finishes and succeeds. A
failed or messy integration therefore never lands on the branch the user
cares about. Multiple runs in the same repository each have their own run
branch and integrate independently.

Subtask branches live under a sibling namespace: `centella/subtasks/<run-id>/<sid>`.
The run-branch and subtask-branch prefixes are deliberately disjoint
(`centella/runs/…` vs. `centella/subtasks/…`) because git's loose ref store
cannot hold both a ref AT a path and a ref UNDER that same path
simultaneously — `centella/<run-id>` as a leaf ref and
`centella/<run-id>/<sid>` as a child ref would collide on the first
`git worktree add`. Sibling prefixes make the collision structurally
impossible.

Integration is **incremental, one wave at a time**. Each wave's results are
merged into the run branch and the merged result is validated before the
next wave starts. Conflicts surface one wave at a time, close to the work
that caused them — not all at once at the end, where they are far harder to
untangle.

### The run branch is the resume contract

The run branch is also the durable record of everything completed so far:
every integrated wave is a commit on it. This is what `--resume` is built on.
Run state records *which wave* to resume from; the run branch holds *the
work* every prior wave produced. The two together are the entire resume
contract.

This places one hard requirement on the design: **a run branch, once
created, is never reset.** Setup creates it only if it does not already
exist (and a `run_id` collision against an existing branch is a preflight
failure, not a silent overwrite). On a resume the branch already carries
the completed waves' commits, and resetting it would silently discard them
while the wave loop resumed past them — delivering a final result that is
missing everything before the interruption. "Create if absent, never reset"
is not an implementation nicety; it is the invariant the resume guarantee
depends on.

When more than one run is in flight in the same repository, `--resume`
needs to know *which* run to resume. The orchestrator auto-picks when
exactly one run exists, and requires an explicit `--run-id` otherwise; the
discovery scans `.centella/runs/*/state.json`. Resume never guesses across
multiple runs.

### Why merge, not cherry-pick

Subtask branches are integrated into the run branch by merging, not by cherry-picking.
A merge records ancestry, which gives the integrator a real common base for
three-way conflict resolution: far more auto-resolves, and only genuine
conflicts surface. Cherry-pick copies commits without ancestry, so it has a
weaker base and produces more spurious conflicts. Recorded ancestry also makes
re-integration idempotent and the run's history a true audit trail rather than
a set of duplicated commits.

### Conflict resolution is behavioral, not textual

When two subtasks' branches conflict, resolving the conflict to git's
satisfaction is not enough. A textually clean merge can still silently break
the behavior one of the subtasks was validated against.

So conflict resolution is defined behaviorally. The integrator reads the intent
and the frozen success criteria of *every* subtask whose work is part of the
conflicting merge — the incoming subtask and every already-integrated subtask
it collides with — and resolves the merge so that each side's intent is
preserved. Resolving a *semantic* conflict is what the integrator is for;
a purely textual merge can satisfy git while silently breaking the behavior
one side was validated against, and only a worker that understands intent
can avoid that.

The behavioral re-check that *catches* a merge gone wrong happens immediately
after, at the wave level: once the integrator commits the merge, the
orchestrator re-runs every wave subtask's frozen criteria against the
integrated run branch (the same validator pass that runs at the end of every wave, whether
an integrator was needed or not). A merge that satisfied git but broke an
already-validated subtask is caught there, not in the integrator itself.
Keeping the re-check in one place — the wave-level validator — means there
is no double-validation and no place to forget.

### When integration cannot succeed

Two outcomes are not failures of the integrator but facts about the work:

- **A `resolved` claim is verified, not trusted.** The orchestrator confirms an
  integrator that reports success actually completed the merge — a worker
  claiming to have finished while leaving the merge incomplete is treated as a
  failure, the same way an implementer claiming success while committing
  nothing is.
- **Genuinely irreconcilable intents are a design conflict.** If two subtasks
  want contradictory things, no merge can satisfy both — that is a problem with
  the decomposition or the task, not a merge to be papered over. The
  orchestrator stops the run, leaves the run branch intact at the last
  fully-integrated wave, and reports the conflict for a human to resolve. An
  unresolved conflict never proceeds silently onto a corrupt run-branch state.

### Finalization

The final step turns the completed run branch into a reviewable artifact
and never touches the user's working branch.

**The run branch is the integration artifact.** Every wave's work is
already integrated on `centella/runs/<run-id>`. Centella does not merge
the run branch into the working branch locally — that would duplicate the
same change in two places (a local commit and a PR) and put the working
branch in a state the user did not request. The working branch is the same
ref at the end of a run as it was at the start; the PR is the proposal to
change that.

**Push and PR.** The run branch is pushed to `origin` and a pull request
is opened via `gh pr create` against the working branch (the branch
HEAD-at-run-start). The PR title is the run id; the body is generated
deterministically from the run state — task, category, source-of-truth,
wave count, worker count, run timestamps. This is the single point at
which centella reaches the network; everything before is local. Two flags
control it:

- `--no-push` skips both the push and the PR; the run completes with the
  run branch local-only. The user can inspect, push, or open a PR manually
  whenever they choose.
- `--no-verify` passes `--no-verify` to `git push`, skipping pre-push hooks.
  Worker commits inside worktrees continue to run all hooks normally — only
  the push gate is affected. This is the per-invocation explicit user
  override called out by the project's "never skip hooks unless asked"
  principle; defaults to off.

**Push and PR are honest about failure.** A push or PR step that fails does
not pretend the run failed: the local work is intact and reachable on the
run branch. The orchestrator records what was attempted and what failed in
a per-run sidecar (`run.json` — `pushed_at`, `push_error`, `pr_url`,
`pr_error`). Push failure exits non-zero with a multi-line message that
names the run branch (where the work lives) and the working branch
(unchanged from run start, but the intended PR base), shows the captured
stderr, and gives the exact retry command. PR-creation failure is treated
as non-fatal: the push has already succeeded, so the user receives a
warning with the GitHub URL of the pushed branch and the exact `gh pr
create` command to retry. The principle is that the user always knows
exactly what state things are in and exactly which branch holds the work
to be resolved.

**Why push by default.** When centella is invoked in CI or any unattended
context, a successful run that leaves work only on a local branch is a
silent failure mode — the work exists but the user has no signal that it
needs to be reviewed. Defaulting to push + PR turns every run into a
reviewable artifact. `--no-push` exists for users running centella offline
or in repositories without a GitHub remote.

### Cleanup on abnormal exit

A run can end abnormally three ways: the user hits Ctrl-C, an external
process sends a signal (SIGTERM/SIGHUP from CI, systemd, a terminal
close), or an unhandled exception fires. In each case the orchestrator
runs a cleanup pass before exiting, and the cleanup *scope* depends on
the cause.

**Ctrl-C (SIGINT) → full purge.** The user explicitly told centella to
abort. Worktrees, run branch + per-subtask branches
(`centella/runs/<run-id>` and `centella/subtasks/<run-id>/*`), and the
per-run state directory are all removed. The run is gone; `--resume`
cannot recover it.

**SIGTERM, SIGHUP, WorkerError, any other exception → worktrees only.**
The orchestrator was likely killed by something external (CI cancel,
systemd stop, terminal close) rather than by the user's intent to abandon
the run. State and the run branch are preserved so `--resume --run-id <id>`
can continue once the underlying issue is fixed. Only the (re-creatable)
worktrees are torn down to leave a clean filesystem.

Both paths use the same `_cleanup_on_abnormal_exit()` helper; the
`full_purge` boolean parameter selects between them. The signal vs.
exception classification happens in `main()`'s try/except: SIGINT keeps
Python's default `KeyboardInterrupt`; SIGTERM and SIGHUP raise a
dedicated `InterruptedBySignal` exception via handlers installed at
program start. SIGINT and SIGHUP are POSIX-only — guarded with
`hasattr(signal, ...)` so the orchestrator still runs on Windows
(degraded: only SIGTERM-equivalent termination works).

A `die()` call (the documented clean-exit mechanism for known failure
modes) is *not* an abnormal exit. The user already got an actionable
error message; running a worktree cleanup pass is correct (the run was
mid-flight) but it is silent unless there were worktrees to clean.

---

## 7. The worker contract

Every worker is a separate process with its own context. The orchestrator and a
worker communicate through a strict contract:

- The orchestrator passes the worker its role, its inputs, and the exact shape
  of the structured result it must return.
- The worker's final output is **validated against that schema** before the
  orchestrator acts on it. A worker cannot, by malformed output, cause the
  orchestrator to do something undefined.
- A worker that fails to produce a schema-valid result is retried once with the
  violation pointed out. A second failure is a hard worker error.

What happens after a hard worker error depends on whether partial progress can
be salvaged. An **implementer** has a worktree branch and possibly a checkpoint,
so its failure is converted into a handoff: a fresh implementer can continue.
The **classifier, planner, integrator, and validator** have no partial-progress
artifact to hand off — there is nothing for a successor to continue from — so
their hard failure aborts the run with state saved for `--resume`. The rule is
general: salvage if there is something to salvage; abort cleanly otherwise.

---

## 8. The evidence-gated loop

The original specification asked each worker to self-report a 1–10 confidence
score and loop until it reached 9. The intent — force the worker to be sure
before it acts — is right. The mechanism is not: a self-reported number is not
a measurement. Models are systematically overconfident and will state high
confidence on a wrong root cause without hesitation. Looping on that number
just loops on the same vibe.

Centella keeps the loop and the high-confidence bar but **anchors the score to
evidence**. Before an implementer writes any code it must clear a set of
domain-specific *evidence gates*, and each gate must carry a concrete artifact
— a file-and-line citation, a reproduction, a measurement, a cited research
source — not an assertion. The confidence score is then a *summary of which
gates carry hard evidence*, not an independent feeling. A bug-fixing task, for
instance, must show a deterministic reproduction, a test that fails because of
this specific bug, a traced symptom-to-cause path, and a mechanistic
explanation of why the fix addresses the cause. Other domains have their own
gate sets.

Three further disciplines apply at every scoring step, regardless of domain.
They are the mechanisms by which the confidence score becomes load-bearing
rather than ornamental.

- **Falsification.** For each major claim — a chosen root cause, a chosen
  solution — the worker explicitly looks for evidence that would *disprove*
  it: a probe, a counter-example, a research source that contradicts. A claim
  earns high confidence only when its falsifier was tested and failed. Looking
  only for confirming evidence is how a wrong hypothesis acquires high
  confidence; the falsification step is the structural defense.
- **Drift reconciliation.** Before scoring, the worker re-reads its own prior
  statements in the same session. Any current claim that contradicts an
  earlier one — or any earlier position the worker has quietly retreated from
  — is named and resolved with evidence for the kept version. An
  unreconciled contradiction blocks the high-confidence bar. This is the
  defense against a worker confidently asserting X early and confidently
  asserting ¬X later without flagging the change.
- **Gap surfacing.** When a score is below the bar, the worker must enumerate
  the specific *artifact* that would raise it — a citation, a measurement, a
  probe output, a research source — and then go obtain that artifact on the
  next iteration. A gap phrased as an activity ("look into it more", "verify
  the design") does not terminate; a gap phrased as an artifact does. This
  converts an open-ended "try harder" loop into a directed search whose next
  move is deterministic.

The loop is bounded. If the gates cannot be cleared within the bound, the
subtask stops and reports itself as *blocked*, stating precisely what evidence
is missing and whether obtaining it needs something only the user can supply —
for example a credential that exists nowhere in the codebase. This is the
narrow, legitimate exception to "never ask the user" (see §11).

### The planner gate

The same discipline applies one layer up. A planner that decomposes a domain
into subtasks self-gates on two axes — *task understanding* (does the planner
genuinely understand what the user wants and how it lands in this codebase)
and *decomposition quality* (are these subtasks the right cut, sized for one
worker, with real dependencies). The same three disciplines — falsification,
drift reconciliation, gap surfacing — apply. A planner whose gate cannot
clear emits `status: "blocked"` with the gap analysis instead of subtasks,
matching the implementer's blocked-with-evidence exit. The principle is the
same at both layers: a worker that cannot justify its confidence in evidence
hands the decision back to a layer that can, rather than fabricating one.

The structural contract of these disciplines is mechanically enforced — the
worker's output schema requires the falsification, reconciliation, and gap
fields to be present, so a worker that skipped them fails its own JSON gate
before the orchestrator ever reads it (see §12). The *quality* of the
artifacts each field names is model-judged; the *presence* of the discipline
is not.

---

## 9. Locked success criteria

Each implementer's first step is to turn its assigned seed into a complete,
concrete success-criteria set — automated tests wherever possible, precise
documented checks where a test is genuinely impossible. The criteria cover both
the explicit success condition and the regression guards: adjacent behavior
that must *not* change.

**Once implementation begins, the criteria are frozen.** The implementer may
not rewrite them. The reason is a specific failure mode: a stuck model, unable
to make the code pass the test, weakens the test instead. "Revise the criteria
if you find evidence they were wrong" sounds reasonable and is exactly the
loophole that failure mode exploits — and it is not reliably detectable from
the evidence the same model offers.

So revision is **proposal-only**. An implementer that believes its criteria are
genuinely wrong returns a *proposed* revision together with the evidence; it
does not apply it. Only the orchestrator approves a revision, only on hard
evidence, and every approved revision is logged with its justification. The
principle: the checker and the thing being checked are never the same agent.
This is enforced structurally — the orchestrator detects after the fact whether
a criteria set changed between worker invocations and rejects the subtask if it
did — so a worker that ignores the rule is caught, not trusted.

---

## 10. Context management — handoff, not compaction

The original specification said each worker should compact its context at 70%
occupancy. This cannot be done as stated: there is no channel for an external
process to make a running worker compact itself, and a worker has no reliable
view of its own context percentage. An external monitor can *observe* context
occupancy but has no way to *act* on it.

Centella replaces compaction with **orchestrator-driven fresh-context handoff**,
which achieves compaction's actual goal — bounded context with preserved
progress — without depending on a channel that does not exist:

1. **Granular sizing is the primary defense.** Subtasks are sized so one worker
   finishes within its context. Handoff is a safety net, not the main path; if
   it fires often, the planner is under-decomposing (§5).
2. **A worker nearing its limit hands off.** It writes a structured checkpoint,
   commits whatever coherent partial work it has, and returns an
   *incomplete-handoff* result. The checkpoint is a *fixed schema*, not free
   prose — success criteria and their current status, files touched, decisions
   and their rationale, the exact next action, open unknowns — because a
   freeform handoff is only as good as what a degrading worker happened to
   write down, and a fixed schema fails loudly when a section is missing.
3. **The orchestrator spawns a fresh worker** with the checkpoint as input. The
   successor's first act is to validate the checkpoint against the actual repo
   state before trusting it — a bad handoff fails fast and visibly rather than
   producing confident wrong work.
4. **Handoff is bounded.** A worker can hand off to a worker that hands off
   again; the chain is capped. Exhausting the cap means the subtask was
   mis-scoped — it is reported as blocked for re-decomposition, not retried
   forever.

A lower auto-compaction threshold on the underlying CLI can be set as an
independent backstop, but it is a parallel safeguard, not the mechanism — the
handoff design stands on its own.

### Where coordination artifacts live

Checkpoints and criteria are coordination state, not code. They are written to
a coordination directory in the main repository, never inside a subtask's
worktree. A worktree is disposable — it is removed at cleanup — so a checkpoint
stored inside it would vanish exactly when a successor worker needs to read it.
Coordination state must outlive the worktree that produced it.

Coordination state is **per-run**, rooted at `.centella/runs/<run-id>/`.
State, plan, criteria, checkpoints, logs, the worktrees themselves, and the
PR-result sidecar all live under that directory. Two runs in the same
repository share no coordination state — each has its own subtree, and
neither can clobber the other's `state.json`, log files, or worktrees by
collision. The parent `.centella/` is otherwise empty of run data; it only
hosts the `runs/` directory.

---

## 11. The clarification procedure

The default is **zero questions**. The original goal — a fully automated run
that does not interrupt the user — is kept. The question is when an interruption
is genuinely unavoidable, and the answer is a strict filter applied by the
classifier:

1. Can it be derived from the **codebase**? Conventions, patterns, integration
   points, and existing behavior are all readable. If the answer is in the
   code, derive it — do not ask.
2. If not, can it be closed by **research**? Best-practice standards for a
   well-understood problem are findable. If research resolves it, do not ask.
3. Ask the user **only** what neither the codebase nor research can resolve.

The only thing that systematically survives this filter is **intent** — *what*
to build, *which* behavior is wanted. The reason is structural: a decision
nobody has made yet exists in no codebase and in no research source. The
codebase and research answer *how* to build something; they cannot answer
*what* to build when that has genuinely not been decided. A fully-specified
request leaves nothing for the filter to catch, so it runs with zero questions.

When a feature task's request leaves the source of truth ambiguous, centella
resolves it from a preference: `codebase` (build from existing patterns only),
`research` (build from researched best-practice standards), `both` (codebase
first; research only where the codebase is insufficient), or `ask` (surface
the question to the user). The preference is read, in order, from a CLI flag
on the invocation, from an environment variable, from a per-repo config file
committed at the repo root, and otherwise defaults to `ask`. The CLI flag and
env var outrank the file because they are session-scoped knobs — a user
reaching for either is making a one-off override of the repo default. When
`ask` fires, the question is presented with a hint listing all three ways to
skip it next time. A request that already names its own source of truth, or a
non-feature task where the question does not apply, runs without it.
Whichever path resolved the preference, its value becomes a setting carried
to every planner and implementer, so the whole run draws from one consistent
source of truth.

When Centella runs in a context where it cannot block for an answer, the
clarification step is non-blocking: it records the questions, exits with a
distinct status, and lets the surrounding layer collect answers and resume. A
mode that skips clarification entirely also exists, for the case where the
caller has already guaranteed the task is fully specified.

### Mid-execution clarification

The clarification filter runs at Phase 1 — early, before any implementer
has done work. That is the right time for *most* intent questions: they
are visible from the task description and the codebase. But some intent
questions surface only after partial implementation work has narrowed the
problem to a decision point neither the codebase nor research can resolve
— for example, whether a refactor should preserve backward compatibility
with a deprecated client, when both choices exist as patterns elsewhere in
the codebase and the task description does not say.

Centella treats this as the same kind of question as a Phase-1 clarification,
not as a different category. The filter is identical: investigate the
codebase first; treat research as the second-line resolver; ask the user
only what neither can settle. The only difference is *when* the question
surfaces. The mechanism reuses the existing handoff infrastructure: the
implementer writes a checkpoint of its work-in-progress, returns a status
that carries the question to the orchestrator, and the orchestrator surfaces
the question through the same interactive/non-interactive paths the Phase-1
clarification step uses. On the user's answer (delivered either interactively
or via a re-run with `--answers`), a fresh implementer is spawned with the
checkpoint as a continuation and the answer added to its clarification
answers — exactly the channel used by Phase-1 answers.

The same constraint that keeps Phase-1 questions narrow applies here: a
question's `why_underivable` must be explicit and grounded in what the
worker tried. Without that gate, a worker is incentivized to ask the user
rather than do the investigative work the filter requires. The schema
makes the field required, and the prompt forbids the exit when
`--no-clarify` is in effect (the worker must make a best-effort decision
and continue, the same way a Phase-1 run under `--no-clarify` defaults
its source-of-truth resolution).

A subtask has a single re-spawn budget — `subtask_continuations` — that is
consumed by *both* context-exhaustion handoffs and mid-execution
clarifications, with no separate allowance for either. A subtask that
exhausts the budget on a mix of the two is fundamentally mis-scoped and
the orchestrator surfaces it as such. The unified cap is a deliberate
defense against the "ask instead of research" drift: making clarifications
a free resource would invite the worker to prefer asking over investigating.

---

## 12. Deterministic enforcement — the central principle

The single governing principle of the whole system:

> **Prompts are advisory. Code enforces.**

A worker prompt can ask for any behavior, but a prompt is an instruction to a
model and a model can drift, misread, or — under pressure — rationalize around
it. Anything that *matters* and *can be checked mechanically* is therefore not
left to the prompt. It is checked by the orchestrator, in code, with no model
judgment involved.

This is why the orchestrator is a real program and not a skill (§2), and it
recurs everywhere in the design:

- The scheduler does not trust a planner's ordering; it computes the wave order
  itself from the dependency graph (§5).
- The orchestrator does not trust an implementer's "complete" claim; it checks
  mechanically that real work was committed (§7-style verification).
- The orchestrator does not trust an integrator's "resolved" claim; it confirms
  the merge was actually completed (§6).
- The orchestrator does not trust that frozen criteria stayed frozen; it detects
  a changed criteria set between invocations (§9).
- Every worker result is schema-validated before it is acted on (§7).

The complementary half of the principle is just as important: **what cannot be
checked mechanically is left to the worker, and not second-guessed by code.**
Understanding intent, writing code, decomposing a domain, resolving the
*semantics* of a merge conflict — these need judgment, so a worker does them.
The orchestrator checks the *outcome* where it can, but it does not pretend to
do the worker's reasoning.

A reader reasoning about *where a given guarantee comes from* should always ask:
is this enforced by code, or only requested by a prompt? The two have different
strengths, and the design depends on keeping them clearly separated. The
concrete enforcement points — which function checks what, at which phase — are
catalogued in `IMPLEMENTATION.md`.

---

## 13. Caps and escalation

Every loop in the system has a hard bound. Nothing spins forever; when a bound
is reached, Centella escalates rather than looping. But the bounds are of **two
different kinds**, and the difference is itself a design point — it is the §12
principle applied to caps.

### Code-enforced caps

Some caps are counted by the orchestrator: the number of subtask continuations
for a subtask, the number of corrective retries, re-validation rounds per wave,
the total number of workers a whole run may spawn, the parallelism within a
wave, and a per-worker time and turn limit. These are real counters in real
code. When one is hit, the orchestrator takes a defined action — block the
subtask, abort the run with state saved, throttle. Because the orchestrator
owns the counter, the cap is a genuine guarantee.

### Worker-internal caps

Other limits — how many times an implementer or planner re-runs its evidence
gate, how many times an implementer re-runs its validation loop — live
*inside* a single worker. The orchestrator never sees these iterations; it
sees only the worker's final result. These limits are therefore
*prompt-governed*: the worker is instructed to bound itself, and the genuine
hard backstop is the worker's overall turn limit, which the orchestrator does
control.

The evidence-gate bound is exposed to users as `--confidence-rounds` (also
`CENTELLA_CONFIDENCE_ROUNDS` and `centella.toml`); the orchestrator passes the
resolved value into each worker's prompt. The user-visible knob is real — the
worker reads it — but the worker is what counts iterations against it, so the
guarantee is still prompt-governed in the sense above. Surfacing the knob
lets a user dial how persistent workers are at building confidence without
changing what kind of guarantee that bound is.

This distinction matters and must not be blurred. Presenting a worker-internal,
prompt-governed limit as if it were a code-enforced guarantee would mislead
anyone reasoning about the system's reliability. The orchestrator enforces the
*consequences* of a worker's result deterministically; it does not count the
iterations inside the worker that produced it. That is acceptable only because
the orchestrator gates on outcomes, not on iteration counts — and because the
overall turn limit is a real backstop regardless of whether a worker honored
its instructed self-discipline.

### The two-tier retry policy

When a subtask fails, whether it is retried depends on *why* it failed. The
governing rule:

> Retry a failure only if a corrective note to a fresh worker can plausibly fix
> it. Terminate immediately on a failure that means the worker is broken or
> dishonest — re-running it burns a worker for no expected gain, and a cold
> restart can discard partial work.

A **retryable** failure is a correctable mistake: the worker did real work but,
say, forgot to commit it, or left its worktree dirty. A fresh worker told
exactly what went wrong can plausibly succeed. A retryable failure is retried
up to the retry cap; a second occurrence terminates it.

A **terminal** failure means the worker itself is unreliable: it returned a
self-contradictory result (claimed success with no supporting evidence), or
wrote to a protected path it was told never to touch, or failed at the process
level even after the schema retry. Re-running a broken worker does not make it
honest. A terminal failure ends the subtask on first occurrence.

Either way a terminated subtask is fatal at its wave boundary: the run stops
with state saved, rather than carrying a broken subtask forward into
integration. The specific failure-to-tier mapping is in `IMPLEMENTATION.md`;
the *principle* — correctable-mistake versus broken-worker — is the design.

---

## 14. Telemetry, judging, and self-healing

Every LLM call in Centella passes through one of the six worker types in
`WORKER_TYPES`: `classifier`, `planner`, `reconciler`, `implementer`,
`integrator`, or `validator`. Each worker type is a distinct **call type** — a
first-class identifier that partitions every captured call into its role in the
system. The call_type partition is exactly `WORKER_TYPES`: one call_type per
worker role, no overlap, no gap.

### The three pillars

Three capabilities build on this partition to make the system observable,
self-diagnosing, and self-improving:

1. **Per-call NDJSON telemetry.** Every `claude -p` invocation emits a
   structured record to a per-run append-only NDJSON file. The file is written
   by the orchestrator — one JSON object per line, one line per call —
   immediately after the call returns. Crash-safety comes from the format
   itself: each line is a complete, self-contained JSON object. A hard kill
   between writes leaves the file valid through the last fully-written line.
   No partial write can corrupt earlier records.

2. **LLM judge skill.** A Claude Code skill that reads a harvest of captured
   calls (one call_type at a time), applies a multi-dimensional rubric to each
   captured prompt/response pair, and writes structured verdicts. The rubric
   evaluates three dimensions: schema adherence (did the worker produce
   well-formed output), factual accuracy (are the claims grounded in the
   codebase or research the worker was given), and hallucination-freeness (does
   the output introduce content absent from the inputs). The judge is advisory
   at the rubric level — its rubric lives in a prompt — but the scoring
   aggregation and pass/fail threshold are real Python in the skill's
   orchestrator script (§12 applied: the rubric is a prompt, the verdict
   accounting is code).

3. **LLM self-heal skill.** A Claude Code skill that takes the judge's verdicts
   for a given call_type, identifies the failure modes, proposes targeted patches
   to the relevant worker system prompt in `prompts/`, applies those patches, and
   replays the failing samples against the patched prompt to measure improvement.
   The loop is capped and its convergence check — whether a heal iteration is an
   improvement, a plateau, or a regression — is real Python (§12 applied: the
   patch proposal is a prompt, the convergence detection is code).

### The subprocess contract — no new runtime

Both the judge skill and the self-heal skill run exclusively through the
existing `claude -p` subprocess invocation path (the same `claude_p()` function
the orchestrator uses for all workers). They introduce no new runtime, no API
key, and no dependency beyond the `claude` CLI already required for the rest of
the system. This is the same resolution as §2: subscriptions rather than the
metered API, and headless CLI subprocesses rather than an agent library.

The judge spawns a fresh `claude -p` worker per batch of calls to be scored;
the self-heal spawns fresh workers for patch generation and for replaying the
failing samples against the patched prompt. Each worker sees exactly the inputs
it needs for its slice of work, and its structured output is schema-validated
before the skill's orchestrator acts on it — the same contract as every other
worker in the system (§7).

### The NDJSON file convention

Each run's telemetry lives at:

```
.centella/runs/<run-id>/calls.ndjson
```

One file per run. The file is opened for append at run start and written to
by the orchestrator as each call completes. It is never read by the runtime —
the orchestrator writes it and moves on. Reading is a post-run operation:
the judge and heal skills are invoked separately, after the run, against a
harvested set of files.

Each line is a JSON object with a fixed envelope:

```
{"ts": "<ISO-8601>", "run_id": "<run-id>", "call_type": "<worker-type>",
 "call_id": "<uuid>", "model": "...", "input_tokens": N, "output_tokens": N,
 "latency_ms": N, "success": true|false, "system_prompt": "...",
 "user_content": "...", "response_content": "...", "parsed_ok": true|false}
```

Fields are sufficient for the judge to evaluate quality (`system_prompt`,
`user_content`, `response_content`, `parsed_ok`) and for the heal loop to
replay the call against a patched prompt (`system_prompt`, `user_content`).
The `call_type` field is how the judge and heal skills partition their input —
they always operate on one call_type at a time, matching Beacon's design.

### §12 applied — prompts are advisory, code enforces

The central principle (§12) governs this subsystem the same way it governs
everything else:

- The **judge rubric** — what counts as schema-valid, factually grounded, or
  hallucination-free — is an instruction to the judge worker. The worker
  applies it under judgment; the same drift risk applies as with any worker
  prompt.
- The **judge verdict aggregation** — counting pass/fail per dimension, computing
  pass rate across a batch, deciding which calls are "failures" for the heal
  loop — is real Python in the skill's orchestrator script. A Python counter
  cannot drift.
- The **heal convergence check** — is the patched prompt's pass rate above the
  success threshold? is improvement plateauing? is there a regression? — is
  real Python. These are measurements over numbers, not model judgment.
- The **patch proposal** itself — what text to change in a system prompt, and
  where — is a worker output and is therefore advisory. The heal loop does not
  trust it unconditionally: it validates the proposed anchor match before
  applying, and it verifies the improvement by replay rather than by the
  subagent's own assessment.

The heal loop re-applies the evidence-gate discipline from §8: each heal
iteration must show measured improvement (a quantitative outcome, not an
assertion) before it updates the "best patch so far." The loop is bounded; a
cap that cannot be cleared within the bound terminates the heal loop rather
than running forever. The same falsification and convergence discipline that
governs an implementer's confidence loop governs the heal loop's patch
iteration — the number of rounds, the success threshold, and the plateau
detection window are all configured, not left open-ended.

---

## 15. Known limitations

These are honest, designed-in limitations — not bugs, but the known edges of
what the architecture can guarantee.

- **Unattended execution requires broad write permission.** A worker that edits
  files without a human approving each action must run with permission prompts
  suppressed. A narrower "auto-approve edits only" mode was considered and
  rejected: it still prompts on shell commands, which would stall an unattended
  run the first time a worker needs to run one. The blast radius is bounded by
  worktree isolation, not eliminated. Centella should be run on repositories the
  user trusts, ideally inside a container, and the run branch reviewed
  before it is relied on.
- **A worker that exhausts its turn limit without checkpointing loses its
  work.** Handoff depends on the worker writing a checkpoint before it stops. A
  worker that runs out of turns first leaves its successor to start cold. This
  is the most likely failure mode for an under-scoped, too-large subtask —
  which is why planner sizing (§5) is the primary defense.
- **Handoff timing is heuristic.** A worker cannot read its own context
  percentage; it estimates pressure from proxies like transcript length and
  tool-call count. The estimate can be wrong in either direction.
- **Checkpoint quality bounds handoff quality.** Schema validation catches a
  *structurally* incomplete checkpoint; it cannot judge whether a
  structurally-complete checkpoint is *semantically* adequate.
- **Evidence gates reduce overconfidence but do not eliminate it.** Anchoring
  the confidence score to artifacts is a large improvement over a self-reported
  number, but a worker can still misjudge the strength of evidence it did
  gather.
- **Cross-domain dependency detection now goes through a reconciler worker.**
  The scheduler wires cross-domain edges by matching capability tags. If two
  planners describe the same capability with different words, the literal-
  string match would miss the equivalence. A reconciler worker (DESIGN §5)
  catches these mismatches before the scheduler runs: it proposes renames,
  added `provides` declarations, or new connector subtasks. Genuinely
  unresolvable gaps (no plausible match and no reasonable connector) abort
  the run with the reconciler's diagnosis — fail-loud rather than the
  silent-edge-drop the v1 design accepted.
- **Headless usage is metered.** Subscription-based headless usage draws on a
  finite pool, and a large multi-wave run consumes a meaningful amount of it.
  Cost scales with worker count.
- **Parallelism is single-clone.** Multiple concurrent runs in the same git
  clone are explicitly supported via the per-run state and branch design.
  Multiple clones running concurrently are also fine — they are independent
  by construction — but the per-run namespacing applies only within one
  clone; centella does nothing to coordinate across clones (it has no need
  to).
- **Push assumes a remote named `origin`.** Finalize pushes to `origin` and
  opens the PR against the same remote's GitHub repo. A fork pattern where
  the user's write-access remote is named something else (e.g., `mine`
  pushing to a personal fork, `origin` reading from upstream) is not
  supported today; the workaround is `--no-push` plus a manual push. A
  follow-up `--remote <name>` flag is possible but outside the current
  design.
- **System-wide worker concurrency scales with run count.** Each run obeys
  its own `max_parallel` cap; with N concurrent runs the total active
  worker count can be N × max_parallel. The blast radius is bounded per
  run but not globally; users running many concurrent centella invocations
  should be aware of the headless-usage cost implication.

---

## 16. Verification status

A design document should be honest about how much of the system has been
*demonstrated* to work, as opposed to *reasoned* to work. The distinction is
the first thing anyone running Centella needs.

**Demonstrated.** The deterministic scaffolding has been exercised. The git
worktree mechanics — branch setup, per-subtask worktrees, wave-to-wave
dependency layering, conflict detection, finalization, cleanup — have been run
against real repositories. The orchestrator's control flow — classification,
planning, scheduling, wave execution, integration, validation, finalize, and
resume — has been exercised end to end against a stubbed worker, including the
failure and retry paths. The deterministic enforcement points have unit tests.

**Not demonstrated.** No worker has been run against a live model. The contract
with the headless CLI is taken from documentation, not from observed behavior;
first contact with the real CLI is the genuine test. The behavioral quality of
the workers — whether the evidence gates, the handoff, and the conflict
resolution actually work as intended — cannot be known until the prompts run
against a live model. The deterministic surface is sound by construction and
by test; the worker behavior is the unverified surface.

Two parts of the surface described in this document are *new* and have not
yet been exercised end-to-end: the per-run namespacing (run-id derivation,
`.centella/runs/<run-id>/` layout, parallel-run coexistence, multi-run
resume), and the push-and-PR finalization step (`gh pr create`, run.json
sidecar with `pushed_at`/`pr_url`/error fields, `--no-push` and
`--no-verify`). The single-run, local-finalize design described in earlier
revisions of this document has been exercised; the broader design here
becomes verified only after the corresponding code lands and a first run
exercises it.

**Recommended first step.** Run Centella once on a throwaway repository with a
small, fully-specified task before trusting it on real work.

---

## 17. Traceability to the original specification

Every requirement of the original eight-step specification is accounted for in
the design. Where the design departs from the original wording, the departure
is deliberate and is justified in the section named.

| Original requirement | Where it lives in the design | Note |
|----------------------|------------------------------|------|
| Classify the task into 8 categories | §4; Phase 1 | — |
| A subagent per category | §3, §4; Phase 2 planners | Planners *return plans*; they do not spawn. Forced by Constraint 1 (§2). |
| Decompose into the most granular subtasks | §5 | Target narrowed to *smallest independently verifiable unit* — "most granular possible" over-decomposes (§5). |
| Determine parallel vs. sequential — waves | §5; Phase 3 | Done globally over a merged dependency graph, not per-domain. |
| A subagent per granular subtask | §3; Phase 5 implementers | — |
| Define success criteria | §9 | Tests preferred; frozen once implementation starts. |
| Plan the change | §8 | — |
| Confidence 1–10 on root cause and solution | §8 | Kept, but anchored to evidence gates — a self-reported number is not a measurement. |
| Loop until confidence ≥ 9 | §8 | Kept, bounded, and gated on evidence rather than intuition. |
| Implement the change | §3; Phase 5 | — |
| Validate against criteria; loop until met | §8, §9 | Criteria revision is proposal-only — the checker cannot rewrite its own bar. |
| Reassess criteria if strong evidence | §9 | Allowed, but proposal-only and orchestrator-approved. |
| Fully automated, no questions | §11 | Default zero questions; the derive-or-research filter defines the only exception. |
| Gather information from the codebase | §11 | Codebase first, research second, user only for genuine intent. |
| Compact context at 70% | §10 | Replaced by orchestrator-driven handoff — no channel exists to trigger self-compaction. A lower auto-compaction threshold is an optional backstop only. |
| (implicit) bounded cost | §13 | A hard cap on total workers; the original bounded every inner loop but not total fan-out. |

---

## 18. Future work

Directions that would strengthen the system but are not part of the current
design:

- **Token-aware budgeting** instead of a blunt worker count — bound a run by
  cost rather than by number of workers.
- **Subtask-level resume.** Resume is currently wave-granular: work done since
  the last fully-completed wave is re-run. Finer-grained resume would re-run
  less.
- ~~A dependency-graph sanity pass~~ — implemented as the reconciler worker
  (§5 and §15). After all planners finish, a reconciler worker
  resolves vocabulary drift between domains' capability tags before the
  scheduler builds its DAG.
- **Per-domain implementer specialization.** One generic implementer serves all
  eight domains today. Eight domain-specialized implementers would allow richer
  per-domain guidance, at the cost of more to maintain.
