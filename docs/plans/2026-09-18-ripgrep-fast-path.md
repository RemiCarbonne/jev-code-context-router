# Ripgrep Lexical Fast Path Implementation Plan

> **For Hermes:** Implement this plan task-by-task with tests before production code.

**Goal:** Add a bounded, secret-safe ripgrep fast path that skips full-repository indexing and Jev only when explicit paths or multiple distinctive identifiers identify a repository file with high confidence.

**Architecture:** A new `lexical.py` module extracts literal code/path terms, runs `rg` without a shell, validates matches through `PathPolicy`, and returns a confidence-scored candidate set. `ContextRouter` uses a high-confidence result to index only matched files and select locally; weak, broad, unavailable, or timed-out lexical searches fall back unchanged to full structural indexing and Jev.

**Tech Stack:** Python standard library, ripgrep executable when available, existing AST/structural indexers, pytest.

---

### Task 1: Specify confidence and safety behavior

**Objective:** Lock the fast-path acceptance criteria in failing tests.

**Files:**
- Create: `tests/test_lexical.py`
- Modify: `tests/test_router.py`

**Steps:**
1. Test extraction of explicit paths and discriminating identifiers.
2. Test that one generic/shared identifier is not high confidence.
3. Test that an explicit allowed source path is high confidence.
4. Test that shell metacharacters remain literal and execute nothing.
5. Test unavailable and timed-out `rg` return a fallback result rather than a routing failure.
6. Run `uv run pytest -q tests/test_lexical.py tests/test_router.py`; expect failures because the lexical module does not exist.

### Task 2: Implement the bounded lexical retriever

**Objective:** Produce safe candidate paths and a deterministic confidence decision.

**Files:**
- Create: `src/jev_context_router/lexical.py`
- Modify: `src/jev_context_router/config.py`

**Steps:**
1. Add settings for enabling the fast path, timeout, candidate cap, and confidence thresholds.
2. Extract explicit source paths plus camelCase, PascalCase, uppercase, snake_case, and long domain terms; remove multilingual request stopwords.
3. Invoke `rg` as an argv list with `-F`, no shell, source extension globs, excluded directory globs, and a subprocess timeout.
4. Revalidate every returned path through `PathPolicy` and the repository boundary.
5. Score each file by distinct matched terms.
6. Mark high confidence only for an existing explicit path or at least two terms concentrated in a leading file with a margin.
7. Run targeted tests and expect pass.

### Task 3: Add partial structural indexing

**Objective:** Reuse existing parsers without walking the whole repository.

**Files:**
- Modify: `src/jev_context_router/index.py`
- Test: `tests/test_index_security.py`

**Steps:**
1. Add an optional `include_paths` argument to `index_repository`.
2. When supplied, iterate only normalized in-repository paths and keep all existing size, symlink, secret-name, file-count, symbol-count, and deadline checks.
3. Keep full `os.walk` behavior unchanged when no paths are supplied.
4. Test that matched source is indexed while secret files, symlink escapes, and unrelated files are not read.
5. Run index security tests and expect pass.

### Task 4: Integrate the fast path into routing

**Objective:** Skip full indexing and external selection only under high confidence.

**Files:**
- Modify: `src/jev_context_router/router.py`
- Modify: `tests/test_router.py`
- Modify: `tests/test_timeout_debug.py`

**Steps:**
1. Run lexical retrieval after repository resolution and before indexing.
2. Emit a `lexical-search` progress stage.
3. On high confidence, partially index candidate files, use the existing shortlist and expansion, select locally, and do not invoke Jev.
4. On low confidence, no match, timeout, missing executable, excess matches, or parse failure, execute the current full-index/Jev path unchanged.
5. Expose `retrieval_mode`, `lexical_seconds`, term count, matched files, confidence, `jev_skipped`, and fallback reason without logging term contents.
6. Test that exact identifiers skip a selector that would fail if called.
7. Test that semantic and broad queries still call the selector.
8. Run targeted routing tests and expect pass.

### Task 5: Exclude generated output and document behavior

**Objective:** Avoid known generated trees and explain when acceleration applies.

**Files:**
- Modify: `src/jev_context_router/security.py`
- Modify: `jev-context.example.toml`
- Modify: `README.md`
- Modify: `docs/architecture.md`
- Modify: `docs/troubleshooting.md`
- Modify: `CHANGELOG.md`

**Steps:**
1. Add `out` to default generated-directory exclusions.
2. Document the confidence gate, fallback semantics, optional ripgrep dependency, and metrics.
3. Document that external Jev is skipped only for high-confidence lexical requests.
4. Add a changelog entry and bump the patch version.

### Task 6: Verify quality and release

**Objective:** Prove the optimization is faster without changing ambiguous-query behavior.

**Files:**
- No new production files.

**Steps:**
1. Run the complete test suite.
2. Build wheel and source distribution.
3. Benchmark full indexing against lexical partial indexing on a representative repository.
4. Verify exact-target recall and fallback cases.
5. Run the public-data/secret audit and `git diff --check`.
6. Commit, tag, push, and compare local and remote SHAs.
