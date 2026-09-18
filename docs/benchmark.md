# Acceptance benchmark

The 0.1.0 implementation was exercised against a synthetic multi-file inventory-reservation defect with public tests and separate hidden acceptance tests. Hidden tests and the expected implementation were never indexed or included in the prompt.

## Observed run

- Repository: 24 Python files, 61 indexed symbols.
- Local shortlist: 20 candidates.
- Jev selection: 4 symbols.
- Expanded context: 19 symbols, 11,270 characters.
- Jev usage: 4,553 input tokens and 761 output tokens.
- Jev routing time: 0.87 seconds.
- Astra: one model call, zero agent tools, 6,540 input tokens and 705 output tokens.
- Astra wall time: 31.35 seconds.
- Result: patch applied cleanly; public tests passed; five hidden acceptance tests passed; compilation passed.
- Files changed by the model: two implementation files.

This is an acceptance run, not a statistical performance claim. Broader language-specific and multi-repository benchmark suites remain roadmap items.

## Reproduction policy

The repository intentionally does not publish hidden tests alongside an evaluated fixture. Public benchmark fixtures should place private acceptance tests outside every configured workspace root and compare their checksums before and after an agent run.
