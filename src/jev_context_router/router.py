from __future__ import annotations

import json
import math
import queue
import threading
import time
import traceback
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

from .config import DIAGNOSTIC_ID_LIMIT, Settings
from .coverage import (
    build_query_plan,
    coverage_report,
    diversify_candidates,
    evidence_for_symbols,
    requires_exhaustive_match,
)
from .discovery import discover_repositories, resolve_repository
from .errors import RoutingLimitExceeded, RoutingStageError, RoutingTimeout
from .index import cache_path_for, index_repository
from .intent import classify_code_request
from .lexical import LexicalResult, retrieve_lexical_candidates
from .models import RouteResult, Selection, SelectionTrace
from .providers import JevSelector, LocalSelector, Selector
from .ranking import expand_context_detailed, shortlist
from .render import render_context_detailed
from .security import PathPolicy
from .structural import structural_base, complete_bindings

ProgressCallback = Callable[[dict[str, Any]], None]


def build_local_base(plan, evidence):
    """Determine mandatory retrieval before any external decision or budget."""
    ids = []
    for requirement in plan.requirements:
        options = [e for e in evidence if requirement.id in e.requirement_ids]
        if options:
            chosen = min(options, key=lambda e: (
                not e.anchor_hit, -e.term_hit_count, e.cost,
                len(e.excerpt.strip()), e.path, e.region, e.candidate_id,
            ))
            if chosen.candidate_id not in ids:
                ids.append(chosen.candidate_id)
    return tuple(ids)


def _selector_failure(exc: Exception) -> dict[str, Any]:
    """Return secret-safe diagnostics while unwrapping the bounded stage wrapper."""
    if isinstance(exc, RoutingStageError):
        error_type = exc.exception_type
        status_code = exc.status_code
        message = exc.safe_message
        category = exc.category
    else:
        error_type = type(exc).__name__
        status = getattr(exc, "code", None)
        status_code = status if isinstance(status, int) else None
        message = f"External selector failed: {error_type}"
        category = "timeout" if isinstance(exc, RoutingTimeout) else "unknown"
    response_failure = category == "provider-response"
    return {
        "selector_error": error_type,
        "selector_error_status_code": status_code,
        "selector_error_message": message,
        "external_error_reason": category,
        "transport_status": "ok" if response_failure else "failed",
        "response_validity": "invalid" if response_failure else "not-applicable",
    }


