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

`lexical-search` runs between repository resolution and indexing when the ripgrep fast path is enabled. Inspect `metrics.retrieval_mode`, `lexical_status`, `lexical_confidence`, `lexical_fallback`, `lexical_fallback_reason`, `lexical_duration_ms`, and `jev_skipped`. A lexical timeout is non-fatal and immediately falls back to the structural index. Set `lexical_enabled = false` to compare or troubleshoot that path directly.

## Diagnose external selector fallback

The router keeps working with a local shortlist when TypeSafe fails. Inspect these result fields:

```text
metrics.selector_error
metrics.selector_error_status_code
metrics.selector_error_message
metrics.external_error_reason
metrics.external_status
metrics.fallback_used
metrics.metrics_persistence.status
```

HTTP authentication failures appear as `TypeSafe request failed: HTTPError status=401` or `status=403`. Transport timeouts, network failures, and invalid JSON remain distinct (`TimeoutError`, `URLError`, and `JSONDecodeError`). `external_error_reason` normalizes safe categories such as `dns`, `tls`, `timeout`, `connection-refused`, `http`, and `network`. The default network timeout is 1.5 seconds, selection is bounded to 2 seconds, and a short in-process circuit breaker avoids repeated calls after a failure. Messages never include the bearer token or raw low-level details.

## Inspect cold, warm, and incremental indexes

`metrics.cache.status` is `cold`, `warm`, `incremental`, or `disabled`. Related fields report `files_reused`, `files_reparsed`, `files_removed`, and whether the cache was written. Override the private cache location with `index_cache_dir` or `JEV_CONTEXT_CACHE_DIR`, or set `index_cache_enabled = false` for a controlled comparison.

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
