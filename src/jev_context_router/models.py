from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping


@dataclass(frozen=True, order=True)
class SourceSpan:
    """Half-open original UTF-8 byte coordinates; lines are descriptive only."""
    start_byte: int
    end_byte: int
    start_line: int = field(default=0, compare=False)
    end_line: int = field(default=0, compare=False)

    def __post_init__(self) -> None:
        if type(self.start_byte) is not int or type(self.end_byte) is not int:
            raise ValueError("byte offsets must be integers")
        if not 0 <= self.start_byte < self.end_byte:
            raise ValueError("invalid source interval")


@dataclass(frozen=True)
class RegionProposal:
    span: SourceSpan
    names: tuple[str, ...] = ()
    kinds: tuple[str, ...] = ()
    languages: tuple[str, ...] = ()
    provenance: tuple[str, ...] = ()
    precision: tuple[str, ...] = ()


@dataclass(frozen=True)
class EvidenceRegion:
    region_id: str
    path: str
    file_sha256: str
    span: SourceSpan
    names: tuple[str, ...] = ()
    kinds: tuple[str, ...] = ()
    languages: tuple[str, ...] = ()
    provenance: tuple[str, ...] = ()
    precision: tuple[str, ...] = ()
    parent_id: str | None = None
    children_ids: tuple[str, ...] = ()
    overlaps: tuple[str, ...] = ()


@dataclass(frozen=True)
class EvidenceBinding:
    requirement_id: str
    region_id: str
    support_spans: tuple[SourceSpan, ...] = ()
    reason: str = ""


@dataclass(frozen=True)
class RenderedBlock:
    path: str
    file_sha256: str
    span: SourceSpan
    context_start: int
    context_end: int
    region_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class RenderedEvidence:
    context: str = ""
    blocks: tuple[RenderedBlock, ...] = ()
    complete_ids: tuple[str, ...] = ()
    partial_ids: tuple[str, ...] = ()
    represented_ids: tuple[str, ...] = ()
    packing_losses: tuple[str, ...] = ()
    metadata: tuple[Mapping[str, Any], ...] = ()


@dataclass(frozen=True)
class Repository:
    root: Path
    name: str
    aliases: tuple[str, ...] = ()
    description: str = ""
    languages: tuple[str, ...] = ()


@dataclass(frozen=True)
class Symbol:
    id: str
    path: str
    name: str
    qualname: str
    kind: str
    language: str
    start_line: int
    end_line: int
    source: str
    imports: str = ""
    calls: frozenset[str] = field(default_factory=frozenset)
    references: frozenset[str] = field(default_factory=frozenset)
    region_id: str = ""
    start_byte: int = 0
    end_byte: int = 0
    file_sha256: str = ""
    snapshot: bytes = field(default=b"", repr=False)
    precision: tuple[str, ...] = ()


@dataclass(frozen=True)
class QueryPlan:
    query: str
    scope: str
    requirements: tuple[CoverageRequirement, ...] = ()
    explicit_anchors: tuple[str, ...] = ()
    excluded_terms: tuple[str, ...] = ()
    network_allowed: bool = False


@dataclass(frozen=True)
class CoverageRequirement:
    id: str
    source: str
    anchors: tuple[str, ...] = ()
    expected_terms: tuple[str, ...] = ()
    mandatory_terms: tuple[str, ...] = ()
    # Each group is an explicit documented alternative: at least one member
    # must occur in rendered proof. Plain mandatory_terms remain conjunctive.
    mandatory_term_groups: tuple[tuple[str, ...], ...] = ()
    rare_terms: tuple[str, ...] = ()
    mandatory: bool = True


@dataclass(frozen=True)
class CandidateEvidence:
    candidate_id: str
    path: str
    region: tuple[int, int]
    requirement_ids: tuple[str, ...] = ()
    excerpt: str = ""
    reason: str = ""
    cost: int = 0
    provenance: str = "local-index"
    term_hit_count: int = 0
    anchor_hit: bool = False


