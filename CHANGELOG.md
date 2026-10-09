# Changelog

## 1.0.1

Stability fixes. Settings, environment variables and session format are unchanged; existing sessions
stay valid.

- A malformed Feishu `user_info` response (non-object `data`, missing or non-string IDs) is now a
  provider error instead of an unhandled exception.
- Session-store failures while creating a session now surface as a provider error (HTTP 503), like
  verify and refresh already did. A session database that cannot be opened at startup skips
  registration with a reason instead of breaking plugin load.
- `public_url` may carry a path prefix (`https://example.com/hermes`), matching how Hermes builds
  the OAuth redirect URI from `dashboard.public_url`.
- The login button reads "Lark" when `domain` is `lark`.
- A refused sign-in logs the tenant_key and open_id Feishu returned, so first-time setup no longer
  needs another tool to find them.
- Removed `provides_middleware` from `plugin.yaml`; it is a catalog field and Hermes warned about it.
- README: Hermes Desktop coexistence split by backend type (Desktop-spawned backends are exempt
  from the gate since Hermes 0.21.6), path-prefix deployments, finding your IDs.
- Tests for malformed identity responses, locked and unusable session stores, and real multi-thread
  concurrent refresh. CI also runs against the oldest 0.21.5 build the plugin is tested on.

## 1.0.0

First public release.

- Feishu and Lark OAuth sign-in for the Hermes dashboard, admitted by tenant + open_id allow-list.
- Local rotating session families with idle/absolute expiry, a 60-second parallel-refresh grace
  window and replay revocation. No upstream tokens are stored.
- Settings under `plugins.entries.dashboard-auth-feishu.settings`, with
  `HERMES_DASHBOARD_FEISHU_*` environment overrides. Secrets are environment-only.
- Session database is created with mode 0600 from the start, under Hermes' per-plugin data directory.
- CI runs unit tests, real `PluginManager` discovery, `plugins doctor` and `plugins validate`
  against a pinned Hermes release.
