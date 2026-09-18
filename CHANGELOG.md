# Changelog

All notable changes follow [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). The project uses semantic versioning.

## [Unreleased]

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
