"""Synthetic-cycle test corpus for the phase 2½ acyclicity gate, the
recommendation heuristic, the retry-prompt builder, the must-include
validation, and the three new cycle-breaking apply-step ops
(`dropped_requires` / `dependency_edges` / `merged_subtasks`).

The corpus is grounded in the two captured failures from
`~/src/enric/summarizer/.pila/runs/` (run 1: `feat-008 ↔ feat-009`
mixed-edge SCC; run 2: `config-005 ↔ feat-001` two-rename SCC) plus
synthetic triangles, 4-cycles, and connector cycles to exercise the
gate's diagnostic shape under topologies we haven't observed yet.

All tests are deterministic and require no `claude` binary — they
exercise the apply step, gate, heuristic, and prompt-builder purely
against synthetic fixtures.
"""
from __future__ import annotations

import asyncio
import copy

import pytest


# ===========================================================================
# Helpers
# ===========================================================================

def _req(tag: str, extent: str = "in_plan") -> dict:
    return {"tag": tag, "extent": extent}


def _subtask(sid: str, *, provides=(), requires=(), depends_on=(),
             files=(), scs: str = "") -> dict:
    return {
        "id": sid,
        "title": f"Subtask {sid}",
        "intent": f"intent for {sid}",
        "provides": list(provides),
        "requires": [_req(r) if isinstance(r, str) else r for r in requires],
        "depends_on": list(depends_on),
        "files_likely_touched": list(files),
        "success_criteria_seed": scs or f"{sid} succeeds",
        "size": "small",
    }


def _plan(domain: str, *subtasks) -> dict:
    return {"domain": domain, "status": "ready", "subtasks": list(subtasks)}


def _build_graph(pila, subtasks_dict):
    """Build (preds, providers, edge_sources, succ) the way the gate
    does, so tests can call Tarjan directly."""
    preds, providers, edge_sources = pila._build_predecessor_graph(
        subtasks_dict)
    succ = {sid: set() for sid in subtasks_dict}
    for tgt, src_set in preds.items():
        for src in src_set:
            succ[src].add(tgt)
    return preds, providers, edge_sources, succ


# Fixture plans matching the two captured cycles. Reconstructed from the
# `structured_output` events in the captured reconciler.log files.

def _run2_post_reconcile_plans() -> list[dict]:
    """Run 2's `feat-001 ↔ config-005` 2-node SCC. Both edges are
    reconciler renames; pre-reconcile graph is acyclic. Returned in
    POST-reconcile shape (renames already applied) so the gate fires."""
    # feat-001 requires "app-runtime-deps" (renamed from
    # "node-server-runtime-libs-present"); provided by config-005.
    feat_001 = _subtask(
        "feat-001",
        provides=["backend-http-server"],
        requires=["app-runtime-deps"],
        files=["package.json", "server/index.ts"],
        scs="server starts and /health returns 200",
    )
    # config-005 requires "backend-http-server" (renamed from
    # "app-server-framework-present"); provided by feat-001.
    config_005 = _subtask(
        "config-005",
        provides=["app-runtime-deps", "app-build-scripts"],
        requires=["backend-http-server"],
        files=["package.json"],
        scs=("package.json exposes build, start (production server), a "
             "worker/start-worker path; pnpm install resolves; "
             "the runtime deps are pinned"),
    )
    return [_plan("feature-implementation", feat_001),
            _plan("configuration-build", config_005)]


def _run2_reconciler_output() -> dict:
    """The two renames the captured reconciler emitted for run 2."""
    return {
        "renames": [
            {"sid": "feat-001",
             "from": "node-server-runtime-libs-present",
             "to": "app-runtime-deps"},
            {"sid": "config-005",
             "from": "app-server-framework-present",
             "to": "backend-http-server"},
        ],
        "added_provides": [],
        "added_subtasks": [],
        "dropped_requires": [],
        "dependency_edges": [],
        "merged_subtasks": [],
        "unresolvable": [],
    }


def _run1_post_reconcile_plans() -> list[dict]:
    """Run 1's `feat-008 ↔ feat-009` 2-node SCC. One edge is planner-
    declared (`feat-009 depends_on feat-008`); the other is a
    reconciler rename. Returned in POST-reconcile shape."""
    feat_008 = _subtask(
        "feat-008",
        provides=["audio-pipeline-driver"],
        # Renamed from some original tag → "prisma-data-access-ready"
        # (provided by feat-009).
        requires=["prisma-data-access-ready"],
        files=["src/lib/audio.ts"],
        scs="audio pipeline drives chunks end to end",
    )
    feat_009 = _subtask(
        "feat-009",
        provides=["prisma-data-access-ready"],
        requires=[],
        # Planner-declared depends_on closing the reverse direction.
        depends_on=["feat-008"],
        files=["src/lib/prisma.ts"],
        scs="prisma client connects and runs a smoke query",
    )
    return [_plan("feature-implementation", feat_008, feat_009)]


def _run1_reconciler_output() -> dict:
    return {
        "renames": [
            {"sid": "feat-008",
             "from": "data-access-ready",
             "to": "prisma-data-access-ready"},
        ],
        "added_provides": [],
        "added_subtasks": [],
        "dropped_requires": [],
        "dependency_edges": [],
        "merged_subtasks": [],
        "unresolvable": [],
    }


# ===========================================================================
# Test 1: Run-2 case — both edges from renames, gate fires
# ===========================================================================

def test_gate_fires_on_run2_two_rename_cycle(pila):
    """Tarjan returns the 2-node SCC; diagnostic names both subtasks
    and attributes each edge to its rename."""
    plans = _run2_post_reconcile_plans()
    output = _run2_reconciler_output()
    by_id = {s["id"]: s for plan in plans for s in plan["subtasks"]}

    _preds, _provs, edge_sources, succ = _build_graph(pila, by_id)
    sccs = pila._tarjan_sccs(set(by_id), succ)
    assert sccs == [["config-005", "feat-001"]], (
        "expected one 2-node SCC sorted lex: ['config-005', 'feat-001']")

    diag = pila._format_cycle_diagnostic(
        sccs, succ, edge_sources, output, by_id)
    assert "config-005" in diag and "feat-001" in diag
    # Both edges are attributed to renames (not planner-declared).
    assert "rename:" in diag
    # Shared files signal surfaces.
    assert "package.json" in diag


# ===========================================================================
# Test 2: Run-1 case — mixed depends_on + rename, gate fires
# ===========================================================================

def test_gate_fires_on_run1_mixed_edge_cycle(pila):
    """Run 1: feat-009 -> feat-008 via planner depends_on; feat-008 ->
    feat-009 via renamed requires. Diagnostic names each edge's source
    separately (depends_on vs. rename)."""
    plans = _run1_post_reconcile_plans()
    output = _run1_reconciler_output()
    by_id = {s["id"]: s for plan in plans for s in plan["subtasks"]}

    _preds, _provs, edge_sources, succ = _build_graph(pila, by_id)
    sccs = pila._tarjan_sccs(set(by_id), succ)
    assert sccs == [["feat-008", "feat-009"]]

    diag = pila._format_cycle_diagnostic(
        sccs, succ, edge_sources, output, by_id)
    # Both source labels appear.
    assert "depends_on" in diag
    assert "requires:" in diag
    assert "planner-declared" in diag
    assert "rename:" in diag


# ===========================================================================
# Test 3: 3-node triangle via mixed edges
# ===========================================================================

def test_gate_fires_on_3node_triangle(pila):
    """A->B->C->A cycle via requires-tag matches."""
    a = _subtask("feat-a", provides=["a"], requires=["c"])
    b = _subtask("feat-b", provides=["b"], requires=["a"])
    c = _subtask("feat-c", provides=["c"], requires=["b"])
    by_id = {s["id"]: s for s in (a, b, c)}

    _preds, _provs, edge_sources, succ = _build_graph(pila, by_id)
    sccs = pila._tarjan_sccs(set(by_id), succ)
    assert len(sccs) == 1
    assert sorted(sccs[0]) == ["feat-a", "feat-b", "feat-c"]


# ===========================================================================
# Test 4: 4-node cycle A->B->C->D->A via mixed depends_on / requires
# ===========================================================================

def test_gate_fires_on_4node_cycle_mixed_edges(pila):
    a = _subtask("a", provides=["a-cap"], depends_on=["d"])
    b = _subtask("b", provides=["b-cap"], requires=["a-cap"])
    c = _subtask("c", provides=["c-cap"], depends_on=["b"])
    d = _subtask("d", provides=["d-cap"], requires=["c-cap"])
    by_id = {s["id"]: s for s in (a, b, c, d)}

    _preds, _provs, _es, succ = _build_graph(pila, by_id)
    sccs = pila._tarjan_sccs(set(by_id), succ)
    assert len(sccs) == 1
    assert sorted(sccs[0]) == ["a", "b", "c", "d"]


# ===========================================================================
# Test 5: Cycle involving a reconciler-added connector
# ===========================================================================

