"""Tests for `synth_mise_go_override` — pila's bridge between Go's
`go.mod` and mise's go plugin.

Mise does NOT parse `go.mod`'s `go 1.X` line (the mise maintainer
explicitly chose not to, per discussions jdx/mise#4136 and #8510 —
`go 1.X` is a minimum-required version, not an exact pin). For repos
that ship only `go.mod` with no companion `.go-version` /
`.tool-versions` / `mise.toml` go pin, pila synthesizes a mise
override file: a TOML snippet read via `MISE_OVERRIDE_CONFIG_FILENAMES`
that pins the go version mise should install.

Critical behavior (DESIGN §6½):
- No `go.mod` → no synthesis.
- `go.mod` present but go pin already exists elsewhere → no synthesis
  (the existing pin wins).
- `go.mod` present and no other pin → synthesize.
- Existing `mise.toml` is concatenated into the override file because
  `MISE_OVERRIDE_CONFIG_FILENAMES` replaces rather than merges the
  default config discovery.
"""
from __future__ import annotations


def test_no_gomod_returns_none(pila, tmp_path):
    run_dir = tmp_path / ".pila" / "runs" / "x"
    out = pila.synth_mise_go_override(tmp_path, run_dir)
    assert out is None


def test_synthesizes_from_simple_gomod(pila, tmp_path):
    (tmp_path / "go.mod").write_text(
        "module example.com/foo\n\ngo 1.22\n")
    run_dir = tmp_path / ".pila" / "runs" / "x"
    out = pila.synth_mise_go_override(tmp_path, run_dir)
    assert out is not None
    body = out.read_text()
    assert '[tools]' in body
    assert 'go = "1.22"' in body


def test_synthesizes_with_patch_version(pila, tmp_path):
    """`go 1.22.3` is also valid in go.mod and should be picked up
    verbatim."""
    (tmp_path / "go.mod").write_text(
        "module example.com/foo\n\ngo 1.22.3\n")
    run_dir = tmp_path / ".pila" / "runs" / "x"
    out = pila.synth_mise_go_override(tmp_path, run_dir)
    body = out.read_text()
    assert 'go = "1.22.3"' in body


def test_existing_go_version_file_blocks_synth(pila, tmp_path):
    """If a `.go-version` file exists, the existing pin wins."""
    (tmp_path / "go.mod").write_text("go 1.22\n")
    (tmp_path / ".go-version").write_text("1.21.5\n")
    run_dir = tmp_path / ".pila" / "runs" / "x"
    out = pila.synth_mise_go_override(tmp_path, run_dir)
    assert out is None


def test_existing_tool_versions_with_go_blocks_synth(pila, tmp_path):
    """A `.tool-versions` line `go 1.21.5` is also a pre-existing pin."""
    (tmp_path / "go.mod").write_text("go 1.22\n")
    (tmp_path / ".tool-versions").write_text("go 1.21.5\nnode 20.11.0\n")
    run_dir = tmp_path / ".pila" / "runs" / "x"
    out = pila.synth_mise_go_override(tmp_path, run_dir)
    assert out is None


def test_existing_tool_versions_without_go_does_not_block(pila, tmp_path):
    """A `.tool-versions` that pins other tools but not Go is NOT a
    blocker — pila synthesizes the Go pin so mise installs Go on top of
    the existing Node/Python pins."""
    (tmp_path / "go.mod").write_text("go 1.22\n")
    (tmp_path / ".tool-versions").write_text("node 20.11.0\npython 3.11.7\n")
    run_dir = tmp_path / ".pila" / "runs" / "x"
    out = pila.synth_mise_go_override(tmp_path, run_dir)
    assert out is not None


