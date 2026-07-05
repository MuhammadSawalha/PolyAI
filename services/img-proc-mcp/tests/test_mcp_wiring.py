import asyncio

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

import app

IMAGE_S3_KEY = "chat123/pred456/original/image.jpg"


def test_blur_tool_is_registered_and_callable(mock_s3):
    async def _run():
        async with Client(app.mcp) as client:
            return await client.call_tool("blur", {"image_s3_key": IMAGE_S3_KEY, "radius": 2.0})

    result = asyncio.run(_run())
    assert "output_s3_key" in result.data
    assert result.data["output_s3_key"] in mock_s3


def test_all_six_tools_are_registered():
    async def _run():
        async with Client(app.mcp) as client:
            return await client.list_tools()

    tools = asyncio.run(_run())
    names = {tool.name for tool in tools}
    assert names == {"rotate", "flip", "blur", "resize", "crop", "add_noise"}


def test_tool_error_surfaces_through_mcp_transport(mock_s3):
    async def _run():
        async with Client(app.mcp) as client:
            await client.call_tool("flip", {"image_s3_key": IMAGE_S3_KEY, "direction": "diagonal"})

    with pytest.raises(ToolError):
        asyncio.run(_run())
