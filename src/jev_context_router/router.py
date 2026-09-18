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
from .index import index_repository
from .intent import is_code_request
from .models import RouteResult
from .providers import JevSelector, LocalSelector, Selector
from .ranking import expand_context, shortlist
from .render import render_context
from .security import PathPolicy

ProgressCallback = Callable[[dict[str, Any]], None]


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

    def _record(self, result: RouteResult) -> None:
        path = self.settings.metrics_path
        if not path:
            return
        row = result.to_dict()
        row.pop("context", None)
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
            emit("complete", status=result.status, metrics=result.metrics)
            self._record(result)
            return result

        repository = None
        partial_metrics: dict[str, Any] = {}
        base_metrics: dict[str, Any] = {
            "model": self.settings.model if isinstance(self.selector, JevSelector) else "local",
            "external_provider": "typesafe-jev" if isinstance(self.selector, JevSelector) else None,
        }
        try:
            emit("intent", message="Classifying coding intent")
            if not force and not is_code_request(query):
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
                    repository_metrics["selector_error"] = type(exc).__name__
                stage_seconds["repository_selection"] = time.perf_counter() - stage_started
            if repository is None:
                return finish(RouteResult(
                    "repository-unresolved", query,
                    message="The request looks like code work, but no single repository could be identified safely.",
                    metrics={**base_metrics, "repository": repository_metrics},
                ))

            policy = PathPolicy(max_file_bytes=self.settings.max_file_bytes)
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
                    deadline=index_deadline,
                    max_files=self.settings.max_source_files,
                    max_symbols=self.settings.max_symbols,
                    progress=index_progress,
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
            emit(
                "external-selection",
                message="Calling external selector" if isinstance(self.selector, JevSelector) else "Using local selector",
                provider=base_metrics["external_provider"] or "local",
                model=base_metrics["model"],
                candidate_count=len(candidates),
            )
            try:
                selection = _bounded_call(
                    lambda: self.selector.select_symbols(query, repository, candidates, self.settings),
                    remaining(self.settings.external_timeout_seconds, "external-selection"),
                    "external-selection",
                )
                selector_error = ""
            except RoutingTimeout:
                raise
            except Exception as exc:
                selection = LocalSelector().select_symbols(query, repository, candidates, self.settings)
                selector_error = type(exc).__name__
                emit("external-selection", message="Selector failed; using local fallback", error=selector_error)
            stage_seconds["selection"] = time.perf_counter() - stage_started

            selected = [index.by_id[symbol_id] for symbol_id in selection.ids if symbol_id in index.by_id]
            emit("expansion", message="Expanding dependencies and tests", selected=len(selected))
            stage_started = time.perf_counter()
            expanded = expand_context(index, selected, query, self.settings.max_expanded_symbols)
            context, included = render_context(repository, expanded, self.settings.max_context_chars)
            stage_seconds["rendering"] = time.perf_counter() - stage_started
            context_bytes = len(context.encode("utf-8"))
            selected_files = sorted({symbol.path for symbol in selected})
            included_files = sorted({index.by_id[symbol_id].path for symbol_id in included if symbol_id in index.by_id})
            metrics = {
                **common_metrics,
                "selected": len(selection.ids),
                "expanded": len(expanded),
                "included": len(included),
                "selected_files": selected_files,
                "included_files": included_files,
                "selection_reason": selection.reason,
                "selector_input_tokens": selection.input_tokens,
                "selector_output_tokens": selection.output_tokens,
                "selector_seconds": selection.seconds,
                "selector_error": selector_error,
                "context_bytes": context_bytes,
                "estimated_context_tokens": math.ceil(context_bytes / 4),
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
