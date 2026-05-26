# Multi-user Face Access Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the smart door recognize multiple named residents (each with one or more reference photos), unlock for any of them, and log which person was granted entry.

**Architecture:** Replace the single cached `owner.jpg` embedding with a relational user model in MySQL (`users` + `user_photos` tables). Photos are stored as files under `assets/users/<photo_id>.jpg` (DB holds the path), and each photo's face embedding is stored in the DB so startup loads vectors without re-embedding. Pure matching/validation logic is factored into a dependency-light `matching.py` so it can be unit-tested without MySQL or DeepFace. `/verify` matches the incoming face against every cached embedding (closest-below-threshold wins) and adds a `"user"` string field on a match.

**Tech Stack:** Python 3.10 (conda env `smartdoor`), Flask, DeepFace (SFace), OpenCV, PyMySQL, Pillow, NumPy. Tests: pytest (pure logic) + `simulate_esp32.py` (integration).

---

## Environment note

All Python commands run inside the conda env that holds the dependencies:

```bash
conda activate smartdoor
cd /Volumes/files/smart-door-system/server
```

MySQL must be running for any task that touches the DB or starts the server:

```bash
sudo /usr/local/mysql/support-files/mysql.server start
```

## File structure

- **Create** `matching.py` — pure, side-effect-free helpers: `cosine_distance`, `best_match`, `embedding_to_bytes`, `bytes_to_embedding`, `validate_name`. No DeepFace/MySQL imports, so it's unit-testable and importable anywhere.
- **Create** `tests/test_matching.py` — pytest unit tests for `matching.py`.
- **Modify** `db.py` — `users`/`user_photos` DDL, `events.person` migration, `assets/users` dir, user/photo CRUD helpers, `person` on `log_event`/`query_events`.
- **Modify** `server.py` — `_OWNERS` cache, `init_owners()` (replaces `init_owner()`), `match_face()` 4-tuple, enrollment helpers (replaces `reenroll_owner`), `/verify` `user` field, `_maybe_log` debounce by `(verdict, person)`, recognizer/overlay show the name, owner-seed-on-empty.
- **Modify** `dashboard.py` — user/photo management routes + `/api/users` + `/user_photos/<id>` (replaces the `/owner` routes); pass `users` to the template.
- **Modify** `templates/dashboard.html` — replace the single "Enrolled owner" card with a "Users" management section; add a "Who" column to the events table.
- **Modify** `static/style.css` — styles for the users section.
- **Modify** `simulate_esp32.py` — add a second-user case.
- **Modify** `.gitignore` — ignore `assets/users/`.
- **Modify** `CLAUDE.md` — document the multi-user model.

---

## Task 1: Pure matching & validation helpers (`matching.py`)

**Files:**
- Create: `matching.py`
- Test: `tests/test_matching.py`

- [ ] **Step 1: Install pytest into the env**

Run:
```bash
conda run -n smartdoor pip install pytest
```
Expected: `Successfully installed pytest-...` (numpy is already present).

- [ ] **Step 2: Write the failing tests**

Create `tests/test_matching.py`:
```python
import numpy as np
import pytest

import matching


def test_cosine_distance_identical_is_zero():
    v = np.array([1.0, 2.0, 3.0], dtype=np.float32)
    assert matching.cosine_distance(v, v) == pytest.approx(0.0, abs=1e-6)


def test_embedding_roundtrip_preserves_values():
    v = np.array([0.1, -0.2, 0.3, 0.4], dtype=np.float32)
    restored = matching.bytes_to_embedding(matching.embedding_to_bytes(v))
    assert restored.dtype == np.float32
    assert np.allclose(restored, v)


def test_best_match_picks_closest_within_threshold():
    target = np.array([1.0, 0.0], dtype=np.float32)
    owners = [
        ("Alice", np.array([1.0, 0.0], dtype=np.float32)),   # distance 0
        ("Bob",   np.array([0.0, 1.0], dtype=np.float32)),   # distance 1
    ]
    name, dist = matching.best_match(target, owners, threshold=0.5)
    assert name == "Alice"
    assert dist == pytest.approx(0.0, abs=1e-6)


def test_best_match_returns_none_but_reports_distance_when_all_above_threshold():
    target = np.array([1.0, 0.0], dtype=np.float32)
    owners = [("Bob", np.array([0.0, 1.0], dtype=np.float32))]  # distance 1
    name, dist = matching.best_match(target, owners, threshold=0.5)
    assert name is None
    assert dist == pytest.approx(1.0, abs=1e-6)


def test_best_match_empty_owners_returns_none_none():
    target = np.array([1.0, 0.0], dtype=np.float32)
    assert matching.best_match(target, [], threshold=0.5) == (None, None)


def test_validate_name_trims_and_accepts():
    assert matching.validate_name("  Alice  ") == (True, "Alice")


def test_validate_name_rejects_empty():
    ok, msg = matching.validate_name("   ")
    assert ok is False
    assert "required" in msg.lower()


def test_validate_name_rejects_too_long():
    ok, msg = matching.validate_name("x" * 65)
    assert ok is False
    assert "64" in msg
```

- [ ] **Step 3: Run the tests to verify they fail**

Run:
```bash
conda run -n smartdoor python -m pytest tests/test_matching.py -v
```
Expected: FAIL — `ModuleNotFoundError: No module named 'matching'`.

- [ ] **Step 4: Write `matching.py`**

