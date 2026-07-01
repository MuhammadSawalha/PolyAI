import os
import sys
import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch

# Set default test environment fallback variables BEFORE importing app to prevent initialization crashes
os.environ.setdefault("MODEL", "amazon.nova-lite-v1:0")
os.environ.setdefault("MODEL_PROVIDER", "bedrock_converse")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("AWS_S3_BUCKET", "fake-bucket")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "fake")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "fake")

# Ensure the parent directory is in the path so we can import app
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from app import app

client = TestClient(app)


def test_health_endpoint():
    """Verify that the health check endpoint returns status ok."""
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@patch("app.run_agent")
@patch("app.build_original_image_key", return_value="chat-1/prediction-1/original/image.jpg")
@patch("app.upload_base64_image", return_value="chat-1/prediction-1/original/image.jpg")
@patch("app.uuid4", side_effect=["chat-1", "prediction-1"])
def test_chat_endpoint_success(mock_uuid4, mock_upload_base64_image, mock_build_original_image_key, mock_run_agent):
    """Test /chat endpoint with image upload context."""
    mock_run_agent.return_value = {
        "response": "There are 2 people in the image.",
        "prediction_id": "prediction-123",
        "annotated_image": "mocked_base64_string",
        "image_url": "http://localhost:8080/prediction/prediction-123/image",
        "agent_loop_time_s": 0.5,
        "iterations": 2,
        "tools_called": ["detect_objects", "show_annotated_image"],
        "context_limit_exceeded": False,
        "tokens_used": {"input": 100, "output": 50, "total": 150}
    }

    payload = {
        "messages": [
            {
                "role": "user",
                "content": "Show me the annotated image",
                "image_base64": "fakebase64text"
            },
            {
                "role": "assistant",
                "content": "Analyzing image..."
            }
        ]
    }

    response = client.post("/chat", json=payload)
    assert response.status_code == 200
    
    data = response.json()
    assert data["response"] == "There are 2 people in the image."
    assert data["tokens_used"]["total"] == 150
    mock_run_agent.assert_called_once()
    assert mock_uuid4.call_count == 2
    mock_build_original_image_key.assert_called_once_with("chat-1", "prediction-1")
    mock_upload_base64_image.assert_called_once_with("fakebase64text", "chat-1/prediction-1/original/image.jpg")


@patch("app.run_agent")
def test_chat_endpoint_text_only_success(mock_run_agent):
    """Test /chat endpoint text-only message to achieve 100% branch coverage."""
    mock_run_agent.return_value = {
        "response": "Hello! How can I assist you with images today?",
        "prediction_id": None,
        "annotated_image": None,
        "image_url": None,
        "agent_loop_time_s": 0.1,
        "iterations": 1,
        "tools_called": [],
        "context_limit_exceeded": False,
        "tokens_used": {"input": 10, "output": 10, "total": 20}
    }

    payload = {
        "messages": [
            {
                "role": "user",
                "content": "Just saying hello!",
                "image_base64": None
            }
        ]
    }

    response = client.post("/chat", json=payload)
    assert response.status_code == 200
    assert response.json()["response"] == "Hello! How can I assist you with images today?"


@patch("app.upload_base64_image", side_effect=RuntimeError("S3 unavailable"))
@patch("app.build_original_image_key", return_value="chat-1/prediction-1/original/image.jpg")
@patch("app.uuid4", side_effect=["chat-1", "prediction-1"])
def test_chat_endpoint_s3_upload_failure(mock_uuid4, mock_build_original_image_key, mock_upload_base64_image):
    payload = {
        "messages": [
            {
                "role": "user",
                "content": "Analyze this image",
                "image_base64": "fakebase64text"
            }
        ]
    }

    response = client.post("/chat", json=payload)
    assert response.status_code == 502
    assert response.json()["detail"] == "Failed to upload image to S3: S3 unavailable"
    assert mock_uuid4.call_count == 2
    mock_build_original_image_key.assert_called_once_with("chat-1", "prediction-1")
    mock_upload_base64_image.assert_called_once_with("fakebase64text", "chat-1/prediction-1/original/image.jpg")


def test_chat_endpoint_invalid_method():
    """Trigger FastAPI routing validation by targeting an invalid method type."""
    response = client.get("/chat")
    assert response.status_code == 405  # Method Not Allowed