from fastapi import FastAPI, UploadFile, File, HTTPException, Request, Depends
from fastapi.responses import FileResponse, Response
from prometheus_fastapi_instrumentator import Instrumentator
from ultralytics import YOLO
from PIL import Image
import logging
import os
import uuid
import shutil
import time
import signal
import sys
from pydantic import BaseModel
from typing import List, Optional
from sqlalchemy.orm import Session

try:
    # When run as a module
    from .db import engine, SessionLocal, Base, get_db
    from .models import PredictionSession, DetectionObject
except ImportError:
    # When run directly
    from db import engine, SessionLocal, Base, get_db
    from models import PredictionSession, DetectionObject

class YoloPredictResponse(BaseModel):
    prediction_uid: str
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

UPLOAD_DIR = "uploads/original"
PREDICTED_DIR = "uploads/predicted"

os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(PREDICTED_DIR, exist_ok=True)

# Download the AI model (tiny model ~6MB)
model = YOLO("yolov8n.pt")  

# Initialize database tables
def init_db():
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
    obj = DetectionObject(prediction_uid=prediction_uid, label=label, score=score, box=box)
    db.add(obj)
    db.commit()

@app.post("/predict", response_model=YoloPredictResponse)
def predict(file: UploadFile = File(...), db: Session = Depends(get_db)):
    """
    Predict objects in an image, validate file format, and track processing time
    """
    # Start the performance stopwatch
    start_time = time.time()

    # Extract and validate the file extension right away
    ext = os.path.splitext(file.filename)[1].lower()  # .lower() catches uppercase extensions like .PNG
    valid_extensions = [".jpg", ".jpeg", ".png"]
    
    if ext not in valid_extensions:
        raise HTTPException(
            status_code=400, 
            detail="Only image files are supported (.jpg, .jpeg, .png)"
        )

    # Proceed with file paths and saving the image
    uid = str(uuid.uuid4())
    original_path = os.path.join(UPLOAD_DIR, uid + ext)
    predicted_path = os.path.join(PREDICTED_DIR, uid + ext)

    with open(original_path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    # Run the YOLO model
    results = model(original_path, device="cpu", conf=CONFIDENCE_THRESHOLD)

    # Draw the bounding boxes and save the new annotated image
    annotated_frame = results[0].plot()  # NumPy array
    annotated_image = Image.fromarray(annotated_frame)
    annotated_image.save(predicted_path)

    # Database Logging (Session & Objects found)
    save_prediction_session(db, uid, original_path, predicted_path)

    detected_labels = []
    for box in results[0].boxes:
        label_idx = int(box.cls[0].item())
        label = model.names[label_idx]
        score = float(box.conf[0])
        bbox = box.xyxy[0].tolist()
        save_detection_object(db, uid, label, score, str(bbox))
        detected_labels.append(label)

    # Stop the stopwatch and calculate total runtime in seconds
    processing_time = round(time.time() - start_time, 2)

    return {
        "prediction_uid": uid,
        "detection_count": len(results[0].boxes),
        "labels": detected_labels,
        "time_took": processing_time
    }

@app.get("/prediction/{uid}")
def get_prediction_by_uid(uid: str, db: Session = Depends(get_db)):
    """
    Get prediction session by uid with all detected objects
    """
    # Get prediction session
    session = db.query(PredictionSession).filter_by(uid=uid).first()
    if not session:
        raise HTTPException(status_code=404, detail="Prediction not found")
    
    # Get all detection objects for this prediction
    objects = db.query(DetectionObject).filter_by(prediction_uid=uid).all()
    
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
            } for obj in objects
        ]
    }


@app.get("/prediction/{uid}/image")
def get_prediction_image(uid: str, db: Session = Depends(get_db)):
    """
    Return the annotated (bounding-box) image for a prediction
    """
    session = db.query(PredictionSession).filter_by(uid=uid).first()
    if not session or not os.path.exists(session.predicted_image):
        raise HTTPException(status_code=404, detail="Image not found")
    return FileResponse(session.predicted_image)


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
    session_rows = db.query(PredictionSession).join(
        DetectionObject, PredictionSession.uid == DetectionObject.prediction_uid
    ).filter(DetectionObject.label == label).distinct().all()
    
    result = []
    
    # Map and fetch all related objects for those matching sessions
    for session in session_rows:
        objects_rows = db.query(DetectionObject).filter_by(prediction_uid=session.uid).all()
        
        result.append({
            "uid": session.uid,
            "timestamp": session.timestamp,
            "detection_objects": [
                {
                    "id": obj.id,
                    "label": obj.label,
                    "score": obj.score,
                    "box": obj.box
                } for obj in objects_rows
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
    
    objects_rows = db.query(DetectionObject).filter(DetectionObject.score >= min_score).all()
    
    return [
        {
            "id": obj.id,
            "prediction_uid": obj.prediction_uid,
            "label": obj.label,
            "score": obj.score,
            "box": obj.box
        } for obj in objects_rows
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

    init_db()
    
    uvicorn.run(app, host="0.0.0.0", port=8080)
