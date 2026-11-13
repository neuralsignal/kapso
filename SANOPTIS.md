# Sanoptis fork of Leeroo-AI/kapso

Branch `sanoptis`, cut from upstream `35f7b9b2` (v0.9.3). Brain pins this branch by commit
(`apps/kapso-loop/pixi.toml`).

## Why the fork exists

Upstream has no provider choice for Codex sessions. This fork adds one patch: OpenRouter as the
fallback when the Codex subscription hits its quota or rate limit, forced to Zero-Data-Retention
endpoints with no provider fallback (`provider: {zdr: true, allow_fallbacks: false}`).

| File | Change |
|---|---|
| `src/kapso/core/openrouter_fallback.py` | trigger (Codex's quota and rate-limit messages), loopback relay that sets the routing on every request body, Codex provider overrides |
| `src/kapso/execution/coding_agents/adapters/codex_agent.py` | a failed session reruns once on OpenRouter when the trigger matches |
| `src/kapso/learning/update_frame.py` | the update crew's `codex exec` workers do the same inside `run-role.sh` |
| `src/kapso/config.yaml`, `agents.yaml` | `hosts.codex.openrouter_fallback`, `enabled: false` by default |
| `tests/test_openrouter_fallback.py` | trigger, ZDR body, relay, adapter and launcher fallback |

Codex reaches a custom provider only through base URL, headers and query parameters; OpenRouter's
routing is a body field. Hence the relay. It holds no credential: Codex sends the key named by
`api_key_env`. Any other failure stays a failure. The Claude Code route has no fallback: its
base-URL route cannot carry the routing object.

## Rules

- Sync upstream by **merge** (`git fetch upstream && git merge upstream/main`), never rebase: Brain
  pins commits on this branch, and a rebase orphans them.
- Retire the fork when upstream offers provider choice with request-body routing; move Brain's pin
  back to `Leeroo-AI/kapso`.
- No upstream PR unless the owner asks for one.
