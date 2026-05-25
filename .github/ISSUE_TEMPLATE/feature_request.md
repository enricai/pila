---
name: Feature request
about: Suggest a new capability or change
labels: enhancement
---

## The problem

<!-- What can't you do today? What is awkward, surprising, or impossible? -->

## Proposed solution

<!-- What should Centella do instead? Concrete behavior, not just "support X". -->

## Which layer would this touch?

(See [`CLAUDE.md`](../../CLAUDE.md) for the three-layer rule.)

- [ ] DESIGN (changes the architecture or the reasoning)
- [ ] IMPLEMENTATION (changes the documented code surface — function names, cap values, schemas, install steps)
- [ ] code only (pure mechanical change, no documented-surface impact)
- [ ] unsure

## Alternatives considered

<!-- What else did you try or rule out, and why? -->

## Out-of-scope check

From `CLAUDE.md`:

> Centella is small (~1600 LOC) and stays small.

Does this feature preserve that? If it grows the orchestrator
substantially, or pulls it toward generality at the cost of the
single-file shape, please say so and make the case explicitly.