def test_gate_fires_on_connector_cycle(pila):
    """A reconciler-added connector closes a loop; edge attribution
    names the connector by id."""
    feat_001 = _subtask("feat-001",
                        provides=["x"], requires=["connector-cap"])
    # Connector required something feat-001 provides → cycle.
    connector = _subtask("recon-001",
                         provides=["connector-cap"], requires=["x"])
    connector["_added_by_reconciler"] = True
    by_id = {s["id"]: s for s in (feat_001, connector)}

    output = {
        "renames": [], "added_provides": [],
        "added_subtasks": [connector], "dropped_requires": [],
        "dependency_edges": [], "merged_subtasks": [], "unresolvable": [],
    }

    _preds, _provs, edge_sources, succ = _build_graph(pila, by_id)
    sccs = pila._tarjan_sccs(set(by_id), succ)
    assert sccs == [["feat-001", "recon-001"]]

    diag = pila._format_cycle_diagnostic(
        sccs, succ, edge_sources, output, by_id)
    assert "added_subtask: recon-001" in diag


# ===========================================================================
# Test 6: dropped_requires resolves the run-2 cycle
# ===========================================================================

def test_dropped_requires_resolves_run2(pila):
    """Apply step removes the named requires entry; the graph becomes
    acyclic; Kahn's produces valid waves."""
    plans = _run2_post_reconcile_plans()
    output = {
        "renames": [], "added_provides": [], "added_subtasks": [],
        "dropped_requires": [{
            "sid": "config-005",
            "tag": "backend-http-server",
            "reason": "framework choice is an authoring decision config-005 "
                      "records, not a code artifact",
        }],
        "dependency_edges": [], "merged_subtasks": [], "unresolvable": [],
    }
    pila._apply_reconciler_output(plans, output)

    # The dropped requires entry is gone.
    config_005 = next(s for plan in plans for s in plan["subtasks"]
                      if s["id"] == "config-005")
    assert all(r.get("tag") != "backend-http-server"
               for r in config_005["requires"])

    # Graph is acyclic now.
    by_id = {s["id"]: s for plan in plans for s in plan["subtasks"]}
    _preds, _provs, _es, succ = _build_graph(pila, by_id)
    sccs = pila._tarjan_sccs(set(by_id), succ)
    assert sccs == []


# ===========================================================================
# Test 7: dependency_edges resolves an asymmetric case
# ===========================================================================

def test_dependency_edges_appends_dedup_and_breaks_cycle(pila):
    """Apply step appends to depends_on (dedup) so the explicit
    ordering is recorded; the existing graph stays consistent."""
    # Two subtasks with no current cycle.
    a = _subtask("a", provides=["a-cap"], requires=[])
    b = _subtask("b", provides=["b-cap"], requires=[])
    plans = [_plan("feat", a, b)]

    output = {
        "renames": [], "added_provides": [], "added_subtasks": [],
        "dropped_requires": [],
        "dependency_edges": [
            {"from": "a", "to": "b", "reason": "..."},
            {"from": "a", "to": "b", "reason": "..."},  # dup → dedup
        ],
        "merged_subtasks": [], "unresolvable": [],
    }
    pila._apply_reconciler_output(plans, output)
    b_after = next(s for plan in plans for s in plan["subtasks"]
                   if s["id"] == "b")
    assert b_after["depends_on"] == ["a"], (
        "duplicate dependency_edges must be deduped on append")


def test_dependency_edges_die_on_missing_id(pila):
    a = _subtask("a", provides=["a-cap"])
    plans = [_plan("feat", a)]
    output = {
        "renames": [], "added_provides": [], "added_subtasks": [],
        "dropped_requires": [],
        "dependency_edges": [
            {"from": "a", "to": "ghost", "reason": "missing"},
        ],
        "merged_subtasks": [], "unresolvable": [],
    }
    with pytest.raises(SystemExit):
        pila._apply_reconciler_output(plans, output)


# ===========================================================================
# Test 8: merged_subtasks resolves the run-2 cycle
# ===========================================================================

def test_merged_subtasks_resolves_run2(pila):
    """Apply step folds config-005 into feat-001, unioning fields,
    dropping self-references, stamping _merged_from, rewriting
    downstream depends_on. Graph becomes acyclic."""
    plans = _run2_post_reconcile_plans()
    # Add a third subtask that depends on `config-005`, to test that
    # downstream depends_on references are rewritten.
    extra = _subtask("feat-002", provides=["y"], depends_on=["config-005"])
    plans[0]["subtasks"].append(extra)

    output = {
        "renames": [], "added_provides": [], "added_subtasks": [],
        "dropped_requires": [], "dependency_edges": [],
        "merged_subtasks": [{
            "into": "feat-001", "from": "config-005",
            "reason": "Both edit package.json; reference repos ship "
                      "bootstrap as one unit.",
        }],
        "unresolvable": [],
    }
    pila._apply_reconciler_output(plans, output)

    # `from` (config-005) is removed.
    all_ids = {s["id"] for plan in plans for s in plan["subtasks"]}
    assert "config-005" not in all_ids
    assert "feat-001" in all_ids

    feat_001 = next(s for plan in plans for s in plan["subtasks"]
                    if s["id"] == "feat-001")
    # Provides union (dedup, order-preserving).
    assert set(feat_001["provides"]) == {
        "backend-http-server", "app-runtime-deps", "app-build-scripts",
    }
    # Requires self-references dropped: feat-001 originally required
    # "app-runtime-deps" (which the merged unit now provides) → dropped.
    # config-005 originally required "backend-http-server" (also self-
    # provided now) → dropped.
    req_tags = {r["tag"] for r in feat_001["requires"]}
    assert "app-runtime-deps" not in req_tags
    assert "backend-http-server" not in req_tags
    # Files union.
    assert set(feat_001["files_likely_touched"]) == {
        "package.json", "server/index.ts"}
    # _merged_from telemetry.
    assert feat_001["_merged_from"] == ["config-005"]
    # success_criteria_seed concatenation.
    assert "AND" in feat_001["success_criteria_seed"]

    # Downstream depends_on rewriting: feat-002 previously depended on
    # config-005; now depends on feat-001.
    feat_002 = next(s for plan in plans for s in plan["subtasks"]
                    if s["id"] == "feat-002")
    assert feat_002["depends_on"] == ["feat-001"]

    # Graph is acyclic.
    by_id = {s["id"]: s for plan in plans for s in plan["subtasks"]}
    _preds, _provs, _es, succ = _build_graph(pila, by_id)
    sccs = pila._tarjan_sccs(set(by_id), succ)
    assert sccs == []


# ===========================================================================
# Test 9: merged_subtasks fail-loud on missing id
# ===========================================================================

def test_merged_subtasks_die_on_missing_id(pila):
    a = _subtask("a", provides=["a"])
    plans = [_plan("feat", a)]
    output = {
        "renames": [], "added_provides": [], "added_subtasks": [],
        "dropped_requires": [], "dependency_edges": [],
        "merged_subtasks": [{
            "into": "a", "from": "ghost", "reason": "...",
        }],
        "unresolvable": [],
    }
    with pytest.raises(SystemExit):
        pila._apply_reconciler_output(plans, output)


def test_merged_subtasks_die_on_self_merge(pila):
    a = _subtask("a", provides=["a"])
    plans = [_plan("feat", a)]
    output = {
        "renames": [], "added_provides": [], "added_subtasks": [],
        "dropped_requires": [], "dependency_edges": [],
        "merged_subtasks": [{
            "into": "a", "from": "a", "reason": "self",
        }],
        "unresolvable": [],
    }
    with pytest.raises(SystemExit):
        pila._apply_reconciler_output(plans, output)


# ===========================================================================
# Test 10: Acyclic plan: gate silent
# ===========================================================================

def test_gate_silent_on_acyclic_plan(pila):
    a = _subtask("a", provides=["a"])
    b = _subtask("b", provides=["b"], requires=["a"])
    c = _subtask("c", provides=["c"], requires=["b"], depends_on=["a"])
    by_id = {s["id"]: s for s in (a, b, c)}
    _preds, _provs, _es, succ = _build_graph(pila, by_id)
    sccs = pila._tarjan_sccs(set(by_id), succ)
    assert sccs == []


# ===========================================================================
# Test 11: Regression fixtures from real successful runs (zero false positives)
# ===========================================================================

# Tiny synthetic stand-ins for the five successful-run plans surveyed in
# the cross-repo canvass. We don't ship the full captured plans here
# (they're large and live in user .pila/runs/ directories) — these
# scaffolds mirror the structural shape (n subtasks, m capability
# matches, no cycles) so the gate's silent-on-acyclic property is
# pinned in the test corpus.

@pytest.mark.parametrize("name,subtasks", [
    ("centella-feat-rebrand-3domains", [
        ("feat-001", ["a"], [], []),
        ("feat-002", ["b"], ["a"], []),
        ("refactor-001", ["c"], ["b"], []),
        ("docs-001", [], ["c"], []),
    ]),
    ("barnacle-12-renames", [
        ("feat-001", ["f1"], [], []),
        ("feat-002", ["f2"], ["f1"], []),
        ("feat-003", ["f3"], ["f1"], []),
        ("config-001", ["c1"], ["f2"], []),
        ("config-002", ["c2"], ["f3"], []),
        ("docs-001", [], ["c1", "c2"], []),
    ]),
    ("navegando-bugfix-no-recon", [
        ("bugfix-001", ["b1"], [], []),
        ("bugfix-002", ["b2"], ["b1"], []),
        ("feat-001", ["f1"], ["b2"], []),
    ]),
    ("pila-feat-please-read-2domains", [
        ("feat-001", ["f1"], [], []),
        ("feat-002", ["f2"], ["f1"], []),
        ("config-001", [], ["f2"], []),
    ]),
    ("finalmemoriam-bugfix-1rename", [
        ("bugfix-001", ["b1"], [], []),
        ("test-001", [], ["b1"], []),
    ]),
])
def test_gate_silent_on_successful_run_shapes(pila, name, subtasks):
    """The gate must NOT fire on any of the five successful-run shapes
    surveyed in the cross-repo canvass. False-positive regression
    guard. (Synthetic stand-ins; the real captured plans pass the same
    check when reconstructed from .pila/runs/.)"""
    by_id = {sid: _subtask(sid, provides=p, requires=r, depends_on=d)
             for (sid, p, r, d) in subtasks}
    _preds, _provs, _es, succ = _build_graph(pila, by_id)
    sccs = pila._tarjan_sccs(set(by_id), succ)
    assert sccs == [], f"{name}: gate fired on a known-acyclic shape"


