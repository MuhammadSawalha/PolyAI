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
import app as app_module
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


@patch("app.upload_base64_image")
@patch("app.run_agent")
def test_chat_endpoint_followup_turn_carries_forward_current_image(mock_run_agent, mock_upload_base64_image):
    """A follow-up text-only turn must NOT re-upload the original image_base64 still sitting in
    history - it should carry forward current_image_s3_key from the last assistant message instead."""
    mock_run_agent.return_value = {
        "response": "Here you go.",
        "prediction_id": None,
        "annotated_image": None,
        "edited_image": None,
        "current_image_s3_key": "chat-1/pred-1/edited/rotate_a.png",
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
                "content": "Rotate the image 90 degrees",
                "image_base64": "fakebase64text"
            },
            {
                "role": "assistant",
                "content": "Rotated!",
                "current_image_s3_key": "chat-1/pred-1/edited/rotate_a.png"
            },
            {
                "role": "user",
                "content": "Now show me the annotated image"
            }
        ]
    }

    response = client.post("/chat", json=payload)
    assert response.status_code == 200
    mock_upload_base64_image.assert_not_called()


@patch("app.build_original_image_key", return_value="chat-2/prediction-2/original/image.jpg")
@patch("app.upload_base64_image", return_value="chat-2/prediction-2/original/image.jpg")
@patch("app.run_agent")
def test_chat_endpoint_new_image_overrides_carried_forward_state(mock_run_agent, mock_upload_base64_image, mock_build_original_image_key):
    """If the newest message uploads a different image, it must override any carried-forward
    current_image_s3_key from earlier in the conversation, not be ignored in favor of it."""
    mock_run_agent.return_value = {
        "response": "New image received.",
        "prediction_id": None,
        "annotated_image": None,
        "edited_image": None,
        "current_image_s3_key": "chat-2/prediction-2/original/image.jpg",
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
                "content": "Rotate the image 90 degrees",
                "image_base64": "fakebase64text"
            },
            {
                "role": "assistant",
                "content": "Rotated!",
                "current_image_s3_key": "chat-1/pred-1/edited/rotate_a.png"
            },
            {
                "role": "user",
                "content": "Here's a different photo",
                "image_base64": "anotherbase64text"
            }
        ]
    }

    response = client.post("/chat", json=payload)
    assert response.status_code == 200
    mock_upload_base64_image.assert_called_once_with("anotherbase64text", "chat-2/prediction-2/original/image.jpg")


def _fake_run_agent_capturing_context(captured: dict):
    def fake_run_agent(history):
        captured["current_image_s3_key"] = app_module._current_image_s3_key.get()
        captured["original_image_s3_key"] = app_module._current_original_image_s3_key.get()
        captured["prediction_id"] = app_module._current_prediction_id.get()
        captured["predicted_image_s3_key"] = app_module._current_predicted_image_s3_key.get()
        return {
            "response": "ok",
            "prediction_id": None,
            "predicted_image_s3_key": None,
            "annotated_image": None,
            "edited_image": None,
            "current_image_s3_key": captured["current_image_s3_key"],
            "original_image_s3_key": captured["original_image_s3_key"],
            "image_url": None,
            "agent_loop_time_s": 0.1,
            "iterations": 1,
            "tools_called": [],
            "context_limit_exceeded": False,
            "tokens_used": {"input": 1, "output": 1, "total": 2},
        }

    return fake_run_agent


