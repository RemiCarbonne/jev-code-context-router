from __future__ import annotations

import math
import hashlib
import re
from dataclasses import dataclass, replace
from typing import Iterable, Mapping, Sequence

from .regions import union_spans
from .models import SourceSpan, CandidateEvidence, CoverageReport, CoverageRequirement, QueryPlan, SelectionTrace, Symbol, RenderedEvidence

# Deliberately vocabulary-light: these are grammatical separators, not domain rules.
_CLAUSE_SPLIT = re.compile(r"\s+(?:and|et|or|ou|then|puis|also|ainsi que|as well as)\s+|[,;\n]+", re.I)
_GENERIC_OBLIGATION_WORDS = {
    "fix", "debug", "refactor", "change", "update", "identify", "find", "trace", "explain",
    "exact", "symbols", "files", "functions", "code", "request", "one", "all", "relevant",
    "the", "a", "an", "les", "des", "une", "un", "et", "the", "then", "do", "not",
    "export", "function", "async", "javascript", "typescript", "data", "flow", "minimal", "edit",
    "every", "call", "without", "changing", "behavior", "editing", "across", "this", "one", "concrete",
    "consumes", "identify", "relevant", "files", "functions", "then", "find", "explain", "do", "not",
    "eliminate", "repeated", "declarations", "while", "preserving", "repository", "needed", "implement", "validate",
    # Request framing and structural labels guide planning but need not appear
    # literally in source code.
    "show", "quote", "include", "complete", "final", "definition", "calls", "attributes", "multiline",
    "decorator", "nested", "aggregation", "working", "calculation", "explicitly", "admitted", "text",
    "contiguous", "declaration", "from", "claiming", "parses", "glossary", "words",
}

_FILE_MARKER = re.compile(r"(?:^|[\s`'\"])([\w./\\-]+\.(?:py|js|jsx|mjs|cjs|ts|tsx|astro|go|rs|java|kt|php|rb|cs|c|h|cpp|cc|hpp|swift|scala))\b", re.I)
_UNIVERSAL_QUANTIFIER = re.compile(
    r"\b(?:all|every|each|tous|toutes|chaque|chacun|chacune)\s+(?:complete|complet|complète|complets|complètes)\b",
    re.I,
)
_NEGATIVE_TAIL = re.compile(
    # "without/sans" commonly constrain the requested operation (for example
    # "without editing files") rather than excluding source evidence.  Keep
    # them in the positive request; only explicit exclusion/negation forms
    # become evidence filters.
    r"(?:[,;.]\s*)?\b(?:exclude|excluding|not|do\s+not|ne\s+pas)\b(?P<tail>[^.;\n]*)",
    re.I,
)
_NEGATIVE_STOP_WORDS = {
    "exclude", "excluding", "without", "not", "do", "treat", "claiming", "the", "a", "an", "as",
    "ne", "pas", "sans", "les", "des", "un", "une", "comme", "from", "dans", "de", "du", "and", "or",
    "et", "ou", "this", "that", "it", "its", "implementation", "claim", "consider", "considérer",
}
_ILLUSTRATIVE_TERMS = {
    "sample", "samples", "example", "examples", "preview", "previews", "story", "stories",
    "glossary", "glossaries", "note", "notes", "comment", "comments", "catalog", "catalogue",
}
def analyze_query_scope(query: str) -> str:
    clauses = _independent_clauses(query)
    # Scope is a property of the obligations, not a vocabulary allow-list.
    # A coordination of distinct targets is cross-cutting in every language.
    if len(clauses) > 1:
        return "cross-cutting"
    if not query.strip():
        return "unknown"
    return "localized"


