# Changelog

All notable changes follow [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). The project uses semantic versioning.

## [Unreleased]

## [0.3.0] - 2026-09-19

### Added

- Persist full repository indexes outside source trees and incrementally reparse files by nanosecond mtime and size.
- Expose cold, warm, and incremental cache metrics, multilingual intent diagnostics, inclusion reasons, rejected low-score files, normalized network failure categories, and structured per-stage telemetry.
- Add a reproducible synthetic benchmark for targeted, semantic, transversal, French, online-simulated, offline-simulated, cold, and warm routes.

### Changed

- Compact external selection to at most eight local candidates, 120 characters of signature each, one fit question per candidate, and a strict estimated 2,000-token request budget.
- Bound provider I/O to 1.5 seconds and external selection to 2 seconds by default, with a short circuit breaker after failures.
- Expand only positively relevant generic callers and neighbors, and keep candidate files separate from rendered files.
- Route French debug, trace, implementation, file, function, pipeline, export, validation, and transaction requests as code with explicit confidence and matched signals.

### Fixed

- Treat lexical timeout or execution failure as a non-fatal structural-index fallback.
- Restrict a high-confidence lexical route to the top-scoring cohort instead of parsing every file that matched any term.

## [0.2.0] - 2026-09-18

### Added

- Add a bounded ripgrep fixed-string fast path for explicit source paths and concentrated distinctive identifiers.
- Add partial structural indexing for policy-validated candidate paths.
- Skip external Jev selection only for high-confidence lexical routes and expose fallback telemetry.

### Changed

- Prune generated `out` directories by default alongside `dist` and `build`.

## [0.1.4] - 2026-09-18

### Fixed

- Preserve the original external selector failure type through bounded stage wrappers.
- Report secret-safe HTTP status codes for TypeSafe 401/403 failures.
- Distinguish HTTP, timeout, network, and invalid JSON failures in metrics and progress events.
- Expose whether persistent JSONL metrics are configured.

## [0.1.3] - 2026-09-18

### Added

- Add a direct Codex CLI `UserPromptSubmit` hook that injects routed context without MCP startup.
- Add an idempotent `jev-context install codex --mode cli --apply` installer for Codex `hooks.json`.

## [0.1.2] - 2026-09-18

### Fixed

- Prevent MCP debug telemetry from creating a circular reference during result serialization.
- Exercise `debug=true`, progress reporting, and bounded execution in the MCP regression test.

## [0.1.1] - 2026-09-18

### Fixed

- Prune `node_modules`, build output, caches, and vendored trees before filesystem traversal instead of rejecting their files after an exhaustive `Path.rglob()`.
- Bound discovery, indexing, total routing, and external selection with structured timeout results.
- Return structured MCP results and stream MCP progress notifications.

### Added

- CLI `--debug`, `--verbose`, and `--timeout` options.
- Per-stage timings, file/byte counts, selected file lists, context bytes, token estimates, and explicit cache status.
- TypeScript integration, hard-timeout, error-traceback, and MCP progress regression tests.

## [0.1.0] - 2026-09-18

### Added

- Runtime-neutral repository discovery and resolution.
- Python AST and generic multilangage structural indexers.
- Local lexical ranking with optional Jev fit/current selection.
- Bounded dependency, caller, neighbor, and test expansion.
- Secret-path exclusions, symlink containment, and candidate redaction.
- CLI, Hermes `pre_llm_call`, Claude Code `UserPromptSubmit`, and MCP adapters.
- Installers for Hermes and Claude Code.
- Unit, integration, MCP, and security regression tests.
