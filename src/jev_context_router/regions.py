"""Language-independent snapshot regions. Adapters supply coordinates, not identity."""
from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from typing import Iterable, Protocol

from .models import EvidenceRegion, RegionProposal, SourceSpan

SCHEMA_VERSION = 1


class RegionAdapter(Protocol):
    """Optional extractors must declare exact original byte coordinates."""
    version: str

    def extract(self, path: str, source: bytes) -> Iterable[RegionProposal]: ...


def union_spans(spans: Iterable[SourceSpan]) -> tuple[SourceSpan, ...]:
    result: list[SourceSpan] = []
    for span in sorted(spans):
        if result and span.start_byte <= result[-1].end_byte:
            prior = result.pop()
            result.append(SourceSpan(prior.start_byte, max(prior.end_byte, span.end_byte)))
        else:
            result.append(span)
    return tuple(result)


def canonicalize(path: str, source: bytes, proposals: Iterable[RegionProposal]) -> tuple[EvidenceRegion, ...]:
    """Merge aliases, then derive strict containment and nonnested overlaps.

    Equal-sized containers are resolved by interval then opaque identity. No
    adapter names, languages or iteration orders participate in identity.
    """
    source.decode('utf-8', errors='strict')
    if not path or path.startswith('/') or any(p in ('', '.', '..') for p in path.split('/')):
        raise ValueError('expected exact relative source path')
    digest = hashlib.sha256(source).hexdigest()
    grouped: dict[SourceSpan, list[RegionProposal]] = {}
    for proposal in proposals:
        span = proposal.span
        if span.end_byte > len(source):
            raise ValueError('interval outside snapshot')
        try:
            source[:span.start_byte].decode('utf-8')
            source[span.start_byte:span.end_byte].decode('utf-8')
        except UnicodeError as error:
            raise ValueError('interval splits UTF-8 code point') from error
        grouped.setdefault(span, []).append(proposal)
    regions = []
    for span, annotations in sorted(grouped.items()):
        identity = json.dumps([SCHEMA_VERSION, path, digest, span.start_byte, span.end_byte], ensure_ascii=False, separators=(',', ':'))
        span = replace(span, start_line=source[:span.start_byte].count(b'\n') + 1,
                       end_line=source[:span.end_byte - 1].count(b'\n') + 1)
        merged = {key: tuple(sorted({v for p in annotations for v in getattr(p, key)}))
                  for key in ('names', 'kinds', 'languages', 'provenance', 'precision')}
        regions.append(EvidenceRegion(hashlib.sha256(identity.encode()).hexdigest(), path, digest, span, **merged))

    def contains(a: EvidenceRegion, b: EvidenceRegion) -> bool:
        return a.span != b.span and a.span.start_byte <= b.span.start_byte and b.span.end_byte <= a.span.end_byte

    parents = {}
    for region in regions:
        containers = [r for r in regions if contains(r, region)]
        parent = min(containers, key=lambda r: (r.span.end_byte - r.span.start_byte, r.span, r.region_id), default=None)
        parents[region.region_id] = parent.region_id if parent else None
    return tuple(replace(r, parent_id=parents[r.region_id],
                         children_ids=tuple(sorted(c.region_id for c in regions if parents[c.region_id] == r.region_id)),
                         overlaps=tuple(sorted(o.region_id for o in regions if o != r
                                              and max(o.span.start_byte, r.span.start_byte) < min(o.span.end_byte, r.span.end_byte)
                                              and not contains(r, o) and not contains(o, r))))
                 for r in regions)


def source_text(region: EvidenceRegion, snapshot: bytes) -> str:
    if hashlib.sha256(snapshot).hexdigest() != region.file_sha256:
        raise ValueError('snapshot fingerprint mismatch')
    if region.span.end_byte > len(snapshot):
        raise ValueError('interval outside snapshot')
    return snapshot[region.span.start_byte:region.span.end_byte].decode('utf-8')
