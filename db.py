"""MySQL access layer for the smart-door access log.

Owns the schema and every query. Connection details come from environment variables
(loaded from a local .env if present): MYSQL_HOST, MYSQL_PORT, MYSQL_USER,
MYSQL_PASSWORD, MYSQL_DB. The password is never logged.
"""
import os
import time
from pathlib import Path

import pymysql

_BASE = Path(__file__).resolve().parent
SNAPSHOT_DIR = _BASE / "assets" / "snapshots"
ASSETS_USERS_DIR = _BASE / "assets" / "users"

# Retention: keep the newest MAX_EVENTS rows and drop anything older than MAX_AGE_DAYS.
MAX_EVENTS = 1000
MAX_AGE_DAYS = 30


def _load_dotenv():
    """Populate os.environ from a local .env (does not override existing vars)."""
    env = _BASE / ".env"
    if not env.exists():
        return
    for line in env.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        os.environ.setdefault(key.strip(), val.strip())


_load_dotenv()


def _connect(use_db=True):
    kw = dict(
        host=os.environ.get("MYSQL_HOST", "127.0.0.1"),
        port=int(os.environ.get("MYSQL_PORT", "3306")),
        user=os.environ.get("MYSQL_USER", "smartdoor"),
        password=os.environ.get("MYSQL_PASSWORD", ""),
        autocommit=True,
        connect_timeout=5,
        cursorclass=pymysql.cursors.DictCursor,
    )
    if use_db:
        kw["database"] = os.environ.get("MYSQL_DB", "smartdoor")
    return pymysql.connect(**kw)


_DDL = """
CREATE TABLE IF NOT EXISTS events (
    id            BIGINT AUTO_INCREMENT PRIMARY KEY,
    ts            DOUBLE NOT NULL,
    verdict       VARCHAR(16) NOT NULL,
    distance      DOUBLE,
    threshold     DOUBLE,
    snapshot_path VARCHAR(255),
    INDEX idx_events_ts (ts)
) ENGINE=InnoDB
"""

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


def init_db():
    """Create dirs and tables if missing, and migrate the events table."""
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
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


def log_event(verdict, distance, threshold, person=None, jpeg_bytes=None):
    """Insert one event; if jpeg_bytes given, save assets/snapshots/<id>.jpg and record it.

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


def query_events(limit=50, verdict=None):
    """Most recent events (optionally filtered to 'granted'/'denied'), newest first."""
    sql = "SELECT id, ts, verdict, distance, threshold, person, snapshot_path FROM events"
    args = []
    if verdict in ("granted", "denied"):
        sql += " WHERE verdict=%s"
        args.append(verdict)
    sql += " ORDER BY ts DESC LIMIT %s"
    args.append(int(limit))
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(sql, args)
        return cur.fetchall()


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
        try:
            (ASSETS_USERS_DIR / name).write_bytes(jpeg_bytes)
        except OSError:
            # autocommit already persisted the row; drop it so we never leave a row
            # pointing at a file that doesn't exist.
            cur.execute("DELETE FROM user_photos WHERE id=%s", (photo_id,))
            raise
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
    """Return the total number of enrolled users."""
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


def _midnight_epoch():
    lt = time.localtime()
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))


def stats(days=7):
    """Today's counts + a per-day granted/denied series for the chart."""
    today = _midnight_epoch()
    since = time.time() - days * 86400
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT verdict, COUNT(*) c FROM events WHERE ts >= %s GROUP BY verdict",
            (today,),
        )
        today_counts = {r["verdict"]: r["c"] for r in cur.fetchall()}

        cur.execute("SELECT MAX(ts) m FROM events")
        last_seen = cur.fetchone()["m"]

        cur.execute(
            "SELECT DATE(FROM_UNIXTIME(ts)) d, verdict, COUNT(*) c "
            "FROM events WHERE ts >= %s GROUP BY d, verdict ORDER BY d",
            (since,),
        )
        series = cur.fetchall()

    # Build dense per-day labels so the chart has no gaps.
    labels, granted, denied = [], [], []
    by_day = {}
    for row in series:
        by_day.setdefault(str(row["d"]), {})[row["verdict"]] = row["c"]
    for i in range(days - 1, -1, -1):
        day = time.strftime("%Y-%m-%d", time.localtime(time.time() - i * 86400))
        labels.append(day)
        granted.append(by_day.get(day, {}).get("granted", 0))
        denied.append(by_day.get(day, {}).get("denied", 0))

    return {
        "granted_today": today_counts.get("granted", 0),
        "denied_today": today_counts.get("denied", 0),
        "last_seen": last_seen,
        "series": {"labels": labels, "granted": granted, "denied": denied},
    }


def prune():
    """Delete events older than MAX_AGE_DAYS or beyond the newest MAX_EVENTS, and remove
    their snapshot files."""
    cutoff = time.time() - MAX_AGE_DAYS * 86400
    with _connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT id, snapshot_path FROM events WHERE ts < %s", (cutoff,))
        doomed = list(cur.fetchall())
        # rows beyond the newest MAX_EVENTS
        cur.execute(
            "SELECT id, snapshot_path FROM events ORDER BY ts DESC LIMIT %s, 18446744073709551615",
            (MAX_EVENTS,),
        )
        doomed += list(cur.fetchall())

        ids = {r["id"] for r in doomed}
        if not ids:
            return 0
        for r in doomed:
            if r["snapshot_path"]:
                try:
                    (SNAPSHOT_DIR / r["snapshot_path"]).unlink(missing_ok=True)
                except OSError:
                    pass
        placeholders = ",".join(["%s"] * len(ids))
        cur.execute(f"DELETE FROM events WHERE id IN ({placeholders})", tuple(ids))
    return len(ids)
