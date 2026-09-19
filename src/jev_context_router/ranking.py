from __future__ import annotations

from .index import CodeIndex
from .coverage import build_query_plan, is_excluded
from .models import Symbol
from .text import tokens


def score_symbol(query: str, symbol: Symbol) -> float:
    query_tokens = tokens(query)
    name_tokens = tokens(symbol.qualname)
    path_tokens = tokens(symbol.path)
    source_tokens = tokens(symbol.source)
    is_test = symbol.path.startswith(("tests/", "test/", "spec/")) or any(part in symbol.path for part in ("/tests/", "/test/", "/spec/"))
    score = (4.0 if is_test else 9.0) * len(query_tokens & name_tokens)
    score += 3.0 * len(query_tokens & path_tokens)
    score += 0.8 * len(query_tokens & source_tokens)
    if symbol.name.lower() in query.lower():
        score += 12.0
    if is_test and {"test", "tests", "bug", "fix", "correctif"} & query_tokens:
        score += 2.0
    return score


def shortlist(query: str, index: CodeIndex, limit: int) -> list[Symbol]:
    ranked = [(score_symbol(query, symbol), symbol) for symbol in index.candidates]
    ranked = [row for row in ranked if row[0] > 0]
    ranked.sort(key=lambda row: (-row[0], row[1].path, row[1].start_line, row[1].id))
    return [symbol for _, symbol in ranked[:limit]]


def expand_context_detailed(
    index: CodeIndex, selected: list[Symbol], query: str, limit: int, *, preserve_units: bool = False
) -> tuple[list[Symbol], dict[str, str]]:
    result: list[Symbol] = []
    seen: set[str] = set()
    reasons: dict[str, str] = {}
    plan = build_query_plan(query)

    def add(symbol: Symbol, reason: str) -> None:
        nonlocal result
        if is_excluded(plan, symbol, index.regions):
            return
        # Admissibility is decided by PathPolicy during indexing.  Ranking is
        # repository-agnostic and must not encode fixture directories/names.
        if symbol.kind == "method" and not preserve_units:
            parent_name = symbol.qualname.rsplit(".", 1)[0]
            parents = [candidate for candidate in index.candidates if candidate.path == symbol.path and candidate.qualname == parent_name and candidate.kind == "class"]
            if parents and not is_excluded(plan, parents[0], index.regions):
                symbol = parents[0]
        if symbol.kind == "class":
            children = {item.id for item in result if item.path == symbol.path and item.qualname.startswith(symbol.qualname + ".")}
            if children:
                result = [item for item in result if item.id not in children]
                seen.difference_update(children)
        if symbol.id not in seen and len(result) < limit:
            result.append(symbol)
            seen.add(symbol.id)
            reasons[symbol.id] = reason

    for symbol in selected:
        add(symbol, "selected")

    for _ in range(2):
        before = len(result)
        # Imports are structural dependencies. Adapters expose the exact import
        # preamble, which is resolved back to canonical same-file regions rather
        # than copied as opaque text by the renderer.
        import_sources = {
            line.strip()
            for symbol in result
            for line in symbol.imports.splitlines()
            if line.strip()
        }
        result_paths = {item.path for item in result}
        for candidate in index.candidates:
            if candidate.id in seen or candidate.path not in result_paths:
                continue
            source_lines = {line.strip() for line in candidate.source.splitlines() if line.strip()}
            if import_sources & source_lines:
                add(candidate, "import-dependency")
        wanted = set().union(*(set(symbol.calls) | set(symbol.references) for symbol in result)) if result else set()
        for name in sorted(wanted):
            for candidate in index.by_name.get(name, []):
                # Exact symbol-name resolution is acceptable for dependencies;
                # generic-language caller expansion requires an actual call.
                if candidate.language == "python" or candidate.name in set().union(*(set(item.calls) for item in result)):
                    add(candidate, "referenced-dependency")
        selected_names = {symbol.name for symbol in result}
        callers = [
            candidate for candidate in index.candidates
            if candidate.id not in seen and selected_names & set(candidate.calls)
        ]
        callers.sort(key=lambda symbol: (symbol.path, symbol.start_line, symbol.id))
        # A caller is admissible only because the indexed call graph proves the
        # relationship. Lexical relevance is not a structural dependency.
        for caller in callers:
            add(caller, "relevant-caller")
        if len(result) == before or len(result) >= limit:
            break

    # Do not add callers/siblings/tests merely because they share query tokens.
    # Such candidates require an explicit uncovered-requirement proof supplied by
    # the router's completion pass, not a lexical score.
    return result, reasons


def expand_context(index: CodeIndex, selected: list[Symbol], query: str, limit: int) -> list[Symbol]:
    expanded, _ = expand_context_detailed(index, selected, query, limit)
    return expanded
