---
description: Launch the Pila orchestrator on a task. Use when the user asks to autonomously decompose and execute an engineering task with pila.
argument-hint: <task description>
---

# Launch Pila

The user wants to run the Pila orchestrator on this task:

```
$ARGUMENTS
```

Pila is a deterministic Python orchestrator (it does not run inside this
session — it spawns its own `claude -p` workers in a container). Launch
it and relay the clarification step if one occurs.

**Runtime prerequisite**: pila runs inside a container per run for
guaranteed subprocess cleanup. The launcher itself does the preflight
and prints `brew install colima` / `apt-get install containerd` hints
on failure. If launching fails with a runtime-missing message, relay
the launcher's hint verbatim — do not try to install runtime
dependencies on the user's behalf.

## Steps

1. Run the orchestrator from the current repository root. Pass
   `--clarify` so the orchestrator surfaces classifier intent
   questions through this Claude Code session rather than running
   unattended — the user is here in chat, so this session is the
   relay channel. (`CLAUDE_PLUGIN_ROOT` is the absolute path to this
   plugin's install directory; Claude Code sets it automatically.)

   ```
   bash "${CLAUDE_PLUGIN_ROOT}/pila" --clarify "$ARGUMENTS"
   ```

   The launcher spins up a container, mounts the user's repo at
   `/work`, mounts `~/.claude/` read-write so workers can authenticate
   and update Claude Code session state (history, sessions, plans),
   bind-mounts `~/.config/gh`, `~/.git-credentials`, and `~/.ssh` so
   the finalize-phase push + PR work with the user's existing GitHub
   auth, and execs the orchestrator inside. Stdout streams back
   through the Bash tool to this chat session.

   Because the launcher detects this session has no TTY on its
   stdin, it runs the container with `-i` only (no pty). Inside the
   container, `sys.stdin.isatty()` returns False, so pila's
   clarification path will use the file-passing dance below
   instead of prompting interactively — exactly as it does today.

2. **If it exits with code 10**, the orchestrator needs the user to answer
   classifier intent questions before it can continue. Read
   `.pila/pending-questions.json`, present each question to the user
   verbatim, and collect their answers.

3. Write the answers as a JSON object to `.pila/answers.json`, keyed by
   each question's `id`. The user can also override the source-of-truth
   preference for this run by including `source_of_truth` set to `codebase`,
   `research`, or `both` (otherwise the resolved preference applies, default
   `both`). They can pin the model with `--model sonnet|opus|haiku` (env:
   `PILA_MODEL`); per-worker overrides via `--model-<worker>` /
   `PILA_MODEL_<WORKER>`. Per-worker defaults: judgment workers
   (classifier, planner, reconciler, provision, integrator) default to `opus`;
   acting workers (implementer, conformer) default to `sonnet`.
   Then resume:

   ```
   bash "${CLAUDE_PLUGIN_ROOT}/pila" --clarify --resume --answers .pila/answers.json
   ```

   (If `--resume` reports the run had not reached scheduling, re-run without
   `--resume`, passing the original task and `--answers .pila/answers.json`.)

4. Relay the orchestrator's final summary to the user. On any non-zero, non-10
   exit, show the error and point them at `.pila/state.json`. If the
   failure looks like a Pila bug rather than a task-execution problem,
   point the user at https://github.com/enricai/pila/issues with the
   contents of `.pila/state.json` (redacted).

For long runs, prefer telling the user to run `pila` directly in a terminal —
this session's context fills with orchestrator output otherwise. (Requires the
terminal install — see README "From a terminal".)
