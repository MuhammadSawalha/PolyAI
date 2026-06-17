import os
import pytest
import sqlite3
import importlib
import signal
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient

os.environ.setdefault("CONFIDENCE_THRESHOLD", "0.5")

import app as app_module
from app import app, init_db, save_prediction_session, save_detection_object

TEST_IMAGE = os.path.join(os.path.dirname(__file__), "data", "beatles.jpeg")


@pytest.fixture(autouse=True)
def setup_db(tmp_path, monkeypatch):
    """Automatically provisions a isolated, clean database for each separate test execution run."""
    db_file = str(tmp_path / "test_predictions.db")
    monkeypatch.setattr("app.DB_PATH", db_file)
    init_db()


@pytest.fixture
def client():
    return TestClient(app)


# ====================================================================================


def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_save_prediction_session_real_db():
    """Tests save_prediction_session by inserting a row into the real temp DB."""
    test_uid = "session-xyz-789"
    test_orig = "uploads/original.jpg"
    test_pred = "predicted/annotated.jpg"

    save_prediction_session(test_uid, test_orig, test_pred)

    with sqlite3.connect(app_module.DB_PATH) as conn:  
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM prediction_sessions WHERE uid = ?", (test_uid,)
        ).fetchone()

    assert row is not None
    assert row["uid"] == test_uid
    assert row["original_image"] == test_orig
    assert row["predicted_image"] == test_pred


def test_save_detection_object_real_db():
    """Tests save_detection_object by inserting a row into the real temp DB."""
    test_uid = "session-xyz-789"
    test_label = "person"
    test_score = 0.98
    test_box = [100, 150, 200, 250]

    save_detection_object(test_uid, test_label, test_score, test_box)

    with sqlite3.connect(app_module.DB_PATH) as conn:  
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM detection_objects WHERE prediction_uid = ?", (test_uid,)
        ).fetchone()

    assert row is not None
    assert row["prediction_uid"] == test_uid
    assert row["label"] == test_label
    assert row["score"] == test_score
    assert row["box"] == str(test_box)


def test_confidence_threshold_default_fallback():
    """Wipes the environment variable to force the app configuration into the 'else' block."""
    old_value = os.environ.get("CONFIDENCE_THRESHOLD")
    try:
        if "CONFIDENCE_THRESHOLD" in os.environ:
            del os.environ["CONFIDENCE_THRESHOLD"]
        
        importlib.reload(app_module)
        assert app_module.CONFIDENCE_THRESHOLD == 0.5
    finally:
        if old_value is not None:
            os.environ["CONFIDENCE_THRESHOLD"] = old_value
        else:
            os.environ["CONFIDENCE_THRESHOLD"] = "0.5"
        importlib.reload(app_module)


# ====================================================================================

@patch("app.Image")
@patch("app.model")
def test_predict_success_with_detections(mock_model, mock_image, client):
    """Tests /predict happy path: Verifies database storage integration concurrently."""
    mock_cls_tensor = MagicMock()
    mock_cls_tensor.item.return_value = 0
    
    mock_xyxy_tensor = MagicMock()
    mock_xyxy_tensor.tolist.return_value = [10, 20, 30, 40]

    fake_box = MagicMock()
    fake_box.cls = [mock_cls_tensor]  
    fake_box.conf = [0.92]
    fake_box.xyxy = [mock_xyxy_tensor]  

    fake_result = MagicMock()
    fake_result.boxes = [fake_box]  
    fake_result.plot.return_value = MagicMock()

    mock_model.return_value = [fake_result]
    mock_model.names = {0: "dog"}

    response = client.post(
        "/predict",
        files={"file": ("test_image.jpg", b"fake_data", "image/jpeg")}
    )

    assert response.status_code == 200
    data = response.json()
    assert data["detection_count"] == 1
    assert data["labels"] == ["dog"]
    assert "time_took" in data


