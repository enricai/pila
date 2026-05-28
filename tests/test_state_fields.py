"""Coupling tests for STATE_FIELDS — the canonical list of keys the
orchestrator writes to `st.data`.

Two parities are enforced:

1. spec ↔ code: the field table in IMPLEMENTATION.md §8 lists exactly
   the names in `STATE_FIELDS`.
2. code ↔ runtime: every `st.data["x"] = ...`, `st.data.setdefault("x", ...)`,
   and key in the run-init dict literal in `orchestrate()` uses a name
   that appears in `STATE_FIELDS`.

If a future change adds a new state field, both this test and the spec
table must be updated in the same commit. That is the point.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PILA_PY = REPO_ROOT / "orchestrator" / "pila.py"
IMPL_MD = REPO_ROOT / "docs" / "IMPLEMENTATION.md"


def _spec_fields() -> set[str]:
    """Parse field names out of the `state.json` markdown table in
    IMPLEMENTATION.md §8. The table is identified by its header row
    `| Field | Shape | Purpose |` and ends at the next blank line."""
    text = IMPL_MD.read_text()
    header = re.search(
        r"^\|\s*Field\s*\|\s*Shape\s*\|\s*Purpose\s*\|\s*$",
        text, re.MULTILINE,
    )
    assert header, "could not locate state.json field table in IMPLEMENTATION.md"
    # Skip the header line and the |---|---|---| separator.
    start = header.end()
    block = text[start:].split("\n\n", 1)[0]
    fields: set[str] = set()
    for line in block.splitlines():
        m = re.match(r"\|\s*`([a-z_]+)`\s*\|", line)
        if m:
            fields.add(m.group(1))
    assert fields, "extracted no field names from the IMPLEMENTATION.md table"
    return fields


def _runtime_field_writes() -> set[str]:
    """Every name used as a key on `st.data` in pila.py — whether via
    `st.data["x"] = ...`, `st.data.setdefault("x", ...)`, or as a key in
    the run-init dict literal in `orchestrate()`."""
    source = PILA_PY.read_text()

    direct = set(re.findall(r'st\.data\["([a-z_]+)"\]\s*=', source))
    setdefault = set(re.findall(r'st\.data\.setdefault\("([a-z_]+)"', source))

    # The init in `orchestrate()` writes several keys in one dict literal:
    #   st.data = {"task": task, "started_at": now(), ...}
    init_match = re.search(
        r"st\.data\s*=\s*\{(.*?)\}", source, re.DOTALL,
    )
    init = set()
    if init_match:
        init = set(re.findall(r'"([a-z_]+)"\s*:', init_match.group(1)))

    return direct | setdefault | init


def test_state_fields_matches_spec_table(pila):
    """STATE_FIELDS and the IMPLEMENTATION.md field table must list the
    same names. Symmetric: catches drift in either direction."""
    code = set(pila.STATE_FIELDS)
    spec = _spec_fields()

    missing_from_spec = code - spec
    missing_from_code = spec - code
    assert not missing_from_spec and not missing_from_code, (
        f"STATE_FIELDS vs IMPLEMENTATION.md §8 field table drift:\n"
        f"  in STATE_FIELDS but not in spec table: "
        f"{sorted(missing_from_spec)}\n"
        f"  in spec table but not in STATE_FIELDS: "
        f"{sorted(missing_from_code)}"
    )


def test_every_st_data_write_is_declared(pila):
    """Every key the orchestrator writes to `st.data` (directly,
    via setdefault, or in the run-init dict literal) must appear in
    STATE_FIELDS. Catches the case where a new write is added without
    updating the canonical tuple."""
    declared = set(pila.STATE_FIELDS)
    written = _runtime_field_writes()

    undeclared = written - declared
    assert not undeclared, (
        f"pila.py writes state keys that are not in STATE_FIELDS: "
        f"{sorted(undeclared)}. Add them to STATE_FIELDS and to the "
        f"IMPLEMENTATION.md §8 field table in the same change."
    )


def test_state_fields_has_no_duplicates(pila):
    """STATE_FIELDS is a tuple, so a stray duplicate would not be caught
    by the set-equality test above. Check explicitly."""
    fields = pila.STATE_FIELDS
    assert len(fields) == len(set(fields)), (
        f"STATE_FIELDS contains duplicates: "
        f"{sorted(f for f in fields if fields.count(f) > 1)}"
    )
