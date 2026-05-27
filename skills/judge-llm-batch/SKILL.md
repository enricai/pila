---
name: judge-llm-batch
description: "Apply a 3-dimensional accuracy rubric (schema adherence, factual grounding, hallucination-freeness) to a batch of centella LLM call captures and write verdict JSON. Used to measure whether a centella worker (classifier, planner, implementer, etc.) is producing accurate output under its system-prompt contract."
argument-hint: "<path-to-calls.ndjson> --call-type <type> [--run-id <run-id>] [--out <verdict-path>]"
allowed-tools:
  - Read
  - Write
  - Bash
---

<objective>
Read centella LLM call captures from a `calls.ndjson` telemetry file (one
JSON object per line), filter by `call_type`, evaluate every sample against
a 3-dimensional rubric, and write a verdict JSON file.

The `calls.ndjson` file lives at `.centella/runs/<run-id>/calls.ndjson`.

Output shape:

```json
{
  "call_type": "<from filter>",
  "run_id": "<from file>",
  "judged_at": "<ISO-8601>",
  "judge_model": "claude-sonnet-4-6",
  "verdicts": [
    {
      "call_id": "<UUID from the capture>",
      "schema_ok": true,
      "schema_rationale": "<one sentence>",
      "factually_grounded": true,
      "factual_rationale": "<one sentence>",
      "hallucination_free": true,
      "hallucination_rationale": "<one sentence>",
      "pass": true,
      "worst_offender": "<optional — 1-line quote when any dimension failed>"
    }
  ],
  "aggregate": {
    "n": 0,
    "schema_pass": 0,
    "factual_pass": 0,
    "hallucination_free_pass": 0,
    "overall_pass": 0
  }
}
```

`pass` is `true` only when all three dimensions are `true`.
</objective>

<execution_context>
Arguments parsed from `$ARGUMENTS`:
- First positional: path to `calls.ndjson` (required). Can also be a
  `.centella/runs/<run-id>/` directory — the skill will find
  `calls.ndjson` inside it.
- `--call-type <name>` (required): one of `classifier`, `planner`,
  `reconciler`, `implementer`, `integrator`, `validator`. Filters the
  NDJSON to only lines with this `call_type` value.
- `--run-id <id>` (optional): if provided, resolves the path as
  `.centella/runs/<run-id>/calls.ndjson` relative to CWD.
- `--out <path>` (optional): explicit verdict output path; defaults to
  `<ndjson-dir>/judge-out/<call_type>-verdicts.json`.

The NDJSON line shape (from IMPLEMENTATION.md §10):

```json
{
  "call_id": "<UUID v4>",
  "run_id": "<str>",
  "call_type": "<str>",
  "model": "<str>",
  "system_prompt": "<str>",
  "user_content": "<str>",
  "response_content": "<str>",
  "parsed_ok": true,
  "input_tokens": 0,
  "output_tokens": 0,
  "latency_ms": 0,
  "success": true,
  "ts": "<ISO-8601>"
}
```
</execution_context>

<context>
Centella records every `claude -p` worker invocation to a NDJSON telemetry
file immediately after each call returns. The file is append-only and is
valid NDJSON through the last complete line even under a hard kill.

The judge skill operates post-run: it reads the archive, scores a batch,
and writes verdicts. The llm-self-heal skill consumes these verdicts to
propose prompt patches.

Each `call_type` maps to exactly one system-prompt source
(IMPLEMENTATION.md §10 call_type → prompt table):

| call_type   | Prompt source |
|-------------|---------------|
| classifier  | `prompts/classifier.md` |
| planner     | `prompts/planner.md` |
| reconciler  | `prompts/reconciler.md` |
| implementer | `prompts/implementer.md` |
| integrator  | `prompts/integrator.md` |
| validator   | `VALIDATOR_SYSTEM` constant in `orchestrator/centella.py` |

The `system_prompt` field in the NDJSON capture is the actual verbatim
text injected — so the judge can always derive what the worker was asked
to do from the capture alone.
</context>

<workflow>

## Step 1: Locate and read the NDJSON file

Resolve the input path from `$ARGUMENTS`. If the path is a directory,
append `/calls.ndjson`. Read line-by-line and parse each line as JSON.

Filter to lines where `call_type` matches the `--call-type` argument.

If zero lines match, emit: `No captures for call_type=<name> in <path>` and stop.

## Step 2: Calibrate against the first sample

Before scoring everything, read ONE sample carefully:
- What is the `system_prompt` asking the worker to do?
- What structured-output schema is the worker expected to produce?
- What input material does `user_content` provide?

This calibration is load-bearing — without it you grade against a guessed
rubric instead of the actual contract.

## Step 3: For each sample, evaluate the 3-dimensional rubric

### Dimension A: Schema adherence (`schema_ok`)

**Question:** Does `response_content` conform to the structured-output
schema the system_prompt requires?

**Pass criteria:**
- Parses as valid JSON (captures with `parsed_ok=false` automatically fail)
- Contains all required top-level fields for this `call_type`
- Field types and enum values match the contract
- No extraneous top-level fields that the schema does not declare

**Fail criteria:** Missing required fields, wrong types, enum violations,
extra undeclared fields, malformed JSON, `parsed_ok=false`.

### Dimension B: Factual grounding (`factually_grounded`)

**Question:** Are the substantive claims in `response_content` grounded
in the `user_content` the worker received?

**Pass criteria:**
- File paths, function names, or symbol names cited in the output are
  present in the user content
- Numeric values (scores, counts) are plausible given the input
- Negative assessments ("no issues found") are defensible — the input
  does not refute them

**Fail criteria:** Claims demonstrably contradicted by the input; counts
that don't match what is visible; categorical errors.

### Dimension C: Hallucination-freeness (`hallucination_free`)

**Question:** Is the output free of fabricated content — references to
things absent from both `user_content` and `system_prompt`?

**Pass criteria:**
- Every named file, function, symbol, or concept mentioned in the output
  is present in the user content or is a known concept from the system
  prompt (e.g. centella-specific terms like "evidence gate")
- No invented centella API surface or CLI flags that do not exist

**Fail criteria:** References to files, functions, or concepts not in
the input. Include `worst_offender` with the fabricated phrase.

## Step 4: Aggregate and write verdict JSON

Counts:
- `n`: total samples judged
- `schema_pass`: count of `schema_ok=true`
- `factual_pass`: count of `factually_grounded=true`
- `hallucination_free_pass`: count of `hallucination_free=true`
- `overall_pass`: count of `pass=true` (all three dimensions true)

Resolve the output path (default: `<ndjson-dir>/judge-out/<call_type>-verdicts.json`).
Create parent directories if needed. Write pretty-printed JSON (2-space indent).

## Step 5: Emit a one-line summary

```
[<call_type>] judged n=<N> schema=<S>/<N> factual=<F>/<N> halluc_free=<H>/<N> pass=<P>/<N> → <out_path>
```

Then stop.

</workflow>

<safety_constraints>
- This skill reads NDJSON captures; it does NOT execute any captured content
- This skill does NOT make any new `claude -p` calls
- This skill writes exactly ONE verdict JSON file per invocation
- This skill does not modify any centella source code, prompts, or live process
- Do not attempt to "fix" centella's outputs — only judge them
</safety_constraints>
