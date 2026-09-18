# Runtime adapters

## Hermes Agent

```bash
uv tool install 'jev-context-router[mcp]'
jev-context install hermes
hermes plugins enable jev-context-router
```

The installer creates a self-contained plugin below `$HERMES_HOME/plugins/jev_context_router` (default: `~/.hermes`). The `pre_llm_call` hook injects context only for actionable coding requests. It reads the session working directory from Hermes state when available.

Set `TYPESAFE_API_KEY` in the Hermes process environment or place it in `$HERMES_HOME/.secrets/typesafe_api_key` with restricted permissions.

## Claude Code prompt hook

```bash
uv tool install jev-context-router
cd /path/to/project
jev-context install claude
```

This merges a `UserPromptSubmit` command hook into `.claude/settings.local.json`. Claude Code passes the current prompt and working directory on stdin. The hook returns `hookSpecificOutput.additionalContext` only when routing succeeds.

This follows Claude Code's documented JSON hook contract. Existing unrelated hook settings are preserved.

## Codex CLI through MCP

```bash
uv tool install 'jev-context-router[mcp]'
codex mcp add jev-context -- jev-context mcp-serve
```

Add the optional `adapters/codex/AGENTS.snippet.md` content to the repository's `AGENTS.md`. It asks Codex to call the routing tool before broad source exploration.

The stdio server exposes:

- `route_code_context(query, cwd)`;
- `discover_code_repositories(cwd)`.

## Any MCP client

Register this command as a local stdio server:

```bash
jev-context mcp-serve
```

The MCP dependency is optional. The core CLI, Hermes adapter, and Claude hook have no third-party runtime dependencies.

## Direct CLI

```bash
jev-context discover --cwd .
jev-context route "Fix tenant scoping in refund lookup" --cwd .
jev-context route "Implement the TypeScript cache invalidation" --cwd . --format json
```

Exit status remains zero for safe non-routing outcomes such as `not-code` or `repository-unresolved`; inspect the JSON `status` when automating.
