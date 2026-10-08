# Changelog

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
