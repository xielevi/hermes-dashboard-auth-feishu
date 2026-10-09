"""Settings resolution and real host discovery (in an isolated Hermes home)."""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import feishu_auth_pkg as pkg
from feishu_auth_pkg import register, resolve_settings

ROOT = Path(__file__).resolve().parents[1]


class Ctx:
    def __init__(self, **settings):
        self.settings, self.registered = settings, []

    def get_config(self, key, default=None):
        return self.settings.get(key, default)

    def register_dashboard_auth_provider(self, provider):
        self.registered.append(provider)


SETTINGS = dict(app_id="cli_example", tenant_key="example_tenant", owner_open_ids=["ou_a"],
                public_url="https://hermes.example.com")


def test_register_is_a_noop_without_config(clean_env):
    ctx = Ctx()
    register(ctx)
    assert ctx.registered == [] and "missing" in pkg.LAST_SKIP_REASON


def test_secret_is_required(clean_env):
    assert resolve_settings(Ctx(**SETTINGS)) is None
    assert "app secret" in pkg.LAST_SKIP_REASON


def test_settings_from_plugin_namespace(clean_env):
    clean_env.setenv("HERMES_DASHBOARD_FEISHU_APP_SECRET", "dedicated")
    s = resolve_settings(Ctx(**SETTINGS))
    assert s["app_secret"] == "dedicated" and s["owner_open_ids"] == ["ou_a"] and s["domain"] == "feishu"
    assert s["session_key"] == ""


def test_environment_overrides_settings(clean_env):
    clean_env.setenv("FEISHU_APP_SECRET", "gateway-app")
    clean_env.setenv("HERMES_DASHBOARD_FEISHU_OWNER_OPEN_IDS", "ou_x, ou_y")
    clean_env.setenv("HERMES_DASHBOARD_FEISHU_DOMAIN", "LARK")
    clean_env.setenv("HERMES_DASHBOARD_FEISHU_SESSION_KEY", "bb" * 32)
    s = resolve_settings(Ctx(**SETTINGS))
    assert s["app_secret"] == "gateway-app"
    assert s["owner_open_ids"] == ["ou_x", "ou_y"] and s["domain"] == "lark" and s["session_key"] == "bb" * 32


def test_public_url_falls_back_to_host(clean_env):
    clean_env.setenv("FEISHU_APP_SECRET", "s")
    clean_env.setattr(pkg, "_host_public_url", lambda: "https://host.example.com")
    settings = {k: v for k, v in SETTINGS.items() if k != "public_url"}
    assert resolve_settings(Ctx(**settings))["public_url"] == "https://host.example.com"


def test_unusable_store_is_a_skip_not_a_crash(clean_env, monkeypatch):
    clean_env.setenv("FEISHU_APP_SECRET", "s")
    from feishu_auth_pkg import provider

    def boom(**_):
        raise PermissionError("plugin-data not writable")
    monkeypatch.setattr(provider.FeishuProvider, "from_settings", boom)
    ctx = Ctx(**SETTINGS)
    register(ctx)
    assert ctx.registered == [] and "not writable" in pkg.LAST_SKIP_REASON


def test_real_plugin_manager_registers_from_plugin_settings(tmp_path):
    home = tmp_path / "home"
    target = home / "plugins" / "dashboard-auth-feishu"
    shutil.copytree(ROOT, target, ignore=shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache", ".ruff_cache"))
    (home / "config.yaml").write_text(json.dumps({
        "plugins": {"enabled": ["dashboard-auth-feishu"],
                    "entries": {"dashboard-auth-feishu": {"settings": SETTINGS}}},
    }))
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("HERMES_DASHBOARD") and k not in ("FEISHU_APP_SECRET", "HERMES_HOME")}
    env.update(HERMES_HOME=str(home), HERMES_DASHBOARD_FEISHU_APP_SECRET="test-only-secret",
               HERMES_DASHBOARD_FEISHU_SESSION_KEY="ab" * 32)
    script = '''
from hermes_cli.plugins import PluginManager
from hermes_cli.dashboard_auth import list_providers
PluginManager().discover_and_load()
providers = [p for p in list_providers() if p.name == "feishu"]
assert len(providers) == 1, "real discovery must register the configured provider"
p = providers[0]
assert p.store.tenant == "example_tenant"
assert p.store.owners == frozenset({"ou_a"})
assert p.callback == "https://hermes.example.com/auth/callback"
assert p.store.path.is_relative_to(__import__("os").environ["HERMES_HOME"])
print("real plugin registration passed")
'''
    result = subprocess.run([sys.executable, "-c", script], env=env, cwd=tmp_path,
                            text=True, capture_output=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "real plugin registration passed" in result.stdout
