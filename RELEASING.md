# Releasing

1. Bump `version` in `plugin.yaml` and add a `CHANGELOG.md` entry.
2. Merge to `main` with CI green on every pinned Hermes revision. When a new Hermes release ships,
   add or bump its row in `.github/workflows/ci.yml` after reviewing it.
3. Tag the merge commit `vX.Y.Z` and publish a GitHub release with the changelog entry.
4. The official plugin catalog pins a commit, not a tag, so a release does not reach
   `hermes plugins install dashboard-auth-feishu` by itself. Open a PR against
   NousResearch/hermes-agent that updates this plugin's catalog entry: `sha` (the tagged commit),
   `version`, and a one-line summary of the change.
5. Existing installs pinned to an older commit upgrade with
   `hermes plugins install <repo> --ref <sha> --force`, then a restart of the dashboard process.
