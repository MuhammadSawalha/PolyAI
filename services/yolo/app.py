import io
import mimetypes
import posixpath
from uuid import uuid4

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Depends
from fastapi.responses import Response
from prometheus_fastapi_instrumentator import Instrumentator
from ultralytics import YOLO
from PIL import Image
from sqlalchemy.orm import Session
import logging
import os
import time
import signal
import sys
from pydantic import BaseModel
from typing import List, Optional

load_dotenv()

from db import engine, get_db, SessionLocal
from models import Base, PredictionSession, DetectionObject
from s3 import download_file_bytes, upload_file_bytes


AWS_REGION = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION", "us-east-1")


def download_image(object_key: str) -> tuple[bytes, str]:
    image_bytes = download_file_bytes(object_key)
    content_type = mimetypes.guess_type(object_key)[0] or "image/jpeg"
    return image_bytes, content_type


def upload_image_bytes(image_bytes: bytes, object_key: str, content_type: Optional[str] = None) -> str:
    uploaded = upload_file_bytes(
        image_bytes,
        object_key,
        content_type=content_type or mimetypes.guess_type(object_key)[0] or "image/jpeg",
    )
    if not uploaded:
        raise RuntimeError(f"Failed to upload image to S3 key {object_key}")
    return object_key


def get_image_response(object_key: str) -> tuple[bytes, str]:
    return download_image(object_key)


class PredictRequest(BaseModel):
    image_s3_key: str


def _validate_image_key(image_s3_key: str) -> tuple[str, str, str]:
    normalized_key = image_s3_key.strip()
    if not normalized_key:
        raise HTTPException(status_code=400, detail="image_s3_key is required")

    ext = os.path.splitext(normalized_key)[1].lower()
    if ext not in {".jpg", ".jpeg", ".png"}:
        raise HTTPException(status_code=400, detail="Only image files are supported (.jpg, .jpeg, .png)")

    parts = [part for part in normalized_key.split("/") if part]
    chat_id = parts[0] if parts else "chat"
    filename = os.path.basename(normalized_key)
    return normalized_key, chat_id, filename


def _build_predicted_image_key(chat_id: str, prediction_uid: str, filename: str) -> str:
    return posixpath.join(chat_id, prediction_uid, "predicted", filename)

class YoloPredictResponse(BaseModel):
    prediction_uid: str
    original_image_s3_key: str
    predicted_image_s3_key: str
    detection_count: int
    labels: List[str]
    time_took: float

is_shutting_down = False

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

# Disable GPU usage
import torch
torch.cuda.is_available = lambda: False

app = FastAPI()

# Expose /metrics endpoint with default process metrics + FastAPI HTTP metrics
Instrumentator().instrument(app).expose(app)

# Confidence threshold for object detection (0.0 - 1.0).
# Detections below this score are discarded.
# Override with: export CONFIDENCE_THRESHOLD=0.7
_raw_threshold = os.environ.get("CONFIDENCE_THRESHOLD")
if _raw_threshold is not None:
    CONFIDENCE_THRESHOLD = float(_raw_threshold)
    logging.info(f"CONFIDENCE_THRESHOLD set to {CONFIDENCE_THRESHOLD} (from environment)")
else:
    CONFIDENCE_THRESHOLD = 0.5
    logging.info(f"CONFIDENCE_THRESHOLD not set, using default: {CONFIDENCE_THRESHOLD}")

# Download the AI model (tiny model ~6MB)
model = YOLO("yolov8n.pt")

# Initialize database tables
Base.metadata.create_all(bind=engine)


def handle_sigterm(signum, frame):
    global is_shutting_down
    is_shutting_down = True
    logging.info("Received SIGTERM. Shutting down gracefully...")
    # Perform cleanup: close DB connections, finish pending work, etc.
    logging.info("Cleanup done. Exiting.")
    sys.exit(0)

signal.signal(signal.SIGTERM, handle_sigterm)

def save_prediction_session(db: Session, uid: str, original_image: str, predicted_image: str):
    """
    Save prediction session to database
    """
    session = PredictionSession(uid=uid, original_image=original_image, predicted_image=predicted_image)
    db.add(session)
    db.commit()

def save_detection_object(db: Session, prediction_uid: str, label: str, score: float, box: str):
    """
    Save detection object to database
    """
    detection = DetectionObject(prediction_uid=prediction_uid, label=label, score=score, box=str(box))
    db.add(detection)
    db.commit()

