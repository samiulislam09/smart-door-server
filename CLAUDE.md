# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

The face-verification backend for a smart door lock. The door-side client is an
ESP32-CAM (`~/Documents/Arduino/smart-door/smart-door.ino`): it captures a frame,
POSTs the raw JPEG bytes to this server, and uses the response to drive a servo lock
and a buzzer. Multiple residents can be enrolled (each with one or more reference photos);
`owner.jpg` seeds an "Owner" user on first run, and the door unlocks for any enrolled face.

Modules:
- `server.py` — core door API (`/verify`), the live camera viewer (`/view`,
  `/annotated_stream`), face matching (`match_face`/`init_owners`), enrollment
  (`enroll_user`/`add_user_photo`), and debounced event logging. Registers the
  dashboard blueprint.
- `matching.py` — pure, dependency-light helpers (`best_match`, `cosine_distance`,
  embedding (de)serialization, `validate_name`); unit-tested in `tests/`.
- `db.py` — MySQL access layer (PyMySQL): `events` table, `log_event`, `query_events`,
  `stats`, `prune`. Loads `.env` on import. Snapshots saved under `assets/snapshots/`.
- `dashboard.py` — Flask blueprint: password login, `/dashboard`, JSON APIs
  (`/api/events`, `/api/stats`, `/api/users`), `/snapshots/<f>`, user photo serving
  (`/user_photos/<id>`), and user management (`/users`, `/users/<id>/photos`,
  `/users/<id>/delete`, `/photos/<id>/delete`).
- `templates/` + `static/` — dashboard UI (dark theme, vendored Chart.js).
- `.env` — MySQL creds + `DASHBOARD_PASSWORD` + `SECRET_KEY` (not committed; loaded by
  `db._load_dotenv`).

## Architecture

`POST /verify` — request body is raw image bytes, read via `request.data`. The client
**must** send a non-form `Content-Type` (e.g. `application/octet-stream` or
`image/jpeg`); with a form content type like `application/x-www-form-urlencoded`,
Flask consumes the body as form fields and `request.data` is empty.

Flow: validate the bytes as an image (Pillow), write them to a temp `.jpg`, embed the
face with `DeepFace.represent(model_name="SFace", detector_backend="opencv",
enforce_detection=True)`, and compare (cosine distance) against **every enrolled face embedding cached at startup**
(`init_owners()` loads them from MySQL); the closest match within threshold wins and its
user name is returned. `owner.jpg` is migrated into an "Owner" user on first run.
Response shape:

```json
{"verified": true,  "reason": "match",    "user": "Alice", "distance": 0.31, "threshold": 0.593}
{"verified": false, "reason": "no_match", "distance": 0.91, "threshold": 0.593}
{"verified": false, "reason": "spoof"}
{"verified": false, "reason": "no_face"}
{"verified": false, "reason": "error", "error": "..."}
```

The firmware maps these to actions: `match` → unlock, `no_match`/`spoof` → buzzer,
`no_face` → stay silent (empty doorway), error/unreachable → idle.

`GET /view` (+ `/annotated_stream`) is a diagnostic viewer that proxies the ESP32's
`:81` MJPEG stream (`ESP_STREAM_URL`, default `http://192.168.1.102:81/stream`,
overridable via `?src=`) and overlays a Haar face box on every frame, yielding smooth
~15-20fps video. Recognition (SFace, ~0.28s) runs in a **background thread**
(`_recognizer_loop`) on the latest viewed frame so it never stalls the video; the
stream loop just draws the cached verdict (green=match / red=no_match / yellow=no_face)
each frame. The recognizer only runs while a frame was seen in the last 2s (i.e. while
someone is watching). Despite the earlier worry, the ESP32 serves the `:81` stream AND
handles `/verify` concurrently fine (~15fps measured). `match_face()` is shared by
`/verify` and the recognizer (accepts a path or BGR numpy frame). `threaded=True` so
the long-lived stream never blocks `/verify`.

### Database + dashboard

Access events are logged to **MySQL** (server at `/usr/local/mysql`, db `smartdoor`).
After each `/verify`, `_maybe_log()` records `granted`/`denied`/`spoof` events only —
`no_face` and `error` are skipped, and repeats of the same `(verdict, person)` within
`LOG_DEBOUNCE_S` (10s) are suppressed (so one person standing there = one row, not seven). This is essential:
the ESP posts ~every 1.5s, so logging everything would flood the DB. Each event saves
the posted JPEG to `assets/snapshots/<id>.jpg`; `prune()` (every 50th insert) caps retention to
`MAX_EVENTS`/`MAX_AGE_DAYS`. Logging failures are swallowed so they never break the door.

The dashboard (`/dashboard`, blueprint in `dashboard.py`) is password-gated
(`DASHBOARD_PASSWORD`, default `admin`) via Flask session. It polls `/api/stats` and
`/api/events` every 3s and embeds `/annotated_stream`. `/verify` stays open (the ESP has
no login); `/view`, `/annotated_stream`, and all dashboard routes require login. User
management (the "Users" card) lets you add a user (`enroll_user`), add more photos to a
user (`add_user_photo`), and delete photos/users; each enroll validates a face is present,
stores the photo under `assets/users/` with its embedding in MySQL, and calls
`init_owners()` to re-cache live.

