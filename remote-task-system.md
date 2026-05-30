# Remote Task Execution System — Platform-Agnostic Design

A developer fires off coding tasks from a laptop. Each runs on remote hardware in an isolated environment, seeded with a faithful snapshot of the developer's full working tree, with live two-way terminal I/O and the option to attach mid-run. Each task ends in a resolution: a GitHub PR, or a paused failure the user handles.

## Driving Mode

Programmatic launch with an interactive escape hatch. Tasks fire autonomously and run unattended (launch-and-forget). A human can attach to a live task to inspect or correct, then detach. The normal path has no human in the loop; the interactive channel is a capability, not the default.

## Components

**Substrate** — Remote, on-demand, no always-on instance. Nothing runs until a task launches. The first task pays cold-start latency; this is the inherent cost of refusing a warm pool.

**Per-task isolation** — Each task runs in its own container/microVM: isolated filesystem, network, and process space. No per-task image build (shared base), so spin-up is fast. Parallel tasks are N independent environments.

**Seeding — two channels, by content type:**

- *Committed bulk* → `git clone --filter=blob:none` from GitHub on the remote side. Full history (required — see worktree constraint), lazy blob backfill on demand over the reliable connection. Latency moves from upfront to spread-across-the-run, which is acceptable. A host-side partial-mirror cache accelerates recurring repos; cold repos clone straight from GitHub (multi-repo, so no single shared mirror).
- *Uncommitted/untracked delta* → scoped rsync from the laptop. Git computes the dirty set (modified + untracked, minus ignored); only those files cross the slow laptop uplink.

Result: a directory identical to the developer's working tree at launch.

**Live I/O** — A PTY over SSH into the running task. Keystrokes up, stdout/stderr streaming back in real time, latency bounded by network RTT. This single channel serves three roles: the "feels-local" interactive terminal, the mid-run attach mechanism, and the failure-inspection surface. Terminal-only access (no full IDE required).

**Two pipes, kept separate — this is what makes it feel local:**

```
files:    laptop -> (git clone committed + rsync uncommitted delta) -> task
                    [seeded once at launch; re-rsync on demand]
terminal: laptop <-> SSH PTY <-> task
                    [live, bidirectional, real-time, when attached]
```

Files are seeded once and re-synced only on request; the terminal streams continuously when attached. Independent channels, each optimized for its own latency profile.

## Task Lifecycle — Two Terminal States

```
running -> +-- success -> git push -> PR -> teardown
           |
           +-- failure -> PAUSE (hold state, report what went wrong) -> user decides:
                            |
                            +-- correct (rsync delta) -> resume
                            |
                            +-- kill -> teardown
```

- **Success (optimistic):** task completes, pushes, opens a PR. No human needed.
- **Failure (pessimistic):** the task does *not* die. It **suspends with state intact** — process tree, filesystem, partial work, logs preserved — and reports the failure out to the user. The user attaches to the failed environment to diagnose, then either corrects-and-resumes or kills.

**Mid-run correction** = a second rsync of current laptop state into the task, user-triggered. No consistency problem — the user picks the moment. **Resume means resume, not restart:** the task continues from its paused state, not the launch snapshot, or "continue" would discard work already done. The mid-run edits get committed inside the task and flow to the PR; nothing returns to the laptop.

**Output** is always a resolution, never silent: either a PR (success) or a surfaced failure report (pause). The failure path requires an outbound notification channel — distinct from the silent success path, and a piece you build rather than get for free.

**Teardown** — Each task's isolation boundary reaps the entire process tree cleanly on success or explicit kill. No descendant-chasing, no escaped daemons. (This is the original problem that motivated the whole design, solved as a structural side effect rather than bolted on.)

## Two Hard Constraints to Hold Onto