def test_existing_mise_toml_with_go_blocks_synth(pila, tmp_path):
    """A `[tools] go = "..."` line in mise.toml is a pre-existing pin."""
    (tmp_path / "go.mod").write_text("go 1.22\n")
    (tmp_path / "mise.toml").write_text('[tools]\ngo = "1.21"\nnode = "20.11.0"\n')
    run_dir = tmp_path / ".pila" / "runs" / "x"
    out = pila.synth_mise_go_override(tmp_path, run_dir)
    assert out is None


def test_existing_mise_toml_without_go_is_concatenated(pila, tmp_path):
    """The replace-not-merge workaround: if the repo has a mise.toml
    pinning Node but not Go, the override file must include both the
    existing pins AND our synthesized Go pin — otherwise pointing mise
    at the override clobbers the original Node pin.

    Critically: the synthesized file must have EXACTLY ONE `[tools]`
    header. An earlier version of the helper blindly appended a fresh
    `[tools]` section, producing two headers in the same file — which
    TOML 1.0 §6.5 ("Defining a table more than once is invalid")
    forbids. The single-header assertion pins the structural fix.
    """
    (tmp_path / "go.mod").write_text("go 1.22\n")
    (tmp_path / "mise.toml").write_text('[tools]\nnode = "20.11.0"\n')
    run_dir = tmp_path / ".pila" / "runs" / "x"
    out = pila.synth_mise_go_override(tmp_path, run_dir)
    body = out.read_text()
    assert 'node = "20.11.0"' in body
    assert 'go = "1.22"' in body
    assert body.count("[tools]") == 1, (
        f"synthesized file has {body.count('[tools]')} `[tools]` "
        f"headers; TOML rejects duplicate tables. Body:\n{body}"
    )


def test_existing_mise_toml_without_tools_section_gets_one_appended(
        pila, tmp_path):
    """If the existing mise.toml has no `[tools]` section at all (only
    other tables like `[env]` or `[settings]`), the synth helper must
    append a fresh `[tools]` section without disturbing the existing
    ones. Single-header assertion guards against any double-append
    regression."""
    (tmp_path / "go.mod").write_text("go 1.22\n")
    (tmp_path / "mise.toml").write_text('[env]\nFOO = "bar"\n')
    run_dir = tmp_path / ".pila" / "runs" / "x"
    out = pila.synth_mise_go_override(tmp_path, run_dir)
    body = out.read_text()
    assert 'FOO = "bar"' in body
    assert 'go = "1.22"' in body
    assert body.count("[tools]") == 1


def test_polyglot_go_plus_nvmrc_injects_node_pin(pila, tmp_path):
    """The MISE_OVERRIDE_CONFIG_FILENAMES suppression bug: when the
    override fires for a go.mod + .nvmrc repo with no mise.toml,
    mise would discover ONLY the override file — `.nvmrc` would be
    silently dropped and workers would run on the image-baked Node
    LTS instead of the repo-pinned version.

    The synth helper bridges that gap by copying `.nvmrc`'s value
    into the override's `[tools]` section alongside the synthesized
    go pin."""
    (tmp_path / "go.mod").write_text("go 1.22\n")
    (tmp_path / ".nvmrc").write_text("20.11.0\n")
    run_dir = tmp_path / ".pila" / "runs" / "x"
    out = pila.synth_mise_go_override(tmp_path, run_dir)
    body = out.read_text()
    assert 'go = "1.22"' in body
    assert 'node = "20.11.0"' in body
    assert body.count("[tools]") == 1


def test_dotted_mise_toml_with_go_blocks_synth(pila, tmp_path):
    """mise officially recognizes `.mise.toml` as a valid config file
    name (https://mise.jdx.dev/configuration.html). A repo pinning
    go via the dotted form must NOT trigger synthesis — that would
    silently overwrite the user's chosen mise config."""
    (tmp_path / "go.mod").write_text("go 1.22\n")
    (tmp_path / ".mise.toml").write_text('[tools]\ngo = "1.20"\n')
    run_dir = tmp_path / ".pila" / "runs" / "x"
    out = pila.synth_mise_go_override(tmp_path, run_dir)
    assert out is None


