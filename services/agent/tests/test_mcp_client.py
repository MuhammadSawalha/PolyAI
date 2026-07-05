import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("AWS_S3_BUCKET", "fake-bucket")

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import mcp_client


@patch("mcp_client.Client")
def test_call_mcp_tool_success(mock_client_class):
    fake_result = MagicMock()
    fake_result.data = {"output_s3_key": "chat-1/pred-1/edited/blur.png", "width": 10, "height": 10}

    mock_client_instance = AsyncMock()
    mock_client_instance.call_tool.return_value = fake_result
    mock_client_class.return_value.__aenter__.return_value = mock_client_instance
    mock_client_class.return_value.__aexit__.return_value = None

    result = mcp_client.call_mcp_tool("blur", {"image_s3_key": "chat-1/pred-1/original/image.jpg", "radius": 2.0})

    assert result == {"output_s3_key": "chat-1/pred-1/edited/blur.png", "width": 10, "height": 10}
    mock_client_instance.call_tool.assert_awaited_once_with(
        "blur", {"image_s3_key": "chat-1/pred-1/original/image.jpg", "radius": 2.0}
    )


@patch("mcp_client.Client")
def test_call_mcp_tool_propagates_errors(mock_client_class):
    mock_client_instance = AsyncMock()
    mock_client_instance.call_tool.side_effect = RuntimeError("tool failed")
    mock_client_class.return_value.__aenter__.return_value = mock_client_instance
    mock_client_class.return_value.__aexit__.return_value = None

    try:
        mcp_client.call_mcp_tool("blur", {"image_s3_key": "key"})
        assert False, "expected RuntimeError to propagate"
    except RuntimeError as exc:
        assert "tool failed" in str(exc)


def test_default_url_from_env(monkeypatch):
    monkeypatch.setenv("IMG_PROC_MCP_URL", "http://img-proc-mcp:9000/mcp")
    import importlib
    importlib.reload(mcp_client)
    try:
        assert mcp_client.IMG_PROC_MCP_URL == "http://img-proc-mcp:9000/mcp"
    finally:
        monkeypatch.undo()
        importlib.reload(mcp_client)
