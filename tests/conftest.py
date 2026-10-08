"""Load the plugin as a package so tests can import it without installing it.

Run from a Hermes checkout environment (``hermes_cli`` must be importable).
"""
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "feishu_auth_pkg", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
pkg = importlib.util.module_from_spec(_spec)
sys.modules["feishu_auth_pkg"] = pkg
_spec.loader.exec_module(pkg)


@pytest.fixture
def clean_env(monkeypatch):
    """Remove any real Feishu/dashboard settings from the developer's environment."""
    import os

    for key in list(os.environ):
        if key.startswith("HERMES_DASHBOARD") or key == "FEISHU_APP_SECRET":
            monkeypatch.delenv(key)
    monkeypatch.setattr(pkg, "_host_public_url", lambda: "")
    return monkeypatch
