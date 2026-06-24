---
name: yolo-api-data-layer
description: Refactor YOLO service from raw sqlite3 to SQLAlchemy ORM with FastAPI dependency injection.
applyTo: ["services/yolo/**"]
---

# YOLO API Data Layer Refactoring

## Overview

Migrate `/services/yolo/app.py` from raw SQLite queries to declarative SQLAlchemy ORM. All API routes, response schemas, and HTTP status codes remain unchanged.

---

## Architecture Changes

### New Module: `services/yolo/db.py`

**Purpose:** Centralize database engine, session factory, and dependency injection.

Create:
- SQLAlchemy engine with `DB_URL` environment variable support (default: `sqlite:///./predictions.db`)
- SQLite-specific settings: `connect_args={"check_same_thread": False}`
- `SessionLocal` factory via `sessionmaker(autocommit=False, autoflush=False, bind=engine)`
- `Base = declarative_base()` for model inheritance
- `get_db()` generator function for FastAPI dependency injection (yield session, close in finally)

### New Module: `services/yolo/models.py`

**Purpose:** Define ORM model classes mapping to database tables.

Create two classes inheriting from `Base`:

**PredictionSession:**
- Table: `prediction_sessions`
- Columns: `uid` (String, PK), `timestamp` (DateTime, UTC default), `original_image` (String), `predicted_image` (String)
- Relationship: one-to-many with DetectionObject

**DetectionObject:**
- Table: `detection_objects`
- Columns: `id` (Integer, PK, autoincrement), `prediction_uid` (String, FK), `label` (String), `score` (Float), `box` (String)
- Relationship: many-to-one back to PredictionSession
- Indices on: `prediction_uid`, `label`, `score`

---

## Code Transformation Patterns

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

## Preservation Rules

### ✅ DO Modify

- All `sqlite3.connect()` blocks → ORM queries
- `conn.row_factory` patterns → Model attribute access
- Endpoint signatures → Add `db: Session = Depends(get_db)`
- Helper functions → Accept `db: Session` parameter
- Database initialization → `Base.metadata.create_all(bind=engine)`

### ❌ DO NOT Modify

- **Routes & Methods:** `/predict`, `/prediction/{uid}`, `/prediction/{uid}/image`, `/predictions/label/{label}`, `/predictions/score/{min_score}`, `/health`, `/ready` (all GET/POST unchanged)
- **Status Codes:** 200, 400, 404, 503
- **Response Schema:** `YoloPredictResponse` structure and all JSON payloads
- **Business Logic:** Image processing, YOLO inference, file I/O, UUID generation
- **Config:** `CONFIDENCE_THRESHOLD` environment variable handling
- **Shutdown:** SIGTERM handler, `is_shutting_down` flag, `/ready` behavior
- **Monitoring:** Prometheus `/metrics` endpoint

---

## Test Refactoring

**⚠️ CRITICAL: Use in-memory SQLite for tests — never touch the real database file.**

Update `services/yolo/tests/test_api.py`:

**Requirements:**
- ✅ Create in-memory SQLite engine: `create_engine("sqlite:///:memory:")`
- ✅ Call `Base.metadata.create_all(bind=engine)` to initialize schema
- ✅ Override `app.dependency_overrides[get_db]` to inject test session (yield from SessionLocal)
- ✅ Clean up `app.dependency_overrides.clear()` after test completes
- ❌ Do NOT touch `DB_PATH` or real `predictions.db` file
- ❌ Do NOT use `monkeypatch` for database file paths
- ❌ Do NOT modify environment variables for `DB_URL` in tests

**Assertion changes:**
- Replace all `sqlite3.connect()` calls with ORM queries via injected `db` session
- Access columns: `session.uid` instead of `row["uid"]`
- Use relationships: `session.detection_objects` instead of manual JOIN queries

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
- Dont delete tests in test_api.py, keep the coverage of all endpoints and edge cases
---

## Implementation Checklist

- [ ] Create `services/yolo/db.py` (engine, SessionLocal, Base, get_db)
- [ ] Create `services/yolo/models.py` (PredictionSession, DetectionObject)
- [ ] Update `services/yolo/app.py`:
  - [ ] Fix imports (add SQLAlchemy, models, db; remove sqlite3)
  - [ ] Replace `init_db()` with `Base.metadata.create_all(bind=engine)`
  - [ ] Add `db: Session = Depends(get_db)` to all endpoints
  - [ ] Convert all DB operations to ORM
- [ ] Update `services/yolo/requirements.txt`: add `sqlalchemy==2.0.23`, `psycopg2-binary==2.9.9`
- [ ] Refactor test fixtures and assertions:
  - [ ] Create `test_engine` fixture (file-based SQLite via `tempfile.mkstemp()`, shared by all fixtures)
  - [ ] Make `setup_db` depend on `test_engine` (not create its own engine)
  - [ ] Make `db_session` depend on `test_engine` (not create its own engine)
  - [ ] Use attribute access: `obj.column` instead of `obj["column"]`
- [ ] Run: `pytest services/yolo/tests/test_api.py -v`
- [ ] Verify identical API responses and status codes

---

## Verification Against Evaluation JSON

**After completing the implementation checklist above, verify that your generated code completely satisfies all `expected_output` requirements mapped in the evaluation JSON file.**

The evaluation suite is defined in `.agents/skills/yolo-api-data-layer/evals/evals.json` and contains 10 test cases:

1. **db-module-creation** — Verify `services/yolo/db.py` has all required SQLAlchemy components
2. **models-module-creation** — Verify `services/yolo/models.py` has correct ORM model definitions
3. **app-imports-and-db-init** — Verify imports are updated and sqlite3 is removed
4. **endpoint-dependency-injection** — Verify all 5 endpoints have `db: Session = Depends(get_db)`
5. **database-operations-orm-transformation** — Verify all queries use ORM (not raw SQL)
6. **test-fixtures-file-based-database** — Verify test fixtures use file-based SQLite (not in-memory)
7. **test-orm-access-patterns** — Verify test code uses ORM and attribute access
8. **api-contracts-preserved** — Verify all routes, status codes, and response schemas unchanged
9. **predict-endpoint-orm-integration** — Verify POST /predict integrates with ORM correctly
10. **no-sqlite3-raw-sql-anywhere** — Verify no raw SQL strings or sqlite3 usage remains

Before marking the refactoring complete:
- Review each assertion in `evals.json` for your implementation
- Verify all file paths, imports, function signatures, and ORM patterns match expectations
- Ensure no sqlite3 or raw SQL appears anywhere in the codebase
- Confirm all tests pass: `pytest services/yolo/tests/test_api.py -v`

---

## Key Principles

- **No API changes** — Routes, status codes, JSON responses must be identical
- **Environment flexibility** — Support SQLite (default) and PostgreSQL via `DB_URL`
- **Dependency injection** — Always pass FastAPI's `Session` to helpers; never create sessions inside helpers
- **Relationships** — Use ORM attributes (`session.detection_objects`) instead of manual JOINs
- **Transactions** — SQLAlchemy commits automatically; no extra wrapping needed
- **Test isolation** — Use file-based SQLite (via `tempfile`) for tests to ensure TestClient background threads can access the database; share a single engine across all fixtures via fixture dependency; never create separate engines in different fixtures
- **Backward compatibility** — All tests pass with only the database backend changed
- **ORM access patterns** — Always use attribute access (`obj.column`) on ORM instances, never dict subscripting (`obj["column"]`)
