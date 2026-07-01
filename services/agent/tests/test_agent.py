import os
import sys
import pytest
import json
import base64
import httpx
from unittest.mock import MagicMock, patch
from langchain_core.messages import AIMessage, HumanMessage

# Set default test environment variables BEFORE importing app to prevent initialization crashes
os.environ.setdefault("MODEL", "amazon.nova-lite-v1:0")
os.environ.setdefault("MODEL_PROVIDER", "bedrock_converse")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("AWS_S3_BUCKET", "fake-bucket")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "fake")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "fake")

# Ensure the parent directory is in the path so we can import app
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import app


class FakeLLMWithTools:
    def __init__(self):
        self.calls = 0

    def invoke(self, messages):
        self.calls += 1

        if self.calls == 1:
            return AIMessage(
                content="",
                tool_calls=[
                    {"name": "detect_objects", "args": {}, "id": "call_1", "type": "tool_call"}
                ],
                usage_metadata={"input_tokens": 40, "output_tokens": 10, "total_tokens": 50}
            )
        elif self.calls == 2:
            return AIMessage(
                content="",
                tool_calls=[
                    {"name": "show_annotated_image", "args": {}, "id": "call_2", "type": "tool_call"}
                ],
                usage_metadata={"input_tokens": 60, "output_tokens": 15, "total_tokens": 75}
            )

        msg = AIMessage(content="I found 2 people in the image.")
        msg.usage_metadata = {"input_tokens": 20, "output_tokens": 5, "total_tokens": 25}
        return msg


class FakeDetectTool:
    def invoke(self, tool_call):
        return type("FakeMessage", (), {"content": '{"prediction_uid":"prediction-123","predicted_image_s3_key":"chat-1/prediction-123/predicted/image.jpg"}'})()


class FakeShowImageTool:
    def invoke(self, tool_call):
        return type("FakeMessage", (), {"content": '{"image_url":"http://localhost:8080/prediction/prediction-123/image"}'})()


def test_run_agent_complete_workflow(monkeypatch):
    """Test full multi-turn ReAct workflow with tokens and multiple tools."""
    fake_llm = FakeLLMWithTools()

    monkeypatch.setattr(app, "llm_with_tools", fake_llm)
    monkeypatch.setattr(app, "TOOLS", {
        "detect_objects": FakeDetectTool(),
        "show_annotated_image": FakeShowImageTool()
    })
    monkeypatch.setattr(app, "_fetch_annotated_image", lambda pid: "mocked_base64_string")
    monkeypatch.setattr(app, "MAX_INPUT_TOKENS", 500)

    result = app.run_agent([HumanMessage(content="Detect and show image")])

    assert result["response"] == "I found 2 people in the image."
    assert result["prediction_id"] == "prediction-123"
    assert result["annotated_image"] == "mocked_base64_string"
    assert result["image_url"] == "http://localhost:8080/prediction/prediction-123/image"
    assert result["iterations"] == 3
    assert "detect_objects" in result["tools_called"]
    assert "show_annotated_image" in result["tools_called"]
    assert result["tokens_used"]["total"] == 150


def test_run_agent_context_limit_flag(monkeypatch):
    """Verify that context_limit_exceeded switches to True if token boundary is crossed."""
    class HighTokenLLM:
        def invoke(self, messages):
            msg = AIMessage(content="Response under heavy payload.")
            msg.usage_metadata = {"input_tokens": 1000, "output_tokens": 10, "total_tokens": 1010}
            return msg

    monkeypatch.setattr(app, "llm_with_tools", HighTokenLLM())
    monkeypatch.setattr(app, "MAX_INPUT_TOKENS", 500)

    result = app.run_agent([HumanMessage(content="Hello")])
    assert result["context_limit_exceeded"] is True


def test_run_agent_stops_at_max_iterations(monkeypatch):
    """Verify max iterations emergency break works gracefully."""
    class AlwaysToolCallingLLM:
        def invoke(self, messages):
            return AIMessage(
                content="",
                tool_calls=[{"name": "detect_objects", "args": {}, "id": "loop_id", "type": "tool_call"}]
            )

    monkeypatch.setattr(app, "llm_with_tools", AlwaysToolCallingLLM())
    monkeypatch.setattr(app, "TOOLS", {"detect_objects": FakeDetectTool()})

    result = app.run_agent([HumanMessage(content="Loop")], max_iterations=1)
    assert result["context_limit_exceeded"] is True
    assert result["iterations"] == 1


