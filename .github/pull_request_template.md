## Summary

<!-- 1-2 sentences. Why is this change being made, and what does it do? -->

## Which layer(s) does this PR touch?

(See [`CLAUDE.md`](../CLAUDE.md) for the three-layer rule.)

- [ ] `docs/DESIGN.md` (architecture)
- [ ] `docs/IMPLEMENTATION.md` (code surface)
- [ ] `orchestrator/centella.py` (code)
- [ ] docs (`README.md`, `docs/USAGE.md`, `CONTRIBUTING.md`, etc.)
- [ ] tests (`tests/`)
- [ ] CI / repo meta (`.github/`, `CHANGELOG.md`)

If multiple, has the change propagated **top-down** per the three-layer rule
(DESIGN → IMPLEMENTATION → code)?

- [ ] yes
- [ ] not applicable (single layer)

## Task-completion checklist

(Mirrors `CLAUDE.md` and `CONTRIBUTING.md`.)

- [ ] `docs/IMPLEMENTATION.md` updated if code surface changed
- [ ] `docs/DESIGN.md` updated if architecture changed
- [ ] `pytest tests/` passes
- [ ] `python3 -c "import ast; ast.parse(open('orchestrator/centella.py').read())"` passes
- [ ] `CHANGELOG.md` `[Unreleased]` updated
- [ ] `git diff --stat` reviewed for scope (no collateral edits)

## Testing notes

<!-- What new test coverage did you add? If none, why not? -->

## Related issue

<!-- Closes #N -->