Create `matching.py`:
```python
"""Pure, dependency-light helpers for face matching and enrollment.

No DeepFace or MySQL imports here on purpose: this module is safe to import in unit
tests and has no side effects. server.py composes these with the heavy embedding step.
"""
import numpy as np


def cosine_distance(a, b):
    a = np.asarray(a, dtype=np.float32)
    b = np.asarray(b, dtype=np.float32)
    return 1.0 - float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b)))


def best_match(embedding, owners, threshold):
    """Find the closest enrolled face.

    owners: list of (name, embedding_np). Returns (name_or_None, distance_or_None):
      - name is set only when the closest owner is within `threshold`
      - distance is the closest distance found (None when there are no owners)
    """
    best_name, best_dist = None, None
    for name, owner_emb in owners:
        d = cosine_distance(embedding, owner_emb)
        if best_dist is None or d < best_dist:
            best_name, best_dist = name, d
    if best_dist is None:
        return None, None
    if best_dist <= threshold:
        return best_name, best_dist
    return None, best_dist


def embedding_to_bytes(emb):
    """Serialize an embedding to raw float32 bytes for DB storage."""
    return np.asarray(emb, dtype=np.float32).tobytes()


def bytes_to_embedding(blob):
    """Inverse of embedding_to_bytes."""
    return np.frombuffer(blob, dtype=np.float32)


def validate_name(raw):
    """Validate a user-supplied name. Returns (True, cleaned) or (False, error)."""
    name = (raw or "").strip()
    if not name:
        return False, "Name is required."
    if len(name) > 64:
        return False, "Name must be 64 characters or fewer."
    return True, name
```

- [ ] **Step 5: Run the tests to verify they pass**

Run:
```bash
conda run -n smartdoor python -m pytest tests/test_matching.py -v
```
Expected: PASS — 8 passed.

- [ ] **Step 6: Commit**

```bash
git add matching.py tests/test_matching.py
git commit -m "feat: pure matching/validation helpers with unit tests

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: Database layer — users, photos, and person logging (`db.py`)

**Files:**
- Modify: `db.py`

This task has no fast unit test (it needs MySQL); it's verified with a small DB script in Step 7.

- [ ] **Step 1: Add the assets dir and new table DDL**

In `db.py`, after the `SNAPSHOT_DIR` line (currently `SNAPSHOT_DIR = _BASE / "snapshots"`), add:
```python
ASSETS_USERS_DIR = _BASE / "assets" / "users"
```

After the existing `_DDL` string (the `events` table), add:
```python
_DDL_USERS = """
CREATE TABLE IF NOT EXISTS users (
    id         INT AUTO_INCREMENT PRIMARY KEY,
    name       VARCHAR(64) NOT NULL UNIQUE,
    created_at DOUBLE NOT NULL
) ENGINE=InnoDB
"""

_DDL_USER_PHOTOS = """
CREATE TABLE IF NOT EXISTS user_photos (
    id         INT AUTO_INCREMENT PRIMARY KEY,
    user_id    INT NOT NULL,
    image_path VARCHAR(255) NOT NULL,
    embedding  BLOB NOT NULL,
    model_name VARCHAR(32) NOT NULL,
    created_at DOUBLE NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
    INDEX idx_user_photos_user (user_id)
) ENGINE=InnoDB
"""
```

- [ ] **Step 2: Extend `init_db()` to create dirs/tables and migrate `events`**

Replace the existing `init_db()`:
```python
def init_db():
    """Create the snapshots dir and the events table if missing."""
    SNAPSHOT_DIR.mkdir(exist_ok=True)
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(_DDL)
```
with:
```python
def init_db():
    """Create dirs and tables if missing, and migrate the events table."""
    SNAPSHOT_DIR.mkdir(exist_ok=True)
    ASSETS_USERS_DIR.mkdir(parents=True, exist_ok=True)
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(_DDL)
        cur.execute(_DDL_USERS)
        cur.execute(_DDL_USER_PHOTOS)
        _ensure_person_column(cur)


def _ensure_person_column(cur):
    """Add events.person once (no IF NOT EXISTS for ADD COLUMN on older MySQL)."""
    cur.execute(
        "SELECT COUNT(*) c FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA=%s AND TABLE_NAME='events' AND COLUMN_NAME='person'",
        (os.environ.get("MYSQL_DB", "smartdoor"),),
    )
    if cur.fetchone()["c"] == 0:
        cur.execute("ALTER TABLE events ADD COLUMN person VARCHAR(64) NULL")
```

- [ ] **Step 3: Thread `person` through logging and queries**

Replace `log_event(...)`:
```python
def log_event(verdict, distance, threshold, jpeg_bytes=None):
    """Insert one event; if jpeg_bytes given, save snapshots/<id>.jpg and record it.

    Returns the new event id.
    """
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO events (ts, verdict, distance, threshold) VALUES (%s,%s,%s,%s)",
            (time.time(), verdict, distance, threshold),
        )
        event_id = cur.lastrowid
        if jpeg_bytes:
            name = f"{event_id}.jpg"
            (SNAPSHOT_DIR / name).write_bytes(jpeg_bytes)
            cur.execute("UPDATE events SET snapshot_path=%s WHERE id=%s", (name, event_id))
    return event_id
```
with:
```python
def log_event(verdict, distance, threshold, person=None, jpeg_bytes=None):
    """Insert one event; if jpeg_bytes given, save snapshots/<id>.jpg and record it.

    `person` is the matched user's name (NULL for denied). Returns the new event id.
    """
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO events (ts, verdict, distance, threshold, person) "
            "VALUES (%s,%s,%s,%s,%s)",
            (time.time(), verdict, distance, threshold, person),
        )
        event_id = cur.lastrowid
        if jpeg_bytes:
            name = f"{event_id}.jpg"
            (SNAPSHOT_DIR / name).write_bytes(jpeg_bytes)
            cur.execute("UPDATE events SET snapshot_path=%s WHERE id=%s", (name, event_id))
    return event_id
```

In `query_events`, replace the SELECT column list line:
```python
    sql = "SELECT id, ts, verdict, distance, threshold, snapshot_path FROM events"
```
with:
```python
    sql = "SELECT id, ts, verdict, distance, threshold, person, snapshot_path FROM events"
```

- [ ] **Step 4: Add user/photo CRUD helpers**

Add these functions to `db.py` (place them after `query_events`):
```python
def create_user(name):
    """Insert a user. Raises pymysql.err.IntegrityError on a duplicate name."""
    with _connect() as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO users (name, created_at) VALUES (%s,%s)",
                    (name, time.time()))
        return cur.lastrowid


