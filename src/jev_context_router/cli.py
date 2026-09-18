from __future__ import annotations

import argparse
import json
import sys
import traceback
from dataclasses import replace
from pathlib import Path

from .config import Settings
from .discovery import discover_repositories
from .router import ContextRouter


def _settings(args, cwd: Path) -> Settings:
    return Settings.load(Path(args.config).resolve() if getattr(args, "config", None) else None, cwd)


def command_route(args) -> int:
    cwd = Path(args.cwd).expanduser().resolve()
    settings = _settings(args, cwd)
    if args.timeout is not None:
        settings = replace(settings, route_timeout_seconds=args.timeout)

    def debug_event(event: dict) -> None:
        print(json.dumps(event, ensure_ascii=False), file=sys.stderr, flush=True)

    result = ContextRouter(settings).route(
        args.query,
        cwd=cwd,
        force=args.force,
        progress=debug_event if args.debug or args.verbose else None,
    )
    if args.format == "json":
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    elif result.context:
        print(result.context)
    else:
        print(json.dumps({"status": result.status, "message": result.message}, ensure_ascii=False))
    if result.status in {"routed", "not-code"}:
        return 0
    if result.status == "timeout":
        return 3
    return 2


def command_discover(args) -> int:
    cwd = Path(args.cwd).expanduser().resolve()
    settings = _settings(args, cwd)
    repositories = discover_repositories(settings.workspace_roots, settings.repository_aliases)
    print(json.dumps([
        {"name": repo.name, "root": str(repo.root), "aliases": repo.aliases, "languages": repo.languages, "description": repo.description}
        for repo in repositories
    ], ensure_ascii=False, indent=2))
    return 0


def command_claude_hook(args) -> int:
    payload = json.load(sys.stdin)
    cwd = Path(payload.get("cwd") or ".").expanduser().resolve()
    prompt = str(payload.get("prompt") or "")
    result = ContextRouter(_settings(args, cwd)).route(prompt, cwd=cwd)
    context = result.context
    if result.status == "repository-unresolved":
        context = '<code_context status="repository-unresolved">Ask the user which repository this coding request targets before exploring files.</code_context>'
    output = {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": context}} if context else {}
    print(json.dumps(output, ensure_ascii=False))
    return 0


def command_mcp(args) -> int:
    try:
        from .mcp_server import run
    except ImportError as exc:
        print("Install the MCP extra: pip install 'jev-context-router[mcp]'", file=sys.stderr)
        return 2
    run(config_path=args.config)
    return 0


def command_install(args) -> int:
    from .installers import install_claude, install_codex, install_hermes
    if args.target == "hermes":
        import os
        home = Path(args.home or os.environ.get("HERMES_HOME", "~/.hermes"))
        print(install_hermes(home, enable=args.apply))
    elif args.target == "claude":
        print(install_claude(Path(args.project or ".")))
    else:
        print(install_codex(apply=args.apply))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jev-context", description="Repository-aware context routing for coding agents.")
    sub = parser.add_subparsers(dest="command", required=True)
    route = sub.add_parser("route", help="Route one coding request and print selected context.")
    route.add_argument("query")
    route.add_argument("--cwd", default=".")
    route.add_argument("--config")
    route.add_argument("--format", choices=("context", "json"), default="context")
    route.add_argument("--force", action="store_true")
    route.add_argument("--timeout", type=float, help="Total wall-clock timeout in seconds (default: configuration value).")
    route.add_argument("--debug", action="store_true", help="Stream JSON progress and full errors to stderr.")
    route.add_argument("--verbose", action="store_true", help="Alias for --debug.")
    route.set_defaults(func=command_route)
    discover = sub.add_parser("discover", help="List repositories visible under configured workspace roots.")
    discover.add_argument("--cwd", default=".")
    discover.add_argument("--config")
    discover.set_defaults(func=command_discover)
    hook = sub.add_parser("claude-hook", help="Claude Code UserPromptSubmit hook over stdin/stdout JSON.")
    hook.add_argument("--config")
    hook.set_defaults(func=command_claude_hook)
    mcp = sub.add_parser("mcp-serve", help="Run the optional stdio MCP server for Codex and other clients.")
    mcp.add_argument("--config")
    mcp.set_defaults(func=command_mcp)
    install = sub.add_parser("install", help="Install an adapter for Hermes, Claude Code, or Codex.")
    install.add_argument("target", choices=("hermes", "claude", "codex"))
    install.add_argument("--home", help="Hermes home (default: HERMES_HOME or ~/.hermes).")
    install.add_argument("--project", help="Claude Code project directory.")
    install.add_argument("--apply", action="store_true", help="Also run the platform enable/register command when available.")
    install.set_defaults(func=command_install)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except Exception as exc:
        payload = {
            "status": "error",
            "repository": None,
            "context": "",
            "selected_symbols": [],
            "included_symbols": [],
            "metrics": {"seconds": 0.0, "error_type": type(exc).__name__},
            "message": str(exc) or type(exc).__name__,
        }
        if getattr(args, "format", None) == "json":
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(json.dumps({"status": "error", "message": payload["message"]}, ensure_ascii=False))
        if getattr(args, "debug", False) or getattr(args, "verbose", False):
            traceback.print_exc(file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
