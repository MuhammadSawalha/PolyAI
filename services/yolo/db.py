import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base
from typing import Generator

# Database URL - supports SQLite (default) or PostgreSQL via environment variable
DB_URL = os.environ.get("DB_URL", "sqlite:///./predictions.db")

# Create engine with SQLite-specific settings
engine = create_engine(
    DB_URL,
    connect_args={"check_same_thread": False} if "sqlite" in DB_URL else {}
)

# Session factory
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# Base class for ORM models
Base = declarative_base()


def get_db() -> Generator:
    """
    FastAPI dependency that yields a database session.
    Ensures proper cleanup in finally block.
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
