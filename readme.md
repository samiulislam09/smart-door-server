# Smart Door System — Face-Verification Door Lock

A face-recognition access-control system for a door lock. A camera at the door
captures a face, a backend server decides whether that face belongs to an enrolled
resident, and the door reacts: **unlock** for a known face, **buzz** for an unknown
face or a spoof attempt, **stay silent** for an empty doorway. Every access attempt is
logged, snapshotted, and viewable from a web dashboard, and suspicious events can fire a
real-time Telegram alert.

This repository is the **server** (the brain). The door-side device is an ESP32-CAM
running the companion firmware.

---

## Table of contents

1. [What it does](#1-what-it-does)
2. [System overview](#2-system-overview)
3. [Hardware](#3-hardware)
4. [How a door check works (request flow)](#4-how-a-door-check-works-request-flow)
5. [The `/verify` API](#5-the-verify-api)
6. [Face matching explained](#6-face-matching-explained)
7. [Anti-spoofing (liveness)](#7-anti-spoofing-liveness)
8. [Low-light flash LED](#8-low-light-flash-led)
9. [Multi-user enrollment](#9-multi-user-enrollment)
10. [The dashboard](#10-the-dashboard)
11. [Event logging & the database](#11-event-logging--the-database)
12. [Telegram alerts](#12-telegram-alerts)
13. [Configuration reference](#13-configuration-reference)
14. [Running the server](#14-running-the-server)
15. [Testing](#15-testing)
16. [Project structure](#16-project-structure)
17. [Design principles](#17-design-principles)
18. [Roadmap / future work](#18-roadmap--future-work)

---

## 1. What it does

- **Recognizes enrolled residents.** Multiple people can be enrolled, each with one or
  more reference photos. The door unlocks for *any* enrolled face.
- **Rejects strangers and spoofs.** An unknown face triggers the buzzer. A photo or
  phone-screen held up to the camera is detected as a *spoof* (liveness check) and
  rejected.
- **Stays quiet at an empty doorway.** A frame with no face does nothing — no false
  buzzing.
- **Logs every access attempt** (granted / denied / spoof) with a timestamped snapshot.
- **Provides a web dashboard** for live camera view, statistics, an event history, and
  user management.
- **Alerts the owner** via Telegram on suspicious events (spoof / denied), with the
  snapshot attached.
- **Handles the dark.** In low light it tells the camera to switch on its built-in LED
  flash and re-capture.

---

## 2. System overview

```
        ┌──────────────────────┐         raw JPEG bytes          ┌────────────────────────────┐
        │      ESP32-CAM        │   POST /verify (octet-stream)   │       Flask server          │
        │  (door-side client)   │ ───────────────────────────────►│         (server.py)         │
        │                       │                                 │                             │
        │  • captures a frame   │◄─────────────────────────────── │  • validates the image      │
        │  • drives servo lock  │   JSON verdict {verified, ...}  │  • liveness (anti-spoof)    │
        │  • drives buzzer      │                                 │  • face match (SFace)       │
        │  • controls flash LED │                                 │  • debounced event logging  │
        │  • serves MJPEG :81   │                                 └──────────────┬──────────────┘
        └──────────────────────┘                                                │
                  ▲                                                              │
                  │  /annotated_stream proxies the :81 MJPEG                     ▼
                  │  stream with face-box overlays                ┌────────────────────────────┐
        ┌─────────┴────────────┐                                 │           MySQL             │
        │   Web dashboard       │  /dashboard, /api/*             │  events, users,             │
        │  (browser, login)     │ ◄───────────────────────────── │  user_photos, settings      │
        └───────────────────────┘                                └────────────────────────────┘
                                                                                 │
                                                                                 ▼
                                                                  ┌────────────────────────────┐
                                                                  │     Telegram (alerts)       │
                                                                  └────────────────────────────┘
```

Two independent jobs run at once:

- **The door path** (`POST /verify`) — the real, hardware-facing API. Fast and robust.
- **The diagnostic path** (`/view`, `/annotated_stream`) — a live human-watchable video
  feed with recognition overlays, used from the dashboard. It never blocks the door path
  (`threaded=True`, recognition runs on a background thread).

---

## 3. Hardware

| Component            | Pin / interface | Role                                                          |
|----------------------|-----------------|---------------------------------------------------------------|
| **ESP32-CAM** (AI-Thinker) | —         | Captures frames (VGA 640×480), POSTs them to the server, drives the lock/buzzer/LED, and serves its own MJPEG stream on port 81. |
| **Server host**      | —               | A Mac (Intel) running the Flask app + MySQL. Could be any always-on machine. |
| **Servo lock**       | GPIO 12 (`SERVO_PIN`) | Drives the deadbolt/latch: unlock = rotate to 90°, hold 5 s, return to 0°. ⚠️ GPIO 12 is a boot strapping pin (MTDI) — a servo signal at power-on can block boot; the firmware notes moving it to GPIO 13 if that happens. |
| **Buzzer**           | GPIO 14 (`BUZZER_PIN`) | Sounds on a denied/spoof verdict (5 beeps, 200 ms each). |
| **Flash LED**        | GPIO 4 (`FLASH_LED_PIN`) | The ESP32-CAM's built-in white LED (active-HIGH), used as a low-light flash. |
| **RFID reader (planned)** | —          | Offline fallback unlock by scanning an authorized tag. See [roadmap](#18-roadmap--future-work). |

The firmware lives in `~/Documents/Arduino/smart-door/smart-door.ino` (outside this
repo). It polls the server every ~1.5 s and maps the server's JSON response to physical
actions:

| Server response                       | Firmware decision | Action                                              |
|---------------------------------------|-------------------|-----------------------------------------------------|
| `"verified": true` (a `match`)        | `DOOR_GRANT`      | **Unlock** servo, then a 10 s cooldown (no instant re-unlock). |
| `no_match` / `spoof` (face, not verified) | `DOOR_DENY`   | **Buzzer**, then an 8 s cooldown (no buzzer spam).  |
| `no_face`                             | `DOOR_IDLE`       | Stay silent, keep polling at 1.5 s.                 |
| `low_light`                           | `DOOR_LOWLIGHT`   | LED is now lit; re-capture quickly (300 ms).        |
| `error` / non-200 / unreachable       | `DOOR_IDLE`       | Idle; also force the LED off so it never sticks on. |
| `led: "on"` / `"off"` (any response)  | —                 | Set GPIO 4 accordingly *before* deciding the action.|

The firmware relies on the server's substring guarantees: it checks for the literal
`"verified":true` (the only `true` boolean in the body) and the `"led"` field, tolerating
an optional space after the colon. It sends the current LED state back each poll via the
`X-LED-State` header.

---

## 4. How a door check works (request flow)

The ESP32 POSTs raw JPEG bytes roughly every 1.5 seconds. For each request the server:

1. **Reads the body as raw bytes** (`request.data`). The client must send a non-form
   `Content-Type` (e.g. `application/octet-stream`); a form content type would make Flask
   consume the body and leave `request.data` empty.
2. **Validates it is a real image** (Pillow). A bad/empty body returns `reason: "error"`,
   not a face mismatch.
3. **Low-light short-circuit.** If the frame is too dark *and* the LED is currently off,
   it immediately returns `led: "on"` so the camera lights up and re-captures — skipping
   the expensive face work on a frame that would only yield "no face".
4. **Liveness check** (anti-spoofing) via DeepFace's Fasnet model. A confident "fake"
   verdict short-circuits to `reason: "spoof"`.
5. **Face matching.** Embeds the incoming face once (SFace) and compares it (cosine
   distance) against every enrolled embedding cached at startup. The closest match within
   threshold wins.
6. **Debounced logging.** `granted`/`denied`/`spoof` events are logged (with snapshot);
   `no_face`/`error` are skipped, and repeats of the same `(verdict, person)` within 10s
   are suppressed so one person standing there = one row, not seven.
7. **Returns the verdict** + the desired LED state for the next capture.

> **The endpoint never raises to the client.** Every failure collapses to
> `verified: false`, so a bug in the server can never leave the door erroring out.

---

## 5. The `/verify` API

**`POST /verify`** — request body is raw image bytes; `Content-Type` must be non-form
(e.g. `application/octet-stream` or `image/jpeg`).

Response shapes:

```json
{"verified": true,  "reason": "match",     "user": "Alice", "distance": 0.31, "threshold": 0.593, "led": "off"}
{"verified": false, "reason": "no_match",  "distance": 0.91, "threshold": 0.593, "led": "off"}
{"verified": false, "reason": "spoof",     "led": "off"}
{"verified": false, "reason": "no_face",   "led": "off"}
{"verified": false, "reason": "low_light", "led": "on"}
{"verified": false, "reason": "error",     "error": "...", "led": "off"}
```

| Field       | Meaning                                                                 |
|-------------|-------------------------------------------------------------------------|
| `verified`  | `true` only on a match. **The only `true` boolean field** — the firmware does a substring check, so no other field may be boolean-`true`. |
| `reason`    | `match` / `no_match` / `spoof` / `no_face` / `low_light` / `error`.     |
| `user`      | The matched resident's name (present only on a match).                  |
| `distance`  | Cosine distance of the closest enrolled face (lower = more similar).    |
| `threshold` | The match cutoff captured at boot.                                      |
| `led`       | Desired flash-LED state for the next capture (`"on"` / `"off"`).        |

Request header `X-LED-State: on|off` tells the server whether the LED is currently lit
(used for the low-light handshake).

Smoke test:

```bash
curl -X POST -H "Content-Type: application/octet-stream" \
     --data-binary @owner.jpg http://127.0.0.1:8080/verify
# → {"verified": true, "reason": "match", "user": "Owner", ...}
```

---

## 6. Face matching explained

The matching pipeline is split into a **pure, testable** layer (`matching.py`) and a
**heavy** layer (`server.py`, which uses DeepFace).

- **Model:** `SFace` (the fastest DeepFace model at comparable accuracy). Override with
  the `FACE_MODEL` env var.
- **Detector:** OpenCV Haar (`opencv`, ~30 ms).
- **Distance metric:** cosine distance, in `[0, 2]`; `0` = identical direction.
- **Threshold:** captured once at boot (SFace cosine ≈ `0.593`). A match requires the
  closest distance to be ≤ threshold. Override with `MATCH_THRESHOLD` (lower = stricter).

**The performance trick — embeddings are cached at startup.** `init_owners()` runs at
import and loads every enrolled photo's embedding from MySQL into memory. So each
`/verify` does just *one* embedding (of the incoming frame) followed by N cheap cosine
comparisons (~0.28 s), instead of re-embedding reference photos every call (~0.86 s).

> **Never re-encode the incoming JPEG at default quality.** Writing the ESP32's bytes
> verbatim is deliberate: a default-quality Pillow re-encode wrecked the embedding
> (owner-vs-owner distance jumped from `0.0` to `0.62` — nearly a false reject). Only
> non-JPEG inputs are re-encoded, at `quality=95`.

Key helpers in `matching.py`:

| Function                | Purpose                                                          |
|-------------------------|------------------------------------------------------------------|
| `cosine_distance(a, b)` | Cosine distance; zero-norm vectors treated as max distance.      |
| `best_match(emb, owners, threshold)` | Closest enrolled face; name only if within threshold. |
| `embedding_to_bytes` / `bytes_to_embedding` | (De)serialize float32 embeddings for the DB. |
| `validate_name(raw)`    | Validate a user-supplied name (non-empty, ≤ 64 chars).           |
| `is_spoof`, `is_low_light`, `next_led_state` | Pure decision helpers for liveness/LED.         |

---

## 7. Anti-spoofing (liveness)

Before recognition, `/verify` runs DeepFace's **Fasnet** liveness model. A photo or
screen of an enrolled face is rejected as `reason: "spoof"` (firmware buzzes it) and the
liveness score is logged to `events.antispoof_score`.

- Toggle with `ANTISPOOF` (default **on**). Requires `torch`.
- `antispoof_min_score` (dashboard setting, default `0.8`) controls how confident the
  model must be before blocking. The low-res ESP32-CAM produces low-confidence "fake"
  verdicts on genuinely blurry/dim frames, so blocking on *any* fake (`0.0`) would
  false-reject real people. `0.8` blocks only confident spoofs.
- **Enrollment is not liveness-checked** — uploading a still photo to enroll is
  intentional.

> **Important runtime detail:** `OMP_NUM_THREADS=1` and `MKL_NUM_THREADS=1` are pinned at
> the very top of `server.py`, before numpy/torch/TensorFlow load. Without this the
> numpy-MKL and torch OpenMP thread pools conflict and segfault inside Fasnet's conv path.

---

## 8. Low-light flash LED

The ESP32-CAM has a built-in white LED on **GPIO 4** used as a flash for dark doorways.

- When a frame's mean luminance is below `LOW_LIGHT_THRESHOLD` (default `45`, on a 0–255
  scale) *and* the LED is currently off, the server returns `led: "on"` and skips the
  expensive face work — the camera lights up and re-captures.
- Presence is confirmed on the *next* (lit) frame. The LED then stays on only while a
  person is present and turns off on an empty doorway (or in daylight, where it was never
  lit). The decision logic lives in `matching.next_led_state` / `matching.is_low_light`.
- Toggle with `FLASH_LED` (default on).

---

## 9. Multi-user enrollment

Multiple residents can be enrolled, each with one or more reference photos.

- On **first run**, the legacy `owner.jpg` is migrated into an "Owner" user.
- Each enrollment **validates that a face is present**, stores the photo under
  `assets/users/`, saves its embedding in MySQL (`users` / `user_photos` tables), and
  calls `init_owners()` to re-cache the live matcher — no restart needed.
- A match returns the person's name in the `user` field and records it in
  `events.person`.

Managed from the dashboard's **Users** page:

- `enroll_user(name, photo)` — create a new user from a name + first photo.
- `add_user_photo(user_id, photo)` — add more reference photos to a user (improves
  accuracy across angles/lighting).
- Delete a single photo or a whole user.

---

## 10. The dashboard

A dark-themed web UI (`dashboard.py` Flask blueprint + `templates/`), password-gated via
a Flask session (`DASHBOARD_PASSWORD`, default `admin`).

Pages:

| Route                  | Page      | Contents                                                       |
|------------------------|-----------|----------------------------------------------------------------|
| `/dashboard`           | Overview  | Today's counts, a per-day granted/denied/spoof chart (Chart.js), recent-events table (paginated, with snapshot detail modal), and the live annotated camera stream. |
| `/dashboard/users`     | Users     | Enroll users, add/delete photos, delete users.                 |
| `/dashboard/settings`  | Settings  | Telegram alert config + anti-spoof sensitivity.                |

JSON / media APIs (all login-required except `/verify`):

| Route                       | Purpose                                            |
|-----------------------------|----------------------------------------------------|
| `GET /api/stats?days=N`     | Today's counts + a dense per-day series.           |
| `GET /api/events?limit&offset&verdict` | Recent events, newest first (paginated). |
| `GET /api/users`            | Enrolled users and their photo IDs.                |
| `GET /snapshots/<f>`        | An event's snapshot image.                         |
| `GET /user_photos/<id>`     | A user's reference photo.                          |
| `GET /view`, `/annotated_stream` | Live camera proxy with face-box overlays.     |

The Overview page polls `/api/stats` and `/api/events` every 3 seconds and embeds the
live stream. **`/verify` deliberately stays open** (the ESP32 has no login); everything
else requires the session.

The live viewer proxies the ESP32's `:81` MJPEG stream and overlays a Haar face box on
every frame (~15–20 fps). Recognition (SFace, ~0.28 s) runs in a **background thread** on
the latest viewed frame so it never stalls the video — the stream loop just draws the
cached verdict (match / no_match / spoof / no_face) each frame. If the camera is
unreachable it shows a calm "camera offline — retrying" placeholder and auto-resumes.

---

## 11. Event logging & the database

Access events are logged to **MySQL** (`db.py`, PyMySQL). Snapshots are saved under
`assets/snapshots/<id>.jpg`.

**What gets logged:** only `granted` / `denied` / `spoof`. `no_face` and `error` are
skipped. Repeats of the same `(verdict, person)` within `LOG_DEBOUNCE_S` (10 s) are
suppressed — essential because the ESP posts ~every 1.5 s, so logging everything would
flood the DB. Logging failures are swallowed so they never break the door.

**Retention:** `prune()` runs every 50th insert and caps the table to `MAX_EVENTS`
(1000) rows and `MAX_AGE_DAYS` (30 days), deleting old snapshot files too.

### Schema

```
events                          users                  user_photos                       settings
─────────────                   ───────                ─────────────                     ──────────
id            BIGINT PK         id    INT PK            id          INT PK                name  VARCHAR PK
ts            DOUBLE            name  VARCHAR UNIQUE     user_id     INT FK→users(id)      value TEXT
verdict       VARCHAR          created_at DOUBLE        image_path  VARCHAR
distance      DOUBLE                                    embedding   BLOB  (float32)
threshold     DOUBLE                                    model_name  VARCHAR
person        VARCHAR                                   created_at  DOUBLE
antispoof_score DOUBLE
snapshot_path VARCHAR
```

- `events` — one row per logged access attempt. `person` is the matched name (NULL for
  denied); `antispoof_score` is set only on spoof rows.
- `users` / `user_photos` — enrolled residents and their reference photos + embeddings.
  Deleting a user cascades to their photos.
- `settings` — key/value store for dashboard-editable config (Telegram, anti-spoof
  sensitivity). Read on the `/verify` hot path with a short (~5 s) in-memory cache so
  edits take effect within seconds without a restart.

The schema is created/migrated on startup in `init_db()`. The `person` and
`antispoof_score` columns are added with an idempotent guard so an existing DB upgrades
in place.

---

## 12. Telegram alerts

`notify.py` sends a Telegram message — with the event snapshot attached — when a
`spoof` or `denied` event is logged, so the owner is notified of suspicious activity in
real time.

- **Config lives in MySQL** (the `settings` table), edited from the dashboard **Settings**
  page — *not* in `.env`. Keys: `telegram_bot_token`, `telegram_chat_id`,
  `alert_cooldown_s` (default 300), `alert_verdicts` (default `spoof,denied`),
  `alerts_enabled` (default off).
- **Fire-and-forget** on a daemon thread; never affects the door. A per-verdict cooldown
  prevents alert spam.
- **No third-party deps** — stdlib `urllib`/`threading`/`json` only.
- A **Send test message** button on the Settings page verifies the bot config
  synchronously.

Pure, unit-tested helpers: `parse_config` (normalize settings → typed config) and
`should_alert` (per-verdict cooldown logic).

---

## 13. Configuration reference

Config comes from a local `.env` (loaded by `db._load_dotenv()` on import, never
committed) plus a few dashboard-editable settings stored in MySQL.

### `.env` / environment variables

| Variable             | Default                         | Purpose                                       |
|----------------------|---------------------------------|-----------------------------------------------|
| `MYSQL_HOST`         | `127.0.0.1`                     | MySQL host.                                   |
| `MYSQL_PORT`         | `3306`                          | MySQL port.                                   |
| `MYSQL_USER`         | `smartdoor`                     | MySQL user.                                   |
| `MYSQL_PASSWORD`     | *(empty)*                       | MySQL password.                               |
| `MYSQL_DB`           | `smartdoor`                     | Database name.                                |
| `DASHBOARD_PASSWORD` | `admin`                         | Dashboard login password — **set a real one.**|
| `SECRET_KEY`         | *(random per run)*              | Flask session key; unset ⇒ sessions reset on restart. |
| `FACE_MODEL`         | `SFace`                         | DeepFace model.                               |
| `MATCH_THRESHOLD`    | *(model default ≈ 0.593)*       | Stricter match cutoff (lower = stricter).     |
| `ANTISPOOF`          | `1` (on)                        | Enable liveness checking.                     |
| `ANTISPOOF_MIN_SCORE`| `0.8` effective default         | Confidence required to block a spoof.         |
| `FLASH_LED`          | `1` (on)                        | Enable the low-light flash LED handshake.     |
| `LOW_LIGHT_THRESHOLD`| `45`                            | Mean luminance below which a frame is "dark". |
| `ESP_STREAM_URL`     | `http://192.168.1.102:81/stream`| ESP32 MJPEG stream the viewer proxies.        |

### Dashboard settings (MySQL `settings` table)

`telegram_bot_token`, `telegram_chat_id`, `alert_cooldown_s`, `alert_verdicts`,
`alerts_enabled`, `antispoof_min_score`.

---

## 14. Running the server

The runtime is the conda env **`smartdoor`** (Python 3.10, with `deepface`, `flask`,
`tensorflow 2.16.2`, `pillow`, `keras`, `opencv`, `torch`). The `venv/` in this directory
is empty/unused — ignore it. (TensorFlow has no Intel-Mac + Python 3.11 wheels, which is
why a separate 3.10 conda env exists.)

1. **Start MySQL:**
   ```bash
   sudo /usr/local/mysql/support-files/mysql.server start
   ```
2. **Create `.env`** with the DB creds + `DASHBOARD_PASSWORD` + `SECRET_KEY`.
3. **Run:**
   ```bash
   conda activate smartdoor
   cd /Volumes/files/smart-door-system/server
   python server.py            # serves on 0.0.0.0:8080 (threaded)
   ```

Dashboard: `http://127.0.0.1:8080/dashboard` (or the Mac's LAN IP). DeepFace downloads
model weights to `~/.deepface` on first use, so the **first `/verify` after a fresh start
is slow**; the server warms the models at boot to mitigate this.

---

## 15. Testing

**Unit tests** (pure logic, no DB or network) — `tests/`:

```bash
python -m pytest tests/
```

- `test_matching.py` — matching, threshold, spoof, and low-light/LED decision logic.
- `test_stats.py` — stats reporting shape.
- `test_notify.py` — `parse_config` and `should_alert` (Telegram) logic.

**End-to-end integration** — `simulate_esp32.py` mimics the ESP32 (posts owner / second
user / stranger / dark / no-face frames and prints the door action each would trigger):

```bash
python simulate_esp32.py                       # owner (UNLOCK) + dark/blank frames
python simulate_esp32.py --user alice.jpg      # also test a second enrolled person (UNLOCK)
python simulate_esp32.py --stranger face.jpg   # also test a non-enrolled face (BUZZER)
```

---

## 16. Project structure

```
server/
├── server.py            Core door API (/verify), live viewer, matching, enrollment, logging.
├── matching.py          Pure, dependency-light helpers (unit-tested).
├── db.py                MySQL access layer: schema, events, users, photos, settings, prune.
├── dashboard.py         Flask blueprint: login, dashboard pages, JSON APIs, user management.
├── notify.py            Telegram alerting (fire-and-forget, stdlib only).
├── simulate_esp32.py    ESP32 stand-in for end-to-end testing.
├── templates/           Dashboard UI (base, login, dashboard, users, settings).
├── static/              CSS + vendored Chart.js.
├── assets/
│   ├── snapshots/        Per-event JPEG snapshots (<id>.jpg).
│   └── users/            Enrolled reference photos (<photo_id>.jpg).
├── tests/               pytest unit tests for the pure logic.
├── docs/superpowers/    Design specs and implementation plans.
├── owner.jpg            Legacy seed photo (migrated to an "Owner" user on first run).
├── .env                 Secrets/config (not committed).
└── CLAUDE.md            Engineering notes for AI-assisted development.
```

---

## 17. Design principles

The codebase follows a few consistent rules worth calling out:

1. **The door never errors.** `/verify` always returns `verified: false` on any failure;
   logging, snapshots, and alerts are all wrapped so they can't affect the verdict.
2. **Pure logic is separated from heavy I/O.** `matching.py` and the pure parts of
   `notify.py` have no DeepFace/MySQL/network imports, so they're fast and trivially
   unit-testable.
3. **Embeddings are cached, not recomputed.** Enrolled faces are embedded once at boot.
4. **Don't touch the bytes.** The incoming JPEG is written verbatim to protect the
   embedding from re-encoding artifacts.
5. **One person = one log row.** Debouncing keeps the high-frequency door polling from
   flooding the database.
6. **Config without restarts.** Dashboard settings are read live (with a short cache), so
   alert and sensitivity changes take effect in seconds.

---

## 18. Roadmap / future work

**Already built:** the servo lock is implemented today — the firmware drives a servo on
GPIO 12 (unlock = 90°, hold 5 s, relock 0°) on a `match` verdict.

**Planned next: RFID offline login.** An RFID reader on the ESP32 so an authorized tag
can unlock the door **without the network/server** — a resilient fallback for when WiFi
or the server is down. The ESP32 keeps a local allowlist of authorized tag UIDs in flash
and decides RFID unlocks on-device; the server DB stays the source of truth and the device
re-syncs the allowlist when online. Authorized tags are managed per-user from the
dashboard. The same servo handles the physical unlock for both face and RFID paths.

The full design — hardware (RC522 reader), GPIO/wiring constraints, the offline allowlist,
online sync, the data model, dashboard changes, and security considerations — is
documented in:

- [`docs/superpowers/specs/2026-05-27-rfid-and-servo-lock-design.md`](docs/superpowers/specs/2026-05-27-rfid-and-servo-lock-design.md)

Other existing specs/plans live under `docs/superpowers/` (multi-user access,
anti-spoofing, multi-page dashboard, events pagination, event-detail modal, low-light
flash LED, Telegram alerts).