def add_photo(user_id, jpeg_bytes, embedding_bytes, model_name):
    """Insert a photo row, write assets/users/<id>.jpg, record the path. Returns id."""
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO user_photos (user_id, image_path, embedding, model_name, created_at) "
            "VALUES (%s,%s,%s,%s,%s)",
            (user_id, "", embedding_bytes, model_name, time.time()),
        )
        photo_id = cur.lastrowid
        name = f"{photo_id}.jpg"
        (ASSETS_USERS_DIR / name).write_bytes(jpeg_bytes)
        cur.execute("UPDATE user_photos SET image_path=%s WHERE id=%s", (name, photo_id))
    return photo_id


def all_photos():
    """Every photo joined to its user's name. Used to build the in-memory match cache."""
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT p.id, p.user_id, p.image_path, p.embedding, p.model_name, u.name "
            "FROM user_photos p JOIN users u ON u.id = p.user_id"
        )
        return cur.fetchall()


def update_embedding(photo_id, embedding_bytes, model_name):
    """Refresh a stored embedding (used when FACE_MODEL changed since enrollment)."""
    with _connect() as conn, conn.cursor() as cur:
        cur.execute("UPDATE user_photos SET embedding=%s, model_name=%s WHERE id=%s",
                    (embedding_bytes, model_name, photo_id))


def user_count():
    with _connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) c FROM users")
        return cur.fetchone()["c"]


def list_users():
    """[{id, name, photos: [photo_id, ...]}, ...], ordered by name."""
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT u.id, u.name, p.id pid FROM users u "
            "LEFT JOIN user_photos p ON p.user_id = u.id "
            "ORDER BY u.name, p.id"
        )
        rows = cur.fetchall()
    users, by_id = [], {}
    for r in rows:
        u = by_id.get(r["id"])
        if u is None:
            u = {"id": r["id"], "name": r["name"], "photos": []}
            by_id[r["id"]] = u
            users.append(u)
        if r["pid"] is not None:
            u["photos"].append(r["pid"])
    return users


def get_photo(photo_id):
    """Row {id, image_path} for serving, or None."""
    with _connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT id, image_path FROM user_photos WHERE id=%s", (photo_id,))
        return cur.fetchone()


def delete_photo(photo_id):
    """Delete one photo row and unlink its file."""
    with _connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT image_path FROM user_photos WHERE id=%s", (photo_id,))
        row = cur.fetchone()
        if not row:
            return
        cur.execute("DELETE FROM user_photos WHERE id=%s", (photo_id,))
    if row["image_path"]:
        (ASSETS_USERS_DIR / row["image_path"]).unlink(missing_ok=True)


def delete_user(user_id):
    """Delete a user (cascades to photo rows) and unlink all their photo files."""
    with _connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT image_path FROM user_photos WHERE user_id=%s", (user_id,))
        paths = [r["image_path"] for r in cur.fetchall()]
        cur.execute("DELETE FROM users WHERE id=%s", (user_id,))
    for p in paths:
        if p:
            (ASSETS_USERS_DIR / p).unlink(missing_ok=True)
```

- [ ] **Step 5: Run a DB smoke check**

Run (MySQL must be up):
```bash
conda run -n smartdoor python -c "
import db
db.init_db()
uid = db.create_user('TestPerson')
pid = db.add_photo(uid, b'\xff\xd8\xff\xd9', b'\x00\x00\x80\x3f', 'SFace')  # dummy
print('users:', db.list_users())
print('photos rows:', len(db.all_photos()))
db.delete_user(uid)
print('after delete:', db.list_users())
"
```
Expected: prints a user with one photo id, `photos rows: 1`, then `after delete: []`. No traceback. (The dummy bytes are not a real image — that's fine here; real validation happens in server.py.)

- [ ] **Step 6: Confirm the events.person migration is idempotent**

Run:
```bash
conda run -n smartdoor python -c "import db; db.init_db(); db.init_db(); print('init_db ran twice cleanly')"
```
Expected: `init_db ran twice cleanly` with no "Duplicate column" error.

- [ ] **Step 7: Commit**

```bash
git add db.py
git commit -m "feat: users/user_photos tables, person logging, photo CRUD

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: Server wiring — cache, matching, enrollment, response (`server.py`)

**Files:**
- Modify: `server.py`

- [ ] **Step 1: Import the matching helpers**

In `server.py`, change:
```python
import db          # loads .env (MYSQL_*, DASHBOARD_PASSWORD, SECRET_KEY) on import
import dashboard
```
to:
```python
import db          # loads .env (MYSQL_*, DASHBOARD_PASSWORD, SECRET_KEY) on import
import dashboard
import matching
```

- [ ] **Step 2: Replace the cache globals**

Change:
```python
# Owner's face embedding is computed once at startup so each /verify only has to embed
# the incoming frame, not re-process owner.jpg every call.
_OWNER_EMBEDDING = None
_THRESHOLD = None
```
to:
```python
# Enrolled faces are cached at startup so each /verify only embeds the incoming frame.
# _OWNERS is a list of (user_name, embedding_np); the closest one within _THRESHOLD wins.
_OWNERS = []
_THRESHOLD = None
```

- [ ] **Step 3: Replace `init_owner()` with `init_owners()`**