**Worktree constraint on the clone.** Once running, the coding system spawns multiple `git worktree`s that share one object database and perform history-dependent operations (rebase, merge, diff across the graph). This forces the per-task clone to be **full-history**: partial clone (`--filter=blob:none`, lazy contents) is fine *because the connection is reliable*; shallow clone (`--depth`) is disqualified — it truncates history and breaks worktrees.

**Uncommitted-state seeding is a build item, not a platform feature.** Every sandbox/orchestration platform seeds from a *git reference*, which by definition excludes uncommitted edits, untracked files, and local config. So the committed half comes from the platform's native clone, but the uncommitted delta-sync (the rsync step) is something you wire in yourself. It won't appear on any platform's feature list, because from their perspective git *is* the input. Any "just use platform X" plan is really "use X *plus* this seeding layer."

## Platform Requirement (Abstract, Not a Vendor)

The chosen execution layer must provide **real suspend-and-resume** — pause a task with full filesystem *and* memory state held, resume later from that exact state — not just create/run/destroy. "Pause on failure, hold for human inspection, then continue" is more specific than "scale to zero when idle"; kill-and-recreate would lose the in-flight state the failure path depends on.

## Requirements Check

- [x] Remote, parallel, isolated execution
- [x] No always-on (cold-start cost accepted)
- [x] No per-task image build / fast spin-up
- [x] Committed + uncommitted + untracked, multi-repo (clone + rsync bolt-on)
- [x] Full-history clone for worktree support (partial OK, shallow excluded)
- [x] Snapshot-and-go + correct mid-run (re-rsync; resume != restart)
- [x] Feels-local, real-time terminal I/O (PTY, RTT latency)
- [x] Two terminal states: PR or paused-failure-with-report
- [x] Clean process-tree teardown
- [x] Crash-durable + resumable (state in git, not memory — see correction below)

---

# Foundation: pila Is Most of This Already

The design above was reasoned in the abstract. In practice, **`enricai/pila`
already implements most of it** — pila is not a driver that plugs into this
architecture; it *is* the orchestration + driver + teardown layers, built to
the same "launch-and-forget → PR" goal. The work is not "assemble a system from
parts." It is "add a remote execution mode to pila."

## A correction to the abstract requirement

The doc above listed "suspend-resume with **memory** state" as a hard platform
requirement. **That was over-specified.** pila demonstrates the better model:
the **run branch is the durable record** (every integrated wave is a commit on
`pila/runs/<run-id>`), and `--resume` reconstructs from it. A reboot, kill, or
rate-limit loses nothing because state lives in git, not in a memory snapshot
that dies with the host. The real requirement was always *crash-durable +
resumable*, which resume-from-git satisfies more robustly than memory-snapshot
ever could. **This widens the runtime field** — any tear-down-and-reprovision
runtime works, because the state isn't in the box.

## What pila already provides (reuse as-is, zero build)

| Layer | pila mechanism (DESIGN §) |
|---|---|
| Orchestration | Deterministic Python: classify → plan → schedule → wave-execute → integrate → PR (§3) |
| Worktree-per-task | Each implementer gets an isolated git worktree; parallel writes never collide (§6) |
| Driver | `claude -p` headless workers on your subscription — no API key (§2) |
| Output = PR | Run branch pushed, PR opened against working branch; working branch untouched (§6) |
| **Clean teardown** | **Container PID-namespace reaping — the kernel reaps every detached/daemonized survivor when the orchestrator exits (§6).** This *is* the answer to the process-tree problem that started this whole design. |
| Resumable failure | Run branch is the durable record; `--resume` picks up from last completed wave; rate-limit auto-resumes (§6) |
| Blocked-with-evidence | A worker that can't justify confidence exits `blocked` with a gap analysis (§8) |
| Uncommitted state (local) | Repo bind-mounted at `/work`; local runs see the working tree natively (§6½) |

## The seam for remote mode: the §6 container boundary

