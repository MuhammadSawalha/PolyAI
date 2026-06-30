---
name: yolo-api-data-layer
description: Use this skill when executing data layer tasks for the YOLO FastAPI service. This includes initial SQLAlchemy migrations, database schema modifications (e.g., adding a processing_time_ms column or a UserFeedback table), creating database-backed endpoints (e.g., fetching recent sessions), implementing cascade deletions by UID, environment-driven database routing (SQLite/PostgreSQL), architectural design enforcement, and writing isolated file-based pytest suites.

---

# YOLO API Data Layer Architecture & Specification

## Overview

This specification details the structural design and engineering guidelines for managing the data persistence layer of the YOLO FastAPI microservice. Whether you're refactoring from raw SQL to SQLAlchemy ORM, adding new endpoints, modifying database schemas, or configuring production database backends, this skill provides patterns and architectural constraints to maintain code quality and API stability.

The data layer architecture is built on three primary pillars:

1. **Production-Ready Flexibility:** Support dynamic database backends—file-based SQLite for local development/testing, PostgreSQL for production—via environment-driven configuration (`DB_BACKEND`).
2. **Strict API Contract Preservation:** All data layer modifications—refactoring, schema extensions, query optimization, new endpoints—must leave existing routes, HTTP status codes, and JSON response schemas completely unchanged.
3. **Automated Reliability:** Enforce structured error handling, transaction safety, N+1 query prevention, and isolated test fixtures with file-based SQLite to prevent environment state pollution.

---

## Core Architecture Principles

These principles apply **to all data layer tasks**—whether refactoring, adding endpoints, modifying schemas, or configuring backends:

- **No API changes** — Routes, status codes, JSON response schemas must remain identical. Only database layer is modified.
- **Dependency injection** — Always inject `Session` via `Depends(get_db)` into endpoints and helper functions. Never create sessions inside functions.
- **ORM-first patterns** — Use SQLAlchemy ORM exclusively; never write raw SQL strings.
- **Environment flexibility** — Use `DB_BACKEND` environment variable to support SQLite (default) and PostgreSQL without code changes.
- **Relationships** — Use ORM relationships (`model.related_objects`) instead of manual JOINs.
- **Attribute access** — Always use `obj.column` syntax; never dict subscripting (`obj["column"]`).
- **Test isolation** — Use file-based SQLite with `tempfile` for tests; share single engine across all fixtures (critical for TestClient threading).

---

## Preservation Rules

### ✅ DO Modify

- All `sqlite3.connect()` blocks → ORM queries
- `conn.row_factory` patterns → Model attribute access
- Endpoint signatures → Add `db: Session = Depends(get_db)` parameters
- Helper functions → Accept `db: Session` parameter
- Database initialization → `Base.metadata.create_all(bind=engine)`
- Database schema → Add columns, tables, relationships as needed
- Query logic → Optimize with ORM filters, joins, ordering, limits

### ❌ DO NOT Modify

- **Routes & Methods:** All existing HTTP methods and paths unchanged (`/predict`, `/prediction/{uid}`, `/prediction/{uid}/image`, `/predictions/label/{label}`, `/predictions/score/{min_score}`, `/health`, `/ready`)
- **Status Codes:** 200, 400, 404, 503 (all unchanged)
- **Response Schema:** `YoloPredictResponse` structure and all JSON payloads
- **Business Logic:** Image processing, YOLO inference, file I/O, UUID generation, confidence thresholding
- **Config:** `CONFIDENCE_THRESHOLD` environment variable handling
- **Shutdown:** SIGTERM handler, `is_shutting_down` flag, `/ready` behavior
- **Monitoring:** Prometheus `/metrics` endpoint

---

## Refactor the API to Use SQLAlchemy

### New Module: `services/yolo/db.py`

**Purpose:** Centralize database engine, session factory, and dependency injection.

**Exact Target Template for `db.py`:**
```python
import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

DB_BACKEND = os.getenv("DB_BACKEND", "sqlite")
DB_USER = os.getenv("DB_USER", "user")
DB_PASSWORD = os.getenv("DB_PASSWORD", "pass")

if DB_BACKEND == "postgres":
    DATABASE_URL = f"postgresql://{DB_USER}:{DB_PASSWORD}@localhost/db"
else:
    DATABASE_URL = "sqlite:///./predictions.db"

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if "sqlite" in DATABASE_URL else {},
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

def get_db():
    db = SessionLocal()
    try:
        yield db
    except Exception:
        db.rollback()  # 💡 Added: Automatically roll back failed transactions
        raise
    finally:
        db.close()
```

### New Module: `services/yolo/models.py`