Replace the whole `init_owner()` function:
```python
def init_owner():
    """Build the model, capture its threshold, and cache the owner embedding."""
    global _OWNER_EMBEDDING, _THRESHOLD
    res = DeepFace.verify(OWNER_IMG, OWNER_IMG, model_name=MODEL_NAME,
                          detector_backend=DETECTOR, enforce_detection=False)
    _THRESHOLD = float(MATCH_THRESHOLD) if MATCH_THRESHOLD is not None \
        else float(res["threshold"])
    _OWNER_EMBEDDING = _embed(OWNER_IMG)
    print(f"[init] model={MODEL_NAME} threshold={_THRESHOLD:.4f} owner embedding cached")
```
with:
```python
def _capture_threshold():
    """The cosine threshold for the active model (MATCH_THRESHOLD override wins)."""
    if MATCH_THRESHOLD is not None:
        return float(MATCH_THRESHOLD)
    if os.path.exists(OWNER_IMG):
        # Doubles as a model warm-up; preserves the previous boot threshold exactly.
        res = DeepFace.verify(OWNER_IMG, OWNER_IMG, model_name=MODEL_NAME,
                              detector_backend=DETECTOR, enforce_detection=False)
        return float(res["threshold"])
    from deepface.modules import verification as _v
    return float(_v.find_threshold(MODEL_NAME, "cosine"))


def init_owners():
    """Capture the model threshold and (re)load all enrolled embeddings from the DB.

    Photos embedded under a different model are re-embedded from their on-disk image and
    the DB row is updated, so changing FACE_MODEL self-heals on the next startup.
    """
    global _OWNERS, _THRESHOLD
    _THRESHOLD = _capture_threshold()
    owners = []
    for row in db.all_photos():
        if row["model_name"] == MODEL_NAME:
            emb = matching.bytes_to_embedding(row["embedding"])
        else:
            path = str(db.ASSETS_USERS_DIR / row["image_path"])
            emb = _embed(path)
            db.update_embedding(row["id"], matching.embedding_to_bytes(emb), MODEL_NAME)
        owners.append((row["name"], emb))
    _OWNERS = owners
    print(f"[init] model={MODEL_NAME} threshold={_THRESHOLD:.4f} owners={len(_OWNERS)}")
```

- [ ] **Step 4: Update `match_face()` to return the matched person**

Replace `match_face()`:
```python
def match_face(img):
    """Compare img against the cached owner embedding. img = file path or BGR frame.

    Returns (reason, distance, threshold). distance/threshold are None when no face is
    present or on error.
    """
    try:
        emb = _embed(img)
    except Exception as e:
        if _is_no_face(e):
            return "no_face", None, None
        return "error", None, None
    distance = _cosine_distance(_OWNER_EMBEDDING, emb)
    reason = "match" if distance <= _THRESHOLD else "no_match"
    return reason, round(distance, 4), round(_THRESHOLD, 4)
```
with:
```python
def match_face(img):
    """Compare img against every enrolled embedding. img = file path or BGR frame.

    Returns (reason, distance, threshold, person). On a match, person is the matched
    user's name. distance is the closest distance (None with no enrolled faces);
    distance/threshold/person are all None for no_face/error.
    """
    try:
        emb = _embed(img)
    except Exception as e:
        if _is_no_face(e):
            return "no_face", None, None, None
        return "error", None, None, None
    name, distance = matching.best_match(emb, _OWNERS, _THRESHOLD)
    reason = "match" if name else "no_match"
    dist = round(distance, 4) if distance is not None else None
    return reason, dist, round(_THRESHOLD, 4), name
```

Then delete the now-unused `_cosine_distance` helper:
```python
def _cosine_distance(a, b):
    return 1.0 - float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b)))
```

- [ ] **Step 5: Replace `reenroll_owner` with enrollment helpers**

Replace the entire `reenroll_owner(jpeg_bytes)` function with:
```python
def _validate_face_and_embed(jpeg_bytes):
    """Validate bytes are an image with a detectable face and embed it.

    Returns (True, embedding_np, normalized_jpeg_bytes) or (False, error_message, None).
    JPEG bytes are kept verbatim (a default-quality re-encode wrecks the embedding);
    non-JPEG inputs are re-encoded at quality=95.
    """
    try:
        img = Image.open(io.BytesIO(jpeg_bytes))
        img.load()
    except Exception as e:
        return False, f"Not a valid image: {e}", None
    fd, tmp_path = tempfile.mkstemp(suffix='.jpg')
    os.close(fd)
    try:
        if (img.format or "").upper() == "JPEG":
            out_bytes = jpeg_bytes
            with open(tmp_path, "wb") as f:
                f.write(jpeg_bytes)
        else:
            img.convert("RGB").save(tmp_path, format="JPEG", quality=95)
            with open(tmp_path, "rb") as f:
                out_bytes = f.read()
        try:
            emb = _embed(tmp_path)
        except Exception as e:
            if _is_no_face(e):
                return False, "No face detected in that photo.", None
            return False, "Could not process that photo.", None
        return True, emb, out_bytes
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


def enroll_user(name_raw, jpeg_bytes):
    """Create a new user from a name + first photo. Returns (ok, message)."""
    ok, name = matching.validate_name(name_raw)
    if not ok:
        return False, name
    ok, emb_or_msg, out_bytes = _validate_face_and_embed(jpeg_bytes)
    if not ok:
        return False, emb_or_msg
    try:
        user_id = db.create_user(name)
    except Exception:
        return False, f"A user named '{name}' already exists."
    db.add_photo(user_id, out_bytes, matching.embedding_to_bytes(emb_or_msg), MODEL_NAME)
    init_owners()
    return True, f"Enrolled {name}."


def add_user_photo(user_id, jpeg_bytes):
    """Add another reference photo to an existing user. Returns (ok, message)."""
    ok, emb_or_msg, out_bytes = _validate_face_and_embed(jpeg_bytes)
    if not ok:
        return False, emb_or_msg
    db.add_photo(int(user_id), out_bytes,
                 matching.embedding_to_bytes(emb_or_msg), MODEL_NAME)
    init_owners()
    return True, "Photo added."


def _seed_owner_if_empty():
    """First run: migrate the legacy owner.jpg into an 'Owner' user."""
    if db.user_count() == 0 and os.path.exists(OWNER_IMG):
        with open(OWNER_IMG, "rb") as f:
            ok, msg = enroll_user("Owner", f.read())
        print(f"[seed] {msg}")
```