def _independent_clauses(query: str) -> list[str]:
    total_anchors = _anchors(query)
    transversal = bool(re.search(r"\b(?:across|through|à travers|a travers|à travers de)\b", query, re.I))


    # A single addressed file (or a prose request with no enumerated targets)
    # remains one obligation even when its acceptance criteria use many
    # conjunctions.  This is the compatibility path for historical requests.
    short_coordination = bool(re.search(r"\s+(?:and|et|or|ou)\s+", query, re.I)) and all(
        len(re.findall(r"[A-Za-zÀ-ÿ_$][\w$-]{3,}", part)) <= 4
        for part in re.split(r"\s+(?:and|et|or|ou)\s+", query, flags=re.I)
    )
    # Coordinations inside an operation's preservation clause are not new
    # retrieval targets ("while preserving ... and ...").
    subordinate = re.search(r"\b(?:while|en préservant)\b", query, re.I)
    coordination = re.search(r"\s+(?:and|et|or|ou|then|puis|also)\s+", query, re.I)
    if len(total_anchors) <= 1 and subordinate and coordination and subordinate.start() < coordination.start():
        return [query.strip()]
    if not total_anchors and not transversal and not short_coordination:
        return [query.strip()] if query.strip() else []
    splitter = _CLAUSE_SPLIT if re.search(r"\b(?:across|through|à travers|a travers|à travers de|across)\b", query, re.I) else re.compile(r"\s+(?:and|et|or|ou|then|puis|also|ainsi que|as well as)\s+|[;\n]+", re.I)
    raw = [part.strip(" ,:.\t") for part in splitter.split(query) if part.strip(" ,:.\t")]
    # A conjunction is not, by itself, a second obligation.  Complements such
    # as "identify exact symbols" belong to the same action as its target.
    # Keep a fragment separate only when it has an independently addressable
    # code target (a file anchor or a concrete noun), or when the whole request
    # is the deliberately transversal "middleware and layout" form.
    if len(raw) > 1:
        merged: list[str] = []
        for part in raw:
            has_anchor = bool(_anchors(part))
            words = {w.casefold() for w in re.findall(r"[A-Za-zÀ-ÿ_$][\w$-]{3,}", part)}
            concrete = words - _GENERIC_OBLIGATION_WORDS - {
                "across", "through", "travers", "traverse", "traverser", "policy", "politique",
            }
            first_word = next(iter(re.findall(r"[A-Za-zÀ-ÿ_$][\w$-]{3,}", part.casefold())), "")
            complement = first_word in {
                "identify", "find", "trace", "explain", "déterminer", "identifier", "trouver",
                "its", "their", "son", "sa", "ses", "leur", "leurs",
            }
            if merged and not has_anchor and (not concrete or complement):
                merged[-1] += " and " + part
            else:
                merged.append(part)
        raw = merged
    meaningful = []
    for clause in raw:
        words = {word.casefold() for word in re.findall(r"[A-Za-zÀ-ÿ_$][\w$-]{3,}", clause)}
        if words - _GENERIC_OBLIGATION_WORDS:
            meaningful.append(clause)
    return meaningful or raw

def _anchors(text: str) -> tuple[str, ...]:
    values = [m.group(1).replace("\\", "/") for m in _FILE_MARKER.finditer(text)]
    return tuple(dict.fromkeys(values))


def _requirement_label(clause: str) -> str:
    words = re.findall(r"[A-Za-zÀ-ÿ_$][\w$-]{3,}", clause)
    meaningful = [w.casefold() for w in words if w.casefold() not in {
        "this", "that", "with", "from", "pour", "dans", "the", "and", "et", "file", "fichier",
        "code", "also", "then", "plus", "must", "should", "doit", "les", "des", "une", "un",
    }]
    return "-".join(meaningful[:4]) or "request"


