# Architecture

## Design goals

- Route every actionable coding request without maintaining a project allowlist.
- Select exactly one bounded repository before reading source bodies.
- Keep the core independent of any agent runtime.
- Degrade safely to deterministic local ranking if Jev is unavailable.
- Never treat unrelated assistant output as evidence of the active project.

## Pipeline

```text
prompt
  -> code-intent gate
  -> repository discovery
  -> repository resolution
  -> bounded ripgrep lexical probe
     -> high confidence: partial safe source index, local selection
     -> otherwise: persistent incremental safe source index
  -> compact local shortlist
  -> bounded Jev fit selection when local confidence is insufficient
  -> scored dependency expansion with inclusion reasons
  -> bounded context renderer
  -> runtime adapter
```

## Repository resolution precedence

1. Repository alias explicitly present in the current prompt.
2. Project root containing the runtime's current working directory.
3. A repository mentioned in recent **user** messages, but only when the current prompt is a strict continuation such as `continue` or `apply`.
4. Jev selection from repository names, descriptions, and language metadata.
5. If the choice remains ambiguous, return `repository-unresolved`; do not scan every repository and do not guess.

Repository selection sends metadata only. Source bodies are not read until one repository has been selected.

## Lexical fast path

After resolving one repository, the router extracts explicit source paths and distinctive literal identifiers. It invokes ripgrep with fixed-string patterns as an argument vector, never through a shell. Returned paths are revalidated by `PathPolicy` before any source body is read.

An existing explicit source path is high confidence. Otherwise, at least two distinct terms must be concentrated in the leading file with a configured margin over the next candidate. High-confidence matches are partially indexed and selected locally, avoiding both the full repository walk and the external Jev request. A single shared identifier, semantic prose, excess matches, timeout, missing ripgrep, or any execution error preserves the complete existing pipeline.

The fast path is an optimization, not a new trust boundary. File size limits, secret-name exclusions, symlink containment, symbol budgets, context budgets, and read-only behavior remain unchanged. Query terms are not persisted in metrics.

## Indexing

Python uses the standard-library AST and extracts classes, functions, async functions, methods, imports, calls, and references. JavaScript, TypeScript, Go, Rust, Java, Kotlin, PHP, Ruby, C#, C/C++, Swift, and Scala use a dependency-free structural parser with brace-aware declaration extraction. Unknown but allowed source files are represented by bounded module chunks.

The generic parser is deliberately conservative. A future Tree-sitter extra can improve language-specific accuracy without changing the core interfaces.

Complete indexes are cached outside the source repository. Each source file is keyed by relative path; cold and incremental parses store its SHA-256, while warm validation uses nanosecond mtime, ctime, and size without reopening unchanged source bodies. A warm route reconstructs symbols without reading source bodies; an incremental route reparses only changed files and drops deleted entries. Cache writes are atomic and private to the current user. Partial high-confidence lexical routes remain uncached because they are already bounded to a few files.

## Selection

The local scorer combines prompt/name overlap, path overlap, source overlap, and test affinity. It produces a small shortlist. A high-confidence ripgrep route selects the leading local candidates directly. Otherwise, when configured, Jev receives at most eight compact records containing path, symbol, kind, language, and a bounded signature. The serialized request is reduced until it fits the configured estimated-token budget. One fit question is used per candidate. If the remote selector fails or accepts nothing, the deterministic local top candidates remain available.

Expansion then adds:

- parent classes for selected methods;
- direct callees and referenced symbols;
- callers with positive query relevance;
- one positively-scored neighboring symbol from the same file;
- matching tests.

All expansion is capped by symbol and character budgets. Candidate files and rendered files are separate metrics; every included file has an inclusion reason, while shortlisted files rejected from the rendered context appear under `excluded_low_score_files`.

## Runtime boundary

The core library knows nothing about Hermes, Claude Code, Codex, SQLite session state, or a particular filesystem layout. Adapters translate runtime state into three inputs:

- current prompt;
- current working directory;
- recent user messages.

The output is a portable `<code_context>` block or a structured `RouteResult`.
