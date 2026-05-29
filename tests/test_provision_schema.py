"""Schema tests for `SCHEMAS["provision"]` — the install recipe the
provision LLM worker emits (DESIGN §6½).

The schema is consumed by `claude_p()` via `--json-schema` to gate the
worker's output. We don't have the live `claude` CLI in tests, so the
gating itself is exercised end-to-end by other means; here we just pin
the schema's structural contract so a future refactor can't silently
drop the worker or weaken the carve-out's containment.

Same style as `test_reconciler_schema.py`: reach into the schema dict
and reason over its `required` / `properties` keys directly.
"""
from __future__ import annotations


def test_provision_schema_exists(pila):
    """SCHEMAS["provision"] is the contract claude_p enforces against
    the worker's output. Existence pin so a future refactor can't
    silently drop the §6½ carve-out's schema-side containment."""
    assert "provision" in pila.SCHEMAS
    schema = pila.SCHEMAS["provision"]
    assert schema["type"] == "object"


def test_provision_requires_recipe(pila):
    """Every payload must carry a recipe — a missing key would let the
    LLM fall through to nothing, which is worse than the validator
    rejecting an empty list (the validator would, but only if it gets
    a list to look at)."""
    schema = pila.SCHEMAS["provision"]
    assert set(schema["required"]) == {"recipe"}


def test_provision_recipe_is_array_of_objects(pila):
    """Recipe is a list of install entries; each entry is an object
    with the documented shape."""
    recipe = pila.SCHEMAS["provision"]["properties"]["recipe"]
    assert recipe["type"] == "array"
    assert recipe["items"]["type"] == "object"


def test_provision_recipe_item_required_fields(pila):
    """Each recipe entry requires kind + command + working_dir. timeout_s
    is optional (caller defaults it to 1800s)."""
    item = pila.SCHEMAS["provision"]["properties"]["recipe"]["items"]
    required = set(item["required"])
    assert required == {"kind", "command", "working_dir"}


def test_provision_kind_enum_is_install_build_none(pila):
    """The kind enum is the closed set the executor and validator
    understand. Adding a value here without teaching the executor
    would silently drop the entry."""
    item = pila.SCHEMAS["provision"]["properties"]["recipe"]["items"]
    kinds = set(item["properties"]["kind"]["enum"])
    assert kinds == {"install", "build", "none"}


def test_provision_command_is_argv_list(pila):
    """command is an array of strings (an argv), NOT a shell string —
    enforced by the schema so the LLM cannot smuggle a shell pipeline
    through a single string."""
    item = pila.SCHEMAS["provision"]["properties"]["recipe"]["items"]
    cmd_schema = item["properties"]["command"]
    assert cmd_schema["type"] == "array"
    assert cmd_schema["items"]["type"] == "string"
    assert cmd_schema["minItems"] == 1


def test_provision_optional_top_level_fields(pila):
    """confidence and notes are documented optional top-level fields
    the LLM may emit for the audit log. Their absence must not reject
    a payload; pin them as declared properties so removing them is a
    deliberate decision, not an accident."""
    props = pila.SCHEMAS["provision"]["properties"]
    assert "confidence" in props
    assert "notes" in props
    # Not in required.
    assert "confidence" not in set(pila.SCHEMAS["provision"]["required"])
    assert "notes" not in set(pila.SCHEMAS["provision"]["required"])


def test_provision_argv0_allowlist_intersects_documented_managers(pila):
    """The argv[0] allowlist enforced by validate_provision_recipe is
    the §12 carve-out's mechanical containment. Pin it against the
    documented set from IMPLEMENTATION §6½ — any drift here must be a
    deliberate update to both the validator and the docs."""
    documented = {
        "pnpm", "npm", "yarn", "pip", "pip3", "uv", "poetry", "pipenv",
        "go", "cargo", "bundle", "gem", "mvn", "gradle", "gradlew", "make",
    }
    assert pila._PROVISION_ARGV0_ALLOW == documented


def test_provision_in_worker_types(pila):
    """`provision` must be wired into WORKER_TYPES so resolve_models()
    picks up its --model-provision / PILA_MODEL_PROVISION / model_provision
    overrides automatically (the argparse setup iterates WORKER_TYPES)."""
    assert "provision" in pila.WORKER_TYPES


def test_provision_default_model_is_opus(pila):
    """provision is a judgment worker — it reads README + configs and
    decides install commands. Default is Opus per the IMPLEMENTATION §2
    model-selection table. (Implemented by being absent from
    MODEL_DEFAULT_PER_WORKER, which means it falls through to
    MODEL_DEFAULT.)"""
    assert pila.MODEL_DEFAULT == "opus"
    assert "provision" not in pila.MODEL_DEFAULT_PER_WORKER
