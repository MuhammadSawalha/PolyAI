from sqlalchemy import Column, String, DateTime, Integer, Float, ForeignKey, Index
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from db import Base


class PredictionSession(Base):
    """
    Represents a prediction session containing detected objects.
    Maps to the 'prediction_sessions' table.
    """
    __tablename__ = "prediction_sessions"

    uid = Column(String, primary_key=True, index=True)
    timestamp = Column(DateTime, server_default=func.now())
    original_image = Column(String)
    predicted_image = Column(String)

    # Relationship to detection objects
    detection_objects = relationship("DetectionObject", back_populates="session")


class DetectionObject(Base):
    """
    Represents a single detected object within a prediction session.
    Maps to the 'detection_objects' table.
    """
    __tablename__ = "detection_objects"

    id = Column(Integer, primary_key=True, autoincrement=True)
    prediction_uid = Column(String, ForeignKey("prediction_sessions.uid"))
    label = Column(String)
    score = Column(Float)
    box = Column(String)  # Stored as JSON string

    # Relationship back to prediction session
    session = relationship("PredictionSession", back_populates="detection_objects")

    # Indices for faster queries
    __table_args__ = (
        Index("idx_prediction_uid", "prediction_uid"),
        Index("idx_label", "label"),
        Index("idx_score", "score"),
    )
