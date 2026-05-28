# Contributing to Pila

Thanks for considering a contribution. Pila is small on purpose — a
single-file Python orchestrator (~1600 LOC) with no runtime dependencies and
one dev dependency (`pytest`). A good contribution preserves that shape: a
focused fix or a clearly-bounded feature that fits inside the documented
architecture, with tests and docs updated to match.

## Before you change anything: read the three-layer rule

The repo separates *theory* (`docs/DESIGN.md`), *mechanism*
(`docs/IMPLEMENTATION.md`), and *code* (`orchestrator/pila.py`), and
the layers are **top-down canonical**: each layer derives from and conforms
to the one above it. Precedence when they disagree: **DESIGN > IMPLEMENTATION
> code**. The lower layer is the defect.

When you change something, change the highest layer that the change touches
*first*, then propagate down. The full version of this rule, and how to
apply it in edge cases, lives in [`CLAUDE.md`](CLAUDE.md). Read it before
opening a PR that touches more than a single layer.

## Development setup

```bash
git clone https://github.com/enricai/pila.git
cd pila
pip install pytest         # the only dev dependency
./pila --help          # smoke-check the entry point
```

There is no `pyproject.toml`; contributors develop out of the checkout.
End-users get a one-command install via the Claude Code plugin marketplace
or `scripts/install.sh` — see [README *Install*](README.md#install).

## Running the tests

```bash
pytest tests/
```

The suite covers the deterministic enforcement functions. See
[`docs/IMPLEMENTATION.md`](docs/IMPLEMENTATION.md) §10 for what is covered
and what is deliberately out of scope (the live `claude -p` invocation path
is not unit-tested).

## The task-completion checklist

Before opening a PR, verify the same checklist that `CLAUDE.md` requires
for any change:

- [ ] `docs/IMPLEMENTATION.md` updated if the change affected code surface
      described there.
- [ ] `docs/DESIGN.md` updated only if the architecture itself changed.
- [ ] `pytest tests/` — all pass.
- [ ] `python3 -c "import ast; ast.parse(open('orchestrator/pila.py').read())"`
      as a static check.
- [ ] `grep -rn <removed-string> .` — confirm no stragglers if the change
      renamed or removed a string used elsewhere.
- [ ] `git diff --stat` — confirm the diff is scoped to what the change
      intended; no collateral edits.
- [ ] `python3 -c 'import json; json.load(open(".claude-plugin/plugin.json")); json.load(open(".claude-plugin/marketplace.json"))'`
      — if either manifest in `.claude-plugin/` was touched, confirm both
      are valid JSON.

(Mirrors `CLAUDE.md`'s checklist — keep in sync if you change either file.)

## Commit and PR conventions

- **Conventional commit prefixes:** `chore:`, `feat:`, `fix:`, `docs:`,
  `refactor:`, `test:`, `ci:`. Match the existing git log.
- **One commit per logical change.** Resist bundling. If two changes can be
  reverted independently, they should be separate commits.
- **PR description should call out which layer(s) of the three-layer rule
  the change touches** — DESIGN, IMPLEMENTATION, code, docs, tests, CI —
  and confirm the change propagated *top-down* if it touches more than one.

## Code style

See [`CLAUDE.md`](CLAUDE.md) § Code style. There is no linter in CI;
style is enforced by review.

## Reporting bugs and requesting features

- **Bugs:** use the [bug report template](.github/ISSUE_TEMPLATE/bug_report.md).
- **Features:** use the [feature request template](.github/ISSUE_TEMPLATE/feature_request.md).
  Bear the "stays small" constraint in mind — features that pull the
  orchestrator toward generality at the cost of the single-file shape are a
  hard sell.
- **Security issues:** do not open a public issue. See
  [`SECURITY.md`](SECURITY.md).