- [ ] **Step 6: Update `_maybe_log` to debounce by (verdict, person)**

Replace:
```python
_last_log = {"verdict": None, "ts": 0.0}
```
with:
```python
_last_log = {"key": None, "ts": 0.0}
```

Replace `_maybe_log(...)`:
```python
def _maybe_log(reason, distance, threshold, jpeg_bytes):
    """Log granted/denied events only, skipping repeats within LOG_DEBOUNCE_S. Logging
    failures never affect the door verdict."""
    global _log_count
    verdict = _VERDICT_MAP.get(reason)
    if verdict is None:                       # no_face / error -> not logged
        return
    now = time.time()
    if verdict == _last_log["verdict"] and now - _last_log["ts"] < LOG_DEBOUNCE_S:
        return
    _last_log["verdict"] = verdict
    _last_log["ts"] = now
    try:
        db.log_event(verdict, distance, threshold, jpeg_bytes=jpeg_bytes)
        _log_count += 1
        if _log_count % 50 == 0:
            db.prune()
    except Exception as ex:
        print("[log] failed (door unaffected):", ex)
```
with:
```python
def _maybe_log(reason, distance, threshold, person, jpeg_bytes):
    """Log granted/denied events only, skipping repeats of the same (verdict, person)
    within LOG_DEBOUNCE_S so two different people in a row each log a row. Logging
    failures never affect the door verdict."""
    global _log_count
    verdict = _VERDICT_MAP.get(reason)
    if verdict is None:                       # no_face / error -> not logged
        return
    now = time.time()
    key = (verdict, person)
    if key == _last_log["key"] and now - _last_log["ts"] < LOG_DEBOUNCE_S:
        return
    _last_log["key"] = key
    _last_log["ts"] = now
    try:
        db.log_event(verdict, distance, threshold, person=person, jpeg_bytes=jpeg_bytes)
        _log_count += 1
        if _log_count % 50 == 0:
            db.prune()
    except Exception as ex:
        print("[log] failed (door unaffected):", ex)
```

- [ ] **Step 7: Update `/verify` to use the 4-tuple and emit `user`**

In `verify()`, replace:
```python
        reason, distance, threshold = match_face(tmp_path)
        _maybe_log(reason, distance, threshold, raw)

        if reason in ("match", "no_match"):
            return jsonify({"verified": reason == "match", "reason": reason,
                            "distance": distance, "threshold": threshold})
        if reason == "no_face":
            return jsonify({"verified": False, "reason": "no_face"})
        return jsonify({"verified": False, "reason": "error",
                        "error": "verification failed"})
```
with:
```python
        reason, distance, threshold, person = match_face(tmp_path)
        _maybe_log(reason, distance, threshold, person, raw)

        if reason in ("match", "no_match"):
            body = {"verified": reason == "match", "reason": reason,
                    "distance": distance, "threshold": threshold}
            if person:                       # string field; never a second boolean true
                body["user"] = person
            return jsonify(body)
        if reason == "no_face":
            return jsonify({"verified": False, "reason": "no_face"})
        return jsonify({"verified": False, "reason": "error",
                        "error": "verification failed"})
```

- [ ] **Step 8: Show the matched name in the live viewer**

Change the recognizer's initial verdict tuple:
```python
_view_verdict = ("warming", None, None)
```
to:
```python
_view_verdict = ("warming", None, None, None)
```

Replace `_draw(...)`:
```python
def _draw(bgr, faces, reason, distance, threshold):
    color, label = _VERDICT_STYLE.get(reason, ((200, 200, 200), reason))
    for (x, y, w, h) in faces:
        cv2.rectangle(bgr, (x, y), (x + w, y + h), color, 2)
    if distance is not None:
        label += f"  d={distance:.2f}/{threshold:.2f}"
    cv2.putText(bgr, label, (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.8, color, 2)
    return bgr
```
with:
```python
def _draw(bgr, faces, reason, distance, threshold, person=None):
    color, label = _VERDICT_STYLE.get(reason, ((200, 200, 200), reason))
    if person:
        label = person                       # show who matched instead of "MATCH (owner)"
    for (x, y, w, h) in faces:
        cv2.rectangle(bgr, (x, y), (x + w, y + h), color, 2)
    if distance is not None:
        label += f"  d={distance:.2f}/{threshold:.2f}"
    cv2.putText(bgr, label, (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.8, color, 2)
    return bgr
```

In `_annotated_stream`, replace:
```python
            reason, distance, threshold = _view_verdict   # atomic tuple read
            _draw(frame, _detect_faces(frame), reason, distance, threshold)
```
with:
```python
            reason, distance, threshold, person = _view_verdict   # atomic tuple read
            _draw(frame, _detect_faces(frame), reason, distance, threshold, person)
```

- [ ] **Step 9: Fix the startup order at the bottom of the module**

Replace:
```python
# Warm up the model and cache the owner embedding at import time so the first /verify
# is fast (and so failures surface at startup, not on the first door check).
init_owner()
db.init_db()
```
with:
```python
# At import: create tables, migrate the legacy owner.jpg into an "Owner" user on first
# run, then cache all enrolled embeddings so the first /verify is fast and failures
# surface at startup rather than on the first door check.
db.init_db()
_seed_owner_if_empty()
init_owners()
```

- [ ] **Step 10: Start the server and run the smoke test**

Start the server (MySQL must be up):
```bash
conda run -n smartdoor python server.py
```
Expected console: `[seed] Enrolled Owner.` (first run only) then
`[init] model=SFace threshold=0.5930 owners=1`.

In another terminal:
```bash
curl -s -X POST -H "Content-Type: application/octet-stream" \
     --data-binary @owner.jpg http://127.0.0.1:8080/verify
```
Expected: `{"distance":0.0,...,"reason":"match","threshold":0.593,"user":"Owner","verified":true}`.

- [ ] **Step 11: Confirm pure-logic tests still pass and commit**

