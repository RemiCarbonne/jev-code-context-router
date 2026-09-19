from __future__ import annotations

from .index import CodeIndex
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
    ranked = [(score_symbol(query, symbol), symbol) for symbol in index.symbols]
    ranked = [row for row in ranked if row[0] > 0]
    ranked.sort(key=lambda row: (-row[0], row[1].path, row[1].start_line, row[1].id))
    return [symbol for _, symbol in ranked[:limit]]


def expand_context_detailed(
    index: CodeIndex, selected: list[Symbol], query: str, limit: int
) -> tuple[list[Symbol], dict[str, str]]:
    result: list[Symbol] = []
    seen: set[str] = set()
    reasons: dict[str, str] = {}

    def add(symbol: Symbol, reason: str) -> None:
        nonlocal result
        if symbol.kind == "method":
            parent_name = symbol.qualname.rsplit(".", 1)[0]
            parents = [candidate for candidate in index.symbols if candidate.path == symbol.path and candidate.qualname == parent_name and candidate.kind == "class"]
            if parents:
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
        wanted = set().union(*(set(symbol.calls) | set(symbol.references) for symbol in result)) if result else set()
        for name in sorted(wanted):
            for candidate in index.by_name.get(name, []):
                if candidate.language == "python" or score_symbol(query, candidate) > 0:
                    add(candidate, "referenced-dependency")
        selected_names = {symbol.name for symbol in result}
        callers = [
            candidate for candidate in index.symbols
            if candidate.id not in seen and selected_names & (set(candidate.calls) | set(candidate.references))
        ]
        callers.sort(key=lambda symbol: (-score_symbol(query, symbol), symbol.path, symbol.start_line))
        for caller in (item for item in callers if score_symbol(query, item) > 0):
            add(caller, "relevant-caller")
        if len(result) == before or len(result) >= limit:
            break

    # Generic-language references are less precise. Preserve neighboring declarations
    # in the same file so interfaces, helpers, and methods do not lose local context.
    for symbol in list(result):
        siblings = [candidate for candidate in index.symbols if candidate.path == symbol.path and candidate.id not in seen]
        siblings.sort(key=lambda candidate: abs(candidate.start_line - symbol.start_line))
        for sibling in (item for item in siblings if score_symbol(query, item) > 0):
            add(sibling, "same-file-relevant-neighbor")
            break

    selected_path_tokens = set().union(*(tokens(symbol.path) for symbol in selected)) if selected else set()
    tests = [
        symbol for symbol in index.symbols
        if (symbol.path.startswith(("tests/", "test/", "spec/")) or any(part in symbol.path for part in ("/tests/", "/test/", "/spec/")))
        and selected_path_tokens & tokens(symbol.path)
    ]
    tests.sort(key=lambda symbol: (-score_symbol(query, symbol), symbol.path, symbol.start_line))
    for symbol in tests:
        if score_symbol(query, symbol) > 0:
            add(symbol, "relevant-test")
    return result, reasons


def expand_context(index: CodeIndex, selected: list[Symbol], query: str, limit: int) -> list[Symbol]:
    expanded, _ = expand_context_detailed(index, selected, query, limit)
    return expanded