@dataclass(frozen=True)
class SelectionTrace:
    indexed: tuple[str, ...] = ()
    shortlisted: tuple[str, ...] = ()
    sent: tuple[str, ...] = ()
    scored: tuple[str, ...] = ()
    selected: tuple[str, ...] = ()
    expanded: tuple[str, ...] = ()
    rendered: tuple[str, ...] = ()
    rejected: Mapping[str, str] = field(default_factory=dict)

    def serialized(self, limit: int = 32) -> dict[str, dict[str, object]]:
        """The single source for trace counts and bounded diagnostic ids."""
        values = {
            "indexed": self.indexed, "shortlisted": self.shortlisted, "sent": self.sent,
            "scored": self.scored, "selected": self.selected, "expanded": self.expanded,
            "rendered": self.rendered,
        }
        payload = {name: {"count": len(ids), "ids": list(ids[:limit]), "truncated": len(ids) > limit}
                   for name, ids in values.items()}
        rejected = dict(self.rejected)
        categories = {name: [] for name in ("low-score", "invalid-score", "budget", "not-selected", "empty-proof")}
        for candidate, reason in rejected.items():
            if reason in categories:
                categories[reason].append(candidate)
        payload["rejected"] = {
            name: {"count": len(ids), "ids": ids[:limit], "truncated": len(ids) > limit}
            for name, ids in categories.items()
        }
        return payload


@dataclass(frozen=True)
class CoverageReport:
    planned: int
    covered: int
    missing: int
    covered_requirements: tuple[str, ...] = ()
    missing_requirements: tuple[str, ...] = ()
    reasons: Mapping[str, str] = field(default_factory=dict)
    packing_losses: tuple[str, ...] = ()
    status: str = "insufficient"


@dataclass(frozen=True)
class Selection:
    ids: tuple[str, ...]
    input_tokens: int = 0
    output_tokens: int = 0
    seconds: float = 0.0
    reason: str = ""
    candidates_sent: int = 0
    prompt_bytes: int = 0
    estimated_prompt_tokens: int = 0
    transport_status: str = "not-attempted"
    response_validity: str = "not-applicable"
    external_selection_outcome: str = "not-attempted"
    fallback_used: bool = False
    fallback_reason: str = ""
    shortlist_ids: tuple[str, ...] = ()
    candidates_sent_ids: tuple[str, ...] = ()
    scored_ids: tuple[str, ...] = ()
    invalid_score_ids: tuple[str, ...] = ()
    external_selected_ids: tuple[str, ...] = ()
    excluded_budget_ids: tuple[str, ...] = ()
    excluded_score_ids: tuple[str, ...] = ()
    expanded_ids: tuple[str, ...] = ()
    rendered_ids: tuple[str, ...] = ()
    semantic_fallback_used: bool = False
    semantic_fallback_reason: str = ""
    local_fallback_used: bool = False
    local_fallback_reason: str = ""
    interpreted_scores: Mapping[str, float] = field(default_factory=dict)
    diagnostic_counts: Mapping[str, int] = field(default_factory=dict)
    diagnostic_truncated: Mapping[str, bool] = field(default_factory=dict)
    ledger: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    prepared_payload_bytes: int = 0
    sent_payload_bytes: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "interpreted_scores", MappingProxyType(dict(self.interpreted_scores)))
        object.__setattr__(self, "diagnostic_counts", MappingProxyType(dict(self.diagnostic_counts)))
        object.__setattr__(self, "diagnostic_truncated", MappingProxyType(dict(self.diagnostic_truncated)))
        object.__setattr__(self, "ledger", MappingProxyType({k: tuple(v) for k, v in self.ledger.items()}))


@dataclass(frozen=True)
class RouteResult:
    status: str
    query: str
    repository: Repository | None = None
    context: str = ""
    selected_symbols: tuple[str, ...] = ()
    included_symbols: tuple[str, ...] = ()
    metrics: dict[str, Any] = field(default_factory=dict)
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "query": self.query,
            "repository": str(self.repository.root) if self.repository else None,
            "repository_name": self.repository.name if self.repository else None,
            "context": self.context,
            "selected_symbols": list(self.selected_symbols),
            "included_symbols": list(self.included_symbols),
            "metrics": self.metrics,
            "message": self.message,
        }