# ===========================================================================
# Test 12: Retry-prompt builder produces expected structure
# ===========================================================================

def test_retry_prompt_builder_contains_required_sections(pila):
    """The retry prompt names the SCC, the edges, the structural
    signals, the recommendation, and the must-include set."""
    plans = _run2_post_reconcile_plans()
    output = _run2_reconciler_output()
    by_id = {s["id"]: s for plan in plans for s in plan["subtasks"]}

    _preds, _provs, edge_sources, succ = _build_graph(pila, by_id)
    sccs = pila._tarjan_sccs(set(by_id), succ)

    # Pre-providers map: at this point, both subtasks still have their
    # ORIGINAL requires; pre-providers is just provides → [sid]. (In
    # production, this comes from the pre-mutation snapshot in
    # phase_reconcile.)
    pre_providers = {
        "backend-http-server": ["feat-001"],
        "app-runtime-deps": ["config-005"],
        "app-build-scripts": ["config-005"],
    }
    recs = [pila._recommend_cycle_resolution(
        scc, succ, edge_sources, by_id, output, pre_providers)
        for scc in sccs]
    prompt = pila._build_cycle_retry_prompt(
        sccs, succ, edge_sources, output, by_id, recs,
        "ORIGINAL USER PROMPT")

    # Required sections.
    assert "1 dependency cycle" in prompt
    assert "CYCLE 1:" in prompt
    assert "config-005" in prompt and "feat-001" in prompt
    assert "RECOMMENDED:" in prompt
    assert "MUST include" in prompt
    assert "unresolvable" in prompt and "NOT a valid" in prompt
    assert "ORIGINAL USER PROMPT" in prompt
    # Structural signals.
    assert "Shared files_likely_touched: ['package.json']" in prompt
    # Recommendation line must inline the actual reason text — not a
    # `reason=...` literal-ellipsis placeholder the model would have to
    # interpolate. Fix 3A: the model should be able to copy the
    # RECOMMENDED line verbatim into its output.
    assert "reason=..." not in prompt, (
        "RECOMMENDED line should inline the actual reason text "
        "(repr-escaped), not a placeholder ellipsis")
    # And a snippet of the actual reason (run-2's case-2 merge rationale)
    # appears somewhere in the prompt — both in the RECOMMENDED line and
    # in the Why: commentary line.
    assert "Both subtasks edit the same file" in prompt


# ===========================================================================
# Test 13: Mutation reversion is clean (deep-copy round trip)
# ===========================================================================

def test_mutation_reversion_via_deep_copy_is_clean(pila):
    """Deep-copy snapshot before apply; revert by restoring from the
    snapshot. Post-revert state must equal the original."""
    plans = _run2_post_reconcile_plans()
    snapshot = copy.deepcopy(plans)

    output = {
        "renames": [], "added_provides": [], "added_subtasks": [],
        "dropped_requires": [], "dependency_edges": [],
        "merged_subtasks": [{
            "into": "feat-001", "from": "config-005", "reason": "...",
        }],
        "unresolvable": [],
    }
    pila._apply_reconciler_output(plans, output)
    # Confirm we actually mutated something.
    all_ids = {s["id"] for plan in plans for s in plan["subtasks"]}
    assert "config-005" not in all_ids

    # Revert by deep-copying the snapshot back into plans.
    plans.clear()
    plans.extend(copy.deepcopy(snapshot))
    # Post-revert equals original.
    assert plans == snapshot


# ===========================================================================
# Test 14: Recommendation heuristic on both captured cycles
# ===========================================================================

def test_recommendation_correct_on_run2_cycle(pila):
    """Run 2's cycle has shared package.json; heuristic case 2 fires.
    feat-001 has the shorter SCS, so it becomes `into`."""
    plans = _run2_post_reconcile_plans()
    output = _run2_reconciler_output()
    by_id = {s["id"]: s for plan in plans for s in plan["subtasks"]}
    _preds, _provs, edge_sources, succ = _build_graph(pila, by_id)
    sccs = pila._tarjan_sccs(set(by_id), succ)
    rec = pila._recommend_cycle_resolution(
        sccs[0], succ, edge_sources, by_id, output,
        pre_providers={
            "backend-http-server": ["feat-001"],
            "app-runtime-deps": ["config-005"],
        })
    assert rec["op"] == "merged_subtasks"
    assert rec["into"] == "feat-001"
    assert rec["from"] == "config-005"
    assert rec["rationale"] == "case-2: shared-files merge"


def test_recommendation_correct_on_run1_cycle(pila):
    """Run 1's cycle has planner-declared feat-009 -> feat-008; case 1
    fires; drop the rename closing the reverse direction."""
    plans = _run1_post_reconcile_plans()
    output = _run1_reconciler_output()
    by_id = {s["id"]: s for plan in plans for s in plan["subtasks"]}
    _preds, _provs, edge_sources, succ = _build_graph(pila, by_id)
    sccs = pila._tarjan_sccs(set(by_id), succ)
    rec = pila._recommend_cycle_resolution(
        sccs[0], succ, edge_sources, by_id, output,
        pre_providers={"prisma-data-access-ready": ["feat-009"]})
    assert rec["op"] == "dropped_requires"
    assert rec["sid"] == "feat-008"
    assert rec["tag"] == "prisma-data-access-ready"
    assert rec["rationale"] == "case-1: planner-edge keeper"


# ===========================================================================
# Test 15: Must-include validation fail-loud
# ===========================================================================

def test_must_include_validation_flags_unaddressed_cycle(pila):
    """If the revised output doesn't include any op addressing a named
    cycle, _validate_must_include returns it as unaddressed."""
    plans = _run2_post_reconcile_plans()
    by_id = {s["id"]: s for plan in plans for s in plan["subtasks"]}
    _preds, _provs, edge_sources, succ = _build_graph(pila, by_id)
    sccs = pila._tarjan_sccs(set(by_id), succ)

    # An "empty" revised output (no cycle-breaking ops at all).
    empty_output = {
        "renames": [], "added_provides": [], "added_subtasks": [],
        "dropped_requires": [], "dependency_edges": [],
        "merged_subtasks": [], "unresolvable": [],
    }
    unaddressed = pila._validate_must_include(empty_output, sccs)
    assert unaddressed == ["config-005 <-> feat-001"]


def test_must_include_validation_passes_when_drop_addresses_cycle(pila):
    plans = _run2_post_reconcile_plans()
    by_id = {s["id"]: s for plan in plans for s in plan["subtasks"]}
    _preds, _provs, edge_sources, succ = _build_graph(pila, by_id)
    sccs = pila._tarjan_sccs(set(by_id), succ)

    output = {
        "renames": [], "added_provides": [], "added_subtasks": [],
        "dropped_requires": [
            {"sid": "config-005", "tag": "backend-http-server",
             "reason": "..."},
        ],
        "dependency_edges": [], "merged_subtasks": [], "unresolvable": [],
    }
    unaddressed = pila._validate_must_include(output, sccs)
    assert unaddressed == []


def test_must_include_validation_passes_when_merge_addresses_cycle(pila):
    plans = _run2_post_reconcile_plans()
    by_id = {s["id"]: s for plan in plans for s in plan["subtasks"]}
    _preds, _provs, edge_sources, succ = _build_graph(pila, by_id)
    sccs = pila._tarjan_sccs(set(by_id), succ)

    output = {
        "renames": [], "added_provides": [], "added_subtasks": [],
        "dropped_requires": [], "dependency_edges": [],
        "merged_subtasks": [
            {"into": "feat-001", "from": "config-005", "reason": "..."},
        ],
        "unresolvable": [],
    }
    unaddressed = pila._validate_must_include(output, sccs)
    assert unaddressed == []


# ===========================================================================
# Test 16: Post-retry cycle detection (revised output introduces new cycle)
# ===========================================================================

