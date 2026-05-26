# Multi-user face access — design

**Date:** 2026-05-26
**Status:** Approved, ready for implementation planning

## Problem

The smart-door backend recognizes exactly one owner: a single `owner.jpg` whose
embedding is cached at startup and compared (cosine distance) against every incoming
frame. A real home has multiple residents, so the system needs to enroll several named
people — each with one or more reference photos — and unlock for any of them, recording
*who* was granted entry.

## Goals

- Enroll multiple **named** users, each with **one or more** reference photos.
- `/verify` unlocks for any enrolled face and reports which user matched.
- The access log records the matched person.
- Manage users and photos from the dashboard (add user, add photo, delete photo,
  delete user).

## Non-goals

- Per-user permissions/schedules (everyone enrolled has equal access).
- Persisted on-disk embedding cache files (`.npy`) — embeddings live in MySQL.
- Changes to the ESP32 firmware. The firmware's substring check for `verified` is
  preserved; the new `user` field is a string, never a boolean.

## Approach

Relational user model in MySQL (the "users table" approach). User **photos are stored
as files** under `assets/users/`, with the DB holding the path link plus the computed
embedding and metadata. This mirrors the existing `snapshots/<id>.jpg` pattern exactly
(INSERT row → write file → UPDATE path) and keeps the user-supplied name out of any
filesystem path (filenames are server-generated numeric ids — no path-traversal risk).

Storing the embedding in the DB is the payoff of going relational: startup just loads
vectors instead of re-embedding every photo. Each embedding is tagged with the model
that produced it; if `FACE_MODEL` changes, stale rows are re-embedded from the on-disk
image and updated.

## Data model (MySQL)

```sql
CREATE TABLE IF NOT EXISTS users (
  id         INT AUTO_INCREMENT PRIMARY KEY,
  name       VARCHAR(64) NOT NULL UNIQUE,
  created_at DOUBLE NOT NULL
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS user_photos (
  id         INT AUTO_INCREMENT PRIMARY KEY,
  user_id    INT NOT NULL,
  image_path VARCHAR(255) NOT NULL,   -- e.g. "12.jpg", relative to assets/users/
  embedding  BLOB NOT NULL,           -- raw float32 bytes of the face embedding
  model_name VARCHAR(32) NOT NULL,    -- which model produced this embedding
  created_at DOUBLE NOT NULL,
  FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
  INDEX idx_user_photos_user (user_id)
) ENGINE=InnoDB;
```

`events` gains one column, applied via an `information_schema` existence check so the
migration runs at most once:

```sql
ALTER TABLE events ADD COLUMN person VARCHAR(64) NULL;  -- matched user, NULL on denied
```

Embedding (de)serialization: `np.asarray(emb, np.float32).tobytes()` to store,
`np.frombuffer(blob, np.float32)` to load.

## Matching (`server.py`)

- `init_owners()` replaces `init_owner()`. It captures the model's cosine threshold
  (or `MATCH_THRESHOLD` override), then loads `_OWNERS = [(name, embedding_np), ...]`
  from the DB. Any photo whose stored `model_name` != current `MODEL_NAME` is
  re-embedded from its on-disk image and the DB row updated.
- `match_face(img)` returns **`(reason, distance, threshold, person)`** (a path or BGR
  numpy frame in). It embeds the incoming frame once, then computes cosine distance to
  every cached embedding. The **closest** embedding below threshold wins; `person` is
  that user's name. With no enrolled faces, every frame is `no_match`. `no_face`/`error`
  return `person=None`.
- `/verify` response gains a `"user"` string field on a match:

```json
{"verified": true,  "reason": "match",    "user": "Alice", "distance": 0.31, "threshold": 0.593}
{"verified": false, "reason": "no_match", "distance": 0.91, "threshold": 0.593}
{"verified": false, "reason": "no_face"}
{"verified": false, "reason": "error", "error": "..."}
```