**Purpose:** Define ORM model classes mapping to database tables.

**Exact Target Template for `models.py`:**
```python
from sqlalchemy import Column, String, DateTime, Integer, Float
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.sql import func
from datetime import datetime

Base = declarative_base()


class PredictionSession(Base):
    __tablename__ = 'prediction_sessions'

    uid = Column(String, primary_key=True)
    timestamp = Column(DateTime(timezone=True), server_default=func.now())
    original_image = Column(String)
    predicted_image = Column(String)


class DetectionObject(Base):
    __tablename__ = 'detection_objects'

    id = Column(Integer, primary_key=True, autoincrement=True)
    prediction_uid = Column(String)
    label = Column(String)
    score = Column(Float)
    box = Column(String)
```

### Code Transformation Patterns

| Task | Before | After |
|------|--------|-------|
| Connect | `with sqlite3.connect(DB_PATH) as conn:` | `db: Session = Depends(get_db)` |
| Insert | `conn.execute("INSERT...")` | `db.add(model); db.commit()` |
| Select | `conn.execute("SELECT...").fetchone()` | `db.query(Model).filter_by(...).first()` |
| Row value | `row["column"]` | `obj.column` |
| Relationships | Manual JOIN | `session.detection_objects` |
| Init | Raw SQL | `Base.metadata.create_all(bind=engine)` |

**Example: Helper function**
```python
# Before: def save_prediction_session(uid, original_image, predicted_image):
#     with sqlite3.connect(DB_PATH) as conn:
#         conn.execute("INSERT INTO prediction_sessions ...", (...))

# After:
def save_prediction_session(db: Session, uid: str, original_image: str, predicted_image: str):
    session = PredictionSession(uid=uid, original_image=original_image, predicted_image=predicted_image)
    db.add(session)
    db.commit()
```

**Example: Endpoint**
```python
# Before: @app.get("/prediction/{uid}")
# def get_prediction_by_uid(uid: str):
#     with sqlite3.connect(DB_PATH) as conn:
#         session = conn.execute("SELECT * FROM ... WHERE uid = ?", (uid,)).fetchone()

# After:
@app.get("/prediction/{uid}")
def get_prediction_by_uid(uid: str, db: Session = Depends(get_db)):
    session = db.query(PredictionSession).filter_by(uid=uid).first()
```

---

## Common Data Layer Tasks

### Task: Add a New Endpoint with ORM Query

**Example:** Add `GET /predictions/recent` returning 10 most recent sessions

```python
from sqlalchemy import desc

@app.get("/predictions/recent")
def get_recent_predictions(limit: int = 10, db: Session = Depends(get_db)):
    """Return the N most recent prediction sessions ordered by timestamp (newest first)."""
    sessions = db.query(PredictionSession).order_by(desc(PredictionSession.timestamp)).limit(limit).all()
    return [
        {
            "uid": s.uid,
            "timestamp": s.timestamp,
            "detection_count": len(s.detection_objects),
            "labels": list(set([obj.label for obj in s.detection_objects]))
        }
        for s in sessions
    ]
```

**Key patterns:**
- Add `db: Session = Depends(get_db)` parameter
- Use `db.query()` with `.order_by()`, `.filter()`, `.limit()`, `.all()`
- Access relationships via `model.related_objects` (e.g., `s.detection_objects`)
- Return new response without changing existing endpoints' schemas

### Task: Add a New Table

**Example:** Add `UserFeedback` table to track ratings per prediction

Update `services/yolo/models.py`:
```python
class UserFeedback(Base):
    __tablename__ = "user_feedback"
    
    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    prediction_uid = Column(String, ForeignKey("prediction_sessions.uid"), index=True)
    rating = Column(Integer)  # 1-5 stars
    comment = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    
    prediction_session = relationship("PredictionSession", backref="feedback")
```

Create new endpoint to receive feedback:
```python
@app.post("/predictions/{uid}/feedback")
def submit_feedback(uid: str, rating: int, comment: str = None, db: Session = Depends(get_db)):
    session = db.query(PredictionSession).filter_by(uid=uid).first()
    if not session:
        raise HTTPException(status_code=404, detail="Prediction not found")
    
    feedback = UserFeedback(prediction_uid=uid, rating=rating, comment=comment)
    db.add(feedback)
    db.commit()
    return {"status": "feedback recorded"}
```

**Key patterns:**
- Define new model with proper foreign keys and relationships
- Use `.backref()` for reverse relationship access (`session.feedback`)
- New endpoints don't modify existing ones
- `db.add()` and `db.commit()` for inserts

### Task: Add a Column to Existing Table

**Example:** Add `processing_time_ms` column to `prediction_sessions`