def test_post_retry_detects_newly_introduced_cycle(pila):
    """If the revised output resolves the named cycle but introduces a
    new one elsewhere, the post-retry Tarjan fires with the new SCC."""
    # Start with run 2's cycle. Imagine the model "resolves" it by
    # dropping config-005's requires (good) but then adds a
    # dependency_edges that creates a NEW cycle with an unrelated
    # subtask.
    plans = _run2_post_reconcile_plans()
    # Add an unrelated subtask that the new edge will cycle with.
    extra = _subtask("feat-x",
                     provides=["x-cap"], requires=["app-runtime-deps"])
    plans[0]["subtasks"].append(extra)

    output = {
        "renames": [], "added_provides": [], "added_subtasks": [],
        "dropped_requires": [
            {"sid": "config-005", "tag": "backend-http-server",
             "reason": "..."},
        ],
        "dependency_edges": [
            # Creates a new cycle: config-005 provides app-runtime-deps,
            # feat-x requires app-runtime-deps → edge config-005 → feat-x.
            # Now we add feat-x → config-005, closing a NEW 2-node SCC.
            {"from": "feat-x", "to": "config-005", "reason": "..."},
        ],
        "merged_subtasks": [], "unresolvable": [],
    }
    pila._apply_reconciler_output(plans, output)
    by_id = {s["id"]: s for plan in plans for s in plan["subtasks"]}
    _preds, _provs, _es, succ = _build_graph(pila, by_id)
    sccs = pila._tarjan_sccs(set(by_id), succ)
    # Original cycle is gone, but a new one exists.
    assert sccs == [["config-005", "feat-x"]]


# ===========================================================================
# Test 17: No-recommendation case falls back to speculative-rename drop
# ===========================================================================

def test_recommendation_case3_speculative_rename(pila):
    """SCC with no shared files and no planner depends_on. Case 3 fires:
    drop the rename whose original tag had no pre-reconcile producer."""
    # Create a 2-rename cycle where the renames don't share files.
    a = _subtask("a",
                 provides=["a-real"],
                 requires=["b-real"],  # post-rename
                 files=["a.ts"])
    b = _subtask("b",
                 provides=["b-real"],
                 requires=["a-real"],  # post-rename
                 files=["b.ts"])
    by_id = {s["id"]: s for s in (a, b)}
    output = {
        "renames": [
            # Rename `a-needs-something` → `b-real`. Original
            # `a-needs-something` had NO producer in pre_providers → speculative.
            {"sid": "a", "from": "a-needs-something", "to": "b-real"},
            # Rename `b-needs-something` → `a-real`. Original ALSO had
            # no producer.
            {"sid": "b", "from": "b-needs-something", "to": "a-real"},
        ],
        "added_provides": [], "added_subtasks": [],
        "dropped_requires": [], "dependency_edges": [],
        "merged_subtasks": [], "unresolvable": [],
    }
    _preds, _provs, edge_sources, succ = _build_graph(pila, by_id)
    sccs = pila._tarjan_sccs(set(by_id), succ)
    rec = pila._recommend_cycle_resolution(
        sccs[0], succ, edge_sources, by_id, output,
        pre_providers={"a-real": ["a"], "b-real": ["b"]})
    assert rec["op"] == "dropped_requires"
    assert rec["rationale"] == "case-3: speculative-rename drop"


# ===========================================================================
# Test 18: Tarjan deterministic ordering
# ===========================================================================

def test_tarjan_returns_sorted_sccs(pila):
    """Both the inner SCC node list AND the order of SCCs returned must
    be deterministic so diagnostic messages don't churn between runs."""
    a = _subtask("z", provides=["z"], requires=["a"])
    b = _subtask("a", provides=["a"], requires=["z"])
    by_id = {s["id"]: s for s in (a, b)}
    _preds, _provs, _es, succ = _build_graph(pila, by_id)
    sccs = pila._tarjan_sccs(set(by_id), succ)
    # Inner list sorted lex.
    assert sccs == [["a", "z"]]


# ===========================================================================
# Test 19: dropped_requires preserves extent: external entries with the
# same tag
# ===========================================================================

def test_dropped_requires_preserves_external_extent(pila):
    """The apply step's `dropped_requires` op must only remove
    `extent: in_plan` entries. If a subtask carries both an in_plan and
    an external entry for the same tag string (rare but possible — the
    external one names an out-of-graph prerequisite that happens to
    share a name with the in_plan tag), only the in_plan entry should
    be removed. The external entry surfaces as a deploy-note
    precondition and must survive."""
    s = _subtask("feat-a", provides=[])
    s["requires"] = [
        {"tag": "shared-name", "extent": "in_plan"},
        {"tag": "shared-name", "extent": "external",
         "reason": "provisioned by the infra repo's CDK stack"},
    ]
    plans = [_plan("feat", s)]
    output = {
        "renames": [], "added_provides": [], "added_subtasks": [],
        "dropped_requires": [{
            "sid": "feat-a", "tag": "shared-name",
            "reason": "in_plan entry was over-specified",
        }],
        "dependency_edges": [], "merged_subtasks": [], "unresolvable": [],
    }
    pila._apply_reconciler_output(plans, output)
    feat_a = next(s for plan in plans for s in plan["subtasks"]
                  if s["id"] == "feat-a")
    extents = sorted(r["extent"] for r in feat_a["requires"])
    assert extents == ["external"], (
        f"only the in_plan entry should be removed; got extents={extents}")
    # The external entry's reason field is preserved.
    ext = feat_a["requires"][0]
    assert ext["reason"].startswith("provisioned by")


# ===========================================================================
# Test 20: merged_subtasks chain carries _merged_from forward
# ===========================================================================

def test_merged_subtasks_chain_carries_merged_from(pila):
    """Three subtasks A, B, C. Merge A into B, then B into C. C must
    carry both ids in `_merged_from` so a downstream consumer can
    trace the full ancestry of the merged unit."""
    a = _subtask("a", provides=["a-cap"], files=["x.ts"])
    b = _subtask("b", provides=["b-cap"], files=["x.ts"])
    c = _subtask("c", provides=["c-cap"], files=["x.ts"])
    plans = [_plan("feat", a, b, c)]

    output = {
        "renames": [], "added_provides": [], "added_subtasks": [],
        "dropped_requires": [], "dependency_edges": [],
        "merged_subtasks": [
            {"into": "b", "from": "a", "reason": "..."},
            {"into": "c", "from": "b", "reason": "..."},
        ],
        "unresolvable": [],
    }
    pila._apply_reconciler_output(plans, output)
    surviving = [s for plan in plans for s in plan["subtasks"]]
    assert len(surviving) == 1
    assert surviving[0]["id"] == "c"
    # First merge: b gets _merged_from = ["a"]. Second merge: c gets
    # _merged_from starting with ["b"], then carries over b's prior
    # ["a"]. Order: [b, a] because b is appended first, then a from
    # b's prior _merged_from.
    assert surviving[0]["_merged_from"] == ["b", "a"]


# ===========================================================================
# Test 21: merged_subtasks override fields take precedence
# ===========================================================================

def test_merged_subtasks_override_fields(pila):
    """When the merge op includes optional `title`, `intent`, and
    `success_criteria_seed`, the surviving subtask must carry the
    overrides verbatim (not the concatenation default for SCS, not the
    `into` value for title/intent)."""
    a = _subtask("a", provides=["a-cap"], scs="A's original criterion")
    b = _subtask("b", provides=["b-cap"], scs="B's original criterion")
    a["title"] = "A's original title"
    a["intent"] = "A's original intent"
    plans = [_plan("feat", a, b)]

    output = {
        "renames": [], "added_provides": [], "added_subtasks": [],
        "dropped_requires": [], "dependency_edges": [],
        "merged_subtasks": [{
            "into": "a", "from": "b", "reason": "...",
            "title": "merged unit title",
            "intent": "merged unit intent",
            "success_criteria_seed": "merged unit criterion",
        }],
        "unresolvable": [],
    }
    pila._apply_reconciler_output(plans, output)
    a_after = next(s for plan in plans for s in plan["subtasks"]
                   if s["id"] == "a")
    assert a_after["title"] == "merged unit title"
    assert a_after["intent"] == "merged unit intent"
    assert a_after["success_criteria_seed"] == "merged unit criterion"
    # No " AND " concatenation when the override is provided.
    assert "AND" not in a_after["success_criteria_seed"]


# ===========================================================================
# Test 22: merged_subtasks requires-cleanup preserves external entries
# ===========================================================================

def test_merged_subtasks_requires_cleanup_preserves_external(pila):
    """When the merged unit provides tag X and an absorbed side had a
    requires entry for X, the cleanup must only drop the entry if its
    `extent: in_plan`. An `extent: external` entry for the same tag
    survives — it names an out-of-graph prerequisite, not a code-
    artifact dependency the merge satisfies."""
    a = _subtask("a", provides=["x"])
    b = _subtask("b", provides=[])
    b["requires"] = [
        {"tag": "x", "extent": "external",
         "reason": "provisioned by another repo's deploy"},
    ]
    plans = [_plan("feat", a, b)]

    output = {
        "renames": [], "added_provides": [], "added_subtasks": [],
        "dropped_requires": [], "dependency_edges": [],
        "merged_subtasks": [{
            "into": "a", "from": "b", "reason": "..."}],
        "unresolvable": [],
    }
    pila._apply_reconciler_output(plans, output)
    a_after = next(s for plan in plans for s in plan["subtasks"]
                   if s["id"] == "a")
    # The merged unit provides "x" but the external requires entry for
    # "x" must survive (it's out-of-graph).
    assert "x" in a_after["provides"]
    ext_entries = [r for r in a_after["requires"]
                   if r.get("extent") == "external" and r.get("tag") == "x"]
    assert len(ext_entries) == 1, (
        "external requires entry for the same tag as a merged provide "
        "must survive self-reference cleanup")


# ===========================================================================
# Test 23: dependency_edges fail-loud on self-loop
# ===========================================================================