pila already abstracts execution behind a container: *"the orchestrator and
every worker run inside a single container — containerd-managed: on Linux
native, on macOS via a Colima-managed Linux VM"* (§6). Today the choice is
**native containerd vs. Colima-wrapped containerd, selected by host OS.**
"Remote" is the same kind of choice one level out: **which containerd the
launcher targets.** Adding remote mode extends an abstraction that already
exists — it does not bolt a new concept onto a local-only design.

**On macOS, Colima exists solely to provide a Linux kernel.** A remote Linux
VM already has one. So in remote mode, **`nerdctl run` talks to containerd
running directly on the remote VM instead of to Colima's local VM.** The
isolation is identical — it comes from containerd + the Linux kernel's PID
namespace, not from Colima. Colima is just the macOS delivery mechanism for
that kernel. Same containerd, same `nerdctl run`, same PID-namespace reaping
— on a different machine.

**Colima does not go remote.** It stays as the local-macOS path. Remote mode
bypasses it entirely because the remote host is already Linux.

## Two modes, mostly shared

```
                         ┌─ LOCAL  (today): nerdctl → Colima VM → containerd (macOS)
                         │                  nerdctl → containerd directly  (Linux)
pila ── container target ┤
                         └─ REMOTE (add):   platform API → microVM instance
                                            (the microVM IS the remote Colima)
                                            pay-per-minute, zero when idle

Isolation: LOCAL = containerd PID namespace inside Colima's VM
           REMOTE = Firecracker microVM (hardware-level — stronger than local)
```

## Build-vs-reuse, scoped against DESIGN.md

| Layer | Local (today) | Remote (to add) | Difficulty |
|---|---|---|---|
| Worker spawn + teardown | container PID-ns reaping | same, on remote container | **Easy** — §6 guarantee transfers verbatim; orchestrator is unmodified |
| Runtime target | `nerdctl run` → Colima → containerd (macOS) or containerd directly (Linux) | microVM platform API starts pila's image (replaces Colima; stronger isolation) | **Medium** — launcher replaces `nerdctl run` with platform SDK/API call; orchestrator unchanged |
| Provisioning | runtime already present | handled by the microVM platform (on-demand, sub-second, pay-per-minute) | **Solved by platform choice** — no DIY provisioner needed |
| Code seeding | bind-mount host repo at `/work` | git clone + rsync uncommitted delta | **Build** — §6½ confirms worktrees skip untracked files anyway; the `/work` bind mount is what local mode relies on |
| Worker auth (credential) | forward `$CLAUDE_CODE_OAUTH_TOKEN` | forward the same token to remote | **Easy-Medium** — one token, already env-forwarded |
| Worker config | bind-mount `~/.claude` / `~/.gitconfig` | seed config files; set git identity directly | **Medium** — plain files, not credentials |
| **Finalize (push/PR)** | **host, post-exit, local auth** | **gh token + SSH keys/agent must cross to remote** | **Hard — no single-token equivalent; see below** |

## Port 1 candidates: the remote equivalent of Colima

The right abstraction: **1 local Colima instance = 1 remote microVM instance.**
Both serve the same role — give pila a Linux environment with process isolation.
The difference is who provides it and how you pay: Colima is free on your
laptop; a microVM bills per minute of actual use, zero when idle.

```
LOCAL                                    REMOTE
macOS                                    microVM platform
  └─ Colima (Linux VM)                     └─ microVM instance
       └─ containerd                            └─ pila's image runs directly
            └─ pila container (PID 1)                (PID 1 = orchestrator)
```

Remote is actually *simpler*: locally, Colima hosts a VM that hosts containerd
that hosts a container — three layers. A microVM runs pila's image directly as
its rootfs — the microVM *is* the isolation boundary. Fewer layers, same
guarantee (actually stronger — hardware-level isolation instead of kernel
namespace isolation).

What the Port 1 substrate must provide:

