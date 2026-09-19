from __future__ import annotations

import json
import math
import queue
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Callable

from .config import Settings
from .discovery import discover_repositories, resolve_repository
from .errors import RoutingLimitExceeded, RoutingStageError, RoutingTimeout
from .index import cache_path_for, index_repository
from .intent import classify_code_request
from .lexical import LexicalResult, retrieve_lexical_candidates
from .models import RouteResult, Selection
from .providers import JevSelector, LocalSelector, Selector
from .ranking import expand_context_detailed, shortlist
from .render import render_context
from .security import PathPolicy

ProgressCallback = Callable[[dict[str, Any]], None]


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
    return {
        "selector_error": error_type,
        "selector_error_status_code": status_code,
        "selector_error_message": message,
        "external_error_reason": category,
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
                "reason": metrics.get("external_error_reason") or metrics.get("lexical_fallback_reason", ""),
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
            partial_paths = lexical.paths if lexical.high_confidence else None
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
            if partial_paths and not index.symbols:
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
            if not index.symbols:
                return finish(RouteResult(
                    "no-supported-source", query, repository=repository,
                    message="Repository resolved, but no supported source symbols were found.",
                    metrics=common_metrics,
                ))

            stage_started = time.perf_counter()
            emit("ranking", message="Ranking local symbol candidates")
            candidates = shortlist(query, index, self.settings.shortlist_size)
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
            if partial_paths:
                selected_ids = tuple(
                    symbol.id for symbol in candidates[: self.settings.max_selected]
                )
                selection = Selection(selected_ids, reason="lexical-fast-path")
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
                if isinstance(self.selector, JevSelector) and time.monotonic() < self._external_disabled_until:
                    selection = LocalSelector().select_symbols(query, repository, candidates, self.settings)
                    selector_failure = {
                        "selector_error": "CircuitOpen",
                        "selector_error_status_code": None,
                        "selector_error_message": "External selector circuit is temporarily open.",
                        "external_error_reason": "circuit-open",
                    }
                else:
                    try:
                        selection = _bounded_call(
                            lambda: self.selector.select_symbols(query, repository, candidates, self.settings),
                            remaining(self.settings.external_timeout_seconds, "external-selection"),
                            "external-selection",
                        )
                    except Exception as exc:
                        selection = LocalSelector().select_symbols(query, repository, candidates, self.settings)
                        selector_failure = _selector_failure(exc)
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
                        }
            stage_seconds["selection"] = time.perf_counter() - stage_started

            selected = [index.by_id[symbol_id] for symbol_id in selection.ids if symbol_id in index.by_id]
            emit("expansion", message="Expanding dependencies and tests", selected=len(selected))
            stage_started = time.perf_counter()
            expanded, inclusion_reasons_by_symbol = expand_context_detailed(
                index, selected, query, self.settings.max_expanded_symbols
            )
            context, included = render_context(repository, expanded, self.settings.max_context_chars)
            stage_seconds["rendering"] = time.perf_counter() - stage_started
            context_bytes = len(context.encode("utf-8"))
            context_tokens = math.ceil(context_bytes / 4)
            selected_files = sorted({symbol.path for symbol in selected})
            included_files = sorted({index.by_id[symbol_id].path for symbol_id in included if symbol_id in index.by_id})
            inclusion_reasons: dict[str, list[str]] = {}
            for symbol_id in included:
                if symbol_id in index.by_id:
                    path = index.by_id[symbol_id].path
                    reason = inclusion_reasons_by_symbol.get(symbol_id, "rendered")
                    inclusion_reasons.setdefault(path, [])
                    if reason not in inclusion_reasons[path]:
                        inclusion_reasons[path].append(reason)
            excluded_low_score_files = sorted(set(shortlisted_files) - set(included_files))
            metrics = {
                **common_metrics,
                "selected": len(selection.ids),
                "expanded": len(expanded),
                "included": len(included),
                "selected_files": selected_files,
                "included_files": included_files,
                "excluded_low_score_files": excluded_low_score_files,
                "inclusion_reasons": inclusion_reasons,
                "selection_reason": selection.reason,
                "selector_input_tokens": selection.input_tokens,
                "selector_output_tokens": selection.output_tokens,
                "selector_seconds": selection.seconds,
                "selector_candidates_sent": selection.candidates_sent,
                "selector_prompt_bytes": selection.prompt_bytes,
                "selector_estimated_prompt_tokens": selection.estimated_prompt_tokens,
                "selector_token_ratio": (
                    (selection.input_tokens + selection.output_tokens) / max(1, context_tokens)
                ),
                "external_status": "failed" if selector_failure["selector_error"] else (
                    "skipped" if partial_paths else "ok"
                ),
                "fallback_used": bool(selector_failure["selector_error"]),
                **selector_failure,
                "context_bytes": context_bytes,
                "estimated_context_tokens": context_tokens,
            }
            return finish(RouteResult(
                "routed", query, repository, context, selection.ids, included, metrics=metrics
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
