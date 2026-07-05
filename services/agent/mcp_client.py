import asyncio
import os
from typing import Any, Dict

from fastmcp import Client

IMG_PROC_MCP_URL = os.environ.get("IMG_PROC_MCP_URL", "http://localhost:9000/mcp")


def call_mcp_tool(tool_name: str, arguments: Dict[str, Any]) -> Any:
    """Synchronously invoke a tool on the img-proc-mcp server and return its structured result."""
    async def _call():
        async with Client(IMG_PROC_MCP_URL) as client:
            result = await client.call_tool(tool_name, arguments)
            return result.data

    return asyncio.run(_call())