def test_dotted_mise_toml_node_pin_survives_synth(pila, tmp_path):
    """The H1 regression case from the fifth-pass audit: a repo with
    `.mise.toml` (dotted form) pinning node + a `go.mod` would silently
    drop the node pin into the override because the synth only checked
    the non-dotted `mise.toml` filename. Now the helper sees both."""
    (tmp_path / "go.mod").write_text("go 1.22\n")
    (tmp_path / ".mise.toml").write_text('[tools]\nnode = "20.11.0"\n')
    run_dir = tmp_path / ".pila" / "runs" / "x"
    out = pila.synth_mise_go_override(tmp_path, run_dir)
    body = out.read_text()
    assert 'go = "1.22"' in body
    assert 'node = "20.11.0"' in body, \
        ".mise.toml node pin must be preserved in the override"
    assert body.count("[tools]") == 1


def test_capital_v_prefix_is_stripped(pila, tmp_path):
    """`.nvmrc` files in the wild sometimes use `V20.11.0` (capital).
    Earlier strip used `lstrip("v")` which only stripped lowercase;
    `V20.11.0` would have passed through to mise as an invalid
    version. The regex-based strip handles both cases."""
    (tmp_path / "go.mod").write_text("go 1.22\n")
    (tmp_path / ".nvmrc").write_text("V20.11.0\n")
    run_dir = tmp_path / ".pila" / "runs" / "x"
    out = pila.synth_mise_go_override(tmp_path, run_dir)
    body = out.read_text()
    assert 'node = "20.11.0"' in body
    assert 'node = "V20.11.0"' not in body


def test_asdf_nodejs_alias_normalized_to_node(pila, tmp_path):
    """`.tool-versions` is asdf-compatible; asdf uses `nodejs` while
    mise uses `node`. Without alias normalization, a repo with
    `.nvmrc: 20.11.0` + `.tool-versions: nodejs 18.17.0` would
    produce an override with BOTH `node = "20.11.0"` AND `nodejs =
    "18.17.0"` — mise treats both as the same tool, resulting in
    ambiguous resolution. The alias map normalizes `nodejs` → `node`
    before the already_pinned check, so the second one is correctly
    skipped."""
    (tmp_path / "go.mod").write_text("go 1.22\n")
    (tmp_path / ".nvmrc").write_text("20.11.0\n")
    (tmp_path / ".tool-versions").write_text(
        "nodejs 18.17.0\npython 3.11.7\n")
    run_dir = tmp_path / ".pila" / "runs" / "x"
    out = pila.synth_mise_go_override(tmp_path, run_dir)
    body = out.read_text()
    assert 'node = "20.11.0"' in body, ".nvmrc must win the precedence"
    assert 'node = "18.17.0"' not in body, "nodejs alias must not clobber"
    assert 'nodejs' not in body, "asdf alias must not leak into override"
    assert 'python = "3.11.7"' in body, "python from .tool-versions ok"


def test_tool_versions_nodejs_alone_normalized_to_node(pila, tmp_path):
    """A repo using only `.tool-versions: nodejs <ver>` (no `.nvmrc`)
    should produce an override pinning `node` (mise's canonical
    name), not `nodejs`."""
    (tmp_path / "go.mod").write_text("go 1.22\n")
    (tmp_path / ".tool-versions").write_text("nodejs 20.11.0\n")
    run_dir = tmp_path / ".pila" / "runs" / "x"
    out = pila.synth_mise_go_override(tmp_path, run_dir)
    body = out.read_text()
    assert 'node = "20.11.0"' in body
    assert 'nodejs' not in body