def build_query_plan(query: str, *, max_requirements: int | None = None) -> QueryPlan:
    negative_tails = [match.group("tail") for match in _NEGATIVE_TAIL.finditer(query)]
    excluded_terms = tuple(dict.fromkeys(
        term for tail in negative_tails
        for term in re.findall(r"[A-Za-zÀ-ÿ_$][\w$-]{3,}", tail.casefold())
        if term not in _NEGATIVE_STOP_WORDS
    ))
    # Explicitly excluded illustrative material is a semantic family rather
    # than a language-specific filename convention.
    if set(excluded_terms) & _ILLUSTRATIVE_TERMS:
        excluded_terms = tuple(dict.fromkeys((*excluded_terms, *_ILLUSTRATIVE_TERMS)))

    positive_query = _NEGATIVE_TAIL.sub("", query).strip(" ,;.")
    clauses = _independent_clauses(positive_query)
    if not clauses:
        clauses = [positive_query or query.strip()]
    requirements: list[CoverageRequirement] = []
    requested = bool(re.match(
        r"\s*(?:show|trace|find|quote|include|fix|debug|refactor|change|update|explain|implement|in|for|montre|trouve|explique)\b",
        positive_query, re.I,
    ))
    shared_anchors = _anchors(positive_query)
    for index, clause in enumerate(clauses[:max_requirements]):
        meaningful_terms = tuple(dict.fromkeys(
            term for term in re.findall(r"[A-Za-zÀ-ÿ_$][\w$-]{3,}", clause.casefold())
            if term not in _GENERIC_OBLIGATION_WORDS
        ))

        requirements.append(CoverageRequirement(
            id=f"requirement-{index + 1}", source=clause,
            anchors=_anchors(clause) or (shared_anchors if len(shared_anchors) == 1 else ()),
            expected_terms=meaningful_terms,
            mandatory_terms=() if requested else meaningful_terms,
            mandatory_term_groups=(),
            rare_terms=tuple(term for term in meaningful_terms if len(term) >= 6),
            mandatory=True,
        ))
    return QueryPlan(
        query=query,
        scope=analyze_query_scope(positive_query),
        requirements=tuple(requirements),
        explicit_anchors=tuple(dict.fromkeys(a for r in requirements for a in r.anchors)),
        excluded_terms=excluded_terms,
    )


def requires_exhaustive_match(query: str) -> bool:
    """Return whether the request explicitly quantifies over every match.

    This is grammatical routing metadata, not a language- or repository-specific
    heuristic.  It only relaxes candidate-count caps; normal relevance and
    context-budget checks still apply.
    """
    return bool(_UNIVERSAL_QUANTIFIER.search(query))


# Public spelling useful to integrations.
analyze_query = build_query_plan


def _term_set(value: str) -> set[str]:
    return set(re.findall(r"[A-Za-zÀ-ÿ_$][\w$-]{3,}", value.casefold()))


def _term_matches(term: str, values: set[str]) -> bool:
    return term in values or any(term in value or value in term for value in values if len(value) >= 4)


def is_excluded(plan: QueryPlan, symbol: Symbol, regions=()) -> bool:
    terms = _term_set(f"{symbol.path} {symbol.name} {symbol.qualname} {symbol.source}")
    if any(_term_matches(term, terms) for term in plan.excluded_terms):
        return True
    # An explicitly excluded adapter annotation denotes a hard source interval.
    # Intersections, including crossing regions and containing parents, are unsafe.
    return any(region.path == symbol.path and region.file_sha256 == symbol.file_sha256
               and max(region.span.start_byte, symbol.start_byte) < min(region.span.end_byte, symbol.end_byte)
               and any(_term_matches(term, _term_set(' '.join(region.names + region.kinds)))
                       for term in plan.excluded_terms) for region in regions)


