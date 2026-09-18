from __future__ import annotations

import asyncio
import copy
import time
import traceback
from dataclasses import replace
from pathlib import Path
from typing import Any

from .config import Settings
from .router import ContextRouter


_STAGE_PROGRESS = {
    "intent": 2,
    "discovery": 12,
    "external-repository-selection": 22,
    "indexing": 40,
    "ranking": 58,
    "external-selection": 70,
    "expansion": 86,
    "error": 100,
    "complete": 100,
}


def _error_payload(exc: Exception, started: float) -> dict[str, Any]:
    return {
        "status": "error",
        "query": "",
        "repository": None,
        "repository_name": None,
        "context": "",
        "selected_symbols": [],
        "included_symbols": [],
        "metrics": {
            "seconds": time.perf_counter() - started,
            "error_type": type(exc).__name__,
            "traceback": traceback.format_exc(),
        },
        "message": str(exc) or type(exc).__name__,
    }


def create_server(config_path: str | None = None):
    from mcp.server import MCPServer
    from mcp.server.mcpserver import Context
    globals()["Context"] = Context

    server = MCPServer("Jev Context Router")

    @server.tool()
    async def route_code_context(
        query: str,
        ctx: Context,
        cwd: str = ".",
        timeout_seconds: float | None = None,
        debug: bool = False,
    ) -> dict[str, Any]:
        """Select bounded repository context and return structured status, metrics, and errors."""
        started = time.perf_counter()
        events: list[dict[str, Any]] = []
        loop = asyncio.get_running_loop()

        def progress(event: dict[str, Any]) -> None:
            if debug:
                # The final event contains result.metrics. Copy it before the
                # debug event list is attached to those metrics, otherwise the
                # MCP serializer observes a reference cycle.
                events.append(copy.deepcopy(event))
            current = _STAGE_PROGRESS.get(str(event.get("stage")), 50)
            message = str(event.get("message") or event.get("stage") or "routing")
            future = asyncio.run_coroutine_threadsafe(
                ctx.report_progress(current, total=100, message=message),
                loop,
            )
            try:
                future.result(timeout=2)
            except Exception:
                pass

        try:
            root = Path(cwd).expanduser().resolve()
            settings = Settings.load(Path(config_path).resolve() if config_path else None, root)
            if timeout_seconds is not None:
                settings = replace(settings, route_timeout_seconds=timeout_seconds)
            result = await asyncio.to_thread(
                ContextRouter(settings).route,
                query,
                root,
                (),
                True,
                progress,
            )
            payload = result.to_dict()
            if debug:
                payload["metrics"]["debug_events"] = events
            return payload
        except Exception as exc:
            payload = _error_payload(exc, started)
            if debug:
                payload["metrics"]["debug_events"] = events
            try:
                await ctx.report_progress(100, total=100, message=f"error: {type(exc).__name__}")
            except Exception:
                pass
            return payload

    @server.tool()
    def discover_code_repositories(cwd: str = ".") -> str:
        """List repository names visible to the router, without reading source bodies."""
        from .discovery import discover_repositories
        root = Path(cwd).expanduser().resolve()
        settings = Settings.load(Path(config_path).resolve() if config_path else None, root)
        repositories = discover_repositories(settings.workspace_roots, settings.repository_aliases)
        return "\n".join(f"- {repository.name}: {', '.join(repository.languages) or 'unknown'}" for repository in repositories)

    return server


def run(config_path: str | None = None) -> None:
    create_server(config_path).run()
