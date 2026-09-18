from __future__ import annotations

import json
import time
from pathlib import Path

from .config import Settings
from .discovery import discover_repositories, resolve_repository
from .index import index_repository
from .intent import is_code_request
from .models import RouteResult
from .providers import JevSelector, LocalSelector, Selector
from .ranking import expand_context, shortlist
from .render import render_context
from .security import PathPolicy


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
    ) -> RouteResult:
        started = time.perf_counter()
        cwd = (cwd or Path.cwd()).resolve()
        if not force and not is_code_request(query):
            result = RouteResult("not-code", query, message="No coding action detected.", metrics={"seconds": time.perf_counter() - started})
            self._record(result)
            return result

        repositories = discover_repositories(self.settings.workspace_roots, self.settings.repository_aliases)
        repository, resolution = resolve_repository(query, cwd, repositories, recent_user_messages)
        repository_metrics: dict = {"resolution": resolution, "repositories_considered": len(repositories)}
        if repository is None and repositories:
            try:
                repository, semantic = self.selector.choose_repository(query, repositories, self.settings.repository_confidence)
                repository_metrics.update(semantic)
                if repository:
                    repository_metrics["resolution"] = "semantic-roster"
            except Exception as exc:
                repository_metrics["selector_error"] = type(exc).__name__
        if repository is None:
            result = RouteResult(
                "repository-unresolved", query,
                message="The request looks like code work, but no single repository could be identified safely.",
                metrics={"repository": repository_metrics, "seconds": time.perf_counter() - started},
            )
            self._record(result)
            return result

        policy = PathPolicy(max_file_bytes=self.settings.max_file_bytes)
        index_started = time.perf_counter()
        index = index_repository(repository, policy)
        index_seconds = time.perf_counter() - index_started
        if not index.symbols:
            result = RouteResult(
                "no-supported-source", query, repository=repository,
                message="Repository resolved, but no supported source symbols were found.",
                metrics={"repository": repository_metrics, "symbols_indexed": 0, "index_seconds": index_seconds, "seconds": time.perf_counter() - started},
            )
            self._record(result)
            return result

        candidates = shortlist(query, index, self.settings.shortlist_size)
        if not candidates:
            result = RouteResult(
                "no-candidates", query, repository=repository,
                message="Repository resolved, but local retrieval found no relevant symbol.",
                metrics={"repository": repository_metrics, "symbols_indexed": len(index.symbols), "index_seconds": index_seconds, "seconds": time.perf_counter() - started},
            )
            self._record(result)
            return result
        try:
            selection = self.selector.select_symbols(query, repository, candidates, self.settings)
        except Exception as exc:
            selection = LocalSelector().select_symbols(query, repository, candidates, self.settings)
            selector_error = type(exc).__name__
        else:
            selector_error = ""
        selected = [index.by_id[symbol_id] for symbol_id in selection.ids if symbol_id in index.by_id]
        expanded = expand_context(index, selected, query, self.settings.max_expanded_symbols)
        context, included = render_context(repository, expanded, self.settings.max_context_chars)
        result = RouteResult(
            "routed", query, repository, context, selection.ids, included,
            metrics={
                "repository": repository_metrics,
                "symbols_indexed": len(index.symbols),
                "candidates": len(candidates),
                "selected": len(selection.ids),
                "expanded": len(expanded),
                "included": len(included),
                "selection_reason": selection.reason,
                "selector_input_tokens": selection.input_tokens,
                "selector_output_tokens": selection.output_tokens,
                "selector_seconds": selection.seconds,
                "selector_error": selector_error,
                "index_seconds": index_seconds,
                "seconds": time.perf_counter() - started,
            },
        )
        self._record(result)
        return result
