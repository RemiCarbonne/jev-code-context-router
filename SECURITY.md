# Security and privacy

## Trust boundary

The router is read-only. It selects context; it does not edit source files, run project commands, or execute indexed code.

A resolved repository root is the filesystem boundary. Paths are canonicalized before reading. Files whose resolved path escapes that root are rejected. Repository descriptions reached through symlinks are not read; source symlinks are accepted only when their target remains inside the selected root.

## Exclusions

The default policy rejects:

- VCS metadata, virtual environments, vendored dependencies, caches, coverage, build and generated output;
- `.env*`, private keys, certificates, credential/token/secret files, auth stores, and common cloud credential directories;
- files larger than the configured byte limit;
- non-source extensions.

Candidate snippets are additionally redacted for obvious inline assignments such as `password = ...`, `token: ...`, and private-key blocks before an external selector sees them.

No heuristic can prove that arbitrary source code contains no sensitive business data. Review this boundary before using a remote selector on confidential repositories.

## What leaves the machine

With `TYPESAFE_API_KEY` configured, Jev receives:

- the current coding request;
- repository metadata while resolving an ambiguous project;
- bounded, redacted shortlisted source snippets while selecting symbols.

It does **not** receive the entire workspace or every repository. The agent runtime receives the final selected context.

Without `TYPESAFE_API_KEY`, routing remains local and no selector request is made.

## Recommended deployment

- Configure the smallest practical `workspace_roots`.
- Prefer one working directory per agent session.
- Keep credentials outside source repositories.
- Do not enable networked MCP transport unless the server is protected by authentication and TLS.
- Use stdio MCP locally.
- Inspect `router-metrics.jsonl` without logging prompt or source bodies.

## Reporting vulnerabilities

Do not open a public issue for a suspected data-exposure vulnerability. Use the repository owner's private security-reporting channel after the public repository is created.