def test_dependency_edges_die_on_self_loop(pila):
    """`dependency_edges: [{from: 'a', to: 'a', ...}]` is a malformed
    op (a subtask cannot depend on itself). Apply step must die at
    apply time — symmetric with `merged_subtasks`'s into==from check —
    rather than allow the self-loop to surface downstream as a 1-node
    SCC."""
    a = _subtask("a", provides=["a-cap"])
    plans = [_plan("feat", a)]
    output = {
        "renames": [], "added_provides": [], "added_subtasks": [],
        "dropped_requires": [],
        "dependency_edges": [
            {"from": "a", "to": "a", "reason": "self-loop"},
        ],
        "merged_subtasks": [], "unresolvable": [],
    }
    with pytest.raises(SystemExit):
        pila._apply_reconciler_output(plans, output)


# ===========================================================================
# Test 24: recommendation case-4 (lexicographic tiebreaker) — the
# always-returns-something guarantee
# ===========================================================================

def test_recommendation_case4_lexicographic_tiebreaker(pila):
    """When none of cases 1-3 apply — no planner-declared depends_on in
    the SCC, no shared files_likely_touched, and every rename's `from`
    tag had a producer in pre_providers (so case 3's speculative-rename
    test doesn't fire) — case 4 fires as the deterministic last resort.
    It drops the rename keyed by the lexicographically later (consumer-
    sid, source-label) pair.

    The function's contract is "always returns a recommendation," so
    a regression that breaks case 4 would silently produce no
    recommendation for an SCC the model then has to resolve unaided.
    Pin the contract here."""
    # Two subtasks with disjoint files and no shared depends_on. Both
    # provides+requires entries are post-rename — the renames simply
    # collapse two synonym tags whose originals BOTH had pre-producers
    # (so case 3 abstains).
    a = _subtask("subtask-a",
                 provides=["a-canonical"],
                 requires=["b-canonical"],
                 files=["a.ts"])
    b = _subtask("subtask-b",
                 provides=["b-canonical"],
                 requires=["a-canonical"],
                 files=["b.ts"])
    by_id = {s["id"]: s for s in (a, b)}
    output = {
        "renames": [
            # subtask-a's original requires tag was `b-synonym`, and
            # `b-synonym` HAD a producer in the pre-reconcile graph
            # (some sibling subtask, not modeled here — pre_providers
            # just needs to claim it). So this rename is NOT
            # speculative — case 3 abstains.
            {"sid": "subtask-a", "from": "b-synonym", "to": "b-canonical"},
            {"sid": "subtask-b", "from": "a-synonym", "to": "a-canonical"},
        ],
        "added_provides": [], "added_subtasks": [],
        "dropped_requires": [], "dependency_edges": [],
        "merged_subtasks": [], "unresolvable": [],
    }
    _preds, _provs, edge_sources, succ = _build_graph(pila, by_id)
    sccs = pila._tarjan_sccs(set(by_id), succ)
    assert sccs == [["subtask-a", "subtask-b"]]

    # Both rename `from` tags claim pre-producers, so case 3 abstains
    # and case 4 fires.
    pre_providers = {
        "b-synonym": ["some-other-subtask"],
        "a-synonym": ["yet-another-subtask"],
        "a-canonical": ["subtask-a"],
        "b-canonical": ["subtask-b"],
    }
    rec = pila._recommend_cycle_resolution(
        sccs[0], succ, edge_sources, by_id, output, pre_providers)
    assert rec["op"] == "dropped_requires"
    assert rec["rationale"] == "case-4: lexicographic tiebreaker"
    # The tiebreaker sorts rename-bearing edges by (e["to"], e["source"])
    # DESC and picks the first. The two edges in the SCC are:
    #   subtask-a -> subtask-b  [requires:a-canonical; rename on subtask-b]
    #   subtask-b -> subtask-a  [requires:b-canonical; rename on subtask-a]
    # The consumer side (e["to"]) gets the drop. DESC order: subtask-b
    # comes before subtask-a, so the dropped requires entry lives on
    # subtask-b and targets tag `a-canonical`.
    assert rec["sid"] == "subtask-b"
    assert rec["tag"] == "a-canonical"


# ===========================================================================
# Test 25: _format_recommendation dropped_requires branch direct unit
# ===========================================================================

def test_format_recommendation_dropped_requires(pila):
    """Direct unit test pins the rendered shape for a dropped_requires
    recommendation. The integration tests only render the merged_subtasks
    branch (via the run-2 fixture in
    test_retry_prompt_builder_contains_required_sections); without
    this direct test, a refactor that broke the dropped_requires
    branch's f-string would not be caught."""
    rec = {
        "op": "dropped_requires",
        "sid": "feat-001",
        "tag": "app-runtime-deps",
        "reason": "Single-quoted 'reason' with a newline\nand a backslash\\",
        "rationale": "case-3: speculative-rename drop",
    }
    rendered = pila._format_recommendation(rec)
    # repr() escapes the embedded quotes and newline so the line stays
    # a valid Python-call literal.
    assert rendered.startswith(
        "dropped_requires(sid='feat-001', tag='app-runtime-deps', reason=")
    assert rendered.endswith(")")
    # reason text is in there, with quote/newline escapes intact.
    assert "Single-quoted" in rendered
    assert "\\n" in rendered or "\\\\n" in rendered, (
        "newline in reason should appear escaped in the rendered string")
    # No literal ellipsis placeholder.
    assert "reason=..." not in rendered


# ===========================================================================
# Test 26: _format_recommendation merged_subtasks branch direct unit
# ===========================================================================

def test_format_recommendation_merged_subtasks(pila):
    """Direct unit test for the merged_subtasks render branch."""
    rec = {
        "op": "merged_subtasks",
        "into": "feat-001",
        "from": "config-005",
        "reason": "Both edit package.json",
        "rationale": "case-2: shared-files merge",
    }
    rendered = pila._format_recommendation(rec)
    assert rendered.startswith(
        "merged_subtasks(into='feat-001', from='config-005', reason=")
    assert rendered.endswith(")")
    assert "Both edit package.json" in rendered


# ===========================================================================
# Test 27: _matches_recommendation marks the recommended option
# ===========================================================================

def test_matches_recommendation_marks_correct_option(pila):
    """For each of the two reachable recommendation ops, an option
    string that starts with the recommendation's prefix returns True;
    a non-matching option returns False. Without this test, a bug that
    caused the function to always return False (no `← recommended`
    marker in the retry prompt) would not be caught."""
    # dropped_requires
    rec_drop = {"op": "dropped_requires", "sid": "a", "tag": "x",
                "reason": "r", "rationale": "case-1: planner-edge keeper"}
    matching = "dropped_requires(sid='a', tag='x', ...)"
    not_matching = "dropped_requires(sid='b', tag='x', ...)"
    assert pila._matches_recommendation(matching, rec_drop) is True
    assert pila._matches_recommendation(not_matching, rec_drop) is False
    # merged_subtasks
    rec_merge = {"op": "merged_subtasks", "into": "a", "from": "b",
                 "reason": "r", "rationale": "case-2: shared-files merge"}
    matching = "merged_subtasks(into='a', from='b', ...)"
    not_matching = "merged_subtasks(into='b', from='a', ...)"
    assert pila._matches_recommendation(matching, rec_merge) is True
    assert pila._matches_recommendation(not_matching, rec_merge) is False


# ===========================================================================
# Test 28: _validate_must_include rejects ops targeting non-SCC sids
# ===========================================================================

def test_must_include_rejects_op_on_non_scc_sid(pila):
    """The validator credits an op against a cycle only when the op
    targets an SCC member. An op on an unrelated subtask should NOT
    satisfy any cycle's must-include set — without this negative test,
    a regression that caused the validator to always return [] would
    not be caught by the existing positive tests."""
    # SCC of A and B; unrelated subtask C.
    a = _subtask("a", provides=["a-cap"], requires=["b-cap"])
    b = _subtask("b", provides=["b-cap"], requires=["a-cap"])
    c = _subtask("c", provides=["c-cap"])
    by_id = {s["id"]: s for s in (a, b, c)}
    _preds, _provs, _es, succ = _build_graph(pila, by_id)
    sccs = pila._tarjan_sccs(set(by_id), succ)
    assert sccs == [["a", "b"]], "fixture: SCC is exactly {a, b}"

    # Op targets C, not A or B → must NOT credit the cycle.
    output = {
        "renames": [], "added_provides": [], "added_subtasks": [],
        "dropped_requires": [
            {"sid": "c", "tag": "c-cap", "reason": "unrelated drop"},
        ],
        "dependency_edges": [], "merged_subtasks": [], "unresolvable": [],
    }
    unaddressed = pila._validate_must_include(output, sccs)
    assert unaddressed == ["a <-> b"], (
        "a dropped_requires on a non-SCC sid must NOT satisfy the cycle's "
        "must-include set; validator should report the cycle as unaddressed")


# ===========================================================================
# Tests 29-37: unresolved-requires retry loop (mirror of cycle-gate corpus)
# Grounded against captured run 075210 where deps-008 required
# 'cdk-stacks-authored' and the reconciler invented 'infra-stacks-authored'
# without renaming the original consumer's tag.
# ===========================================================================

