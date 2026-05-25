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
SNAPSHOT_DIR = _BASE / "snapshots"

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


def init_db():
    """Create the snapshots dir and the events table if missing."""
    SNAPSHOT_DIR.mkdir(exist_ok=True)
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(_DDL)


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


def query_events(limit=50, verdict=None):
    """Most recent events (optionally filtered to 'granted'/'denied'), newest first."""
    sql = "SELECT id, ts, verdict, distance, threshold, snapshot_path FROM events"
    args = []
    if verdict in ("granted", "denied"):
        sql += " WHERE verdict=%s"
        args.append(verdict)
    sql += " ORDER BY ts DESC LIMIT %s"
    args.append(int(limit))
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(sql, args)
        return cur.fetchall()


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
