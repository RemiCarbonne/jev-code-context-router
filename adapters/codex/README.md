# Codex CLI

## Direct CLI hook (recommended)

```bash
uv tool install jev-context-router
jev-context install codex --mode cli --apply
```

This merges an idempotent `UserPromptSubmit` command hook into `${CODEX_HOME:-~/.codex}/hooks.json`. Codex sends the current `cwd` and `prompt` as JSON on stdin; `jev-context codex-hook` returns bounded source in `hookSpecificOutput.additionalContext`.

Restart Codex after installation. Run `/hooks`, inspect the command, and trust it. If hooks are disabled in your Codex build, enable `features.hooks` in `~/.codex/config.toml` or update Codex.

The direct hook has no MCP subprocess initialization and does not depend on the model deciding to call a tool. The route itself still performs local indexing and, when configured, one bounded Jev request.

## MCP alternative

```bash
uv tool install 'jev-context-router[mcp]'
jev-context install codex --mode mcp --apply
```

If you use MCP, add this project instruction to `AGENTS.md`:

```markdown
For repository coding tasks, call `route_code_context` before broad filesystem search. Use the returned source first. If it reports `repository-unresolved`, ask which repository is intended instead of guessing from unrelated history.
```

The MCP tool is read-only. It receives a query and working directory and returns selected source context.