The `user` field is a string and is omitted on non-match, so it never introduces a
second boolean-`true` field — the firmware's `verified` substring check is unaffected.

## Enrollment & management

DB helpers in `db.py`:
- `create_user(name) -> user_id` (raises/returns error on duplicate name)
- `add_photo(user_id, jpeg_bytes, embedding_bytes, model_name) -> photo_id`
  — INSERT row, write `assets/users/<photo_id>.jpg`, UPDATE `image_path`
- `list_users()` — users with their photo ids/counts
- `delete_user(id)` — cascade-deletes photo rows; unlink their files
- `delete_photo(id)` — delete row; unlink file
- `get_photo(id)` — row incl. `image_path` for serving

`server.py` keeps the face-validation + embedding logic (refactor of today's
`reenroll_owner`): validate the bytes are an image, confirm a face is detectable
(`_embed`), compute the embedding, then call the DB helper and refresh the in-memory
`_OWNERS` cache. Names are validated: trimmed, non-empty, ≤64 chars. JPEG bytes are
written verbatim (non-JPEG re-encoded at quality=95), preserving the embedding-quality
rule from CLAUDE.md.

`reenroll_owner` / the single-owner `init_owner` are removed/replaced; `OWNER_IMG` is
retained only as the migration seed and smoke-test fixture.

## Dashboard

New routes (POST forms to match the existing no-DELETE-verb style):
- `POST /users` — name + first photo → create user and enroll the photo
- `POST /users/<id>/photos` — add another photo to a user
- `POST /users/<id>/delete` — delete a user (cascades)
- `POST /photos/<id>/delete` — delete one photo
- `GET /user_photos/<id>` — serve a photo via
  `send_from_directory(ASSETS_USERS_DIR, image_path)` (replaces `/owner.jpg`)
- `GET /api/users` — JSON list of users + their photo ids (for the UI)

UI: the single "Enrolled owner" card becomes a **"Users"** card listing each person with
their photo thumbnails, a delete control per photo, a "+ add photo" form per user, an
"add user" (name + photo) form, and a delete-user control. All actions redirect back to
the dashboard with a status message (existing `msg` pattern).

## Logging

`_maybe_log` and `db.log_event` thread `person` through to `events.person`. The debounce
key changes from `verdict` to `(verdict, person)`, so two different people in succession
each log a row instead of the second being suppressed as a repeat "granted". `query_events`
returns `person`; `denied` rows have `person = NULL`.

## Migration & compatibility

On `init_db()`:
1. Create `assets/users/` (like `SNAPSHOT_DIR`).
2. Create the `users` / `user_photos` tables; add `events.person` if absent.
3. If `users` is empty but `owner.jpg` exists, seed a user named **"Owner"**: copy
   `owner.jpg` into `assets/users/<id>.jpg`, compute and store its embedding.

`owner.jpg` stays at the repo root so the README smoke test
(`curl --data-binary @owner.jpg`) still returns `verified: true` (now with
`"user": "Owner"`). `simulate_esp32.py` is unaffected. `assets/users/` is added to
`.gitignore` (user photos are not committed, same reasoning as `snapshots/`).

## Testing

Extend the smoke/simulation checks to confirm:
- owner frame → `verified: true, user: "Owner"`
- a second enrolled person matches as themselves
- a stranger → `no_match`
- a blank frame → `no_face`
- deleting a user's only photo (or the user) makes their face stop matching

`simulate_esp32.py` remains the integration test; add a stranger/second-user case.

## Risks / notes

- **Embedding-quality rule** (CLAUDE.md): never re-encode JPEG at default quality. Write
  incoming JPEG bytes verbatim; only re-encode non-JPEG at `quality=95`.
- **Empty enrollment**: if all users/photos are deleted, every frame is `no_match`
  (buzzer on any face) — acceptable and expected.
- **Model change**: changing `FACE_MODEL` triggers a one-time re-embed of all photos at
  next startup (files on disk make this possible).
