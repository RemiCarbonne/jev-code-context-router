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

## 0.3.0 routing benchmark

Run the public, read-only benchmark with:

```bash
uv run python benchmarks/run_benchmark.py --output /tmp/jev-context-benchmark.json
```

The harness creates a temporary JavaScript/TypeScript repository containing the documented stale-task/ICS defect, three named distractor scripts, and 30 unrelated modules. It validates a targeted route, a semantic transversal route, a French route, cold and warm indexing, and simulated available/unavailable external selection. It never modifies a user repository and requires no credential.

Observed on 2026-09-19:

- targeted Doctolib route: one file, 409 bytes read, zero selector tokens, about 0.003 seconds;
- transversal cold route: 38 files parsed, about 0.012 seconds total;
- transversal warm route: 38 files reused, zero source bytes read, about 0.009 seconds total;
- compact selector request: 680 simulated input tokens versus 8,475–10,549 reported with the previous verbose payload;
- final transversal context: 508 estimated tokens, exactly the four expected files;
- named distractor noise: 0%;
- French request: routed as code to the expected Doctolib script with no selector call;
- unavailable network: local fallback returned all four expected files in about 0.010 seconds.

The external provider in this public harness is simulated deterministically so CI does not need a secret and results do not depend on network service. The 674-token figure is the exact serialized compact benchmark payload estimate, not a claim about provider-side accounting. A real agent-token comparison still requires paired clean agent runs because filesystem bytes or rendered context tokens are not substitutes for `tokens_agent_sans_jev`.

## Reproduction policy

The repository intentionally does not publish hidden tests alongside an evaluated fixture. Public benchmark fixtures should place private acceptance tests outside every configured workspace root and compare their checksums before and after an agent run.
