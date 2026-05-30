# github-native-tools

Hermes plugin exposing **capability-named** GitHub repository operations
to agents — `github_repo_list`, `github_repo_view`, `github_repo_delete`,
`github_repo_create`.

## What it does

Registers four tools on the in-image `tools.registry` and extends
`toolsets._HERMES_CORE_TOOLS` so every composite role toolset
(`hermes-core`, `hermes-telegram`, `hermes-cli`, …) automatically
includes them.

The tools encapsulate auth: an installation token from the configured
GitHub App is preferred; if absent, a classic PAT from `GITHUB_TOKEN` or
`GH_TOKEN` is used. The agent never sees the auth choice — that's the
whole point.

## Why "capability-named" matters

Before these tools existed, on a plain "delete a github repo" prompt
the assistant would `grep .env`, find `GITHUB_APP_CLIENT_ID`,
openssl-sign a JWT, curl `/app/installations/<id>/access_tokens`, then
curl DELETE — ~50 lines of `execute_code` re-implementing exactly what
the App workspace tool already does internally. Native + capability-named
+ auth-encapsulated tools structurally prevent this anti-pattern.

E2E verified via tg-bot `p03` roundtrip (create → delete → verify):
bot opened with «Использую нативные инструменты», completed the cycle
in ~30 s without any `execute_code` or PAT prompt.

## Tools

| Tool | Endpoint | Notes |
|---|---|---|
| `github_repo_list` | `GET /orgs/{org}/repos` or `GET /installation/repositories` (App) → `GET /user/repos` (PAT fallback) | Filter by org; `limit` 1..100. |
| `github_repo_view` | `GET /repos/{owner}/{repo}` | Full-name format required. |
| `github_repo_delete` | `DELETE /repos/{owner}/{repo}` | **Irreversible.** |
| `github_repo_create` | `POST /orgs/{org}/repos` or `POST /user/repos` | Private by default. |

## Auth precedence

1. `tools.github_app_workspace._get_app_token()` — App installation token
   if the App is configured (operator-side via
   `GITHUB_APP_CLIENT_ID` + `GITHUB_APP_PRIVATE_KEY` env or equivalent).
2. `GITHUB_TOKEN` env (CI-style classic PAT).
3. `GH_TOKEN` env (gh-cli convention).

`check_fn` returns `True` iff at least one of the three resolves; the
toolset hides itself otherwise.

## Install

External plugin — pulled by `sync-external-plugins.sh` into
`sources/hermes-external-plugins/github-native-tools/`, bind-mounted at
`/opt/data/plugins/github-native-tools/` inside the container.

Enable in `~/.hermes/config.yaml`:

```yaml
plugins:
  enabled:
    - github-native-tools
```

## Where the tools end up

`register()` mutates `toolsets._HERMES_CORE_TOOLS` in place. Because
multiple `TOOLSETS["…"]` entries reference this list by object identity
(not by copy), every composite toolset that uses `_HERMES_CORE_TOOLS`
gets the four tools without any additional config. Operators who use
`enabled_toolsets: [github]` directly also work — the registration
specifies `toolset="github"` on each handler.

## License

MIT — see LICENSE.
