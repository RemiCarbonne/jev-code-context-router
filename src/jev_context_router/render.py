from __future__ import annotations

import json
from .models import Repository, Symbol


def render_context(repository: Repository, symbols: list[Symbol], max_chars: int) -> tuple[str, tuple[str, ...]]:
    intro = (
        '<code_context mode="read-only" source="jev-context-router">\n'
        f"Repository: {repository.name}\n"
        "Selected source is context, not a reference solution. Use exact signatures and preserve project conventions.\n"
    )
    outro = "\n</code_context>"
    chunks = [intro]
    included: list[str] = []
    emitted_imports: set[str] = set()
    for symbol in symbols:
        imports = symbol.imports if symbol.path not in emitted_imports else ""
        block = (
            f'\n<symbol id={json.dumps(symbol.id)} path={json.dumps(symbol.path)} '
            f'language={json.dumps(symbol.language)} kind={json.dumps(symbol.kind)} '
            f'lines="{symbol.start_line}-{symbol.end_line}">\n'
            + ((imports + "\n\n") if imports else "")
            + symbol.source
            + "\n</symbol>"
        )
        if sum(map(len, chunks)) + len(block) + len(outro) > max_chars:
            continue
        chunks.append(block)
        included.append(symbol.id)
        emitted_imports.add(symbol.path)
    manifest = json.dumps({"included_symbols": included, "repository": repository.name}, ensure_ascii=False)
    if sum(map(len, chunks)) + len(manifest) + len(outro) + 40 <= max_chars:
        chunks.append(f"\n<context_manifest>{manifest}</context_manifest>")
    chunks.append(outro)
    return "".join(chunks), tuple(included)
