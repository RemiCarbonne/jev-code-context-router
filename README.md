# Jev Context Router

Repository-aware, read-only source-context routing for coding agents. The core library discovers repositories dynamically, resolves the intended project without guessing from unrelated assistant history, indexes multiple programming languages, asks TypeSafe Jev to select useful symbols, expands dependencies locally, and injects a bounded context into an agent turn.

> Alpha software. The public API and adapters may change before 1.0.

## Supported integrations

- Hermes Agent: `pre_llm_call` plugin
- Claude Code: `UserPromptSubmit` hook
- OpenAI Codex: direct `UserPromptSubmit` hook or stdio MCP server
- Any agent or script: Python API and `jev-context` CLI

## Design principles

- no hard-coded project names or private paths;
- one repository boundary per route;
- current prompt and working directory take precedence;
- previous user messages are consulted only for pure continuations such as `continue`;
- assistant history never selects a repository;
- secret-like files, symlink escapes, caches, dependencies and generated files are excluded;
- TypeSafe receives only a bounded shortlist of redacted source candidates;
- local-only fallback works without an API key.

## Quick start

```bash
uv tool install jev-context-router
export TYPESAFE_API_KEY=...
cd ~/code/my-project
jev-context route "Fix the failing payment idempotency test"
```

For bounded execution with live diagnostics:

```bash
jev-context route --cwd . --format json --timeout 30 --debug \
  "Refactor src/Root.tsx and identify the exact files and symbols required"
```

`stdout` remains valid JSON. Progress events are emitted as JSON Lines on `stderr` and include the current stage, timings, indexed file/byte counts, shortlisted and selected files, context bytes, token estimates, external provider/model, and cache status. A timeout returns a structured `status: "timeout"` result instead of hanging.

From a source checkout:

```bash
uv sync --extra dev
uv run pytest
uv run jev-context route "Fix the router tests" --cwd .
```

## Configuration

Copy `jev-context.example.toml` to either:

- `./jev-context.toml`; or
- `~/.config/jev-context-router/config.toml`.

```toml
workspace_roots = ["~/code"]
model = "jev-latest"
max_context_chars = 12000
route_timeout_seconds = 30
discovery_timeout_seconds = 5
index_timeout_seconds = 15
external_timeout_seconds = 2
max_source_files = 25000
lexical_enabled = true
lexical_timeout_seconds = 0.5
lexical_max_files = 40
index_cache_enabled = true
selector_max_candidates = 8
selector_max_input_tokens = 2000
metrics_path = "~/.local/state/jev-context-router/metrics.jsonl"

[repository_aliases]
web = "~/code/acme-web"
```

`workspace_roots` are security boundaries. Configured aliases cannot grant access outside them.

Persistent metrics are opt-in. Configure `metrics_path` above or export `JEV_CONTEXT_METRICS`. When disabled, every result reports `metrics.metrics_persistence.status = "disabled"`. External selector failures preserve only secret-safe diagnostics: `selector_error`, `selector_error_status_code`, `selector_error_message`, and normalized `external_error_reason`. For example, an expired credential is reported as `TypeSafe request failed: HTTPError status=401`; authorization headers and API keys are never logged.

When ripgrep is available, explicit source paths or several distinctive identifiers concentrated in one file activate a bounded lexical fast path. Only matched source files are structurally parsed and the external Jev call is skipped. Broad, semantic, ambiguous, timed-out, unavailable, or overly large searches fall back to the complete structural/Jev pipeline. Metrics expose `retrieval_mode`, lexical timings and counts, confidence, fallback reason, and `jev_skipped`; literal query terms are never logged.

Full structural indexes are persisted outside the repository. Cold and incremental parses store a SHA-256 per file; warm validation uses `mtime_ns`, `ctime_ns`, and size without reopening unchanged source bodies. Warm routes reuse unchanged symbols; incremental routes parse only changed files. The external selector receives at most eight compact candidates with a strict estimated 2,000-token payload budget. Results expose both backward-compatible flat metrics and structured `intent`, `index`, `lexical`, `ranking`, `external_selector`, `context`, `fallback`, `cache`, and `total` sections.

## Hermes Agent

```bash
uv tool install jev-context-router
jev-context install hermes --home ~/.hermes
hermes plugins enable jev-context-router
```

The adapter uses the documented `pre_llm_call` hook and injects context into the current user-message copy. It does not alter the cached system prompt or expose tools.

## Claude Code

```bash
uv tool install jev-context-router
jev-context install claude --project .
```

This adds an idempotent `UserPromptSubmit` command hook to `.claude/settings.local.json`. The hook emits `hookSpecificOutput.additionalContext` as required by Claude Code.

## Codex CLI hook (recommended)

```bash
uv tool install jev-context-router
jev-context install codex --mode cli --apply
```

This installs a Codex `UserPromptSubmit` command hook in `${CODEX_HOME:-~/.codex}/hooks.json`. Every coding prompt is routed through `jev-context codex-hook` before the first model request, without starting MCP or relying on the model to call a tool. Restart Codex, open `/hooks`, and trust the new hook definition when prompted.

## Codex / MCP alternative

```bash
uv tool install 'jev-context-router[mcp]'
jev-context install codex --mode mcp --apply
```

MCP remains useful for clients without command hooks or when the model must call the router explicitly. See `adapters/codex/README.md`.

The MCP tool returns the complete structured routing result and streams standard MCP progress notifications. It accepts optional `timeout_seconds` and `debug` arguments. With `debug: true`, the returned metrics also include the ordered progress events.

## Language support

Python uses the standard-library AST and gets exact classes, functions, methods, references and calls. A dependency-free structural indexer supports JavaScript, TypeScript, Go, Rust, Java, Kotlin, PHP, Ruby, C#, C/C++, Swift and Scala. Generic-language extraction is intentionally conservative; Tree-sitter and LSP enrichments are planned as optional adapters.

## Data flow and privacy

1. Repository discovery and lexical ranking run locally.
2. If `TYPESAFE_API_KEY` is configured, Jev receives:
   - the current request;
   - repository metadata when the repository is ambiguous;
   - a bounded, redacted shortlist of source symbols.
3. Full selected symbols and dependency expansion stay local and are injected into the downstream agent.

Run without `TYPESAFE_API_KEY` for local-only lexical routing. Never put secrets in source files; filename filters and redaction reduce risk but are not a substitute for secret management.

## Status codes

- `routed`: context was selected;
- `not-code`: no coding action was detected;
- `repository-unresolved`: more than one project remains plausible;
- `no-supported-source`: repository found, no supported source files;
- `no-candidates`: source indexed, no relevant symbol found.
- `timeout`: a named stage exceeded its explicit wall-clock bound;
- `limit-exceeded`: repository file or symbol safety budget exceeded;
- `error`: an unexpected failure occurred, with stage, exception type, and traceback in metrics.

## Development

```bash
uv sync --extra dev
uv run pytest
uv build
```

## License

MIT
