# Contributing

Contributions are welcome for indexers, runtime adapters, security hardening, and reproducible benchmarks.

## Development

```bash
git clone https://github.com/RemiCarbonne/jev-code-context-router.git
cd jev-code-context-router
uv sync --all-extras
uv run pytest
uv build
```

## Rules

- Keep the core independent of agent runtimes and local directory layouts.
- Add regression tests for repository resolution and every filesystem-boundary change.
- Never commit credentials, private corpora, proprietary benchmark fixtures, or hidden tests.
- New remote providers must document exactly what data leaves the machine.
- New language support must degrade safely when parsing fails.
- Run the full test suite and build before opening a pull request.

## Pull requests

Describe the behavior change, security implications, tests executed, and any external data transmission. Keep changes focused. API-breaking changes require a changelog entry.