@app.post("/predict", response_model=YoloPredictResponse)
def predict(request: PredictRequest, db: Session = Depends(get_db)):
    """
    Predict objects in an image, validate file format, and track processing time
    """
    # Start the performance stopwatch
    start_time = time.time()

    original_image_s3_key, chat_id, filename = _validate_image_key(request.image_s3_key)
    uid = str(uuid4())
    predicted_image_s3_key = _build_predicted_image_key(chat_id, uid, filename)

    try:
        image_bytes, content_type = download_image(original_image_s3_key)
        source_image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    except Exception as exc:
        logging.exception("Failed to download source image from S3 key %s", original_image_s3_key)
        raise HTTPException(status_code=502, detail=f"Failed to download source image from S3: {exc}") from exc

    # Run the YOLO model
    results = model(source_image, device="cpu", conf=CONFIDENCE_THRESHOLD)

    # Draw the bounding boxes and save the new annotated image
    annotated_frame = results[0].plot()  # NumPy array
    if isinstance(annotated_frame, Image.Image):
        annotated_image = annotated_frame
    else:
        annotated_image = Image.fromarray(annotated_frame)
    predicted_buffer = io.BytesIO()
    save_format = "PNG" if content_type == "image/png" else "JPEG"
    annotated_image.save(predicted_buffer, format=save_format)
    try:
        upload_image_bytes(predicted_buffer.getvalue(), predicted_image_s3_key, content_type=content_type)
    except Exception as exc:
        logging.exception("Failed to upload predicted image to S3 key %s", predicted_image_s3_key)
        raise HTTPException(status_code=502, detail=f"Failed to upload predicted image to S3: {exc}") from exc

    # Database Logging (Session & Objects found)
    save_prediction_session(db, uid, original_image_s3_key, predicted_image_s3_key)

    detected_labels = []
    for box in results[0].boxes:
        label_idx = int(box.cls[0].item())
        label = model.names[label_idx]
        score = float(box.conf[0])
        bbox = box.xyxy[0].tolist()
        save_detection_object(db, uid, label, score, bbox)
        detected_labels.append(label)

    # Stop the stopwatch and calculate total runtime in seconds
    processing_time = round(time.time() - start_time, 2)

    return {
        "prediction_uid": uid,
        "original_image_s3_key": original_image_s3_key,
        "predicted_image_s3_key": predicted_image_s3_key,
        "detection_count": len(results[0].boxes),
        "labels": detected_labels,
        "time_took": processing_time,
    }

@app.get("/prediction/{uid}")
def get_prediction_by_uid(uid: str, db: Session = Depends(get_db)):
    """
    Get prediction session by uid with all detected objects
    """
    session = db.query(PredictionSession).filter_by(uid=uid).first()
    if not session:
        raise HTTPException(status_code=404, detail="Prediction not found")
    
    return {
        "uid": session.uid,
        "timestamp": session.timestamp,
        "original_image": session.original_image,
        "predicted_image": session.predicted_image,
        "detection_objects": [
            {
                "id": obj.id,
                "label": obj.label,
                "score": obj.score,
                "box": obj.box
            } for obj in session.detection_objects
        ]
    }


@app.get("/prediction/{uid}/image")
def get_prediction_image(uid: str, db: Session = Depends(get_db)):
    """
    Return the annotated (bounding-box) image for a prediction
    """
    session = db.query(PredictionSession).filter_by(uid=uid).first()
    if not session or not session.predicted_image:
        raise HTTPException(status_code=404, detail="Image not found")

    try:
        image_bytes, content_type = get_image_response(session.predicted_image)
    except Exception:
        raise HTTPException(status_code=404, detail="Image not found")

    return Response(content=image_bytes, media_type=content_type)


@app.get("/predictions/label/{label}")
def get_predictions_by_label(label: str, db: Session = Depends(get_db)):
    """
    Return all prediction sessions that contain at least one detected object with the given label.
    """
    # Clean up whitespace
    label = label.strip()
    
    # Validation rule: Empty strings throw an HTTP 400 Error
    if not label:
        raise HTTPException(status_code=400, detail="Label cannot be empty")
    
    # Find all unique session uids that contain the requested label
    sessions = db.query(PredictionSession).join(DetectionObject).filter(DetectionObject.label == label).distinct().all()
    
    result = []
    
    # Map and fetch all related objects for those matching sessions
    for session in sessions:
        result.append({
            "uid": session.uid,
            "timestamp": session.timestamp,
            "detection_objects": [
                {
                    "id": obj.id,
                    "label": obj.label,
                    "score": obj.score,
                    "box": obj.box
                } for obj in session.detection_objects
            ]
        })
        
    return result


@app.get("/predictions/score/{min_score}")
def get_predictions_by_score(min_score: float, db: Session = Depends(get_db)):
    """
    Return all detection objects whose confidence score is greater than or equal to min_score.
    """
    # Validation rule: Must be structurally bounded between 0.0 and 1.0
    if not (0.0 <= min_score <= 1.0):
        raise HTTPException(status_code=400, detail="min_score must be between 0.0 and 1.0")
    
    objects = db.query(DetectionObject).filter(DetectionObject.score >= min_score).all()
    
    return [
        {
            "id": obj.id,
            "prediction_uid": obj.prediction_uid,
            "label": obj.label,
            "score": obj.score,
            "box": obj.box
        } for obj in objects
    ]


@app.get("/ready")
def ready():
    if is_shutting_down:
        raise HTTPException(status_code=503, detail="Service is shutting down")
    return {"status": "ready"}


@app.get("/health")
def health():
    """
    Health check endpoint
    """
    return {"status": "ok"}

if __name__ == "__main__": # pragma: no cover
    import uvicorn

    Base.metadata.create_all(bind=engine)
    
    uvicorn.run(app, host="0.0.0.0", port=8080)
