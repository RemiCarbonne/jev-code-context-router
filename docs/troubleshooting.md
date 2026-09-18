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

Stages are `intent`, `discovery`, `external-repository-selection`, `indexing`, `ranking`, `external-selection`, `expansion`, and `complete`.

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