def test_nvmrc_v_prefix_is_stripped(pila, tmp_path):
    """`.nvmrc` commonly carries a `v` prefix (`v20.11.0`); mise
    expects bare versions. Strip it before injecting."""
    (tmp_path / "go.mod").write_text("go 1.22\n")
    (tmp_path / ".nvmrc").write_text("v20.11.0\n")
    run_dir = tmp_path / ".pila" / "runs" / "x"
    out = pila.synth_mise_go_override(tmp_path, run_dir)
    body = out.read_text()
    assert 'node = "20.11.0"' in body
    assert 'node = "v20.11.0"' not in body


def test_polyglot_go_plus_python_version(pila, tmp_path):
    """`.python-version` is the most common idiomatic Python pin; it
    must survive when the go.mod synth fires."""
    (tmp_path / "go.mod").write_text("go 1.22\n")
    (tmp_path / ".python-version").write_text("3.11.7\n")
    run_dir = tmp_path / ".pila" / "runs" / "x"
    out = pila.synth_mise_go_override(tmp_path, run_dir)
    body = out.read_text()
    assert 'go = "1.22"' in body
    assert 'python = "3.11.7"' in body


def test_existing_mise_toml_pin_wins_over_idiomatic_file(pila, tmp_path):
    """If `mise.toml` already pins node, the user has made an explicit
    choice — `.nvmrc`'s value must NOT clobber it. The injection step
    skips any tool already present in the existing `[tools]` section.

    Note: a repo carrying both a mise.toml node pin AND a different
    .nvmrc value is almost certainly a misconfiguration — but choosing
    mise.toml is the same rule mise itself documents (mise.toml beats
    idiomatic discovery), so pila matches that precedence."""
    (tmp_path / "go.mod").write_text("go 1.22\n")
    (tmp_path / ".nvmrc").write_text("20.11.0\n")
    (tmp_path / "mise.toml").write_text('[tools]\nnode = "18.17.0"\n')
    run_dir = tmp_path / ".pila" / "runs" / "x"
    out = pila.synth_mise_go_override(tmp_path, run_dir)
    body = out.read_text()
    assert 'node = "18.17.0"' in body
    assert 'node = "20.11.0"' not in body
    assert 'go = "1.22"' in body
    assert body.count("[tools]") == 1


def test_tool_versions_multiple_entries_injected(pila, tmp_path):
    """`.tool-versions` is asdf's multi-tool format (`tool version` per
    line). When the synth fires, every tool in it must be copied to the
    override; comments and blank lines must be tolerated."""
    (tmp_path / "go.mod").write_text("go 1.22\n")
    (tmp_path / ".tool-versions").write_text(
        "# pinned for prod parity\n"
        "node 20.11.0\n"
        "python 3.11.7  # interpreter for tooling\n"
        "\n"
        "ruby 3.3.0\n"
    )
    run_dir = tmp_path / ".pila" / "runs" / "x"
    out = pila.synth_mise_go_override(tmp_path, run_dir)
    body = out.read_text()
    assert 'node = "20.11.0"' in body
    assert 'python = "3.11.7"' in body
    assert 'ruby = "3.3.0"' in body
    assert 'go = "1.22"' in body


def test_malformed_gomod_returns_none(pila, tmp_path):
    """A go.mod without a `go` directive (rare but possible in stub
    files) is not synthesizable — the helper returns None rather than
    guessing."""
    (tmp_path / "go.mod").write_text("module example.com/foo\n")
    run_dir = tmp_path / ".pila" / "runs" / "x"
    out = pila.synth_mise_go_override(tmp_path, run_dir)
    assert out is None


def test_run_dir_is_created_if_missing(pila, tmp_path):
    """The helper creates its target run_dir, so callers don't have to
    mkdir before invoking."""
    (tmp_path / "go.mod").write_text("go 1.22\n")
    run_dir = tmp_path / ".pila" / "runs" / "fresh" / "nested"
    assert not run_dir.exists()
    out = pila.synth_mise_go_override(tmp_path, run_dir)
    assert out is not None
    assert out.parent == run_dir
    assert run_dir.is_dir()