def _bounded_call(function: Callable[[], Any], timeout: float, stage: str) -> Any:
    """Run a stage behind a hard wall-clock bound without blocking process exit."""
    if timeout <= 0:
        raise RoutingTimeout(stage, f"Routing deadline reached before {stage} started.")
    output: queue.Queue[tuple[str, Any, str]] = queue.Queue(maxsize=1)

    def worker() -> None:
        try:
            output.put(("ok", function(), ""))
        except Exception as exc:  # preserve the worker traceback for --debug and MCP
            output.put(("error", exc, traceback.format_exc()))

    thread = threading.Thread(target=worker, name=f"jev-context-{stage}", daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        raise RoutingTimeout(stage, f"Stage `{stage}` exceeded {timeout:.2f} seconds.")
    kind, payload, trace = output.get_nowait()
    if kind == "error":
        if isinstance(payload, (RoutingTimeout, RoutingLimitExceeded)):
            raise payload
        raise RoutingStageError(stage, payload, trace)
    return payload


class ContextRouter:
    def __init__(self, settings: Settings | None = None, selector: Selector | None = None):
        self.settings = settings or Settings.load()
        self.selector: Selector = selector or (
            JevSelector(self.settings.api_key, self.settings) if self.settings.api_key else LocalSelector()
        )
        self._external_disabled_until = 0.0

    def _record(self, result: RouteResult) -> None:
        path = self.settings.metrics_path
        if not path:
            return
        row = result.to_dict()
        row.pop("context", None)
        row.pop("query", None)
        row["timestamp"] = time.time()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def route(
        self,
        query: str,
        cwd: Path | None = None,
        recent_user_messages: tuple[str, ...] = (),
        force: bool = False,
        progress: ProgressCallback | None = None,
    ) -> RouteResult:
        started = time.perf_counter()
        deadline = started + self.settings.route_timeout_seconds
        stage_seconds: dict[str, float] = {}
        observed_ids: tuple[str, ...] = ()
        cwd = (cwd or Path.cwd()).resolve()

        def emit(stage: str, **details: Any) -> None:
            if not progress:
                return
            event = {"stage": stage, "elapsed_seconds": time.perf_counter() - started, **details}
            try:
                progress(event)
            except Exception:
                pass

        def remaining(cap: float, stage: str) -> float:
            value = min(cap, deadline - time.perf_counter())
            if value <= 0:
                raise RoutingTimeout(stage, "The total routing deadline was exceeded.")
            return value

        def finish(result: RouteResult) -> RouteResult:
            result.metrics.setdefault("seconds", time.perf_counter() - started)
            result.metrics.setdefault("stage_seconds", dict(stage_seconds))
            result.metrics.setdefault("cache", {"hit": False, "status": "disabled"})
            metrics = result.metrics
            if 'ledger' not in metrics:
                boundaries = {key: [] for key in ('indexed', 'shortlisted', 'proposed', 'sent', 'score_valid',
                              'score_missing', 'score_invalid', 'external_selected', 'local_base', 'final_selected', 'considered')}
                boundaries['indexed'] = list(observed_ids)
                boundaries['considered'] = list(observed_ids)
                metrics['ledger'] = {
                    **{key + '_ids': ids for key, ids in boundaries.items()},
                    'counts': {key: len(ids) for key, ids in boundaries.items()},
                    'diagnostics': {key: {'ids': ids[:32], 'truncated': len(ids) > 32} for key, ids in boundaries.items()},
                    'dispositions': [{'id': rid, 'state': 'excluded'} for rid in observed_ids],
                    'response_received': False, 'completion_passes': 0,
                    'source_bytes_billed': 0, 'sent_payload_bytes': 0,
                    'terminal_status': result.status,
                }
            for name in (
                "candidate_ids", "selected_ids", "candidates_sent_ids", "scored_ids",
                "invalid_score_ids", "external_selected_ids", "excluded_budget_ids",
                "excluded_score_ids", "expanded_ids", "rendered_ids", "indexed_symbol_ids",
            ):
                values = metrics.get(name)
                if isinstance(values, list):
                    metrics.setdefault(f"{name}_count", len(values))
                    metrics.setdefault(f"{name}_truncated", len(values) > DIAGNOSTIC_ID_LIMIT)
                    metrics[name] = values[:DIAGNOSTIC_ID_LIMIT]
            metrics.setdefault("index", {
                "seconds": metrics.get("index_seconds"), "mode": metrics.get("index_mode"),
                "files_seen": metrics.get("files_seen"), "files_indexed": metrics.get("files_indexed"),
                "symbols_indexed": metrics.get("symbols_indexed"), "bytes_read": metrics.get("bytes_read"),
                "cache_hit": metrics.get("index_cache_hit", False),
            })
            metrics.setdefault("lexical", {
                "status": metrics.get("lexical_status"), "duration_ms": metrics.get("lexical_duration_ms"),
                "matched_files": metrics.get("lexical_matched_files"), "confidence": metrics.get("lexical_confidence"),
                "fallback": metrics.get("lexical_fallback"), "fallback_reason": metrics.get("lexical_fallback_reason"),
            })
            metrics.setdefault("ranking", {
                "candidates": metrics.get("candidates"), "shortlisted_files": metrics.get("shortlisted_files", []),
            })
            metrics.setdefault("external_selector", {
                "status": metrics.get("external_status"), "provider": metrics.get("external_provider"),
                "transport_status": metrics.get("transport_status"), "response_validity": metrics.get("response_validity"),
                "selection_outcome": metrics.get("external_selection_outcome"),
                "model": metrics.get("model"), "input_tokens": metrics.get("selector_input_tokens", 0),
                "output_tokens": metrics.get("selector_output_tokens", 0),
                "estimated_prompt_tokens": metrics.get("selector_estimated_prompt_tokens", 0),
                "prompt_bytes": metrics.get("selector_prompt_bytes", 0),
                "error_type": metrics.get("selector_error", ""),
                "error_reason": metrics.get("external_error_reason", ""),
            })
            metrics.setdefault("context", {
                "bytes": metrics.get("context_bytes", 0), "estimated_tokens": metrics.get("estimated_context_tokens", 0),
                "selected_files": metrics.get("selected_files", []), "included_files": metrics.get("included_files", []),
                "excluded_low_score_files": metrics.get("excluded_low_score_files", []),
                "inclusion_reasons": metrics.get("inclusion_reasons", {}),
            })
            metrics.setdefault("fallback", {
                "used": metrics.get("fallback_used", False),
                "reason": metrics.get("fallback_reason") or metrics.get("external_error_reason") or metrics.get("lexical_fallback_reason", ""),
                "semantic": {
                    "used": metrics.get("semantic_fallback_used", False),
                    "reason": metrics.get("semantic_fallback_reason", ""),
                },
                "syntax": {
                    "used": any('syntax-fallback' in block.get('precision', ())
                                for block in metrics.get('rendered_evidence', {}).get('blocks', ())),
                    "reason": "adapter-parse-failure" if any('syntax-fallback' in block.get('precision', ())
                                for block in metrics.get('rendered_evidence', {}).get('blocks', ())) else "",
                },
                "local": {
                    "used": metrics.get("local_fallback_used", False),
                    "reason": metrics.get("local_fallback_reason", ""),
                },
            })
            metrics.setdefault("total", {"seconds": metrics["seconds"]})
            emit("complete", status=result.status, metrics=result.metrics)
            self._record(result)
            return result

        repository = None
        partial_metrics: dict[str, Any] = {}
        base_metrics: dict[str, Any] = {
            "model": self.settings.model if isinstance(self.selector, JevSelector) else "local",
            "external_provider": "typesafe-jev" if isinstance(self.selector, JevSelector) else None,
            "metrics_persistence": {
                "enabled": self.settings.metrics_path is not None,
                "status": "configured" if self.settings.metrics_path is not None else "disabled",
            },
        }
        try:
            emit("intent", message="Classifying coding intent")
            intent = classify_code_request(query)
            base_metrics["intent"] = intent
            emit("intent", message="Coding intent classified", **intent)
            if not force and intent["intent"] != "code":
                return finish(RouteResult(
                    "not-code", query, message="No coding action detected.", metrics=dict(base_metrics)
                ))

            stage_started = time.perf_counter()
            emit("discovery", message="Discovering bounded repositories", roots=len(self.settings.workspace_roots))
            repositories = _bounded_call(
                lambda: discover_repositories(self.settings.workspace_roots, self.settings.repository_aliases),
                remaining(self.settings.discovery_timeout_seconds, "discovery"),
                "discovery",
            )
            stage_seconds["discovery"] = time.perf_counter() - stage_started
            emit("discovery", message="Repository discovery complete", repositories=len(repositories))

            repository, resolution = resolve_repository(query, cwd, repositories, recent_user_messages)
            repository_metrics: dict[str, Any] = {
                "resolution": resolution,
                "repositories_considered": len(repositories),
            }
            if repository is None and repositories:
                stage_started = time.perf_counter()
                emit("external-repository-selection", message="Calling Jev with repository metadata only")
                try:
                    repository, semantic = _bounded_call(
                        lambda: self.selector.choose_repository(
                            query, repositories, self.settings.repository_confidence
                        ),
                        remaining(self.settings.external_timeout_seconds, "external-repository-selection"),
                        "external-repository-selection",
                    )
                    repository_metrics.update(semantic)
                    if repository:
                        repository_metrics["resolution"] = "semantic-roster"
                except RoutingTimeout:
                    raise
                except Exception as exc:
                    failure = _selector_failure(exc)
                    repository_metrics.update(failure)
                    emit("external-repository-selection", message=failure["selector_error_message"], **failure)
                stage_seconds["repository_selection"] = time.perf_counter() - stage_started
            if repository is None:
                return finish(RouteResult(
                    "repository-unresolved", query,
                    message="The request looks like code work, but no single repository could be identified safely.",
                    metrics={**base_metrics, "repository": repository_metrics},
                ))

            policy = PathPolicy(max_file_bytes=self.settings.max_file_bytes)
            emit("lexical-search", message="Searching for distinctive literal code signals")
            try:
                lexical = _bounded_call(
                    lambda: retrieve_lexical_candidates(repository, query, self.settings, policy),
                    remaining(self.settings.lexical_timeout_seconds + 0.5, "lexical-search"),
                    "lexical-search",
                )
            except Exception as exc:
                lexical = LexicalResult(
                    "error", reason=f"lexical-error-{type(exc).__name__}"
                )
            query_plan = replace(
                build_query_plan(query),
                network_allowed=isinstance(self.selector, JevSelector) and bool(self.settings.api_key),
            )
            # Lexical concentration is only a fast path for a genuinely local request.
            partial_paths = lexical.paths if lexical.high_confidence and query_plan.scope == "localized" else None
            lexical_metrics: dict[str, Any] = {
                "retrieval_mode": "rg-fast-path" if partial_paths else "structural-full",
                "lexical_status": lexical.status,
                "lexical_seconds": lexical.seconds,
                "lexical_terms_count": lexical.terms_count,
                "lexical_matched_files": lexical.matched_files,
                "lexical_top_score": lexical.top_score,
                "lexical_second_score": lexical.second_score,
                "lexical_confidence": "high" if lexical.high_confidence else "fallback",
                "lexical_fallback_reason": "" if lexical.high_confidence else lexical.reason,
                "lexical_fallback": "" if lexical.high_confidence else "structural-index",
                "lexical_duration_ms": round(lexical.seconds * 1000, 3),
                "jev_skipped": lexical.high_confidence,
            }
            emit(
                "lexical-search",
                message="Using lexical fast path" if partial_paths else "Using structural fallback",
                **lexical_metrics,
            )
            stage_started = time.perf_counter()
            emit("indexing", message="Indexing supported source files", repository=repository.name)
            index_deadline = min(deadline, time.perf_counter() + self.settings.index_timeout_seconds)

            def index_progress(details: dict[str, Any]) -> None:
                partial_metrics.update(details)
                emit("indexing", message="Indexing source files", **details)

            index = _bounded_call(
                lambda: index_repository(
                    repository,
                    policy,
                    include_paths=partial_paths,
                    deadline=index_deadline,
                    max_files=(
                        min(self.settings.max_source_files, self.settings.lexical_max_files)
                        if partial_paths else self.settings.max_source_files
                    ),
                    max_symbols=self.settings.max_symbols,
                    progress=index_progress,
                    cache_path=(
                        cache_path_for(repository, self.settings.index_cache_dir)
                        if self.settings.index_cache_enabled and partial_paths is None else None
                    ),
                ),
                remaining(self.settings.index_timeout_seconds, "indexing"),
                "indexing",
            )
            if partial_paths and not index.candidates:
                emit(
                    "lexical-search",
                    message="Partial index was empty; using structural fallback",
                    lexical_fallback_reason="partial-index-empty",
                )
                partial_paths = None
                lexical_metrics.update({
                    "retrieval_mode": "structural-full",
                    "lexical_confidence": "fallback",
                    "lexical_fallback_reason": "partial-index-empty",
                    "jev_skipped": False,
                })
                index = _bounded_call(
                    lambda: index_repository(
                        repository,
                        policy,
                        deadline=index_deadline,
                        max_files=self.settings.max_source_files,
                        max_symbols=self.settings.max_symbols,
                        progress=index_progress,
                        cache_path=(
                            cache_path_for(repository, self.settings.index_cache_dir)
                            if self.settings.index_cache_enabled else None
                        ),
                    ),
                    remaining(self.settings.index_timeout_seconds, "indexing"),
                    "indexing",
                )
            stage_seconds["indexing"] = time.perf_counter() - stage_started
            index_metrics = dict(index.stats)
            emit("indexing", message="Indexing complete", **index_metrics)
            common_metrics = {
                **base_metrics,
                "repository": repository_metrics,
                **lexical_metrics,
                **index_metrics,
                "index_seconds": stage_seconds["indexing"],
            }
            if not index.candidates:
                return finish(RouteResult(
                    "no-supported-source", query, repository=repository,
                    message="Repository resolved, but no supported source symbols were found.",
                    metrics=common_metrics,
                ))

            observed_ids = tuple(s.id for s in index.candidates)
            stage_started = time.perf_counter()
            emit("ranking", message="Ranking local symbol candidates")
            exhaustive = requires_exhaustive_match(query)
            candidate_limit = (
                len(index.candidates)
                if exhaustive
                else max(
                    self.settings.shortlist_size,
                    min(len(index.candidates), max(self.settings.max_expanded_symbols * 2, len(query_plan.requirements))),
                ) if query_plan.scope == "cross-cutting" else self.settings.shortlist_size
            )
            ranked_candidates = shortlist(query, index, candidate_limit)
            # The ranked list is a preference, not the admissibility boundary:
            # every mandatory obligation gets a local candidate before Jev.
            evidence_pool = [symbol for symbol in index.candidates
                             if evidence_for_symbols(query_plan, (symbol,), index.regions)]
            candidates = diversify_candidates(
                query_plan, list(dict.fromkeys(ranked_candidates + evidence_pool)),
                max(candidate_limit, len(query_plan.requirements))
            )
            stage_seconds["ranking"] = time.perf_counter() - stage_started
            shortlisted_files = sorted({symbol.path for symbol in candidates})
            common_metrics.update({
                "candidates": len(candidates),
                "shortlisted_files": shortlisted_files,
            })
            emit("ranking", message="Local shortlist ready", candidates=len(candidates), files=shortlisted_files)
            if not candidates:
                return finish(RouteResult(
                    "no-candidates", query, repository=repository,
                    message="Repository resolved, but local retrieval found no relevant symbol.",
                    metrics=common_metrics,
                ))

            stage_started = time.perf_counter()
            local_evidence = evidence_for_symbols(query_plan, candidates, index.regions)
            local_base_ids = structural_base(
                query_plan, index, build_local_base(query_plan, local_evidence), self.settings.max_context_chars
            )
            alternatives = {requirement.id: [item for item in local_evidence
                            if requirement.id in item.requirement_ids]
                            for requirement in query_plan.requirements}
            jev_choice = isinstance(self.selector, JevSelector) and any(
                len(items) > 1 for items in alternatives.values()
            )
            if partial_paths:
                selected_ids = tuple(
                    symbol.id for symbol in candidates[: self.settings.max_selected]
                )
                selection = Selection(
                    ids=selected_ids, reason="lexical-fast-path",
                    external_selection_outcome="not-attempted",
                    fallback_used=True, fallback_reason="lexical-fast-path",
                    shortlist_ids=tuple(symbol.id for symbol in candidates[:DIAGNOSTIC_ID_LIMIT]),
                    local_fallback_used=True, local_fallback_reason="lexical-fast-path",
                    diagnostic_counts={"candidate_ids": len(candidates)},
                    diagnostic_truncated={"candidate_ids": len(candidates) > DIAGNOSTIC_ID_LIMIT},
                )
                selector_failure = {
                    "selector_error": "",
                    "selector_error_status_code": None,
                    "selector_error_message": "",
                    "external_error_reason": "",
                }
                emit(
                    "external-selection", message="Skipped external selector after high-confidence lexical match",
                    provider="skipped", model=base_metrics["model"], candidate_count=len(candidates),
                )
            else:
                emit(
                    "external-selection",
                    message="Calling external selector" if isinstance(self.selector, JevSelector) else "Using local selector",
                    provider=base_metrics["external_provider"] or "local",
                    model=base_metrics["model"],
                    candidate_count=len(candidates),
                )
                if (isinstance(self.selector, JevSelector) and
                        time.monotonic() < self._external_disabled_until):
                    selection = LocalSelector().select_symbols(query, repository, candidates, self.settings)
                    selector_failure = {
                        "selector_error": "CircuitOpen",
                        "selector_error_status_code": None,
                        "selector_error_message": "External selector circuit is temporarily open.",
                        "external_error_reason": "circuit-open",
                        "transport_status": "not-attempted",
                        "response_validity": "not-applicable",
                    }
                else:
                    try:
                        selection = _bounded_call(
                            lambda: (
                                self.selector.rerank_alternatives(
                                    query, repository, query_plan.requirements, alternatives, self.settings
                                ) if isinstance(self.selector, JevSelector) and jev_choice
                                else self.selector.select_symbols(query, repository, candidates, self.settings)
                            ),
                            remaining(self.settings.external_timeout_seconds, "external-selection"),
                            "external-selection",
                        )
                    except Exception as exc:
                        selection = LocalSelector().select_symbols(query, repository, candidates, self.settings)
                        selector_failure = _selector_failure(exc)
                        selection = replace(
                            selection,
                            transport_status=selector_failure["transport_status"],
                            response_validity=selector_failure["response_validity"],
                            external_selection_outcome="failed",
                            fallback_reason=selector_failure["external_error_reason"],
                            shortlist_ids=tuple(symbol.id for symbol in candidates),
                        )
                        if isinstance(self.selector, JevSelector):
                            self._external_disabled_until = time.monotonic() + self.settings.circuit_breaker_seconds
                        emit(
                            "external-selection",
                            message=f'{selector_failure["selector_error_message"]}; using local fallback',
                            **selector_failure,
                        )
                    else:
                        selector_failure = {
                            "selector_error": "",
                            "selector_error_status_code": None,
                            "selector_error_message": "",
                            "external_error_reason": "",
                            "transport_status": selection.transport_status,
                            "response_validity": selection.response_validity,
                        }
            stage_seconds["selection"] = time.perf_counter() - stage_started

            # A shortlist or reranker response is not a rendering instruction.
            # The local base is the minimal admissible proof per requirement;
            # optional alternatives remain available for completion but are not
            # injected merely because a local/provider selector considered them.
            # Explicit universal requests are the sole generic exception: every
            # matching candidate is part of the requested result.
            if exhaustive:
                eligible = {item.candidate_id for item in local_evidence}
                selected = [candidate for candidate in candidates if candidate.id in eligible]
            else:
                selected = [index.by_id[item] for item in local_base_ids if item in index.by_id]
            selected = list(dict((item.id, item) for item in selected).values())
            bindings = complete_bindings(query_plan, selected, index)
            emit("expansion", message="Expanding dependencies and tests", selected=len(selected))
            stage_started = time.perf_counter()
            expansion_limit = len(candidates) if exhaustive else self.settings.max_expanded_symbols
            expanded, inclusion_reasons_by_symbol = expand_context_detailed(
                index, selected, query, expansion_limit, preserve_units=bool(bindings)
            )
            # Only the per-requirement local base is mandatory.  Making every
            # locally plausible alternative required turns the shortlist into
            # output and defeats reranking, packing, and negative constraints.
            expanded_ids = {symbol.id for symbol in expanded}
            mandatory_ids = (
                tuple(symbol.id for symbol in expanded)
                if exhaustive
                else tuple(item for item in local_base_ids if item in expanded_ids)
            )
            rendered = render_context_detailed(repository, expanded, self.settings.max_context_chars, required_ids=mandatory_ids)
            context = rendered.context
            included = rendered.complete_ids + rendered.represented_ids + rendered.partial_ids
            stage_seconds["rendering"] = time.perf_counter() - stage_started
            context_bytes = len(context.encode("utf-8"))
            context_tokens = math.ceil(context_bytes / 4)
            rendered_set = set(included)
            initial_packing_losses = sorted({rid for rid in (r.id for r in query_plan.requirements)
                                             if not any(rid in item.requirement_ids and item.candidate_id in rendered_set
                                                        for item in local_evidence)})
            all_evidence = evidence_for_symbols(query_plan, index.candidates, index.regions)
            report = coverage_report(query_plan, index.candidates, rendered, all_evidence, bindings=bindings, packing_losses=initial_packing_losses)
            completion_used = False
            completion_reason = "not-needed"
            if report.status == "insufficient":
                missing_ids = set(report.missing_requirements)
                # One bounded complementary pass over the full local index;
                # this is intentionally broader than the initial shortlist.
                completion_evidence = evidence_for_symbols(query_plan, index.candidates, index.regions)
                completion = [index.by_id[item.candidate_id] for item in completion_evidence
                              if missing_ids.intersection(item.requirement_ids)
                              and (item.anchor_hit or item.term_hit_count >= 2)
                              and item.candidate_id in index.by_id and item.candidate_id not in set(rendered.complete_ids + rendered.represented_ids)]
                all_evidence.extend(completion_evidence)
                if completion:
                    completion_used = True
                    completion_reason = "missing-requirement-local-pass"
                    expanded, extra_reasons = expand_context_detailed(
                        index, selected + completion, query, expansion_limit, preserve_units=bool(bindings)
                    )
                    inclusion_reasons_by_symbol.update(extra_reasons)
                    rendered = render_context_detailed(
                        repository, expanded, self.settings.max_context_chars,
                        required_ids=mandatory_ids + tuple(item.id for item in completion)
                    )
                    context = rendered.context
                    included = rendered.complete_ids + rendered.represented_ids + rendered.partial_ids
                    rendered_set = set(included)
                    report = coverage_report(query_plan, index.candidates, rendered, all_evidence, bindings=bindings, packing_losses=initial_packing_losses)
                else:
                    completion_reason = "no-local-candidate"
            # Recompute every render-derived metric after completion.
            context_bytes = len(context.encode('utf-8'))
            context_tokens = math.ceil(context_bytes / 4)
            selected_files = sorted({symbol.path for symbol in selected})
            included_files = sorted({index.by_id[symbol_id].path for symbol_id in included if symbol_id in index.by_id})
            inclusion_reasons: dict[str, list[str]] = {}
            for symbol_id in included:
                if symbol_id in index.by_id:
                    path = index.by_id[symbol_id].path
                    reason = inclusion_reasons_by_symbol.get(symbol_id, "rendered")
                    inclusion_reasons.setdefault(path, [])
                    if reason not in inclusion_reasons[path]: inclusion_reasons[path].append(reason)
            excluded_low_score_files = sorted(set(shortlisted_files) - set(included_files))
            rendered_set = set(included)
            final_packing_losses = sorted({rid for rid in (r.id for r in query_plan.requirements)
                                           if not any(rid in item.requirement_ids and item.candidate_id in rendered_set
                                                      for item in all_evidence)})
            recovered_packing_losses = sorted(set(initial_packing_losses) - set(final_packing_losses))
            report = coverage_report(query_plan, index.candidates, rendered, all_evidence, bindings=bindings, packing_losses=final_packing_losses)
            metrics = {
                **common_metrics,
                "rendered_evidence": {
                    "blocks": [{"path": b.path, "file_sha256": b.file_sha256,
                                "start_byte": b.span.start_byte, "end_byte": b.span.end_byte,
                                "context_start": b.context_start, "context_end": b.context_end,
                                "region_ids": list(b.region_ids),
                                "precision": sorted({p for rid in b.region_ids
                                    if rid in index.regions_by_id for p in index.regions_by_id[rid].precision}),
                                "adapter_sources": sorted({p for rid in b.region_ids
                                    if rid in index.regions_by_id for p in index.regions_by_id[rid].provenance})}
                               for b in rendered.blocks],
                    "metadata": list(rendered.metadata),
                    "complete_ids": list(rendered.complete_ids), "partial_ids": list(rendered.partial_ids),
                    "represented_ids": list(rendered.represented_ids), "packing_losses": list(rendered.packing_losses),
                },
                "selected": len(selection.ids),
                "expanded": len(expanded),
                "included": len(included),
                "selected_files": selected_files,
                "included_files": included_files,
                "excluded_low_score_files": excluded_low_score_files,
                "inclusion_reasons": inclusion_reasons,
                "selection_reason": selection.reason,
                "transport_status": selector_failure.get("transport_status", selection.transport_status),
                "response_validity": selector_failure.get("response_validity", selection.response_validity),
                "external_selection_outcome": (
                    selection.external_selection_outcome
                    if selection.external_selection_outcome != ""
                    else "not-attempted"
                ),
                "fallback_reason": selection.fallback_reason,
                "semantic_fallback_used": selection.semantic_fallback_used or selection.fallback_reason == "no-external-candidate-above-threshold",
                "semantic_fallback_reason": selection.semantic_fallback_reason or (selection.fallback_reason if selection.fallback_reason == "no-external-candidate-above-threshold" else ""),
                "local_fallback_used": selection.local_fallback_used or selection.fallback_used,
                "local_fallback_reason": selection.local_fallback_reason or ("semantic-fallback" if selection.fallback_used else ""),
                "interpreted_scores": dict(selection.interpreted_scores),
                "candidate_ids": list(selection.shortlist_ids or tuple(symbol.id for symbol in candidates[:DIAGNOSTIC_ID_LIMIT])),
                "selected_ids": list(selection.ids),
                "candidates_sent_ids": list(selection.candidates_sent_ids),
                "scored_ids": list(selection.scored_ids),
                "invalid_score_ids": list(selection.invalid_score_ids),
                "external_selected_ids": list(selection.external_selected_ids),
                "excluded_budget_ids": list(selection.excluded_budget_ids),
                "excluded_score_ids": list(selection.excluded_score_ids),
                "expanded_ids": [symbol.id for symbol in expanded[:DIAGNOSTIC_ID_LIMIT]],
                "rendered_ids": list(included[:DIAGNOSTIC_ID_LIMIT]),
                **{
                    f"{name}_count": selection.diagnostic_counts.get(name, len(values))
                    for name, values in {
                        "candidate_ids": selection.shortlist_ids or tuple(symbol.id for symbol in candidates),
                        "selected_ids": selection.ids,
                        "candidates_sent_ids": selection.candidates_sent_ids,
                        "scored_ids": selection.scored_ids,
                        "invalid_score_ids": selection.invalid_score_ids,
                        "external_selected_ids": selection.external_selected_ids,
                        "excluded_budget_ids": selection.excluded_budget_ids,
                        "excluded_score_ids": selection.excluded_score_ids,
                    }.items()
                },
                **{
                    f"{name}_truncated": selection.diagnostic_truncated.get(name, len(values) > DIAGNOSTIC_ID_LIMIT)
                    for name, values in {
                        "candidate_ids": selection.shortlist_ids or tuple(symbol.id for symbol in candidates),
                        "selected_ids": selection.ids,
                        "candidates_sent_ids": selection.candidates_sent_ids,
                        "scored_ids": selection.scored_ids,
                        "invalid_score_ids": selection.invalid_score_ids,
                        "external_selected_ids": selection.external_selected_ids,
                        "excluded_budget_ids": selection.excluded_budget_ids,
                        "excluded_score_ids": selection.excluded_score_ids,
                    }.items()
                },
                "expanded_ids_count": len(expanded),
                "expanded_ids_truncated": len(expanded) > DIAGNOSTIC_ID_LIMIT,
                "rendered_ids_count": len(included),
                "rendered_ids_truncated": len(included) > DIAGNOSTIC_ID_LIMIT,
                "selector_input_tokens": selection.input_tokens,
                "selector_output_tokens": selection.output_tokens,
                "selector_seconds": selection.seconds,
                "selector_candidates_sent": selection.candidates_sent,
                "selector_prompt_bytes": selection.prompt_bytes,
                "selector_estimated_prompt_tokens": selection.estimated_prompt_tokens,
                "selector_token_ratio": (
                    (selection.input_tokens + selection.output_tokens) / max(1, context_tokens)
                ),
                "external_status": (
                    "failed"
                    if selector_failure.get("transport_status", selection.transport_status) == "failed"
                    else "skipped" if partial_paths else (
                        "not-attempted"
                        if selector_failure.get("transport_status", selection.transport_status) == "not-attempted"
                        else "ok"
                    )
                ),
                "fallback_used": bool(selector_failure["selector_error"]) or selection.fallback_used,
                **selector_failure,
                "context_bytes": context_bytes,
                "estimated_context_tokens": context_tokens,
                "query_scope": query_plan.scope,
                "requirements_planned": report.planned,
                "requirements_covered": report.covered,
                "requirements_missing": report.missing,
                "coverage": {
                    "status": report.status, "covered": list(report.covered_requirements),
                    "missing": list(report.missing_requirements), "reasons": dict(report.reasons),
                    "packing_losses": list(report.packing_losses),
                    "packing_losses_initial": initial_packing_losses,
                    "packing_losses_recovered": recovered_packing_losses,
                    "packing_losses_unrecovered": final_packing_losses,
                },
                "completion": {"used": completion_used, "reason": completion_reason,
                               "attempts": 1 if completion_used else 0},
            }
            rejected: dict[str, str] = {}
            for candidate_id in selection.excluded_score_ids:
                rejected[candidate_id] = "low-score"
            for candidate_id in selection.invalid_score_ids:
                rejected[candidate_id] = "invalid-score"
            for candidate_id in selection.excluded_budget_ids:
                rejected[candidate_id] = "budget"
            selected_ids = set(selection.ids)
            sent_ids = set(selection.candidates_sent_ids)
            # Every shortlisted local candidate must have one terminal trace
            # state. This includes candidates that never crossed the external
            # transport boundary.
            for candidate in candidates:
                candidate_id = candidate.id
                if candidate_id in selected_ids or candidate_id in sent_ids or candidate_id in rejected:
                    continue
                rejected[candidate_id] = "not-selected"
            for candidate_id in selection.candidates_sent_ids:
                if candidate_id not in selected_ids and candidate_id not in rejected:
                    rejected[candidate_id] = "not-selected"
            for candidate_id in selection.ids:
                if candidate_id not in rendered_set:
                    rejected[candidate_id] = "empty-proof"
            trace = SelectionTrace(
                indexed=tuple(s.id for s in index.candidates),
                shortlisted=tuple(s.id for s in candidates),
                sent=tuple(selection.candidates_sent_ids),
                scored=tuple(selection.scored_ids), selected=tuple(selection.ids),
                expanded=tuple(s.id for s in expanded), rendered=tuple(included), rejected=rejected,
            )
            metrics["selection_trace"] = trace.serialized(DIAGNOSTIC_ID_LIMIT)
            boundaries = {
                'indexed': tuple(s.id for s in index.candidates),
                'shortlisted': tuple(s.id for s in candidates),
                'proposed': selection.ledger.get('proposed', tuple(s.id for s in candidates)),
                'sent': selection.ledger.get('sent', selection.candidates_sent_ids),
                'score_valid': selection.ledger.get('score_valid', selection.scored_ids),
                'score_missing': selection.ledger.get('score_missing', ()),
                'score_invalid': selection.ledger.get('score_invalid', selection.invalid_score_ids),
                'external_selected': selection.ledger.get('external_selected', selection.external_selected_ids),
                'local_base': local_base_ids,
                'final_selected': tuple(dict.fromkeys(s.id for s in selected + expanded)),
                'considered': tuple(dict.fromkeys(s.id for s in candidates + selected + expanded)),
            }
            states = {rid: 'complete' for rid in rendered.complete_ids}
            states.update({rid: 'represented' for rid in rendered.represented_ids})
            states.update({rid: 'partial' for rid in rendered.partial_ids})
            metrics['ledger'] = {
                **{key + '_ids': list(ids) for key, ids in boundaries.items()},
                'counts': {key: len(ids) for key, ids in boundaries.items()},
                'diagnostics': {key: {'ids': list(ids[:32]), 'truncated': len(ids) > 32} for key, ids in boundaries.items()},
                'dispositions': [{'id': rid, 'state': states.get(rid, 'excluded')} for rid in boundaries['considered']],
                'response_received': selection.transport_status == 'ok',
                'completion_passes': int(completion_used),
                'source_bytes_billed': sum(b.span.end_byte - b.span.start_byte for b in rendered.blocks),
                'sent_payload_bytes': selection.sent_payload_bytes,
                'prepared_payload_bytes': selection.prepared_payload_bytes,
                'transport_status': selection.transport_status,
                'response_validity': selection.response_validity,
                'external_selection_outcome': selection.external_selection_outcome,
                'semantic_fallback_used': selection.semantic_fallback_used,
                'semantic_fallback_reason': selection.semantic_fallback_reason,
                'local_fallback_used': selection.local_fallback_used,
                'fallback_reason': selection.reason if selection.reason == 'selector-budget-local-fallback' else selection.fallback_reason,
                'scored_ids': list(boundaries['score_valid']),
                'budget_loss': bool(rendered.packing_losses),
            }
            return finish(RouteResult(
                "routed" if report.status == "sufficient" else "insufficient",
                query, repository, context, tuple(item.id for item in selected), included, metrics=metrics,
                message="Mandatory coverage is incomplete in rendered context." if report.missing else "",
            ))
        except RoutingTimeout as exc:
            return finish(RouteResult(
                "timeout", query, repository=repository,
                message=exc.message,
                metrics={
                    **base_metrics,
                    **partial_metrics,
                    "timeout_stage": exc.stage,
                    "error_type": type(exc).__name__,
                },
            ))
        except RoutingLimitExceeded as exc:
            return finish(RouteResult(
                "limit-exceeded", query, repository=repository,
                message=exc.message,
                metrics={
                    **base_metrics,
                    "limit_stage": exc.stage,
                    "error_type": type(exc).__name__,
                },
            ))
        except RoutingStageError as exc:
            emit("error", stage_failed=exc.stage, error_type=exc.exception_type, traceback=exc.trace)
            return finish(RouteResult(
                "error", query, repository=repository,
                message=exc.message,
                metrics={
                    **base_metrics,
                    "error_stage": exc.stage,
                    "error_type": exc.exception_type,
                    "traceback": exc.trace,
                },
            ))
        except Exception as exc:
            trace = traceback.format_exc()
            emit("error", stage_failed="routing", error_type=type(exc).__name__, traceback=trace)
            return finish(RouteResult(
                "error", query, repository=repository,
                message=str(exc) or type(exc).__name__,
                metrics={
                    **base_metrics,
                    "error_stage": "routing",
                    "error_type": type(exc).__name__,
                    "traceback": trace,
                },
            ))