Stop the server (Ctrl-C). Run:
```bash
conda run -n smartdoor python -m pytest tests/test_matching.py -v
```
Expected: PASS — 8 passed.

```bash
git add server.py
git commit -m "feat: multi-user matching, enrollment, and owner seeding

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: Dashboard routes (`dashboard.py`)

**Files:**
- Modify: `dashboard.py`

- [ ] **Step 1: Add `abort` to the Flask imports**

Change:
```python
from flask import (Blueprint, request, session, redirect, url_for,
                   render_template, jsonify, send_from_directory)
```
to:
```python
from flask import (Blueprint, request, session, redirect, url_for,
                   render_template, jsonify, send_from_directory, abort)
```

- [ ] **Step 2: Pass users to the dashboard template**

Replace:
```python
@bp.route("/dashboard")
@login_required
def home():
    return render_template("dashboard.html")
```
with:
```python
@bp.route("/dashboard")
@login_required
def home():
    return render_template("dashboard.html", users=db.list_users())
```

- [ ] **Step 3: Replace the owner routes with user/photo routes**

Replace this block:
```python
@bp.route("/owner.jpg")
@login_required
def owner_photo():
    import server
    return send_from_directory(str(db._BASE), server.OWNER_IMG,
                               mimetype="image/jpeg")


@bp.route("/owner", methods=["POST"])
@login_required
def owner_upload():
    import server
    file = request.files.get("photo")
    if not file:
        return redirect(url_for("dashboard.home", msg="No file selected"))
    ok, message = server.reenroll_owner(file.read())
    return redirect(url_for("dashboard.home", msg=message))
```
with:
```python
@bp.route("/api/users")
@login_required
def api_users():
    return jsonify(db.list_users())


@bp.route("/user_photos/<int:pid>")
@login_required
def user_photo(pid):
    row = db.get_photo(pid)
    if not row:
        abort(404)
    return send_from_directory(str(db.ASSETS_USERS_DIR), row["image_path"],
                               mimetype="image/jpeg")


@bp.route("/users", methods=["POST"])
@login_required
def users_create():
    import server
    file = request.files.get("photo")
    if not file:
        return redirect(url_for("dashboard.home", msg="No file selected"))
    ok, message = server.enroll_user(request.form.get("name", ""), file.read())
    return redirect(url_for("dashboard.home", msg=message))


@bp.route("/users/<int:uid>/photos", methods=["POST"])
@login_required
def users_add_photo(uid):
    import server
    file = request.files.get("photo")
    if not file:
        return redirect(url_for("dashboard.home", msg="No file selected"))
    ok, message = server.add_user_photo(uid, file.read())
    return redirect(url_for("dashboard.home", msg=message))


@bp.route("/users/<int:uid>/delete", methods=["POST"])
@login_required
def users_delete(uid):
    import server
    db.delete_user(uid)
    server.init_owners()
    return redirect(url_for("dashboard.home", msg="User removed."))


@bp.route("/photos/<int:pid>/delete", methods=["POST"])
@login_required
def photo_delete(pid):
    import server
    db.delete_photo(pid)
    server.init_owners()
    return redirect(url_for("dashboard.home", msg="Photo removed."))
```

- [ ] **Step 4: Verify the app imports cleanly**

Run:
```bash
conda run -n smartdoor python -c "import server; print('import OK')"
```
Expected: prints the `[init]` line and `import OK`, no traceback.

- [ ] **Step 5: Commit**

```bash
git add dashboard.py
git commit -m "feat: dashboard routes for user/photo management

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: Dashboard UI (`templates/dashboard.html`, `static/style.css`)

**Files:**
- Modify: `templates/dashboard.html`
- Modify: `static/style.css`

- [ ] **Step 1: Shrink the stat grid to 3 columns**

In `static/style.css`, change:
```css
.cards { display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; }
```
to:
```css
.cards { display: grid; grid-template-columns: repeat(3, 1fr); gap: 16px; }
```

- [ ] **Step 2: Remove the owner card from the stat grid**

In `templates/dashboard.html`, delete this block (lines within the `<section class="cards">`):
```html
      <div class="card owner">
        <div class="stat-label">Enrolled owner</div>
        <img id="owner-img" src="{{ url_for('dashboard.owner_photo') }}?t=0" alt="owner">
        <form class="owner-form" method="post" action="{{ url_for('dashboard.owner_upload') }}" enctype="multipart/form-data">
          <input type="file" name="photo" accept="image/*" required>
          <button type="submit">Re-enroll</button>
        </form>
      </div>
```

- [ ] **Step 3: Add the Users management section**

In `templates/dashboard.html`, immediately after the closing `</section>` of `<section class="cards">` (and before `<section class="grid-2">`), insert:
```html
    <section class="card users-card">
      <div class="card-title">Users <span class="muted">· {{ users|length }} enrolled</span></div>

      {% for u in users %}
      <div class="user-row">
        <div class="user-head">
          <span class="user-name">{{ u.name }}</span>
          <form method="post" action="{{ url_for('dashboard.users_delete', uid=u.id) }}"
                onsubmit="return confirm('Remove {{ u.name }} and all their photos?');">
            <button class="btn-del" type="submit">delete user</button>
          </form>
        </div>
        <div class="user-photos">
          {% for pid in u.photos %}
          <div class="user-photo">
            <img src="{{ url_for('dashboard.user_photo', pid=pid) }}" alt="{{ u.name }}">
            <form method="post" action="{{ url_for('dashboard.photo_delete', pid=pid) }}">
              <button class="btn-del-x" type="submit" title="remove photo">&times;</button>
            </form>
          </div>
          {% else %}
          <span class="muted">no photos — this user can't match</span>
          {% endfor %}
          <form class="add-photo" method="post"
                action="{{ url_for('dashboard.users_add_photo', uid=u.id) }}"
                enctype="multipart/form-data">
            <input type="file" name="photo" accept="image/*" required>
            <button type="submit">+ add photo</button>
          </form>
        </div>
      </div>
      {% else %}
      <p class="muted">No users enrolled yet. Add one below.</p>
      {% endfor %}

      <form class="add-user" method="post" action="{{ url_for('dashboard.users_create') }}"
            enctype="multipart/form-data">
        <input type="text" name="name" placeholder="Name" maxlength="64" required>
        <input type="file" name="photo" accept="image/*" required>
        <button type="submit">Add user</button>
      </form>
    </section>
```