Update `services/yolo/models.py`:
```python
class PredictionSession(Base):
    __tablename__ = "prediction_sessions"
    
    uid = Column(String, primary_key=True, index=True)
    timestamp = Column(DateTime, server_default=func.now())
    original_image = Column(String)
    predicted_image = Column(String)
    processing_time_ms = Column(Integer, nullable=True)  # ← NEW COLUMN
    
    detection_objects = relationship("DetectionObject", back_populates="prediction_session")
```

Update helper function that saves predictions:
```python
def save_prediction_session(db: Session, uid: str, original_image: str, predicted_image: str, processing_time_ms: int):
    session = PredictionSession(
        uid=uid,
        original_image=original_image,
        predicted_image=predicted_image,
        processing_time_ms=processing_time_ms  # ← PASS NEW VALUE
    )
    db.add(session)
    db.commit()
```

Update `POST /predict` to capture and pass duration:
```python
import time

@app.post("/predict", response_model=YoloPredictResponse)
def predict(file: UploadFile = File(...), db: Session = Depends(get_db)):
    start_time = time.time()
    
    # ... YOLO inference code ...
    
    processing_time_ms = int((time.time() - start_time) * 1000)
    save_prediction_session(db, uid, original_path, predicted_path, processing_time_ms)
```

**Key patterns:**
- Add new `Column()` to existing model
- Make new columns `nullable=True` initially for backward compatibility
- Update helper functions to accept and use new parameters
- Update endpoints to capture and pass new values
- Existing response schemas unchanged; new column available via `.prediction_sessions` query

### Task: Configure Database Backend (SQLite vs PostgreSQL)

**Requirement:** Support both SQLite (local/test) and PostgreSQL (production) via environment variables.

Implement in `services/yolo/db.py`:
```python
import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

DB_BACKEND = os.getenv("DB_BACKEND", "sqlite")
DB_USER = os.getenv("DB_USER", "user")
DB_PASSWORD = os.getenv("DB_PASSWORD", "pass")

if DB_BACKEND == "postgres":
    DATABASE_URL = f"postgresql://{DB_USER}:{DB_PASSWORD}@localhost/db"
else:
    DATABASE_URL = "sqlite:///./predictions.db"

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if "sqlite" in DATABASE_URL else {},
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

def get_db():
    db = SessionLocal()
    try:
        yield db
    except Exception:
        db.rollback()  # 💡 Added: Automatically roll back failed transactions
        raise
    finally:
        db.close()
```

**Usage:**
```bash
# Local development (default SQLite)
python -m uvicorn app:app --reload

# Production with PostgreSQL
export DB_BACKEND=postgres
export DB_USER=prod_user
export DB_PASSWORD=secret_pass
python -m uvicorn app:app
```

**Key patterns:**
- Centralize database configuration in `db.py`
- Use environment variables with sensible defaults
- Support multiple backends with conditional URL construction
- No code changes needed to switch databases—only environment variables

### Task: Implement Cascade Delete (Delete Session & All Its Detection Objects)

**In Model Definition:**
```python
class PredictionSession(Base):
    __tablename__ = "prediction_sessions"
    
    uid = Column(String, primary_key=True, index=True)
    # ... other columns ...
    
    detection_objects = relationship(
        "DetectionObject", 
        back_populates="prediction_session",
        cascade="all, delete-orphan"  # ← CRITICAL: Auto-delete detection objects
    )
```

**Endpoint to Delete Session:**
```python
@app.delete("/predictions/{uid}")
def delete_prediction(uid: str, db: Session = Depends(get_db)):
    """Delete prediction session and all its detection objects."""
    session = db.query(PredictionSession).filter_by(uid=uid).first()
    if not session:
        raise HTTPException(status_code=404, detail="Prediction not found")
    
    # cascade="all, delete-orphan" ensures detection_objects are deleted automatically
    db.delete(session)
    db.commit()
    return {"status": "deleted", "uid": uid}
```

**Key patterns:**
- Use SQLAlchemy `cascade="all, delete-orphan"` on relationship
- Cascade deletes are automatic when parent is deleted
- No manual deletion of related objects needed
- Transaction safety: entire operation succeeds or rolls back atomically

---

## Test Fixtures & Database Isolation


### 🔥 CRITICAL: Fixture Database Isolation Pattern

**Problem:** In-memory SQLite databases (`sqlite:///:memory:`) have a **fundamental threading limitation**: each thread gets a separate database instance. When `TestClient` runs the app in a background thread, it connects to a different in-memory database than the main test thread. Result: "no such table" errors when the app tries to query tables seeded by the test.

