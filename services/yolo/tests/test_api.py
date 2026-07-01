import os
import pytest
import tempfile
import importlib
import signal
import io
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from PIL import Image
import numpy as np

os.environ.setdefault("CONFIDENCE_THRESHOLD", "0.5")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("AWS_S3_BUCKET", "fake-bucket")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "fake")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "fake")

import app as app_module
from app import app, save_prediction_session, save_detection_object
from db import get_db
from models import Base, PredictionSession, DetectionObject

TEST_IMAGE = os.path.join(os.path.dirname(__file__), "data", "beatles.jpeg")


@pytest.fixture(scope="function")
def test_engine():
    """
    Creates a file-based SQLite test database shared across threads.
    
    ✅ CRITICAL: File-based SQLite (not :memory:) ensures TestClient background thread
    can access the same database as the main test thread.
    """
    # Create a temporary file for the test database
    db_fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(db_fd)  # Close file descriptor; SQLite will handle the file
    
    database_url = f"sqlite:///{db_path}"
    engine = create_engine(database_url, connect_args={"check_same_thread": False})
    
    Base.metadata.create_all(bind=engine)
    yield engine
    
    # Cleanup
    Base.metadata.drop_all(bind=engine)
    engine.dispose()
    os.unlink(db_path)


@pytest.fixture(autouse=True)
def setup_db(test_engine):
    """Provisions an isolated, clean file-based database for each test."""
    TestingSessionLocal = sessionmaker(bind=test_engine)
    
    def override_get_db():
        db = TestingSessionLocal()
        try:
            yield db
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
    
    app.dependency_overrides[get_db] = override_get_db
    yield
    app.dependency_overrides.clear()


@pytest.fixture
def db_session(test_engine):
    """Provides a fresh ORM session using the SAME file-based database as the app."""
    SessionLocal = sessionmaker(bind=test_engine)
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture
def client():
    return TestClient(app)


# ====================================================================================


def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_save_prediction_session_real_db(db_session):
    """Tests save_prediction_session by inserting a row into the real temp DB."""
    test_uid = "session-xyz-789"
    test_orig = "uploads/original.jpg"
    test_pred = "predicted/annotated.jpg"

    save_prediction_session(db_session, test_uid, test_orig, test_pred)

    row = db_session.query(PredictionSession).filter_by(uid=test_uid).first()

    assert row is not None
    assert row.uid == test_uid
    assert row.original_image == test_orig
    assert row.predicted_image == test_pred


def test_save_detection_object_real_db(db_session):
    """Tests save_detection_object by inserting a row into the real temp DB."""
    test_uid = "session-xyz-789"
    test_label = "person"
    test_score = 0.98
    test_box = [100, 150, 200, 250]

    # First create a session
    save_prediction_session(db_session, test_uid, "orig.jpg", "pred.jpg")
    # Then save detection object
    save_detection_object(db_session, test_uid, test_label, test_score, test_box)

    row = db_session.query(DetectionObject).filter_by(prediction_uid=test_uid).first()

    assert row is not None
    assert row.prediction_uid == test_uid
    assert row.label == test_label
    assert row.score == test_score
    assert row.box == str(test_box)


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


@patch("app.download_file_bytes", return_value=b"jpeg-binary-payload")
def test_download_image_wrapper(mock_download_file_bytes):
    image_bytes, content_type = app_module.download_image("chat-1/original/image.jpg")
    assert image_bytes == b"jpeg-binary-payload"
    assert content_type == "image/jpeg"
    mock_download_file_bytes.assert_called_once_with("chat-1/original/image.jpg")


@patch("app.upload_file_bytes", return_value=True)
def test_upload_image_bytes_wrapper_success(mock_upload_file_bytes):
    result = app_module.upload_image_bytes(b"jpeg-binary-payload", "chat-1/predicted/image.jpg")
    assert result == "chat-1/predicted/image.jpg"
    mock_upload_file_bytes.assert_called_once()


@patch("app.upload_file_bytes", return_value=False)
def test_upload_image_bytes_wrapper_failure(mock_upload_file_bytes):
    with pytest.raises(RuntimeError, match="Failed to upload image to S3 key chat-1/predicted/image.jpg"):
        app_module.upload_image_bytes(b"jpeg-binary-payload", "chat-1/predicted/image.jpg")

    mock_upload_file_bytes.assert_called_once()


@patch("app.download_image", return_value=(b"jpeg-binary-payload", "image/jpeg"))
def test_get_image_response_wrapper(mock_download_image):
    result = app_module.get_image_response("chat-1/predicted/image.jpg")
    assert result == (b"jpeg-binary-payload", "image/jpeg")
    mock_download_image.assert_called_once_with("chat-1/predicted/image.jpg")


# ====================================================================================

