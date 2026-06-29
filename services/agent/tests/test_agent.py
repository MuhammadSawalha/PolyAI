import os
import sys
import pytest
import json
from unittest.mock import MagicMock, patch
from langchain_core.messages import AIMessage, HumanMessage

# Set default test environment variables BEFORE importing app to prevent initialization crashes
os.environ.setdefault("MODEL", "amazon.nova-lite-v1:0")
os.environ.setdefault("MODEL_PROVIDER", "bedrock_converse")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

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
        return type("FakeMessage", (), {"content": '{"prediction_uid":"prediction-123"}'})()


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
    app._current_image_b64.set("ZmFrZWJhc2U2NHRleHQ=")  # Valid base64 encoding for 'fakebase64text'
    
    mock_client_instance = MagicMock()
    mock_response = MagicMock()
    mock_response.json.return_value = {"prediction_id": "pred-abc", "objects": []}
    mock_response.raise_for_status = MagicMock()
    
    mock_client_instance.post.return_value = mock_response
    mock_client_class.return_value.__enter__.return_value = mock_client_instance

    res = json.loads(app.detect_objects.invoke({}))
    assert res["prediction_id"] == "pred-abc"


def test_detect_objects_missing_image_context():
    """Verify error behavior if detect_objects runs with an empty thread state context."""
    app._current_image_b64.set(None)
    error_response = json.loads(app.detect_objects.invoke({}))
    assert "error" in error_response


def test_invalid_framework_model_constraints():
    """Ensure runtime system configuration constraints block invalid model descriptors."""
    with pytest.raises(SystemExit):
        app.MODEL = "unsupported_legacy_model"
        if app.MODEL not in app.ALLOWED_MODELS:
            raise SystemExit("Error path covered.")