@patch("app.run_agent")
def test_chat_endpoint_carries_forward_prediction_state_when_present(mock_run_agent):
    """If the most recent state-carrying message has a prediction_id, it should be carried
    forward so show_annotated_image can succeed without re-running detect_objects."""
    captured = {}
    mock_run_agent.side_effect = _fake_run_agent_capturing_context(captured)

    payload = {
        "messages": [
            {"role": "user", "content": "Detect objects", "image_base64": "fakebase64text"},
            {
                "role": "assistant",
                "content": "Detected!",
                "current_image_s3_key": "chat-1/pred-1/original/image.jpg",
                "prediction_id": "pred-1",
                "predicted_image_s3_key": "chat-1/pred-1/predicted/image.jpg",
            },
            {"role": "user", "content": "Show me the annotated image"},
        ]
    }

    response = client.post("/chat", json=payload)
    assert response.status_code == 200
    assert captured["prediction_id"] == "pred-1"
    assert captured["predicted_image_s3_key"] == "chat-1/pred-1/predicted/image.jpg"


@patch("app.run_agent")
def test_chat_endpoint_does_not_resurrect_stale_prediction_after_edit(mock_run_agent):
    """Regression test: after an edit clears prediction_id/predicted_image_s3_key on its
    message, a later turn must NOT fall back to an OLDER message's prediction_id - that
    would resurrect a prediction computed against an image that's since been replaced."""
    captured = {}
    mock_run_agent.side_effect = _fake_run_agent_capturing_context(captured)

    payload = {
        "messages": [
            {"role": "user", "content": "Detect objects", "image_base64": "fakebase64text"},
            {
                "role": "assistant",
                "content": "Detected!",
                "current_image_s3_key": "chat-1/pred-1/original/image.jpg",
                "prediction_id": "pred-1",
                "predicted_image_s3_key": "chat-1/pred-1/predicted/image.jpg",
            },
            {"role": "user", "content": "Rotate it"},
            {
                "role": "assistant",
                "content": "Rotated!",
                "current_image_s3_key": "chat-1/pred-1/edited/rotate_a.png",
                # no prediction_id/predicted_image_s3_key here - the edit invalidated them
            },
            {"role": "user", "content": "Show me the annotated image"},
        ]
    }

    response = client.post("/chat", json=payload)
    assert response.status_code == 200
    assert captured["current_image_s3_key"] == "chat-1/pred-1/edited/rotate_a.png"
    assert captured["prediction_id"] is None
    assert captured["predicted_image_s3_key"] is None


@patch("app.build_original_image_key", return_value="chat-2/prediction-2/original/image.jpg")
@patch("app.upload_base64_image", return_value="chat-2/prediction-2/original/image.jpg")
@patch("app.run_agent")
def test_chat_endpoint_new_image_resets_prediction_state(mock_run_agent, mock_upload_base64_image, mock_build_original_image_key):
    """A brand-new image upload must reset prediction_id/predicted_image_s3_key even if
    history carried values for the previous image."""
    captured = {}
    mock_run_agent.side_effect = _fake_run_agent_capturing_context(captured)

    payload = {
        "messages": [
            {"role": "user", "content": "Detect objects", "image_base64": "fakebase64text"},
            {
                "role": "assistant",
                "content": "Detected!",
                "current_image_s3_key": "chat-1/pred-1/original/image.jpg",
                "prediction_id": "pred-1",
                "predicted_image_s3_key": "chat-1/pred-1/predicted/image.jpg",
            },
            {"role": "user", "content": "Here's a new photo", "image_base64": "anotherbase64text"},
        ]
    }

    response = client.post("/chat", json=payload)
    assert response.status_code == 200
    assert captured["prediction_id"] is None
    assert captured["predicted_image_s3_key"] is None


@patch("app.build_original_image_key", return_value="chat-1/prediction-1/original/image.jpg")
@patch("app.upload_base64_image", return_value="chat-1/prediction-1/original/image.jpg")
@patch("app.run_agent")
def test_chat_endpoint_fresh_upload_sets_original_equal_to_current(mock_run_agent, mock_upload_base64_image, mock_build_original_image_key):
    """A brand-new image upload has no edits yet, so original_image_s3_key must equal
    the freshly uploaded current_image_s3_key."""
    captured = {}
    mock_run_agent.side_effect = _fake_run_agent_capturing_context(captured)

    payload = {
        "messages": [
            {"role": "user", "content": "What's in this image?", "image_base64": "fakebase64text"},
        ]
    }

    response = client.post("/chat", json=payload)
    assert response.status_code == 200
    assert captured["current_image_s3_key"] == "chat-1/prediction-1/original/image.jpg"
    assert captured["original_image_s3_key"] == "chat-1/prediction-1/original/image.jpg"