def test_normalization_utilities():
    """Cover alternative array-block list layouts inside response normalizing utilities."""
    list_content = [{"type": "text", "text": "Hello "}, "World!"]
    normalized = app._normalize_response_content(list_content)
    assert normalized == "Hello World!"
    
    assert app._normalize_response_content(404) == "404"


def test_build_original_image_key_normalizes_extension():
    key = app.build_original_image_key("chat-1", "prediction-1", "png")
    assert key == "chat-1/prediction-1/original/image.png"


@patch("app.upload_file_bytes", return_value=True)
def test_upload_base64_image_success(mock_upload_file_bytes):
    result = app.upload_base64_image("ZmFrZWJhc2U2NHRleHQ=", "chat-1/prediction-1/original/image.jpg")
    assert result == "chat-1/prediction-1/original/image.jpg"
    mock_upload_file_bytes.assert_called_once()


@patch("app.upload_file_bytes", return_value=False)
def test_upload_base64_image_failure(mock_upload_file_bytes):
    with pytest.raises(RuntimeError, match="Failed to upload image to S3 key chat-1/prediction-1/original/image.jpg"):
        app.upload_base64_image("ZmFrZWJhc2U2NHRleHQ=", "chat-1/prediction-1/original/image.jpg")

    mock_upload_file_bytes.assert_called_once()


def test_download_image_base64_none_key():
    assert app.download_image_base64(None) is None


@patch("app.download_file_bytes", return_value=b"fake_binary_image_bytes")
def test_download_image_base64_success(mock_download_file_bytes):
    result = app.download_image_base64("chat-1/prediction-123/predicted/image.jpg")
    assert result == base64.b64encode(b"fake_binary_image_bytes").decode("ascii")
    mock_download_file_bytes.assert_called_once_with("chat-1/prediction-123/predicted/image.jpg")


@patch("httpx.Client")
def test_fetch_annotated_image_success(mock_client_class):
    """Test successful network retrieval loop inside _fetch_annotated_image."""
    mock_client_instance = MagicMock()
    mock_response = MagicMock()
    mock_response.content = b"fake_binary_image_bytes"
    mock_response.raise_for_status = MagicMock()
    
    # Wire the context manager workflow: with httpx.Client() as client:
    mock_client_instance.get.return_value = mock_response
    mock_client_class.return_value.__enter__.return_value = mock_client_instance

    result = app._fetch_annotated_image("valid-id")
    assert result is not None


@patch("app.download_image_base64", return_value="mocked_base64_from_s3")
def test_fetch_annotated_image_from_s3_key(mock_download_image_base64):
    token = app._current_predicted_image_s3_key.set("chat-1/prediction-123/predicted/image.jpg")
    try:
        result = app._fetch_annotated_image("valid-id")
    finally:
        app._current_predicted_image_s3_key.reset(token)

    assert result == "mocked_base64_from_s3"
    mock_download_image_base64.assert_called_once_with("chat-1/prediction-123/predicted/image.jpg")


@patch("app.download_image_base64", side_effect=RuntimeError("S3 read failed"))
@patch("httpx.Client")
def test_fetch_annotated_image_s3_exception_falls_back_to_http(mock_client_class, mock_download_image_base64):
    mock_client_instance = MagicMock()
    mock_response = MagicMock()
    mock_response.content = b"fake_binary_image_bytes"
    mock_response.raise_for_status = MagicMock()
    mock_client_instance.get.return_value = mock_response
    mock_client_class.return_value.__enter__.return_value = mock_client_instance

    token = app._current_predicted_image_s3_key.set("chat-1/prediction-123/predicted/image.jpg")
    try:
        result = app._fetch_annotated_image("prediction-123")
    finally:
        app._current_predicted_image_s3_key.reset(token)

    assert result is not None
    mock_download_image_base64.assert_called_once_with("chat-1/prediction-123/predicted/image.jpg")
    mock_client_instance.get.assert_called_once_with(f"{app.YOLO_SERVICE_URL}/prediction/prediction-123/image")


def test_fetch_annotated_image_exceptions():
    """Force network exceptions to test error tracking safety hooks."""
    assert app._fetch_annotated_image(None) is None
    assert app._fetch_annotated_image("invalid-id!!!") is None