- [ ] **Step 4: Add a "Who" column to the events table**

In `templates/dashboard.html`, change the events table head:
```html
        <thead><tr><th></th><th>Time</th><th>Verdict</th><th>Distance</th></tr></thead>
```
to:
```html
        <thead><tr><th></th><th>Time</th><th>Verdict</th><th>Who</th><th>Distance</th></tr></thead>
```

In the `refreshEvents()` JS, change the row template:
```javascript
        <tr>
          <td>${r.snapshot_path ? `<img class="thumb" src="/snapshots/${r.snapshot_path}">` : ''}</td>
          <td>${fmtTime(r.ts)}</td>
          <td><span class="badge ${r.verdict === 'granted' ? 'ok' : 'bad'}">${r.verdict}</span></td>
          <td>${r.distance != null ? r.distance.toFixed(3) : '—'}</td>
        </tr>`).join('') || `<tr><td colspan="4" class="muted">No events yet.</td></tr>`;
```
to:
```javascript
        <tr>
          <td>${r.snapshot_path ? `<img class="thumb" src="/snapshots/${r.snapshot_path}">` : ''}</td>
          <td>${fmtTime(r.ts)}</td>
          <td><span class="badge ${r.verdict === 'granted' ? 'ok' : 'bad'}">${r.verdict}</span></td>
          <td>${r.person || '—'}</td>
          <td>${r.distance != null ? r.distance.toFixed(3) : '—'}</td>
        </tr>`).join('') || `<tr><td colspan="5" class="muted">No events yet.</td></tr>`;
```

- [ ] **Step 5: Add styles for the users section**

In `static/style.css`, replace the owner block:
```css
.owner { display: flex; flex-direction: column; }
.owner img { width: 64px; height: 64px; object-fit: cover; border-radius: 10px;
             margin: 8px 0; border: 1px solid var(--border); }
.owner-form { display: flex; gap: 6px; }
.owner-form input[type=file] { font-size: 11px; color: var(--muted); width: 100%; }
.owner-form button, #range, #filter {
  background: var(--panel-2); color: var(--text);
  border: 1px solid var(--border); border-radius: 8px; padding: 6px 10px;
  font-size: 13px; cursor: pointer;
}
```
with:
```css
/* shared control look (was .owner-form button) */
.owner-form button, #range, #filter,
.users-card button, .users-card input[type=text] {
  background: var(--panel-2); color: var(--text);
  border: 1px solid var(--border); border-radius: 8px; padding: 6px 10px;
  font-size: 13px; cursor: pointer;
}

/* ---- users management ---- */
.user-row { padding: 10px 0; border-bottom: 1px solid var(--border); }
.user-head { display: flex; align-items: center; justify-content: space-between; }
.user-name { font-weight: 600; }
.user-photos { display: flex; flex-wrap: wrap; gap: 10px; align-items: center;
               margin-top: 8px; }
.user-photo { position: relative; }
.user-photo img { width: 56px; height: 56px; object-fit: cover; border-radius: 8px;
                  border: 1px solid var(--border); display: block; }
.btn-del-x { position: absolute; top: -6px; right: -6px; width: 20px; height: 20px;
             line-height: 1; padding: 0; border-radius: 999px; }
.btn-del { background: rgba(239,68,68,.15); color: var(--bad);
           border: 1px solid var(--border); border-radius: 8px; padding: 4px 10px;
           font-size: 12px; cursor: pointer; }
.add-photo input[type=file] { font-size: 11px; color: var(--muted); width: 130px; }
.add-user { display: flex; gap: 8px; flex-wrap: wrap; align-items: center;
            margin-top: 14px; }
.add-user input[type=file] { font-size: 12px; color: var(--muted); }
```

- [ ] **Step 6: Visually verify in the browser**

Start the server (`conda run -n smartdoor python server.py`), open `http://127.0.0.1:8080/dashboard`, log in (`DASHBOARD_PASSWORD`, default `admin`). Confirm:
- The "Users" card lists "Owner" with one thumbnail.
- "Add user" with a name + a face photo adds a second user; a non-face image shows "No face detected in that photo."; a duplicate name shows the "already exists" message.
- "+ add photo" adds a second thumbnail to a user.
- The "×" on a thumbnail removes that photo; "delete user" removes the user (after the confirm dialog).
- The events table shows a "Who" column.

- [ ] **Step 7: Commit**

```bash
git add templates/dashboard.html static/style.css
git commit -m "feat: dashboard UI for managing users and photos

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 6: Integration test, gitignore, docs, end-to-end check

**Files:**
- Modify: `.gitignore`
- Modify: `simulate_esp32.py`
- Modify: `CLAUDE.md`

- [ ] **Step 1: Ignore the user photos directory**

In `.gitignore`, add a line after `.env`:
```
assets/users/
```

- [ ] **Step 2: Add a second-user case to the simulator**

In `simulate_esp32.py`, replace the `--stranger` argument and its handling.

Change the argparse block:
```python
    ap.add_argument("--owner", default="owner.jpg")
    ap.add_argument("--stranger", help="path to a non-owner face image (expect BUZZER)")
    args = ap.parse_args()
```
to:
```python
    ap.add_argument("--owner", default="owner.jpg")
    ap.add_argument("--user", help="path to a second enrolled person's face (expect UNLOCK)")
    ap.add_argument("--stranger", help="path to a non-enrolled face image (expect BUZZER)")
    args = ap.parse_args()