@patch("app.upload_image_bytes")
@patch("app.download_image")
@patch("app.model")
def test_predict_success_with_detections(mock_model, mock_download_image, mock_upload_image_bytes, client):
    """Tests /predict happy path: Verifies database storage integration concurrently."""
    image_buffer = io.BytesIO()
    Image.new("RGB", (4, 4), color="white").save(image_buffer, format="JPEG")
    mock_download_image.return_value = (image_buffer.getvalue(), "image/jpeg")

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
    fake_result.plot.return_value = Image.new("RGB", (4, 4), color="black")

    mock_model.return_value = [fake_result]
    mock_model.names = {0: "dog"}

    response = client.post(
        "/predict",
        json={"image_s3_key": "chat-1/original/test_image.jpg"}
    )

    assert response.status_code == 200
    data = response.json()
    assert data["original_image_s3_key"] == "chat-1/original/test_image.jpg"
    assert data["detection_count"] == 1
    assert data["labels"] == ["dog"]
    assert "time_took" in data
    assert data["predicted_image_s3_key"].endswith("/predicted/test_image.jpg")
    mock_download_image.assert_called_once_with("chat-1/original/test_image.jpg")
    mock_upload_image_bytes.assert_called_once()


def test_predict_invalid_file_extension(client):
    response = client.post(
        "/predict",
        json={"image_s3_key": "chat-1/original/document.txt"}
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "Only image files are supported (.jpg, .jpeg, .png)"


def test_predict_missing_image_s3_key(client):
    response = client.post(
        "/predict",
        json={"image_s3_key": "   "}
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "image_s3_key is required"


@patch("app.model")
@patch("app.download_image", side_effect=RuntimeError("download failed"))
def test_predict_download_failure(mock_download_image, mock_model, client):
    response = client.post(
        "/predict",
        json={"image_s3_key": "image.jpg"}
    )

    assert response.status_code == 502
    assert response.json()["detail"] == "Failed to download source image from S3: download failed"
    mock_model.assert_not_called()
    mock_download_image.assert_called_once_with("image.jpg")


@patch("app.upload_image_bytes", side_effect=RuntimeError("upload failed"))
@patch("app.download_image")
@patch("app.Image.fromarray")
@patch("app.model")
def test_predict_upload_failure_and_array_plot_output(
    mock_model,
    mock_fromarray,
    mock_download_image,
    mock_upload_image_bytes,
    client,
):
    image_buffer = io.BytesIO()
    Image.new("RGB", (4, 4), color="white").save(image_buffer, format="PNG")
    mock_download_image.return_value = (image_buffer.getvalue(), "image/png")

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
    fake_result.plot.return_value = np.zeros((4, 4, 3), dtype=np.uint8)

    mock_model.return_value = [fake_result]
    mock_model.names = {0: "dog"}
    mock_fromarray.return_value = Image.new("RGB", (4, 4), color="black")

    response = client.post(
        "/predict",
        json={"image_s3_key": "image.png"}
    )

    assert response.status_code == 502
    assert response.json()["detail"] == "Failed to upload predicted image to S3: upload failed"
    mock_download_image.assert_called_once_with("image.png")
    mock_fromarray.assert_called_once()
    mock_upload_image_bytes.assert_called_once()


# ====================================================================================

def test_get_prediction_by_uid_success(db_session, client):
    """Seed data directly to the real test database and read it via API."""
    save_prediction_session(db_session, "abc-123", "uploads/abc-123.jpg", "predicted/abc-123.jpg")
    save_detection_object(db_session, "abc-123", "dog", 0.95, [10, 20, 30, 40])

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


@patch("app.get_image_response", return_value=(b"jpeg-binary-payload", "image/jpeg"))
def test_get_prediction_image_success(mock_get_image_response, db_session, client):
    save_prediction_session(db_session, "img-123", "orig.jpg", "chat-1/img-123/predicted/real_file.jpg")

    response = client.get("/prediction/img-123/image")
    assert response.status_code == 200
    assert response.content == b"jpeg-binary-payload"
    mock_get_image_response.assert_called_once_with("chat-1/img-123/predicted/real_file.jpg")


def test_get_prediction_image_not_found(db_session, client):
    response = client.get("/prediction/missing-id/image")
    assert response.status_code == 404

    save_prediction_session(db_session, "ghost-id", "orig.jpg", "chat-1/ghost-id/predicted/image.jpg")
    with patch("app.get_image_response", side_effect=FileNotFoundError):
        response = client.get("/prediction/ghost-id/image")
        assert response.status_code == 404


def test_get_predictions_by_label_success(db_session, client):
    """Tests label search across rows populated in our real test database environment."""
    save_prediction_session(db_session, "sess-1", "o1.jpg", "p1.jpg")
    save_prediction_session(db_session, "sess-2", "o2.jpg", "p2.jpg")
    
    save_detection_object(db_session, "sess-1", "person", 0.91, [10, 20, 100, 200])
    save_detection_object(db_session, "sess-2", "cat", 0.85, [5, 5, 20, 20])

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


def test_get_predictions_by_score_success(db_session, client):
    """Validates real math scoring evaluation boundaries inside SQLite."""
    save_prediction_session(db_session, "uid-1", "o1.jpg", "p1.jpg")
    save_detection_object(db_session, "uid-1", "person", 0.91, [10, 20, 100, 200])
    save_detection_object(db_session, "uid-1", "cup", 0.32, [0, 0, 10, 10])

   
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