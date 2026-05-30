"""Structural tests for fly.toml.

Ensures the file is valid TOML (parseable by Python's tomllib / tomli),
declares the required keys, and enforces the zero-warm-pool requirement
(min_machines_running = 0, no [[services]] stanza).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
FLY_TOML = REPO_ROOT / "fly.toml"


def _load_fly_toml() -> dict:
    if sys.version_info >= (3, 11):
        import tomllib
        with FLY_TOML.open("rb") as f:
            return tomllib.load(f)
    else:
        try:
            import tomli  # type: ignore[import]
            with FLY_TOML.open("rb") as f:
                return tomli.load(f)
        except ImportError:
            pytest.skip("tomli not installed and Python < 3.11")


def test_fly_toml_exists():
    assert FLY_TOML.exists(), "fly.toml is missing from the repo root"


def test_fly_toml_is_valid_toml():
    data = _load_fly_toml()
    assert isinstance(data, dict)


def test_fly_toml_declares_app():
    data = _load_fly_toml()
    assert "app" in data, "fly.toml must declare 'app'"
    assert data["app"], "fly.toml 'app' must be non-empty"


def test_fly_toml_declares_primary_region():
    data = _load_fly_toml()
    assert "primary_region" in data, "fly.toml must declare 'primary_region'"


def test_fly_toml_declares_image():
    data = _load_fly_toml()
    build = data.get("build", {})
    assert "image" in build, "fly.toml [build] must declare 'image'"
    assert build["image"].startswith("registry.fly.io/"), (
        "fly.toml image must point to registry.fly.io/"
    )


def test_fly_toml_vm_sizing():
    data = _load_fly_toml()
    vm = data.get("vm", {})
    cpus = vm.get("cpus", 0)
    memory_mb = vm.get("memory_mb", 0)
    assert 2 <= cpus <= 8, f"cpus={cpus} outside 2–8 range from INSTALL.md guidance"
    assert 4096 <= memory_mb <= 16384, (
        f"memory_mb={memory_mb} outside 4–16 GB range from INSTALL.md guidance"
    )


def test_fly_toml_zero_warm_pool():
    data = _load_fly_toml()
    deploy = data.get("deploy", {})
    min_machines = deploy.get("min_machines_running", None)
    assert min_machines == 0, (
        f"min_machines_running must be 0 (zero warm pool); got {min_machines!r}"
    )


def test_fly_toml_no_services_stanza():
    data = _load_fly_toml()
    assert "services" not in data, (
        "fly.toml must not have a [[services]] stanza — pila is not an HTTP service"
    )
