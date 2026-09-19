"""Proof closure over adapter-supplied containment, never source syntax."""
from __future__ import annotations

import re

from .coverage import evidence_for_symbols, is_excluded, _term_matches, _term_set
from .models import EvidenceBinding, SourceSpan
from .render import render_context_detailed


def identity_bindings(plan, index):
    """Bind unambiguous requested identities that are structurally independent."""
    result = []
    for requirement in plan.requirements:
        chosen = []
        for term in requirement.expected_terms:
            options = [region for region in index.regions
                       if term in {name.casefold() for name in region.names}
                       and (not requirement.anchors or any(a.casefold() in region.path.casefold()
                                                          for a in requirement.anchors))
                       and region.region_id in index.by_id
                       and not is_excluded(plan, index.by_id[region.region_id], index.regions)]
            if len(options) == 1:
                chosen.append(options[0])
        chosen = list({region.region_id: region for region in chosen}.values())
        if len(chosen) < 2:
            continue
        chosen_ids = {region.region_id for region in chosen}
        nested = False
        for region in chosen:
            parent_id = region.parent_id
            seen = set()
            while parent_id and parent_id not in seen:
                if parent_id in chosen_ids:
                    nested = True
                    break
                seen.add(parent_id)
                parent = index.regions_by_id.get(parent_id)
                parent_id = parent.parent_id if parent is not None else ""
            if nested:
                break
        if nested:
            continue
        result.extend(EvidenceBinding(requirement.id, region.region_id, (region.span,),
                                      'independent-requested-identity') for region in chosen)
    return tuple(dict.fromkeys(result))


def complete_bindings(plan, symbols, index):
    """Bind complete requests to exact selected source intervals before packing."""
    identities = identity_bindings(plan, index)
    if not re.search(r'\b(?:complete|whole|entire|complet|complète)\b', plan.query, re.I):
        return identities
    regions = index.regions_by_id
    by_id = {symbol.id: symbol for symbol in symbols}
    pending = list(by_id)
    while pending:
        region = regions.get(pending.pop())
        if region is None:
            continue
        for child in region.children_ids:
            if child not in by_id and child in index.by_id:
                by_id[child] = index.by_id[child]
                pending.append(child)
    # A selected parent can represent the requested named child. Bind the
    # child's interval, not the parent's kind or the child's selection flag.
    evidence = evidence_for_symbols(plan, by_id.values(), index.regions)
    result = list(identities)
    for requirement in plan.requirements:
        requested = set(re.findall(r'\b(?:function|method|class|callback|object|template|element)\b', requirement.source.casefold()))
        if 'function' in requested:
            requested.add('method')
        options = [by_id[item.candidate_id] for item in evidence if requirement.id in item.requirement_ids]
        units = [symbol for symbol in options if not requested or
                 set(regions[symbol.id].kinds if symbol.id in regions else (symbol.kind,)) & requested]
        if not units:
            result.append(EvidenceBinding(requirement.id, '', (), 'missing-structural-unit'))
        for symbol in units:
            spans = (SourceSpan(symbol.start_byte, symbol.end_byte),) if symbol.snapshot else ()
            result.append(EvidenceBinding(requirement.id, symbol.id, spans, 'complete-structural-unit'))
    return tuple(result)