def test_tag_jaccard_known_pairs(pila):
    """Pin the similarity function on the captured-failure pair + edge
    cases. The 0.500 result on the 075210 pair is load-bearing for the
    case-1 heuristic firing."""
    # Captured run 075210: shared {stacks, authored} of {cdk, stacks,
    # authored, infra} → 2/4 = 0.5.
    assert pila._tag_jaccard(
        "cdk-stacks-authored", "infra-stacks-authored") == 0.5
    # Identical tags → 1.0.
    assert pila._tag_jaccard("foo-bar", "foo-bar") == 1.0
    # Disjoint → 0.0.
    assert pila._tag_jaccard("foo-bar", "baz-qux") == 0.0
    # Both empty → 0.0 (not div-by-zero).
    assert pila._tag_jaccard("", "") == 0.0
    # One empty → 0.0.
    assert pila._tag_jaccard("foo", "") == 0.0
    # Single-token tags with overlap.
    assert pila._tag_jaccard("foo", "foo-bar") == 0.5


def test_recommend_unresolved_resolution_075210_case(pila):
    """The captured failure: deps-008 requires 'cdk-stacks-authored';
    config-011 (added by reconciler) provides 'infra-stacks-authored'.
    Heuristic case-1 must fire and recommend the missing rename. This
    is the load-bearing test for the retry path's value."""
    providers = {
        "infra-stacks-authored": ["config-011"],
        "infra-cdk-deps-present": ["deps-008"],  # self — should be skipped
        "prisma-deps-present": ["deps-001"],
        "node-engine-bumped": ["deps-007"],
    }
    rec = pila._recommend_unresolved_resolution(
        "deps-008", "cdk-stacks-authored", providers)
    assert rec is not None
    assert rec["op"] == "rename"
    assert rec["sid"] == "deps-008"
    assert rec["from"] == "cdk-stacks-authored"
    assert rec["to"] == "infra-stacks-authored"
    assert rec["rationale"] == "case-1: unique-strong-similarity"


def test_recommend_unresolved_resolution_self_loop_guard(pila):
    """Self-loop guard: if the top-similar candidate is provided by the
    consumer's OWN sid, skip it. Caught the historical deps-011
    'supabase-client-imports-removed' case where Jaccard would rank
    deps-011's own 'supabase-client-dep-removed' as top match, creating
    a self-edge in the dependency graph."""
    providers = {
        "supabase-client-dep-removed": ["deps-011"],  # SELF — must skip
        "node-engine-bumped": ["deps-007"],
    }
    rec = pila._recommend_unresolved_resolution(
        "deps-011", "supabase-client-imports-removed", providers)
    # Self-match skipped; nothing else has j >= 0.5; abstain.
    assert rec is None


def test_recommend_unresolved_resolution_no_match(pila):
    """No candidate has j >= 0.5 → return None, model decides unaided.
    Historical scan showed ~88% of post-mutation unresolved entries hit
    this branch — the heuristic abstains gracefully."""
    providers = {
        "totally-unrelated-thing": ["sub-a"],
        "another-unrelated": ["sub-b"],
    }
    rec = pila._recommend_unresolved_resolution(
        "consumer", "something-completely-different", providers)
    assert rec is None


def test_recommend_unresolved_resolution_multi_strong_abstains(pila):
    """Multiple candidates with j >= 0.5 and none >= 0.7 → abstain
    (model picks unaided). Avoids confidently picking between two
    near-equal candidates."""
    # Two candidates both at Jaccard 0.6.
    providers = {
        "infra-aws-stacks-authored": ["sub-x"],   # j with target = 0.6
        "cdk-aws-stacks-deployed": ["sub-y"],     # j with target = 0.6
        "unrelated": ["sub-z"],
    }
    rec = pila._recommend_unresolved_resolution(
        "consumer", "cdk-aws-stacks-authored", providers)
    # Neither hits the j>=0.7 very-high threshold; case-1 needs unique
    # top match so it also doesn't fire. Abstain.
    assert rec is None


def test_validate_unresolved_must_include_accepts_rename(pila):
    """A rename on the unresolved (sid, tag) addresses the entry."""
    unresolved = [{"domain": "deps", "sid": "deps-008",
                   "tag": "cdk-stacks-authored"}]
    output = {
        "renames": [{"sid": "deps-008", "from": "cdk-stacks-authored",
                     "to": "infra-stacks-authored"}],
        "added_provides": [], "added_subtasks": [],
        "dropped_requires": [], "dependency_edges": [],
        "merged_subtasks": [], "unresolvable": [],
    }
    assert pila._validate_unresolved_must_include(output, unresolved) == []


def test_validate_unresolved_must_include_accepts_added_provides(pila):
    """An added_provides covering the unresolved tag (on any sid)
    addresses the entry."""
    unresolved = [{"domain": "deps", "sid": "deps-008",
                   "tag": "cdk-stacks-authored"}]
    output = {
        "renames": [], "added_provides": [{"sid": "config-001",
                                            "tag": "cdk-stacks-authored"}],
        "added_subtasks": [], "dropped_requires": [],
        "dependency_edges": [], "merged_subtasks": [], "unresolvable": [],
    }
    assert pila._validate_unresolved_must_include(output, unresolved) == []


def test_validate_unresolved_must_include_accepts_added_subtask_with_provides(pila):
    """An added_subtask whose `provides` includes the unresolved tag
    addresses the entry."""
    unresolved = [{"domain": "deps", "sid": "deps-008",
                   "tag": "cdk-stacks-authored"}]
    output = {
        "renames": [], "added_provides": [],
        "added_subtasks": [{"id": "config-011",
                             "provides": ["cdk-stacks-authored"]}],
        "dropped_requires": [], "dependency_edges": [],
        "merged_subtasks": [], "unresolvable": [],
    }
    assert pila._validate_unresolved_must_include(output, unresolved) == []


def test_validate_unresolved_must_include_accepts_unresolvable(pila):
    """An `unresolvable` on the same (sid, tag) addresses the entry —
    surfaces a clean die() instead of failing must-include validation."""
    unresolved = [{"domain": "deps", "sid": "deps-008",
                   "tag": "cdk-stacks-authored"}]
    output = {
        "renames": [], "added_provides": [], "added_subtasks": [],
        "dropped_requires": [], "dependency_edges": [],
        "merged_subtasks": [],
        "unresolvable": [{"sid": "deps-008", "tag": "cdk-stacks-authored",
                          "reason": "no real producer in this plan"}],
    }
    assert pila._validate_unresolved_must_include(output, unresolved) == []


def test_validate_unresolved_must_include_rejects_unrelated_op(pila):
    """A rename on a DIFFERENT sid+tag does NOT address an unresolved
    entry. Without this negative test, a regression that caused the
    validator to always return [] would not be caught."""
    unresolved = [{"domain": "deps", "sid": "deps-008",
                   "tag": "cdk-stacks-authored"}]
    output = {
        "renames": [{"sid": "config-005", "from": "some-other-tag",
                     "to": "infra-stacks-authored"}],
        "added_provides": [], "added_subtasks": [],
        "dropped_requires": [], "dependency_edges": [],
        "merged_subtasks": [], "unresolvable": [],
    }
    unaddressed = pila._validate_unresolved_must_include(output, unresolved)
    assert unaddressed == ["deps/deps-008 requires 'cdk-stacks-authored'"]


def test_build_unresolved_retry_prompt_contains_required_sections(pila):
    """The retry prompt must surface the unresolved tags, top-3
    similarity ranking, the recommendation (if computed), the
    must-include set, and the original user prompt at the end."""
    unresolved = [{"domain": "dependency-migration", "sid": "deps-008",
                   "tag": "cdk-stacks-authored"}]
    providers = {
        "infra-stacks-authored": ["config-011"],
        "prisma-deps-present": ["deps-001"],
    }
    rec = pila._recommend_unresolved_resolution(
        "deps-008", "cdk-stacks-authored", providers)
    recs = {("deps-008", "cdk-stacks-authored"): rec}
    prompt = pila._build_unresolved_retry_prompt(
        unresolved, providers, recs, "ORIGINAL USER PROMPT")

    # Required sections.
    assert "1 cross-domain" in prompt
    assert "UNRESOLVED 1:" in prompt
    assert "dependency-migration/deps-008" in prompt
    assert "cdk-stacks-authored" in prompt
    assert "infra-stacks-authored" in prompt
    assert "HINT" in prompt  # recommendation surfaces as HINT, not "RECOMMENDED:"
    assert "false friend" in prompt  # softened framing
    assert "MUST include" in prompt
    assert "unresolvable" in prompt
    assert "ORIGINAL USER PROMPT" in prompt
    # Recommendation rendered as a rename literal.
    assert "rename(sid='deps-008'" in prompt
    assert "to='infra-stacks-authored'" in prompt
    # The must-include `renames:` example uses explicit-keyword syntax
    # matching the actual reconciler schema ({sid, from, to}) — not the
    # informal arrow form. Fix 8C: a literal-minded model emitting the
    # arrow form would produce malformed JSON (e.g.
    # `{"from": "cdk-stacks-authored → infra-stacks-authored", "to": ""}`).
    assert "'cdk-stacks-authored' → 'infra-stacks-authored'" not in prompt, (
        "must-include renames example must use explicit-keyword syntax "
        "(rename(sid='X', from='Y', to='Z')), not the informal arrow "
        "form — the arrow could mislead a literal-minded model")
    # And the explicit-keyword form IS present as the example.
    assert "rename(sid='deps-008', from='cdk-stacks-authored', " in prompt


