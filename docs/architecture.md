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
  -> safe source index
  -> lexical shortlist
  -> Jev fit/current selection (optional)
  -> deterministic dependency expansion
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

## Indexing

Python uses the standard-library AST and extracts classes, functions, async functions, methods, imports, calls, and references. JavaScript, TypeScript, Go, Rust, Java, Kotlin, PHP, Ruby, C#, C/C++, Swift, and Scala use a dependency-free structural parser with brace-aware declaration extraction. Unknown but allowed source files are represented by bounded module chunks.

The generic parser is deliberately conservative. A future Tree-sitter extra can improve language-specific accuracy without changing the core interfaces.

## Selection

The local scorer combines prompt/name overlap, path overlap, source overlap, and test affinity. It produces a small shortlist. When configured, Jev evaluates only that shortlist using independent `fit` and `current` questions. If the remote selector fails or accepts nothing, the deterministic local top candidates remain available.

Expansion then adds:

- parent classes for selected methods;
- direct callees and referenced symbols;
- callers of selected symbols;
- neighboring symbols from the same file;
- matching tests.

All expansion is capped by symbol and character budgets.

## Runtime boundary

The core library knows nothing about Hermes, Claude Code, Codex, SQLite session state, or a particular filesystem layout. Adapters translate runtime state into three inputs:

- current prompt;
- current working directory;
- recent user messages.

The output is a portable `<code_context>` block or a structured `RouteResult`.
