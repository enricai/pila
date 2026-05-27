---
name: Bug report
about: Report a defect in Centella
labels: bug
---

## What happened

<!-- One or two sentences. What did Centella do that it shouldn't have? -->

## What you expected

<!-- What should it have done instead? -->

## Reproduction

- **Task you ran:**
  ```
  <the exact `centella "..."` invocation, or the slash-command form>
  ```
- **Repo state:** branch, roughly how dirty, any non-default `centella.toml`
- **Other relevant flags:** `--source-of-truth`, `--model` / `--model-<worker>`, `--max-workers`, `--max-parallel`, `--clarify`, etc.

## Environment

- OS: <e.g., macOS 14.5, Ubuntu 22.04>
- Python: `python3 --version`
- `claude --version`:
- Centella commit: `git -C /path/to/centella rev-parse HEAD`

## Relevant state

Paste the relevant fields from `.centella/state.json` (redact anything
sensitive — task descriptions can contain repo-internal context). The full
schema is in [`docs/IMPLEMENTATION.md`](../../docs/IMPLEMENTATION.md) §8 if
you want to know what each field means.

```json
{ }
```

## Which layer is the defect in?

Centella separates theory (`docs/DESIGN.md`), mechanism
(`docs/IMPLEMENTATION.md`), and code (`orchestrator/centella.py`). Knowing
which layer the bug lives in helps triage. (Quiet reinforcement of the
three-layer rule — see [`CLAUDE.md`](../../CLAUDE.md).)

- [ ] DESIGN (architecture is wrong or incomplete)
- [ ] IMPLEMENTATION (code surface spec is wrong or incomplete)
- [ ] code (code drifts from the spec)
- [ ] unclear
