# Troubleshooting

## A coding request appears to hang

Use JSON output and progress diagnostics:

```bash
jev-context route \
  --cwd /path/to/repository \
  --format json \
  --timeout 30 \
  --debug \
  "Refactor src/Root.tsx and identify the required files and symbols"
```

- `stdout` is reserved for the final JSON result.
- `stderr` receives one JSON progress event per line.
- `metrics.stage_seconds` reports completed-stage durations.
- `metrics.timeout_stage` identifies a timed-out stage.
- Exit code `3` means timeout; other structured non-success results use exit code `2`.

Stages are `intent`, `discovery`, `external-repository-selection`, `lexical-search`, `indexing`, `ranking`, `external-selection`, `expansion`, and `complete`.

`lexical-search` runs between repository resolution and indexing when the ripgrep fast path is enabled. Inspect `metrics.retrieval_mode`, `lexical_status`, `lexical_confidence`, `lexical_fallback_reason`, and `jev_skipped`. Set `lexical_enabled = false` to compare or troubleshoot the complete structural path.

## Diagnose external selector fallback

The router keeps working with a local shortlist when TypeSafe fails. Inspect these result fields:

```text
metrics.selector_error
metrics.selector_error_status_code
metrics.selector_error_message
metrics.metrics_persistence.status
```

HTTP authentication failures appear as `TypeSafe request failed: HTTPError status=401` or `status=403`. Transport timeouts, network failures, and invalid JSON remain distinct (`TimeoutError`, `URLError`, and `JSONDecodeError`). Messages are deliberately sanitized and never include the bearer token or low-level exception details.

To persist one secret-safe JSON object per route:

```bash
export JEV_CONTEXT_METRICS="$HOME/.local/state/jev-context-router/metrics.jsonl"
```

Alternatively set `metrics_path` in `jev-context.toml`. Persistence remains opt-in; no file is created otherwise.

## 0.1.0 TypeScript/JavaScript slowdown

Version 0.1.0 enumerated `sorted(root.rglob("*"))` before applying `PathPolicy`. Although files under `node_modules` were eventually rejected, the filesystem had already traversed and sorted every dependency file. On WSL repositories stored below `/mnt/*`, this could take minutes and produced no output because CLI diagnostics did not yet exist.

Version 0.1.1 uses `os.walk(topdown=True)` and removes dependencies, build output, caches, VCS metadata, and symlinked directories before traversal. Discovery, indexing, external selection, and the complete route now have independent wall-clock bounds.

## Validate an installation

```bash
jev-context --help
jev-context route --help

jev-context route \
  --cwd /path/to/typescript-repository \
  --format json \
  --timeout 30 \
  --debug \
  "Refactor src/Root.tsx without editing files"
```

Expected properties:

```text
status = routed, timeout, limit-exceeded, or error
metrics.seconds is present
metrics.files_indexed is present after successful indexing
metrics.context_bytes is present after successful rendering
metrics.cache.status is explicit
```

For MCP, restart the MCP server after upgrading, then call:

```json
{
  "query": "Refactor src/Root.tsx without editing files",
  "cwd": "/path/to/typescript-repository",
  "timeout_seconds": 30,
  "debug": true
}
```

The result is a structured object, not an unlabelled context string. Clients that opt into MCP progress receive progress notifications while the tool is running.
