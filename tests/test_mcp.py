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
