import asyncio

import pytest

pytest.importorskip("mcp")
from mcp import Client

from jev_context_router.mcp_server import create_server


def test_mcp_server_exposes_and_executes_repository_tools(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n")

    async def scenario():
        async with Client(create_server()) as client:
            tools = await client.list_tools()
            names = {tool.name for tool in tools.tools}
            assert names == {"route_code_context", "discover_code_repositories"}
            result = await client.call_tool("discover_code_repositories", {"cwd": str(tmp_path)})
            assert not result.is_error
            assert tmp_path.name in result.structured_content["result"]

    asyncio.run(scenario())


def test_mcp_route_returns_structured_metrics_and_progress(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "Root.tsx").write_text(
        "export const Root = () => <Composition id=\"One\" />;\n"
    )
    progress = []

    async def on_progress(current, total, message):
        progress.append((current, total, message))

    async def scenario():
        async with Client(create_server()) as client:
            result = await client.call_tool(
                "route_code_context",
                {"query": "Refactor src/Root.tsx and identify the exact symbols", "cwd": str(tmp_path)},
                progress_callback=on_progress,
            )
            assert not result.is_error
            payload = result.structured_content
            assert payload["status"] == "routed"
            assert payload["metrics"]["files_indexed"] == 1
            assert payload["metrics"]["seconds"] >= 0

    asyncio.run(scenario())
    assert progress
    assert progress[-1][0] == 100
