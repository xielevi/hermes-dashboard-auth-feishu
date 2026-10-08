# Security policy

## Reporting a vulnerability

Use GitHub's private reporting: **Security → Report a vulnerability** on this repository. Please
do not open a public issue for security problems, and never paste app secrets, session keys,
tenant keys or open_ids into an issue.

Fixes land on `main` and in the latest release. Modified copies are not supported.

## Trust boundary

- Everyone on the allow-list has full dashboard authority. Tenant and open_id checks are an
  admission gate, not per-profile access control.
- The plugin is trusted Python running inside the dashboard process. It has no sandbox and adds no
  rate limiter, CSRF middleware or proxy. Hermes core (cookies, routes, WebSocket tickets) and your
  TLS / reverse-proxy setup are part of the boundary; review them too.
- Upstream PKCE is not used. Single-use, cookie-bound state does not replace it, so the app secret
  must stay confidential. Protect it, the session key and the local session database.

## Operational guidance

- **Sign everyone out**: rotate `HERMES_DASHBOARD_FEISHU_SESSION_KEY` and restart the dashboard.
  This also closes WebSockets, which logout alone may leave open.
- **Remove a person**: take their open_id off the allow-list and restart the dashboard.
- **Delete stored identity data**: remove
  `<HERMES_HOME>/plugin-data/dashboard-auth-feishu/session.sqlite3`. That signs everyone out and
  touches nothing else; do not delete Hermes `state.db` or chat history.
- Run one dashboard worker: pending OAuth state is held in memory.
- A previous token pair is accepted for 60 seconds after a refresh, by design, so parallel tabs do
  not sign each other out.

## Test coverage

Feishu sign-in has been exercised end to end in a real deployment. Lark endpoints and Hermes
Desktop's native sign-in have not. Unit tests simulate Feishu responses and are not evidence of
either.