@patch("app.run_agent")
def test_chat_endpoint_original_image_survives_an_edit(mock_run_agent):
    """After an edit overwrites current_image_s3_key, original_image_s3_key must still
    point at the pre-edit upload, carried forward from the same assistant message."""
    captured = {}
    mock_run_agent.side_effect = _fake_run_agent_capturing_context(captured)

    payload = {
        "messages": [
            {"role": "user", "content": "Rotate the image 90 degrees", "image_base64": "fakebase64text"},
            {
                "role": "assistant",
                "content": "Rotated!",
                "current_image_s3_key": "chat-1/pred-1/edited/rotate_a.png",
                "original_image_s3_key": "chat-1/pred-1/original/image.jpg",
            },
            {"role": "user", "content": "Now blur the person on the left"},
        ]
    }

    response = client.post("/chat", json=payload)
    assert response.status_code == 200
    assert captured["current_image_s3_key"] == "chat-1/pred-1/edited/rotate_a.png"
    assert captured["original_image_s3_key"] == "chat-1/pred-1/original/image.jpg"


@patch("app.run_agent")
def test_chat_endpoint_original_image_falls_back_to_current_for_legacy_messages(mock_run_agent):
    """Assistant messages produced before original_image_s3_key existed won't carry that
    field - the server should degrade gracefully and fall back to current_image_s3_key
    rather than losing track of the image entirely."""
    captured = {}
    mock_run_agent.side_effect = _fake_run_agent_capturing_context(captured)

    payload = {
        "messages": [
            {"role": "user", "content": "Rotate the image 90 degrees", "image_base64": "fakebase64text"},
            {
                "role": "assistant",
                "content": "Rotated!",
                "current_image_s3_key": "chat-1/pred-1/edited/rotate_a.png",
                # no original_image_s3_key here - simulates a pre-existing session
            },
            {"role": "user", "content": "What's in the image?"},
        ]
    }

    response = client.post("/chat", json=payload)
    assert response.status_code == 200
    assert captured["current_image_s3_key"] == "chat-1/pred-1/edited/rotate_a.png"
    assert captured["original_image_s3_key"] == "chat-1/pred-1/edited/rotate_a.png"


@patch("app.build_original_image_key", return_value="chat-2/prediction-2/original/image.jpg")
@patch("app.upload_base64_image", return_value="chat-2/prediction-2/original/image.jpg")
@patch("app.run_agent")
def test_chat_endpoint_new_image_resets_original_image(mock_run_agent, mock_upload_base64_image, mock_build_original_image_key):
    """A brand-new image upload must reset original_image_s3_key to the new upload, not
    carry forward the previous conversation's original image."""
    captured = {}
    mock_run_agent.side_effect = _fake_run_agent_capturing_context(captured)

    payload = {
        "messages": [
            {"role": "user", "content": "Rotate the image 90 degrees", "image_base64": "fakebase64text"},
            {
                "role": "assistant",
                "content": "Rotated!",
                "current_image_s3_key": "chat-1/pred-1/edited/rotate_a.png",
                "original_image_s3_key": "chat-1/pred-1/original/image.jpg",
            },
            {"role": "user", "content": "Here's a new photo", "image_base64": "anotherbase64text"},
        ]
    }

    response = client.post("/chat", json=payload)
    assert response.status_code == 200
    assert captured["current_image_s3_key"] == "chat-2/prediction-2/original/image.jpg"
    assert captured["original_image_s3_key"] == "chat-2/prediction-2/original/image.jpg"