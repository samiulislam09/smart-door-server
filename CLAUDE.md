# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

The face-verification backend for a smart door lock. The door-side client is an
ESP32-CAM (`~/Documents/Arduino/smart-door/smart-door.ino`): it captures a frame,
POSTs the raw JPEG bytes to this server, and uses the response to drive a servo lock
and a buzzer. `owner.jpg` is the enrolled reference face — replacing it re-enrolls the
owner. The whole backend is `server.py` (one Flask app, one endpoint).

## Architecture

`POST /verify` — request body is raw image bytes, read via `request.data`. The client
**must** send a non-form `Content-Type` (e.g. `application/octet-stream` or
`image/jpeg`); with a form content type like `application/x-www-form-urlencoded`,
Flask consumes the body as form fields and `request.data` is empty.

Flow: validate the bytes as an image (Pillow), write them to a temp `.jpg`, embed the
face with `DeepFace.represent(model_name="SFace", detector_backend="opencv",
enforce_detection=True)`, and compare (cosine distance) against the **owner embedding
cached at startup** — `owner.jpg` is embedded once in `init_owner()`, not per request.
Response shape:

```json
{"verified": true,  "reason": "match",    "distance": 0.31, "threshold": 0.593}
{"verified": false, "reason": "no_match", "distance": 0.91, "threshold": 0.593}
{"verified": false, "reason": "no_face"}
{"verified": false, "reason": "error", "error": "..."}
```

The firmware maps these to actions: `match` → unlock, `no_match` → buzzer, `no_face` →
stay silent (empty doorway), error/unreachable → idle.

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

Key behavior to keep in mind when changing the matching logic:
- **Never re-encode the JPEG at default quality.** Writing the incoming bytes verbatim
  is deliberate: a default-quality Pillow re-encode (`img.save(...)` without `quality=`)
  wrecked the face embedding (owner-vs-owner distance jumped from `0.0` to `0.62`,
  nearly a false reject). Non-JPEG inputs are re-encoded at `quality=95`.
- **`enforce_detection=True`** makes DeepFace raise when no face is present. The raised
  `ValueError` wraps a chained `FaceNotDetected` cause — `_is_no_face()` walks the
  `__cause__`/`__context__` chain to map it to `reason: "no_face"` rather than `error`.
  This is what keeps the buzzer silent at an empty doorway.
- **Owner embedding is cached at startup** (`init_owner()` runs at import). Per-check
  time is ~0.28s (vs ~0.86s when `DeepFace.verify` re-embedded `owner.jpg` every call).
  If you replace `owner.jpg`, restart the server to re-cache. Model = SFace (fastest);
  override with `FACE_MODEL` env. Distance metric is cosine; threshold captured at boot.
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

```bash
conda activate smartdoor
cd /Volumes/files/smart-door-system/server
python server.py         # serves on 0.0.0.0:8080
```

`deepface` downloads model weights to `~/.deepface` on first use, so the first
`/verify` after a fresh start is slow.

Smoke test:

```bash
curl -X POST -H "Content-Type: application/octet-stream" \
     --data-binary @owner.jpg http://127.0.0.1:8080/verify   # → {"verified": true, ...}
```

End-to-end check without the hardware — `simulate_esp32.py` mimics the ESP32 (posts
owner / stranger / no-face frames and prints the door action each would trigger):

```bash
python simulate_esp32.py                      # owner (UNLOCK) + blank frame (IDLE)
python simulate_esp32.py --stranger face.jpg  # also test a non-owner face (BUZZER)
```

There are no linters or build steps; `simulate_esp32.py` is the integration test.