```

Change the case-running block:
```python
    with open(args.owner, "rb") as f:
        run_case("owner    (expect UNLOCK)", f.read(), args.url)

    if args.stranger:
        with open(args.stranger, "rb") as f:
            run_case("stranger (expect BUZZER)", f.read(), args.url)
    else:
        print("[stranger (expect BUZZER)] skipped - pass --stranger <path> to test a non-owner face")

    run_case("no-face  (expect IDLE)  ", blank_image_bytes(), args.url)
```
to:
```python
    with open(args.owner, "rb") as f:
        run_case("owner    (expect UNLOCK)", f.read(), args.url)

    if args.user:
        with open(args.user, "rb") as f:
            run_case("2nd user (expect UNLOCK)", f.read(), args.url)
    else:
        print("[2nd user (expect UNLOCK)] skipped - pass --user <path> to test a second enrolled person")

    if args.stranger:
        with open(args.stranger, "rb") as f:
            run_case("stranger (expect BUZZER)", f.read(), args.url)
    else:
        print("[stranger (expect BUZZER)] skipped - pass --stranger <path> to test a non-enrolled face")

    run_case("no-face  (expect IDLE)  ", blank_image_bytes(), args.url)
```

- [ ] **Step 3: Run the full integration check**

Start the server (`conda run -n smartdoor python server.py`). Then:
```bash
conda run -n smartdoor python simulate_esp32.py
```
Expected: the owner line prints `"user": "Owner"` and `door action: UNLOCK`; the no-face line prints `IDLE (silent)`.

If you have a second face photo handy, enroll that person via the dashboard, then:
```bash
conda run -n smartdoor python simulate_esp32.py --user /path/to/second_person.jpg --stranger /path/to/stranger.jpg
```
Expected: 2nd user → `UNLOCK` with their name in `user`; stranger → `BUZZER`.

- [ ] **Step 4: Update CLAUDE.md**

In `CLAUDE.md`, make these documentation updates so the doc matches the new behavior:

Replace the `server.py` module bullet:
```
- `server.py` — core door API (`/verify`), the live camera viewer (`/view`,
  `/annotated_stream`), face matching (`match_face`/`init_owner`), `reenroll_owner`,
  and debounced event logging. Registers the dashboard blueprint.
```
with:
```
- `server.py` — core door API (`/verify`), the live camera viewer (`/view`,
  `/annotated_stream`), face matching (`match_face`/`init_owners`), enrollment
  (`enroll_user`/`add_user_photo`), and debounced event logging. Registers the
  dashboard blueprint.
- `matching.py` — pure, dependency-light helpers (`best_match`, `cosine_distance`,
  embedding (de)serialization, `validate_name`); unit-tested in `tests/`.
```

Replace the JSON response shape block:
```json
{"verified": true,  "reason": "match",    "distance": 0.31, "threshold": 0.593}
{"verified": false, "reason": "no_match", "distance": 0.91, "threshold": 0.593}
{"verified": false, "reason": "no_face"}
{"verified": false, "reason": "error", "error": "..."}
```
with:
```json
{"verified": true,  "reason": "match",    "user": "Alice", "distance": 0.31, "threshold": 0.593}
{"verified": false, "reason": "no_match", "distance": 0.91, "threshold": 0.593}
{"verified": false, "reason": "no_face"}
{"verified": false, "reason": "error", "error": "..."}
```

Replace the owner-embedding sentence in the `/verify` flow paragraph:
```
compare (cosine distance) against the **owner embedding
cached at startup** — `owner.jpg` is embedded once in `init_owner()`, not per request.
```
with:
```
compare (cosine distance) against **every enrolled face embedding cached at startup**
(`init_owners()` loads them from MySQL); the closest match within threshold wins and its
user name is returned. `owner.jpg` is migrated into an "Owner" user on first run.
```

Add a sentence to the "Owner embedding is cached at startup" bullet — after `If you replace owner.jpg, restart the server to re-cache.` add:
```
Multiple users are supported: each person has one or more reference photos stored under
`assets/users/` with their embedding in MySQL (`users`/`user_photos` tables). Enroll and
manage them from the dashboard ("Users" card). A match returns the person's name in the
`user` field and logs it to `events.person`.
```

In the testing section, replace:
```
There are no linters or build steps; `simulate_esp32.py` is the integration test.
```
with:
```
Pure matching/validation logic has pytest unit tests: `python -m pytest tests/`.
`simulate_esp32.py` is the end-to-end integration test (`--user` tests a second enrolled
person, `--stranger` a non-enrolled face).
```

- [ ] **Step 5: Final verification — unit tests + clean import**

```bash
conda run -n smartdoor python -m pytest tests/ -v
conda run -n smartdoor python -c "import server; print('import OK')"
```
Expected: all unit tests pass; import prints the `[init]` line and `import OK`.

- [ ] **Step 6: Commit**

```bash
git add .gitignore simulate_esp32.py CLAUDE.md
git commit -m "test+docs: second-user simulation, gitignore assets, update CLAUDE.md

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Self-review notes

- **Spec coverage:** data model (Task 2), embedding-in-DB + model-change re-embed (Task 3 `init_owners`), matching closest-wins + `user` field (Tasks 1, 3), enrollment/validation (Task 3), dashboard CRUD + routes (Tasks 4, 5), debounce by `(verdict, person)` + `events.person` (Tasks 2, 3), migration/seed + `.gitignore` (Tasks 3, 6), testing (Tasks 1, 6). All sections mapped.
- **Type consistency:** `match_face` returns a 4-tuple everywhere it's consumed (`/verify`, `_recognizer_loop` via `_view_verdict`, `_draw`). `best_match` returns `(name|None, distance|None)`. `db.log_event(..., person=, jpeg_bytes=)` and `db.add_photo(user_id, jpeg_bytes, embedding_bytes, model_name)` signatures match their call sites. `list_users()` returns `[{id, name, photos:[...]}]`, consumed by the template (`u.id`, `u.name`, `u.photos`) and `/api/users`.
- **No placeholders:** every code step shows full before/after content.