def test_predict_invalid_file_extension(client):
    response = client.post(
        "/predict",
        files={"file": ("document.txt", b"hello world", "text/plain")}
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "Only image files are supported (.jpg, .jpeg, .png)"


# ====================================================================================

def test_get_prediction_by_uid_success(client):
    """Seed data directly to the real test database and read it via API."""
    save_prediction_session("abc-123", "uploads/abc-123.jpg", "predicted/abc-123.jpg")
    save_detection_object("abc-123", "dog", 0.95, [10, 20, 30, 40])

    response = client.get("/prediction/abc-123")
    
    assert response.status_code == 200
    data = response.json()
    assert data["uid"] == "abc-123"
    assert len(data["detection_objects"]) == 1
    assert data["detection_objects"][0]["label"] == "dog"


def test_get_prediction_by_uid_not_found(client):
    response = client.get("/prediction/missing-id")
    assert response.status_code == 404
    assert response.json()["detail"] == "Prediction not found"


def test_get_prediction_image_success(tmp_path, monkeypatch, client):
    """Verifies image payload distribution against real storage paths."""
    fake_img = tmp_path / "real_file.jpg"
    fake_img.write_bytes(b"jpeg-binary-payload")

    save_prediction_session("img-123", "orig.jpg", str(fake_img))

    response = client.get("/prediction/img-123/image")
    assert response.status_code == 200
    assert response.content == b"jpeg-binary-payload"


def test_get_prediction_image_not_found(client):
    response = client.get("/prediction/missing-id/image")
    assert response.status_code == 404

    save_prediction_session("ghost-id", "orig.jpg", "/nonexistent/disk/image.jpg")
    response = client.get("/prediction/ghost-id/image")
    assert response.status_code == 404


def test_get_predictions_by_label_success(client):
    """Tests label search across rows populated in our real test database environment."""
    save_prediction_session("sess-1", "o1.jpg", "p1.jpg")
    save_prediction_session("sess-2", "o2.jpg", "p2.jpg")
    
    save_detection_object("sess-1", "person", 0.91, [10, 20, 100, 200])
    save_detection_object("sess-2", "cat", 0.85, [5, 5, 20, 20])

    response = client.get("/predictions/label/person")
    
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 1
    assert data[0]["uid"] == "sess-1"
    assert data[0]["detection_objects"][0]["label"] == "person"


def test_get_predictions_by_label_empty_result(client):
    response = client.get("/predictions/label/unicorn")
    assert response.status_code == 200
    assert response.json() == []


def test_get_predictions_by_label_empty_string(client):
    response = client.get("/predictions/label/%20") 
    assert response.status_code == 400
    assert response.json()["detail"] == "Label cannot be empty"


def test_get_predictions_by_score_success(client):
    """Validates real math scoring evaluation boundaries inside SQLite."""
    save_prediction_session("uid-1", "o1.jpg", "p1.jpg")
    save_detection_object("uid-1", "person", 0.91, [10, 20, 100, 200])
    save_detection_object("uid-1", "cup", 0.32, [0, 0, 10, 10])

   
    response = client.get("/predictions/score/0.5")
    
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 1
    assert data[0]["label"] == "person"
    assert data[0]["score"] == 0.91


def test_get_predictions_by_score_out_of_bounds(client):
    response = client.get("/predictions/score/1.1")
    assert response.status_code == 400
    assert response.json()["detail"] == "min_score must be between 0.0 and 1.0"


# ====================================================================================
# Graceful Shutdown & Readiness Tests
# ====================================================================================

def test_ready_endpoint_healthy(client):
    """Test that the /ready endpoint returns 200 when the server is healthy."""
    # Force the state to False to simulate normal operations
    app_module.is_shutting_down = False
    
    response = client.get("/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


def test_ready_endpoint_shutting_down(client):
    """Test that /ready returns 503 Service Unavailable during a shutdown sequence."""
    # Mock the global variable state to simulate a shutdown in progress
    with patch("app.is_shutting_down", True):
        response = client.get("/ready")
        assert response.status_code == 503
        assert response.json()["detail"] == "Service is shutting down"


def test_sigterm_handler_execution():
    """Test that the SIGTERM handler cleans up and exits cleanly."""
    # We use pytest.raises(SystemExit) because sys.exit(0) raises a SystemExit exception
    with pytest.raises(SystemExit) as exit_info:
        # Call the handler manually to simulate Linux passing a SIGTERM signal
        app_module.handle_sigterm(signal.SIGTERM, None)
        
    # Verify it exits with code 0 (clean shutdown) and flips the boolean state
    assert exit_info.value.code == 0
    assert app_module.is_shutting_down is True