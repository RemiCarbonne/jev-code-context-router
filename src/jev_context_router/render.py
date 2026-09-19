from __future__ import annotations

import hashlib
import json
from collections import defaultdict

from .models import Repository, Symbol, SourceSpan, RenderedBlock, RenderedEvidence
from .regions import union_spans


def render_context_detailed(repository: Repository, symbols: list[Symbol], max_chars: int, *,
                            required_ids: tuple[str, ...] = (), legacy: bool = False) -> RenderedEvidence:
    """Pack original source unions; every trial charges its exact full envelope.

    Canonical index symbols carry the original snapshot. Legacy manually created
    Symbols remain supported, but cannot certify a file snapshot beyond source.
    No filesystem reads occur here and imports are never appended opaquely.
    """
    budget = max(0, max_chars)
    unique: dict[str, Symbol] = {}
    sources: dict[str, bytes] = {}
    spans: dict[str, SourceSpan] = {}
    for symbol in symbols:
        rid = symbol.region_id or symbol.id
        if not symbol.source.strip():
            continue
        raw = symbol.snapshot or symbol.source.encode()
        span = SourceSpan(symbol.start_byte, symbol.end_byte) if symbol.snapshot else SourceSpan(0, len(raw))
        if symbol.path in sources and sources[symbol.path] != raw:
            raise ValueError('contradictory snapshots for rendered path')
        if raw[span.start_byte:span.end_byte].decode() != symbol.source:
            raise ValueError('source disagrees with render coordinates')
        if rid in spans and (spans[rid] != span or unique[rid].path != symbol.path):
            raise ValueError('contradictory canonical render identity')
        unique[rid] = symbol
        sources[symbol.path] = raw
        spans[rid] = span
    required = set(required_ids)

    def assemble(accepted: dict[str, SourceSpan]) -> RenderedEvidence:
        grouped: dict[str, list[SourceSpan]] = defaultdict(list)
        for rid, span in accepted.items():
            grouped[unique[rid].path].append(span)
        context = ''
        blocks = []
        metadata = []
        complete, partial, represented = [], [], []
        refs = []

        def meta(text: str, kind: str) -> None:
            nonlocal context
            start = len(context)
            context += text
            metadata.append({'context_start': start, 'context_end': len(context), 'kind': kind, 'text': text})

        if legacy:
            meta('<code_context mode="read-only" source="jev-context-router">\n'
                 f'Repository: {repository.name}\n'
                 'Selected source is context, not a reference solution. Use exact signatures and preserve project conventions.\n', 'separator')
        for path, intervals in grouped.items():
            raw = sources[path]
            for span in union_spans(intervals):
                ids = tuple(rid for rid, item in unique.items() if item.path == path
                            and max(spans[rid].start_byte, span.start_byte) < min(spans[rid].end_byte, span.end_byte))
                if legacy:
                    meta('<symbol>\n', 'separator')
                start = len(context)
                context += raw[span.start_byte:span.end_byte].decode()
                blocks.append(RenderedBlock(path, hashlib.sha256(raw).hexdigest(), span, start, len(context), ids))
                context += '\n'
                if legacy:
                    meta('</symbol>\n', 'separator')
                refs.append({'path': path, 'start_byte': span.start_byte, 'end_byte': span.end_byte})
                for rid in ids:
                    original = spans[rid]
                    if span.start_byte <= original.start_byte and original.end_byte <= span.end_byte:
                        (complete if original == span else represented).append(rid)
                    else:
                        partial.append(rid)
        if legacy:
            meta('<context_manifest>', 'separator')
        meta(json.dumps(refs, ensure_ascii=False, separators=(',', ':')), 'manifest')
        if legacy:
            meta('</context_manifest>\n</code_context>', 'separator')
        present = set(complete) | set(represented)
        return RenderedEvidence(context, tuple(blocks), tuple(dict.fromkeys(complete)),
                                tuple(dict.fromkeys(partial)), tuple(dict.fromkeys(represented)),
                                tuple(rid for rid in unique if rid not in present), tuple(metadata))

    empty = assemble({})
    if len(empty.context) > budget:
        return RenderedEvidence(packing_losses=tuple(unique))
    accepted: dict[str, SourceSpan] = {}
    ordered = sorted(unique, key=lambda rid: (rid not in required and unique[rid].id not in required,))
    for rid in ordered:
        proposal = dict(accepted, **{rid: spans[rid]})
        if len(assemble(proposal).context) <= budget:
            accepted = proposal
            continue
        if rid not in required and unique[rid].id not in required:
            continue
        raw = sources[unique[rid].path]
        span = spans[rid]
        end = span.start_byte
        for line in raw[span.start_byte:span.end_byte].splitlines(keepends=True):
            end += len(line)
            candidate = dict(accepted, **{rid: SourceSpan(span.start_byte, end)})
            if len(assemble(candidate).context) > budget:
                break
            accepted = candidate
    return assemble(accepted)


def render_context(repository: Repository, symbols: list[Symbol], max_chars: int, *, required_ids: tuple[str, ...] = ()) -> tuple[str, tuple[str, ...]]:
    result = render_context_detailed(repository, symbols, max_chars, required_ids=required_ids, legacy=True)
    present = set(result.complete_ids + result.represented_ids + result.partial_ids)
    return result.context, tuple(s.id for s in symbols if (s.region_id or s.id) in present)