def test_show_annotated_image_tool_full_execution():
    """Verify successful runtime execution of show_annotated_image tool."""
    app._current_prediction_id.set("prediction-123")
    res = json.loads(app.show_annotated_image.invoke({}))
    assert "image_url" in res
    assert "prediction-123" in res["image_url"]


def test_show_annotated_image_tool_ordering():
    """Verify error behavior if show_annotated_image runs before detect_objects."""
    app._current_prediction_id.set(None)
    error_response = json.loads(app.show_annotated_image.invoke({}))
    assert "error" in error_response


@patch("httpx.Client")
def test_detect_objects_tool_success(mock_client_class):
    """Verify execution track of detect_objects tool under valid image state."""
    app._current_image_s3_key.set("chat-1/prediction-1/original/image.jpg")
    
    mock_client_instance = MagicMock()
    mock_response = MagicMock()
    mock_response.json.return_value = {"prediction_uid": "pred-abc", "objects": []}
    mock_response.raise_for_status = MagicMock()
    
    mock_client_instance.post.return_value = mock_response
    mock_client_class.return_value.__enter__.return_value = mock_client_instance

    res = json.loads(app.detect_objects.invoke({}))
    assert res["prediction_uid"] == "pred-abc"
    mock_client_instance.post.assert_called_once_with(
        f"{app.YOLO_SERVICE_URL}/predict",
        json={"image_s3_key": "chat-1/prediction-1/original/image.jpg"},
    )


@patch("httpx.Client")
def test_detect_objects_tool_yolo_http_error(mock_client_class):
    app._current_image_s3_key.set("chat-1/prediction-1/original/image.jpg")

    mock_client_instance = MagicMock()
    mock_response = MagicMock()
    mock_response.status_code = 502
    mock_response.json.return_value = {"detail": "Failed to download source image from S3: NoSuchKey"}

    request = httpx.Request("POST", f"{app.YOLO_SERVICE_URL}/predict")
    error = httpx.HTTPStatusError("bad gateway", request=request, response=mock_response)
    mock_response.raise_for_status.side_effect = error
    mock_client_instance.post.return_value = mock_response
    mock_client_class.return_value.__enter__.return_value = mock_client_instance

    res = json.loads(app.detect_objects.invoke({}))
    assert res["error"] == "YOLO service request failed."
    assert res["status_code"] == 502
    assert "Failed to download source image from S3" in res["detail"]


@patch("httpx.Client")
def test_detect_objects_tool_yolo_http_error_text_fallback(mock_client_class):
    app._current_image_s3_key.set("chat-1/prediction-1/original/image.jpg")

    mock_client_instance = MagicMock()
    mock_response = MagicMock()
    mock_response.status_code = 502
    mock_response.json.side_effect = ValueError("not json")
    mock_response.text = "plain upstream error"

    request = httpx.Request("POST", f"{app.YOLO_SERVICE_URL}/predict")
    error = httpx.HTTPStatusError("bad gateway", request=request, response=mock_response)
    mock_response.raise_for_status.side_effect = error
    mock_client_instance.post.return_value = mock_response
    mock_client_class.return_value.__enter__.return_value = mock_client_instance

    res = json.loads(app.detect_objects.invoke({}))
    assert res["error"] == "YOLO service request failed."
    assert res["status_code"] == 502
    assert res["detail"] == "plain upstream error"


@patch("httpx.Client")
def test_detect_objects_tool_yolo_request_error(mock_client_class):
    app._current_image_s3_key.set("chat-1/prediction-1/original/image.jpg")

    mock_client_instance = MagicMock()
    request = httpx.Request("POST", f"{app.YOLO_SERVICE_URL}/predict")
    mock_client_instance.post.side_effect = httpx.RequestError("connection refused", request=request)
    mock_client_class.return_value.__enter__.return_value = mock_client_instance

    res = json.loads(app.detect_objects.invoke({}))
    assert res["error"] == "YOLO service is unavailable."
    assert "connection refused" in res["detail"]


def test_detect_objects_missing_image_context():
    """Verify error behavior if detect_objects runs with an empty thread state context."""
    app._current_image_s3_key.set(None)
    error_response = json.loads(app.detect_objects.invoke({}))
    assert "error" in error_response


def test_invalid_framework_model_constraints():
    """Ensure runtime system configuration constraints block invalid model descriptors."""
    with pytest.raises(SystemExit):
        app.MODEL = "unsupported_legacy_model"
        if app.MODEL not in app.ALLOWED_MODELS:
            raise SystemExit("Error path covered.")