- A Linux environment that can run pila's existing Docker image.
- PID-namespace-grade isolation or stronger — pila's §6 teardown depends on it.
- On-demand, sub-second startup — no always-on, no VM-boot-per-task.
- Pay only for actual execution time — zero cost when nothing runs.
- Templates / custom images supported (so pila's Dockerfile can be the base).

The candidates, scored against those criteria:

| Candidate | Isolation | Startup | Cost model | Self-host | Fit for remote-pila |
|---|---|---|---|---|---|
| **E2B** | Firecracker microVM | ~150ms | **per-second; ~$0.05/hr per vCPU; $0 when idle** | OSS core self-hostable; BYOC enterprise | **Best fit.** Runs any Docker image as a template. Docker-in-Sandbox supports nested containers. Per-second billing, no always-on. Pila's image runs directly inside the microVM as PID 1 — same as Colima locally but pay-per-use. |
| **Fly.io Machines** | Firecracker microVM | seconds | **per-second; from ~$0.003/hr; suspend/resume for cost control** | no (managed) | Good fit. Runs Docker images. Machines can auto-stop and resume. Slightly more general-purpose (not sandbox-first), but the Firecracker base is the same. |
| **Daytona** | container (+VM runners) | sub-90ms | **per-second; $200 free credit** | managed; self-host AGPL | Good fit for fast start. Container-default isolation is weaker than microVM but sufficient for pila's cleanup contract. |
| **Northflank (BYOC)** | microVM (Kata/Firecracker/gVisor) | sub-second | **per-vCPU-hour; BYOC = flat fee + your cloud cost** | yes (BYOC) | Managed orchestration on your cloud. Strongest if you need BYOC/data-sovereignty. Introduces a second orchestrator above pila — some overlap. |
| ~~Raw cloud VM + containerd~~ | container (PID-ns) | **VM-boot = minutes** | always-on or per-hour | yes | ~~Previously recommended; **violates no-always-on and no-VM-warm-up requirements.** Only viable if you keep the VM running (rejected) or accept minute-scale cold starts per task (rejected).~~ |

**The honest read:** for pila's workload — launch on demand, run for minutes to
hours, tear down, pay nothing when idle — **a microVM platform (E2B, Fly.io, or
Daytona) is the right substrate.** The microVM replaces Colima remotely: same
role, same abstraction, pay-per-minute instead of free-on-your-laptop. Pila's
existing Docker image runs inside it. The launcher's adaptation is: replace the
`nerdctl run` call with the platform's API/SDK call to start the same image in
a microVM. The orchestrator is unchanged.

The raw-cloud-VM recommendation was wrong — it requires either an always-on
host or a cold VM boot per task, both of which violate established requirements.

## Patching pila vs. building around it

Because pila is yours, the local/remote mode-switch can be either patched
*into* pila (extending the launcher to accept a remote `nerdctl` target) or
implemented *around* it (a wrapper that prepares a remote host, then invokes
pila normally). Same tradeoff that applied to extending any open-source tool:

| Change | Patch *into* pila | Build *around* pila |
|---|---|---|
| Launcher microVM target | moderate — replace `nerdctl run` with platform SDK call in the launcher | unnecessary indirection; the launcher is the right place |
| Provisioning | **eliminated** — the microVM platform handles on-demand start/stop/billing | — |
| Auth/config plumbing | touches pila's mount table / env forwarding | wrapper can prep auth state before pila starts |
| Remote finalize | conflicts with pila's host-finalize design (§6) | wrapper can intercept finalize, or stream branch back |

The launcher change belongs *in* pila (it's a localized extension of an
existing abstraction). The provisioning + auth-staging + finalize handling
belong *around* pila (they're a new lifecycle that wraps a pila run, not a
modification of how pila works internally). Treating them as one project
muddies pila's concern boundary; treating them as two preserves pila's local
mode unchanged and makes the remote mode a composable layer.

## The hard problem is auth-crossing — but it splits into three difficulties, not one

This is the key correction, grounded in the bind-mount table (IMPLEMENTATION.md
§0.5). The runtime swap is the *easy* part: the launcher's whole container
invocation is one `nerdctl run` call, and *"`orchestrator/pila.py` is unmodified
by this design — container/process isolation is the launcher's concern."* The
orchestrator is decoupled from where it runs. Pointing the launcher at a remote
`nerdctl`/containerd is a bash-level change, not Python surgery.

The four host mounts the container depends on are **not one problem** — they are
three, at very different difficulties:

| Host mount | What it actually is | Remote-mode difficulty |
|---|---|---|
| `$CLAUDE_CODE_OAUTH_TOKEN` | the worker **auth credential** | **The real crossing — but it's one token, already forwarded as an env var.** It exists *because* the true source (macOS Keychain / Linux `~/.claude/credentials.json`) doesn't cross cleanly; the env pass-through is already the workaround. Extending it to a remote host is plausibly straightforward. |
| `~/.claude` + `~/.claude.json` | Claude Code **config / session state** | **Medium — plain files, not credentials.** Workers read/write these for non-auth behavior. Seed or sync them to the remote container; nothing Keychain-bound. |
| `~/.gitconfig` | commit **identity** (name/email) | **Trivial.** Reconstruct on the remote side — set `user.name`/`user.email` directly; the file isn't even needed. |

So **worker auth is narrower than it first looks**: it reduces to "forward one
OAuth token," which the design *already does*. The config mounts are a separate,
easier "make these files present remotely" task. Don't conflate the credential
(one token) with the config files (seed them) — they're different jobs.

**The genuinely hard auth problem is finalize.** §6 moved push/PR to the host
specifically because the gh token, SSH keys, and SSH agent socket are
Keychain/agent-bound and have *no* clean single-token equivalent to forward:

> *"Auth state — gh tokens, SSH agent sockets, Claude Code's OAuth token in
> macOS Keychain — lives in host processes that don't traverse the Lima VM
> boundary cleanly."* (§6)

Local mode dodges this entirely by running push/PR on the host after the
container exits. Remote mode can't: the work is now on a different machine than
the gh/SSH credentials. This is the part with no easy answer — options (each a
subproject): push from the remote host with forwarded short-lived gh
credentials; run an auth proxy; or keep workers remote but stream the run branch
back to the host and finalize locally as today. The credential-forwarding trick
that solves worker auth (one env token) doesn't transfer, because git/gh auth
isn't a single forwardable token.

## Sequencing risk (DESIGN §16)

pila's own verification status: **no worker has been run against a live model
yet** ("first contact with the real CLI is the genuine test"), and the per-run
namespacing + push/PR finalize are flagged as *new and not yet exercised
end-to-end*. Remote mode depends on the finalize/launcher boundary — which is
itself unverified. Order of operations: **prove local-mode finalize against a
live model first; only then add the remote boundary on top of a known-good
base.** Building remote on top of an unexercised finalize layer stacks two
unknowns.

## What to prove, in order

1. **Local mode, live model, end to end** — the §16 "genuine test." Everything
   else is built on this.
2. **Local finalize (push/PR) works** — the new, unexercised §6 finalize path.
3. **Remote microVM target** — adapt the launcher to start pila's image on a
   microVM platform (E2B / Fly.io / Daytona) instead of via `nerdctl run`;
   confirm pila runs inside a microVM the same way it runs inside Colima locally.
   This is the "1 Colima = 1 microVM" validation.
4. **Remote worker auth + config** — forward `$CLAUDE_CODE_OAUTH_TOKEN` (one token,
   already env-forwarded) and seed `~/.claude` config + git identity into the
   microVM, so the `claude -p` workers authenticate and run.
5. **Remote seeding** — git clone (full history, `--filter=blob:none`) + rsync
   uncommitted delta into the microVM; confirm untracked files arrive and
   worktrees still function.
6. **Remote finalize/auth** — the genuinely hard one: gh token + SSH keys/agent
   crossing for push + PR, with no single-token equivalent. Last to solve, on top of
   five known-good layers.

