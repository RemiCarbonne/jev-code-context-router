from __future__ import annotations

from pathlib import Path

from .config import Settings
from .router import ContextRouter


def create_server(config_path: str | None = None):
    from mcp.server import MCPServer

    server = MCPServer("Jev Context Router")

    @server.tool()
    def route_code_context(query: str, cwd: str = ".") -> str:
        """Select repository source context before broadly exploring a coding project."""
        root = Path(cwd).expanduser().resolve()
        settings = Settings.load(Path(config_path).resolve() if config_path else None, root)
        result = ContextRouter(settings).route(query, cwd=root, force=True)
        if result.context:
            return result.context
        return f'<code_context status="{result.status}">{result.message}</code_context>'

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