# ===========================================================================
# Test 49: end-to-end integration test of the unresolved-retry loop
#
# The plan for the unresolved-retry feature called for this test (and a
# stubbed-claude_p replay verification) when it shipped; pass 12 of the
# audit caught that it wasn't actually implemented. Adding it now so a
# regression in the retry-loop wiring (e.g., refactor breaks attempt-2's
# prompt construction, or the revert step doesn't fully restore the
# snapshot) would be caught by pytest, not only by live PR-review runs.
# ===========================================================================

def _minimal_state_for_retry(pila, tmp_path):
    """Stub State with just enough plumbing for phase_reconcile +
    _spawn_reconciler to call bump_workers + st.save without crashing.

    Duplicates the pattern in tests/test_phase_reconcile.py:_minimal_state
    inline — keeping this test file independent of others (no cross-file
    test imports). Acceptable to duplicate ~5 lines of setup pattern
    rather than introduce a fixture coupling."""
    pila_root = tmp_path / ".pila"
    run_id = "test-unresolved-retry-aaa111"
    (pila_root / "runs" / run_id).mkdir(parents=True)
    st = pila.State(pila_root, run_id)
    st.data = {"task": "test", "worker_count": 0}
    st.save()
    return st


def test_unresolved_retry_loop_integration_with_stubbed_reconciler(
    pila, monkeypatch, tmp_path
):
    """End-to-end integration of the unresolved-tags retry loop.

    Fake `claude_p` returns the 075210-shape broken output on attempt 1
    — the model invents `infra-stacks-authored` without renaming
    deps-008's tag — and a fixture revising-output on attempt 2 that
    adds the missing rename. Asserts:
    1. `phase_reconcile` returns successfully (no `die`).
    2. Exactly 2 `claude_p` calls were made (initial + 1 retry).
    3. The merged plan has zero unresolved requires post-return.
    4. The 2nd call's prompt contains the unresolved-retry retry-prompt
       markers (so we know the retry path actually fired and the model
       was given the structural feedback).
    """
    # Fixture: post-classifier plans matching the 075210 shape's
    # relevant subset. deps-008 requires `cdk-stacks-authored`;
    # nothing provides it; config-001 provides the cdk-project scaffold
    # (so the connector the reconciler will invent has somewhere to
    # depend on).
    plans = [
        {"domain": "dependency-migration", "status": "ready",
         "subtasks": [{
             "id": "deps-008",
             "title": "Add infra/cdk deps",
             "intent": "Add @aws-cdk/* deps to infra/package.json",
             "provides": ["infra-cdk-deps-present"],
             "requires": [
                 {"tag": "cdk-stacks-authored", "extent": "in_plan"}],
             "depends_on": [],
             "files_likely_touched": ["infra/package.json"],
             "success_criteria_seed": "cdk lib deps installed",
             "size": "small"}]},
        {"domain": "configuration-build", "status": "ready",
         "subtasks": [{
             "id": "config-001",
             "title": "Scaffold CDK project",
             "intent": "Initialize infra/ with cdk.json + tsconfig",
             "provides": ["infra-cdk-project-scaffold"],
             "requires": [],
             "depends_on": [],
             "files_likely_touched": ["infra/cdk.json"],
             "success_criteria_seed": "cdk init succeeds",
             "size": "small"}]},
    ]

    # Attempt 1: the model invents config-011 providing
    # `infra-stacks-authored` but forgets to rename deps-008's
    # `cdk-stacks-authored`. This matches the captured 075210 failure.
    attempt_1_output = {
        "renames": [], "added_provides": [],
        "added_subtasks": [{
            "id": "config-011",
            "title": "Author CDK foundation + compute stacks",
            "intent": "Implement infra/lib/*-stack.ts",
            "success_criteria_seed": "cdk synth produces stack templates",
            "provides": ["infra-stacks-authored"],
            "requires": [
                {"tag": "infra-cdk-project-scaffold", "extent": "in_plan"}],
            "depends_on": ["config-001"],
            "size": "medium",
            "_added_by_reconciler": True}],
        "dropped_requires": [], "dependency_edges": [],
        "merged_subtasks": [], "unresolvable": [],
    }
    # Attempt 2: revised output adds the missing rename on deps-008.
    # The connector definition is preserved; deps-008's tag now matches.
    attempt_2_output = {
        "renames": [{"sid": "deps-008",
                     "from": "cdk-stacks-authored",
                     "to": "infra-stacks-authored"}],
        "added_provides": [],
        "added_subtasks": [{
            "id": "config-011",
            "title": "Author CDK foundation + compute stacks",
            "intent": "Implement infra/lib/*-stack.ts",
            "success_criteria_seed": "cdk synth produces stack templates",
            "provides": ["infra-stacks-authored"],
            "requires": [
                {"tag": "infra-cdk-project-scaffold", "extent": "in_plan"}],
            "depends_on": ["config-001"],
            "size": "medium",
            "_added_by_reconciler": True}],
        "dropped_requires": [], "dependency_edges": [],
        "merged_subtasks": [], "unresolvable": [],
    }

    calls: list[dict] = []

    async def fake_claude_p(**kwargs):
        # Capture the kwargs (user_prompt is what the retry test cares
        # about — confirms the unresolved-retry prompt actually got built
        # and sent on the second call).
        calls.append(kwargs)
        if len(calls) == 1:
            return attempt_1_output
        return attempt_2_output

    monkeypatch.setattr(pila, "claude_p", fake_claude_p)

    st = _minimal_state_for_retry(pila, tmp_path)
    # Caps need the keys phase_reconcile + bump_workers touch.
    caps = dict(pila.DEFAULT_CAPS)
    models = {"reconciler": "opus"}
    efforts = {"reconciler": "high"}

    result = asyncio.run(pila.phase_reconcile(
        plans, "migrate to AWS", st, caps, models, efforts))

    # 1. phase_reconcile returned (didn't die).
    assert result is not None

    # 2. Exactly 2 claude_p calls (initial + 1 retry).
    assert len(calls) == 2, (
        f"expected 2 claude_p calls (initial + unresolved-retry); "
        f"got {len(calls)} — retry path didn't fire correctly")

    # 3. The 2nd call's user_prompt contains the unresolved-retry markers,
    #    confirming the retry-prompt-builder was invoked.
    retry_prompt = calls[1]["user_prompt"]
    assert "cross-domain `requires` tag(s) still unresolved" in retry_prompt, (
        "2nd call's user_prompt should be the unresolved-retry prompt")
    assert "cdk-stacks-authored" in retry_prompt
    assert "HINT" in retry_prompt  # heuristic computed a recommendation

    # 4. Final plan has zero unresolved requires.
    final_unresolved = pila._compute_unresolved_requires(result)
    assert final_unresolved == [], (
        f"phase_reconcile should converge with zero unresolved entries; "
        f"got {final_unresolved}")

    # 5. The rename actually landed on deps-008 (apply-step executed).
    deps_008 = next(s for plan in result for s in plan.get("subtasks", [])
                    if s.get("id") == "deps-008")
    deps_008_tags = [r["tag"] for r in (deps_008.get("requires") or [])
                     if isinstance(r, dict)]
    assert "infra-stacks-authored" in deps_008_tags, (
        f"deps-008's requires should be renamed to 'infra-stacks-authored'; "
        f"got {deps_008_tags}")
    assert "cdk-stacks-authored" not in deps_008_tags, (
        "the original tag should have been renamed away")


# ===========================================================================
# Test 50 — failure-path integration test for the unresolved-retry loop
#
# Plan line 718 called for `test_unresolved_retry_dies_after_attempt_2`
# when the unresolved-retry feature shipped; pass 12 added the happy-path
# companion but missed this failure-path. Pass 13 closes the gap.
# ===========================================================================