Key behavior to keep in mind when changing the matching logic:
- **Never re-encode the JPEG at default quality.** Writing the incoming bytes verbatim
  is deliberate: a default-quality Pillow re-encode (`img.save(...)` without `quality=`)
  wrecked the face embedding (owner-vs-owner distance jumped from `0.0` to `0.62`,
  nearly a false reject). Non-JPEG inputs are re-encoded at `quality=95`.
- **`enforce_detection=True`** makes DeepFace raise when no face is present. The raised
  `ValueError` wraps a chained `FaceNotDetected` cause — `_is_no_face()` walks the
  `__cause__`/`__context__` chain to map it to `reason: "no_face"` rather than `error`.
  This is what keeps the buzzer silent at an empty doorway.
- **Enrolled embeddings are cached at startup** (`init_owners()` runs at import, loading
  every photo's embedding from MySQL). Per-check time is ~0.28s (one embed of the incoming
  frame, then N cheap cosine comparisons; vs ~0.86s when `DeepFace.verify` re-embedded
  `owner.jpg` every call).
  If you replace `owner.jpg`, restart the server to re-cache. Multiple users are supported: each person has one or more reference photos stored under
  `assets/users/` with their embedding in MySQL (`users`/`user_photos` tables). Enroll and
  manage them from the dashboard ("Users" card). A match returns the person's name in the
  `user` field and logs it to `events.person`. Model = SFace (fastest);
  override with `FACE_MODEL` env. Distance metric is cosine; threshold captured at boot.
- **Anti-spoofing (liveness).** Before recognition, `/verify` (and the live viewer) run
  DeepFace's Fasnet model via `check_liveness()`; a photo/screen of an enrolled face is
  rejected as `reason:"spoof"` (firmware buzzes it) and logged to `events.antispoof_score`.
  Toggle with `ANTISPOOF` (default on); raise `ANTISPOOF_MIN_SCORE` (default 0.0) to block
  only confident spoofs if the camera ever false-rejects a real person. Requires `torch`.
  **`OMP_NUM_THREADS=1`/`MKL_NUM_THREADS=1` are pinned at the very top of `server.py`** —
  without this the numpy-MKL/torch OpenMP pools segfault in Fasnet's conv path. Enrollment
  is NOT liveness-checked (uploading a still photo is intentional).
- Only the literal `verified` field is ever `true` in the JSON — the firmware relies on
  a substring check, so don't add other boolean-`true` fields.
- Optional `MATCH_THRESHOLD` env var tightens the match (lower = stricter); unset uses
  the model's own threshold (SFace cosine ≈ 0.593).
- The endpoint never raises to the client; all failures collapse to `verified: false`.

## Running

The runtime environment is the conda env **`smartdoor`** (Python 3.10, with
`deepface`, `flask`, `tensorflow 2.16.2`, `pillow`, `keras`, `opencv`). The `venv/` in
this directory is empty/unused — ignore it. TensorFlow has no Intel-Mac + Python 3.11
wheels, which is why a separate Python 3.10 conda env exists.

MySQL must be running (`sudo /usr/local/mysql/support-files/mysql.server start`) and
`.env` must hold the DB creds (`MYSQL_PASSWORD`, optionally `MYSQL_USER`/`MYSQL_DB`/...)
plus `DASHBOARD_PASSWORD` and `SECRET_KEY`. `db._load_dotenv()` reads `.env` on import,
so just run normally:

```bash
conda activate smartdoor
cd /Volumes/files/smart-door-system/server
python server.py         # serves on 0.0.0.0:8080 (threaded)
```

Dashboard: `http://127.0.0.1:8080/dashboard` (or the Mac's LAN IP). `deepface` downloads
model weights to `~/.deepface` on first use, so the first `/verify` after a fresh start
is slow. Set a real `DASHBOARD_PASSWORD` in `.env` (default is `admin`); `SECRET_KEY`
unset means sessions reset on restart.

Smoke test:

```bash
curl -X POST -H "Content-Type: application/octet-stream" \
     --data-binary @owner.jpg http://127.0.0.1:8080/verify   # → {"verified": true, ...}
```

End-to-end check without the hardware — `simulate_esp32.py` mimics the ESP32 (posts
owner / stranger / no-face frames and prints the door action each would trigger):

```bash
python simulate_esp32.py                       # owner (UNLOCK) + blank frame (IDLE)
python simulate_esp32.py --user alice.jpg      # also test a second enrolled person (UNLOCK)
python simulate_esp32.py --stranger face.jpg   # also test a non-enrolled face (BUZZER)
```

Pure matching/validation logic has pytest unit tests: `python -m pytest tests/`.
`simulate_esp32.py` is the end-to-end integration test (`--user` tests a second enrolled
person, `--stranger` a non-enrolled face).
