# Codex CLI / MCP

Install the optional MCP dependency and register the stdio server:

```bash
uv tool install 'jev-context-router[mcp]'
codex mcp add jev-context --env TYPESAFE_API_KEY="$TYPESAFE_API_KEY" -- jev-context mcp-serve
```

Add this project instruction to `AGENTS.md` if you want Codex to call the router before broad exploration:

```markdown
For repository coding tasks, call `route_code_context` before broad filesystem search. Use the returned source first. If it reports `repository-unresolved`, ask which repository is intended instead of guessing from unrelated history.
```

The MCP tool is read-only. It receives a query and working directory and returns selected source context.