def structural_base(plan, index, seed_ids, max_chars):
    """Raise a fragment to the nearest admissible complete requested unit.

    A module/remainder is not an implicit whole-file substitute. Missing
    structure remains a fallback; no delimiter or language inference lives here.
    """
    seed_ids = list(seed_ids)
    for requirement in plan.requirements:
        if not requirement.mandatory_terms:
            continue
        options = [symbol for symbol in index.candidates if not is_excluded(plan, symbol, index.regions)
                   and (not requirement.anchors or any(anchor.casefold() in symbol.path.casefold()
                                                       for anchor in requirement.anchors))]
        hits = {symbol.id: {term for term in requirement.mandatory_terms
                            if _term_matches(term, _term_set(symbol.source))} for symbol in options}
        missing = set(requirement.mandatory_terms) - set().union(*(hits.get(rid, set()) for rid in seed_ids))
        while missing:
            useful = [symbol for symbol in options if hits[symbol.id] & missing]
            if not useful:
                break
            chosen = min(useful, key=lambda symbol: (-len(hits[symbol.id] & missing),
                         len(symbol.source.encode()), symbol.path, symbol.start_byte, symbol.id))
            if chosen.id not in seed_ids:
                seed_ids.append(chosen.id)
            missing.difference_update(hits[chosen.id])
    complete = bool(re.search(r'\b(?:complete|whole|entire|complet|complète)\b', plan.query, re.I))
    requested = set(re.findall(r'\b(?:function|method|class|callback|object|template|element)\b', plan.query.casefold()))
    if 'function' in requested:
        requested.add('method')
    if complete and requested:
        # A named declaration is authoritative over a cheaper name occurrence
        # (for example an import). Search adapter metadata, never source syntax.
        named = []
        for requirement in plan.requirements:
            options = [region for region in index.regions
                       if set(region.kinds) & requested
                       and set(name.casefold() for name in region.names) & set(requirement.expected_terms)
                       and (not requirement.anchors or any(anchor.casefold() in region.path.casefold()
                                                           for anchor in requirement.anchors))
                       and region.region_id in index.by_id
                       and evidence_for_symbols(plan, (index.by_id[region.region_id],), index.regions)]
            if options:
                named.append(min(options, key=lambda r: (r.span.end_byte - r.span.start_byte,
                                                         r.path, r.span, r.region_id)).region_id)
            else:
                named.extend(item.candidate_id for item in evidence_for_symbols(
                    plan, (index.by_id[seed] for seed in seed_ids))
                    if requirement.id in item.requirement_ids)
        seed_ids = tuple(dict.fromkeys(named))
    seed_ids = list(seed_ids)
    seed_ids.extend(binding.region_id for binding in identity_bindings(plan, index))
    seed_ids = [seed for seed in dict.fromkeys(seed_ids)
                if seed in index.by_id and not is_excluded(plan, index.by_id[seed], index.regions)]
    result = []
    for seed in seed_ids:
        current = index.regions_by_id.get(seed)
        while complete and current is not None:
            kinds = set(current.kinds)
            structural = bool(kinds & requested) if requested else bool(kinds - {'module', 'block', 'text'})
            symbol = index.by_id.get(current.region_id)
            if structural and symbol and evidence_for_symbols(plan, (symbol,), index.regions):
                seed = current.region_id
                break
            current = index.regions_by_id.get(current.parent_id)
        if seed not in result:
            result.append(seed)
    # Build closures from the adapter's parent links, not from filenames or
    # guessed delimiters. Disjoint roots/files remain a union of proof regions.
    ancestors = {}
    for seed in result:
        current = index.regions_by_id.get(seed)
        seen = set()
        while current is not None and current.region_id not in seen:
            seen.add(current.region_id)
            ancestors.setdefault(current.region_id, set()).add(seed)
            current = index.regions_by_id.get(current.parent_id)
    ordered = sorted(ancestors, key=lambda rid: (
        -len(ancestors[rid]),
        index.regions_by_id[rid].span.end_byte - index.regions_by_id[rid].span.start_byte, rid))
    consumed = set()
    replacements = {}
    for rid in ordered:
        children = ancestors[rid]
        region = index.regions_by_id[rid]
        symbol = index.by_id.get(rid)
        if (len(children) < 2 or children & consumed or
                not set(region.kinds) - {'module', 'text'} or
                not symbol or not evidence_for_symbols(plan, (symbol,), index.regions)):
            continue
        trial_ids = tuple(dict.fromkeys(rid if seed in children else replacements.get(seed, seed)
                                        for seed in result))
        trial = render_context_detailed(index.repository, [index.by_id[seed] for seed in trial_ids],
                                        max_chars, required_ids=trial_ids)
        if trial.packing_losses:
            continue
        for child in children:
            replacements[child] = rid
        consumed.update(children)
    return tuple(dict.fromkeys(replacements.get(rid, rid) for rid in result))