def test_unresolved_retry_dies_after_attempt_2(
    pila, monkeypatch, tmp_path
):
    """The model returns the same broken output twice (doesn't fix the
    unresolved tag, doesn't emit `unresolvable`, doesn't address the
    named entry). Pila's must-include validator must fire on attempt 2
    and `die()` cleanly with the structured report.

    Without this test, a regression in the validator's `die()` wiring
    (e.g., the `if unaddressed:` branch silently swallows the error)
    would only surface in live runs.
    """
    # Same 075210-shape fixture as the happy-path test: deps-008
    # requires `cdk-stacks-authored`, no producer exists, the model
    # invents `infra-stacks-authored` and forgets the rename.
    plans = [
        {"domain": "dependency-migration", "status": "ready",
         "subtasks": [{
             "id": "deps-008",
             "title": "Add infra/cdk deps",
             "intent": "Add @aws-cdk/* deps",
             "provides": ["infra-cdk-deps-present"],
             "requires": [
                 {"tag": "cdk-stacks-authored", "extent": "in_plan"}],
             "depends_on": [],
             "files_likely_touched": ["infra/package.json"],
             "success_criteria_seed": "cdk lib deps installed",
             "size": "small"}]},
        {"domain": "configuration-build", "status": "ready",
         "subtasks": [{
             "id": "config-001",
             "title": "Scaffold CDK project",
             "intent": "Initialize infra/",
             "provides": ["infra-cdk-project-scaffold"],
             "requires": [],
             "depends_on": [],
             "files_likely_touched": ["infra/cdk.json"],
             "success_criteria_seed": "cdk init succeeds",
             "size": "small"}]},
    ]

    # The broken output (returned on BOTH calls — the model fails to fix).
    broken_output = {
        "renames": [], "added_provides": [],
        "added_subtasks": [{
            "id": "config-011",
            "title": "Author CDK stacks",
            "intent": "...",
            "success_criteria_seed": "cdk synth succeeds",
            "provides": ["infra-stacks-authored"],  # different name from deps-008's required tag
            "requires": [
                {"tag": "infra-cdk-project-scaffold", "extent": "in_plan"}],
            "depends_on": ["config-001"],
            "size": "medium",
            "_added_by_reconciler": True}],
        "dropped_requires": [], "dependency_edges": [],
        "merged_subtasks": [], "unresolvable": [],
    }

    calls: list[dict] = []

    async def fake_claude_p(**kwargs):
        calls.append(kwargs)
        # Return the SAME broken output on both calls.
        return broken_output

    monkeypatch.setattr(pila, "claude_p", fake_claude_p)
    st = _minimal_state_for_retry(pila, tmp_path)
    caps = dict(pila.DEFAULT_CAPS)
    models = {"reconciler": "opus"}
    efforts = {"reconciler": "high"}

    # phase_reconcile must die. `die()` calls sys.exit(); pytest catches
    # SystemExit.
    with pytest.raises(SystemExit) as exc_info:
        asyncio.run(pila.phase_reconcile(
            plans, "migrate to AWS", st, caps, models, efforts))

    # Confirm the retry actually fired (2 calls) — the die came AFTER
    # attempt 2, not before the retry started.
    assert len(calls) == 2, (
        f"expected 2 claude_p calls (initial + retry); got {len(calls)} "
        "— retry didn't fire OR died before attempt 2")

    # Confirm the die came from the must-include validator (the path that
    # checks the revised output addresses every named unresolved entry).
    # The `die()` message includes a specific phrase only the must-include
    # validator emits: "ignored N named unresolved-requires".
    assert exc_info.value.code != 0, "die() should exit non-zero"


# ===========================================================================
# Test 51 — happy-path integration test for the cycle-resolution retry loop
#
# Pass 12 added the analogue for the unresolved-retry and explicitly
# deferred this symmetric test to pass 13. Pass 13 closes the gap.
# ===========================================================================

def test_cycle_retry_loop_integration_with_stubbed_reconciler(
    pila, monkeypatch, tmp_path
):
    """End-to-end integration of the cycle-resolution retry loop.

    Fixture: two subtasks with mutually-requiring tags so the model's
    renames close a 2-node SCC. Fake `claude_p` returns cycle-closing
    renames on attempt 1, then `dropped_requires` on attempt 2 to
    break the cycle. Asserts:
    1. `phase_reconcile` returns successfully (no `die`).
    2. Exactly 2 `claude_p` calls (initial + cycle retry).
    3. The 2nd call's user_prompt contains cycle-retry markers
       (CYCLE 1:, RECOMMENDED:, MUST include).
    4. Final plan is acyclic.
    5. The drop landed on the right subtask's requires.

    Mirror of test_unresolved_retry_loop_integration_with_stubbed_reconciler
    for the cycle-retry path. The cycle-retry plan never explicitly named
    this test, but pass 12 flagged the symmetric gap as a pass-13 candidate.
    """
    # Pre-reconcile fixture: two subtasks whose unresolved requires
    # the model will rename onto each other's provides, closing a cycle.
    # feat-001 provides "backend-http-server" and requires the unresolved
    # tag "node-server-runtime-libs-present".
    # config-005 provides "app-runtime-deps" + "app-build-scripts" and
    # requires the unresolved tag "app-server-framework-present".
    # The model's attempt-1 renames both → producing the cycle.
    plans = [
        {"domain": "feature-implementation", "status": "ready",
         "subtasks": [{
             "id": "feat-001",
             "title": "Node HTTP backend entrypoint",
             "intent": "Long-lived Node process exposing /health",
             "provides": ["backend-http-server"],
             "requires": [
                 {"tag": "node-server-runtime-libs-present",
                  "extent": "in_plan"}],
             "depends_on": [],
             "files_likely_touched": ["server/index.ts"],
             "success_criteria_seed": "server starts, /health → 200",
             "size": "small"}]},
        {"domain": "configuration-build", "status": "ready",
         "subtasks": [{
             "id": "config-005",
             "title": "Update package.json scripts and deps",
             "intent": "Pin AWS runtime deps",
             "provides": ["app-runtime-deps", "app-build-scripts"],
             "requires": [
                 {"tag": "app-server-framework-present",
                  "extent": "in_plan"}],
             "depends_on": [],
             "files_likely_touched": ["package.json"],
             "success_criteria_seed": "package.json exposes build, start",
             "size": "small"}]},
    ]

    # Attempt 1: cycle-closing renames (exactly the captured run-2 shape).
    # feat-001's tag renamed → "app-runtime-deps" (provided by config-005).
    # config-005's tag renamed → "backend-http-server" (provided by feat-001).
    # Closes a 2-node SCC.
    attempt_1_output = {
        "renames": [
            {"sid": "feat-001",
             "from": "node-server-runtime-libs-present",
             "to": "app-runtime-deps"},
            {"sid": "config-005",
             "from": "app-server-framework-present",
             "to": "backend-http-server"},
        ],
        "added_provides": [], "added_subtasks": [],
        "dropped_requires": [], "dependency_edges": [],
        "merged_subtasks": [], "unresolvable": [],
    }
    # Attempt 2: model uses `dropped_requires` to break the cycle.
    # NOTE: the cycle-retry's revert restores the PRE-mutation plans, so
    # attempt-2's apply runs against the original (un-renamed) requires
    # entries. Therefore both renames must be re-asserted, AND the drop
    # must target the post-rename tag on the consumer side. Equivalently,
    # we can drop the ORIGINAL requires tag (pre-rename) — both lead to
    # the same final state. Use the original-tag approach: cleaner, no
    # dependency on the rename being applied first.
    attempt_2_output = {
        # Keep feat-001's rename so feat-001's requires gets satisfied
        # by config-005's `app-runtime-deps`.
        "renames": [
            {"sid": "feat-001",
             "from": "node-server-runtime-libs-present",
             "to": "app-runtime-deps"},
        ],
        "added_provides": [], "added_subtasks": [],
        # Drop config-005's ORIGINAL `app-server-framework-present`
        # requires entry. This breaks the cycle because config-005 no
        # longer requires anything feat-001 provides.
        "dropped_requires": [
            {"sid": "config-005",
             "tag": "app-server-framework-present",
             "reason": "framework decision recorded by config-005 itself, "
                       "not a code artifact feat-001 produces"},
        ],
        "dependency_edges": [], "merged_subtasks": [], "unresolvable": [],
    }

    calls: list[dict] = []

    async def fake_claude_p(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return attempt_1_output
        return attempt_2_output

    monkeypatch.setattr(pila, "claude_p", fake_claude_p)
    st = _minimal_state_for_retry(pila, tmp_path)
    caps = dict(pila.DEFAULT_CAPS)
    models = {"reconciler": "opus"}
    efforts = {"reconciler": "high"}

    result = asyncio.run(pila.phase_reconcile(
        plans, "migrate to AWS", st, caps, models, efforts))

    # 1. phase_reconcile returned (no die).
    assert result is not None

    # 2. Exactly 2 claude_p calls (initial + cycle retry).
    assert len(calls) == 2, (
        f"expected 2 claude_p calls (initial + cycle retry); got "
        f"{len(calls)} — cycle retry didn't fire correctly")

    # 3. The 2nd call's user_prompt contains the cycle-retry markers.
    retry_prompt = calls[1]["user_prompt"]
    assert "dependency cycle(s)" in retry_prompt, (
        "2nd call's user_prompt should be the cycle-retry prompt")
    assert "CYCLE 1:" in retry_prompt
    assert "RECOMMENDED:" in retry_prompt
    assert "MUST include" in retry_prompt
    # Both SCC members named in the retry prompt.
    assert "feat-001" in retry_prompt
    assert "config-005" in retry_prompt

    # 4. Final plan is acyclic — rebuild the graph from the post-retry
    #    state and run Tarjan.
    by_id = {s["id"]: s for plan in result for s in plan.get("subtasks", [])}
    _preds, _provs, _es = pila._build_predecessor_graph(by_id)
    succ: dict[str, set[str]] = {sid: set() for sid in by_id}
    for tgt, src_set in _preds.items():
        for src in src_set:
            succ[src].add(tgt)
    sccs = pila._tarjan_sccs(set(by_id), succ)
    assert sccs == [], (
        f"final plan should be acyclic; Tarjan found SCCs: {sccs}")

    # 5. The drop actually landed on config-005's requires (apply-step
    #    executed the dropped_requires op against the original tag).
    config_005 = next(s for plan in result for s in plan.get("subtasks", [])
                      if s.get("id") == "config-005")
    config_005_tags = [r["tag"] for r in (config_005.get("requires") or [])
                       if isinstance(r, dict)]
    assert "app-server-framework-present" not in config_005_tags, (
        f"config-005's `app-server-framework-present` requires should have "
        f"been dropped by the cycle-retry; remaining tags: {config_005_tags}")