**Solution:** Use **file-based SQLite** for tests via temporary files. File-based databases are thread-safe and work correctly with `TestClient`:

```python
import tempfile
import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

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
          db.rollback()  # 💡 Added: Automatically roll back failed transactions
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
```

**Key points:**
- `test_engine` uses **file-based SQLite** (`tempfile.mkstemp()`) instead of `:memory:`
- File-based databases are accessible from **all threads** (main thread + TestClient background thread)
- Both `setup_db` (autouse) and `db_session` depend on `test_engine`, ensuring they share the same database
- Tests using `client` (FastAPI TestClient) query the same database as `db_session` assertions
- Temporary database file is cleaned up after each test via `os.unlink()`
- Test isolation: Each test gets a fresh temporary database file

**❌ COMMON MISTAKES:**
- Using `sqlite:///:memory:` → Each thread gets a separate in-memory database → "no such table" errors with TestClient
- Creating separate engines in different fixtures → Multiple databases instead of one shared database
- Not calling `engine.dispose()` before cleanup → Connection pool not released properly
- Accessing ORM objects like dicts: `row["column"]` → TypeError (use `row.column`)
- Not sharing `test_engine` as a dependency → fixtures use different databases
---

## Implementation Guidelines

### For SQLAlchemy Refactoring Task
- [ ] Create `services/yolo/db.py` (engine, SessionLocal, Base, get_db)
- [ ] Create `services/yolo/models.py` (PredictionSession, DetectionObject ORM models)
- [ ] Update `services/yolo/app.py`:
  - [ ] Fix imports (add SQLAlchemy, models, db; remove sqlite3)
  - [ ] Replace `init_db()` with `Base.metadata.create_all(bind=engine)`
  - [ ] Add `db: Session = Depends(get_db)` to all endpoints
  - [ ] Convert all DB operations to ORM
- [ ] Update `services/yolo/requirements.txt`: append `sqlalchemy>=2.0.0` and `psycopg2-binary>=2.9.0`
- [ ] Refactor test fixtures:
  - [ ] Create `test_engine` fixture (file-based SQLite via `tempfile.mkstemp()`)
  - [ ] Make `setup_db` depend on `test_engine`
  - [ ] Make `db_session` depend on `test_engine`
  - [ ] Update all tests to use ORM patterns
- [ ] Run: `pytest services/yolo/tests/test_api.py -v`
- [ ] Run Core Verification Pipeline:
  - [ ] Run `grep` legacy cleanup checks for `import sqlite3` and `sqlite3.connect` (must return empty)
  - [ ] Run `grep` dependency injections check to verify `Depends(get_db)` anchors
  - [ ] Run `pytest --cov=app --cov-report=term-missing` and confirm coverage is **= 100%** (Coverage is required only for `services/yolo/app.py`)


### For Schema Modifications (Add Column, Add Table)
- [ ] Update model definition in `services/yolo/models.py`
- [ ] Update helper function signatures to accept new parameters
- [ ] Update relevant endpoints to pass new data
- [ ] Create tests for new functionality
- [ ] Run: `pytest services/yolo/tests/test_api.py -v`

### For New Endpoints
- [ ] Define endpoint with `db: Session = Depends(get_db)` parameter
- [ ] Use ORM queries: `db.query()`, `.filter()`, `.order_by()`, `.limit()`
- [ ] Access relationships via model attributes: `model.related_objects`
- [ ] Test new endpoint with proper database seeding
- [ ] Verify existing endpoints unchanged

### For Database Backend Configuration
- [ ] Ensure `services/yolo/db.py` uses environment variables (DB_BACKEND, DB_USER, DB_PASSWORD)
- [ ] Document required environment variables in README
- [ ] Test with both SQLite (default) and PostgreSQL
- [ ] Verify connection pooling and timeout settings for production backend

---

## Prompts That Activate This Skill

This skill handles all of these requests:

- **"Refactor the API to use SQLAlchemy"** — Full migration from raw `sqlite3` to ORM with dependency injection
- **"Add an endpoint GET /predictions/recent that returns the 10 most recent sessions"** — New query endpoint with ordering & limits
- **"Add a UserFeedback table to track user ratings per prediction"** — New model, relationships, and endpoints
- **"Write tests for the /predict endpoint"** — Test fixtures with file-based SQLite isolation
- **"The database layer doesn't follow our architectural design, fix it"** — Refactoring for dependency injection and contract preservation
- **"Delete a prediction session and all its detection objects by uid"** — Cascade deletion patterns
- **"Add a column `processing_time_ms` to the prediction_sessions table"** — Schema modifications with backward compatibility
- **"Make the database backend configurable so we can use postgres in production"** — Environment-driven database routing
