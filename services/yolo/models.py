from sqlalchemy import Column, String, DateTime, Integer, Float, ForeignKey
from sqlalchemy.orm import declarative_base, relationship
from sqlalchemy.sql import func
from datetime import datetime

Base = declarative_base()


class PredictionSession(Base):
    __tablename__ = 'prediction_sessions'

    uid = Column(String, primary_key=True, index=True)
    timestamp = Column(DateTime(timezone=True), server_default=func.now())
    original_image = Column(String)
    predicted_image = Column(String)
    
    detection_objects = relationship("DetectionObject", back_populates="prediction_session", cascade="all, delete-orphan")


class DetectionObject(Base):
    __tablename__ = 'detection_objects'

    id = Column(Integer, primary_key=True, autoincrement=True)
    prediction_uid = Column(String, ForeignKey("prediction_sessions.uid"), index=True)
    label = Column(String, index=True)
    score = Column(Float, index=True)
    box = Column(String)
    
    prediction_session = relationship("PredictionSession", back_populates="detection_objects")
