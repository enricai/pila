---
description: Launch the Centella orchestrator on a task. Use when the user asks to autonomously decompose and execute an engineering task with centella.
argument-hint: <task description>
---

# Launch Centella

The user wants to run the Centella orchestrator on this task:

```
$ARGUMENTS
```

Centella is a deterministic Python orchestrator (it does not run inside this
session — it spawns its own `claude -p` workers). Launch it and relay the
clarification step if one occurs.

## Steps

1. Run the orchestrator from the current repository root. Pass
   `--clarify` so the orchestrator surfaces classifier intent
   questions through this Claude Code session rather than running
   unattended — the user is here in chat, so this session is the
   relay channel:

   ```
   bash "${CLAUDE_PLUGIN_ROOT}/centella" --clarify "$ARGUMENTS"
   ```

2. **If it exits with code 10**, the orchestrator needs the user to answer
   classifier intent questions before it can continue. Read
   `.centella/pending-questions.json`, present each question to the user
   verbatim, and collect their answers.

3. Write the answers as a JSON object to `.centella/answers.json`, keyed by
   each question's `id`. The user can also override the source-of-truth
   preference for this run by including `source_of_truth` set to `codebase`,
   `research`, or `both` (otherwise the resolved preference applies, default
   `both`). They can pin the model with `--model sonnet|opus|haiku` (env:
   `CENTELLA_MODEL`); per-worker overrides via `--model-<worker>` /
   `CENTELLA_MODEL_<WORKER>`. Then resume:

   ```
   bash "${CLAUDE_PLUGIN_ROOT}/centella" --clarify --resume --answers .centella/answers.json
   ```

   (If `--resume` reports the run had not reached scheduling, re-run without
   `--resume`, passing the original task and `--answers .centella/answers.json`.)

4. Relay the orchestrator's final summary to the user. On any non-zero, non-10
   exit, show the error and point them at `.centella/state.json`. If the
   failure looks like a Centella bug rather than a task-execution problem,
   point the user at https://github.com/enricai/centella/issues with the
   contents of `.centella/state.json` (redacted).

For long runs, prefer telling the user to run `centella` directly in a terminal —
this session's context fills with orchestrator output otherwise.