def evidence_for_symbols(plan: QueryPlan, symbols: Iterable[Symbol], regions=()) -> list[CandidateEvidence]:
    result: list[CandidateEvidence] = []
    for symbol in symbols:
        haystack = _term_set(f"{symbol.name} {symbol.qualname} {symbol.source}")
        if is_excluded(plan, symbol, regions):
            continue
        for requirement in plan.requirements:
            overlap = {term for term in requirement.expected_terms if _term_matches(term, haystack)}
            anchor_hit = any(anchor.casefold() in symbol.path.casefold() for anchor in requirement.anchors)
            if overlap or anchor_hit:
                result.append(CandidateEvidence(
                    candidate_id=symbol.id, path=symbol.path, region=(symbol.start_line, symbol.end_line),
                    requirement_ids=(requirement.id,), excerpt=symbol.source[:1000],
                    reason="explicit-anchor" if anchor_hit else f"local-term-evidence:{len(overlap)}",
                    cost=max(1, len(symbol.source.encode("utf-8")) // 4), provenance="local-index",
                    term_hit_count=len(overlap), anchor_hit=anchor_hit,
                ))
    return result


def diversify_candidates(plan: QueryPlan, symbols: Sequence[Symbol], limit: int) -> list[Symbol]:
    evidence = evidence_for_symbols(plan, symbols)
    by_requirement: dict[str, list[Symbol]] = {r.id: [] for r in plan.requirements}
    by_id = {s.id: s for s in symbols}
    for item in evidence:
        if item.candidate_id in by_id:
            for rid in item.requirement_ids:
                by_requirement[rid].append(by_id[item.candidate_id])
    result: list[Symbol] = []
    seen: set[str] = set()
    # One pass per obligation guarantees that global symbol concentration cannot consume the base.
    for requirement in plan.requirements:
        options = sorted(by_requirement[requirement.id], key=lambda s: (s.path, s.start_line, s.id))
        for symbol in options:
            if symbol.id not in seen:
                result.append(symbol); seen.add(symbol.id); break
    for symbol in symbols:
        if len(result) >= limit: break
        if symbol.id not in seen:
            result.append(symbol); seen.add(symbol.id)
    return result[:limit]


def can_finish_locally(plan: QueryPlan, evidence: Iterable[CandidateEvidence]) -> bool:
    covered = {rid for item in evidence for rid in item.requirement_ids}
    return all((not r.mandatory) or r.id in covered for r in plan.requirements)


def _rendered_spans(symbol, detailed):
    """Certify coordinates against actual emitted bytes and the snapshot."""
    if not symbol.snapshot or hashlib.sha256(symbol.snapshot).hexdigest() != symbol.file_sha256:
        return ()
    valid = []
    for block in detailed.blocks:
        if block.path != symbol.path or block.file_sha256 != symbol.file_sha256:
            continue
        if not 0 <= block.context_start < block.context_end <= len(detailed.context):
            continue
        if block.span.end_byte > len(symbol.snapshot):
            continue
        emitted = detailed.context[block.context_start:block.context_end].encode('utf-8')
        if emitted == symbol.snapshot[block.span.start_byte:block.span.end_byte]:
            valid.append(block.span)
    return union_spans(valid)


def coverage_report(plan: QueryPlan, selected: Iterable[Symbol], rendered_ids: Iterable[str] | RenderedEvidence, evidence: Iterable[CandidateEvidence], *, packing_losses: Iterable[str] = (), bindings=()) -> CoverageReport:
    selected = list(selected)
    evidence = list(evidence)
    if isinstance(rendered_ids, RenderedEvidence):
        detailed = rendered_ids
        rendered = set(detailed.complete_ids + detailed.represented_ids + detailed.partial_ids)
        by_id = {s.id: s for s in selected}
        exact = []
        for item in evidence:
            symbol = by_id.get(item.candidate_id)
            if symbol is None:
                continue
            fragments = []
            for span in _rendered_spans(symbol, detailed):
                start = max(span.start_byte, symbol.start_byte)
                end = min(span.end_byte, symbol.end_byte)
                if start < end:
                    fragments.append(symbol.snapshot[start:end].decode('utf-8'))
            if fragments:
                rendered.add(item.candidate_id)
            exact.append(replace(item, excerpt='\n'.join(fragments)))
        evidence = exact
        packing_losses = detailed.packing_losses
    else:
        # Legacy callers have no packing ledger. Intersect the provided excerpt
        # with actual symbol source; this path cannot certify detailed rendering.
        rendered = set(rendered_ids)
    # ``rendered_ids`` is only authoritative when the candidate contains useful
    # proof.  Empty/truncated identifiers must never manufacture coverage.
    # CandidateEvidence carries the bounded proof excerpt, while requirement
    # terms live on the plan.  Check it structurally rather than trusting ids.
    present = set()
    proof_by_requirement: dict[str, set[str]] = {r.id: set() for r in plan.requirements}
    if isinstance(rendered_ids, RenderedEvidence):
        # Merge source intervals across candidates before tokenization. A token
        # can cross adapter boundaries; output separators are not source bytes.
        scopes = {}
        by_id = {s.id: s for s in selected}
        for item in evidence:
            symbol = by_id.get(item.candidate_id)
            if symbol is None:
                continue
            for span in _rendered_spans(symbol, rendered_ids):
                start, end = max(span.start_byte, symbol.start_byte), min(span.end_byte, symbol.end_byte)
                if start >= end:
                    continue
                for rid in item.requirement_ids:
                    key = (rid, symbol.path, symbol.file_sha256)
                    scopes.setdefault(key, (symbol.snapshot, []))[1].append(SourceSpan(start, end))
        requirements = {r.id: r for r in plan.requirements}
        for (rid, _, _), (snapshot, spans) in scopes.items():
            if rid not in requirements:
                continue
            terms = _term_set('\n'.join(snapshot[s.start_byte:s.end_byte].decode('utf-8')
                                        for s in union_spans(spans)))
            proof_by_requirement[rid].update(term for term in requirements[rid].expected_terms
                                             if _term_matches(term.casefold(), terms))
    for item in evidence:
        if item.candidate_id not in rendered or not item.excerpt.strip():
            continue
        requirement_map = {r.id: r for r in plan.requirements}
        for rid in item.requirement_ids:
            requirement = requirement_map.get(rid)
            if not requirement:
                continue

            # Paths are structural metadata, while the excerpt supplies behavior.
            proof_terms = _term_set(item.excerpt)
            proof_by_requirement[rid].update(term for term in requirement.expected_terms
                                              if _term_matches(term.casefold(), proof_terms))
            # An anchor identifies where to look; it is never behavioral proof.
            # Plain mandatory terms and every mandatory alternative group are
            # conjunctive.  Other expected terms use a documented strict
            # majority threshold, with rare terms preventing generic overlap
            # from satisfying an obligation.  In particular, an anchor cannot
            # bypass a missing behavioral term.
            hits = proof_by_requirement[rid]
            requested = bool(re.match(
                r"\s*(?:show|trace|find|quote|include|fix|debug|refactor|change|update|explain|implement|in|for|montre|trouve|explique)\b",
                plan.query, re.I,
            ))
            ratio = 0.4 if requested else 0.6
            threshold = (1 if requirement.anchors else
                         max(1, math.ceil(len(requirement.expected_terms) * ratio))) if requirement.expected_terms else 1
            rare_ok = bool(requirement.anchors) or not requirement.rare_terms or any(term in hits for term in requirement.rare_terms)
            mandatory_ok = all(_term_matches(term, hits) for term in requirement.mandatory_terms)
            groups_ok = all(any(_term_matches(term, hits) for term in group)
                            for group in requirement.mandatory_term_groups)
            expected_ok = len(hits) >= threshold and rare_ok
            if mandatory_ok and groups_ok and expected_ok:
                present.add(rid)
    # Structural completeness is conjunctive with lexical proof. Check the
    # emitted byte union, not IDs or a selector's assertion of sufficiency.
    by_id = {symbol.id: symbol for symbol in selected}
    for binding in bindings:
        symbol = by_id.get(binding.region_id)
        spans = []
        if isinstance(rendered_ids, RenderedEvidence) and symbol:
            spans = _rendered_spans(symbol, rendered_ids)
        if not binding.support_spans or not all(
                any(span.start_byte <= support.start_byte and support.end_byte <= span.end_byte for span in spans)
                for support in binding.support_spans):
            present.discard(binding.requirement_id)
    selected_ids = {s.id for s in selected}
    selected_present = {rid for item in evidence if item.candidate_id in selected_ids for rid in item.requirement_ids}
    covered = tuple(r.id for r in plan.requirements if r.id in present)
    missing = tuple(r.id for r in plan.requirements if r.mandatory and r.id not in present)
    return CoverageReport(
        planned=len(plan.requirements), covered=len(covered), missing=len(missing),
        covered_requirements=covered, missing_requirements=missing,
        reasons={r.id: ("rendered-proof" if r.id in present else "selected-but-not-rendered" if r.id in selected_present else "no-local-proof") for r in plan.requirements},
        packing_losses=tuple(packing_losses), status="sufficient" if not missing else "insufficient",
    )


# Compatibility aliases for callers that prefer verbs.
plan_query = build_query_plan
validate_coverage = coverage_report
