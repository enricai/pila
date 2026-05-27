# Security Policy

## Supported versions

Centella is pre-1.0. Only the latest minor release line receives security
fixes.

| Version | Supported |
|---------|-----------|
| 0.2.x   | yes       |
| < 0.2   | no        |

Because Centella is pre-1.0, the public surface (CLI flags, `.centella/`
layout, worker schemas, `centella.toml` keys) may change between minor
versions. Pin a commit if you need stability.

## Reporting a vulnerability

Email **andres@enricai.com** with the subject prefix **`[centella-security]`**.
Please do not open a public GitHub issue or pull request for a suspected
vulnerability.

What to include:

- A description of the issue and its impact
- A minimal reproduction (task, repo state, invocation)
- The Centella commit you reproduced on
- Your contact info for follow-up

What to expect:

- **Acknowledgment within 7 days** of receipt
- A coordinated disclosure timeline negotiated with the reporter, typically
  30–90 days depending on severity and the fix's complexity
- Credit in `CHANGELOG.md` on release of the fix, unless you ask to remain
  anonymous

## Threat model context

Centella's threat model is shaped by one load-bearing fact: **acting workers
run `claude -p --dangerously-skip-permissions`**. That is intentional — it is
what makes the run unattended. The mitigation is not removing the flag; it
is the worktree isolation and staging-branch review documented in
[`docs/DESIGN.md`](docs/DESIGN.md) §6, and the deterministic enforcement
boundary documented in [`docs/DESIGN.md`](docs/DESIGN.md) §12. See also
the [README "Safety" section](README.md#safety).

### Vulnerabilities (please report)

Any defect that violates the documented isolation or enforcement boundary:

- **Worktree escape** — a script in `scripts/*.sh` resolving `..` or a
  symlink into the main checkout, letting a worker write outside its
  worktree
- **State-write vulnerabilities** — `validate_resume_state()` or the
  `State.save()` write path being exploitable via a poisoned `.centella/`
  directory (e.g., an attacker writing `.centella/state.json` so the next
  `--resume` does something unintended)
- **Command injection** — unquoted or unsanitized expansion in
  `scripts/*.sh` that lets a task description, repo name, or filename
  inject shell commands
- **Auto-merge bypass** — any defect causing a subtask branch to land
  on the run branch (`centella/runs/<id>`) without the documented
  integrator gates, or causing the run-branch validation step to be
  skipped. (Phase 6 does not merge into the working branch — it pushes
  the run branch and opens a PR; the human review on that PR is the
  user-facing safety boundary.)
- **Schema bypass** — any path where a worker's output is consumed
  without passing through its `SCHEMAS` entry (see CLAUDE.md "Mandatory
  requirements")

### Not vulnerabilities

These are accepted risks of running Centella as designed; please do not
report them as security issues:

- **A worker doing something destructive inside its own worktree** —
  that is the expected behavior under `--dangerously-skip-permissions`,
  bounded by worktree isolation. Review staging before merging.
- **A worker's commit being merged into staging by the integrator** —
  the integrator does exactly that by design; the safety boundary is at
  the user's review of the phase-6 PR, not at staging.
- **Running on a repository whose `claude` CLI is misconfigured** —
  Centella does not validate the user's `claude` credentials or
  permissions; this is upstream of the orchestrator.
- **High disk usage from worktrees** — each subtask gets its own
  worktree; resource consumption is operational, not adversarial.
