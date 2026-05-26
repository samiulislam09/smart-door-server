# Anti-spoofing (liveness) at the door — design

**Date:** 2026-05-26
**Status:** Approved, ready for implementation planning

## Problem

The face matcher recognizes the *appearance* of a face, so holding a phone showing a
photo of the owner in front of the ESP32-CAM unlocks the door — a presentation
(replay) attack. Recognition cannot distinguish a live face from a photo/screen of that
face because the embeddings are nearly identical. We need a **liveness / anti-spoofing**
check, which is a separate problem from recognition.

## Goals

- Detect and reject photo/screen replays at the door (`/verify`).
- Record spoof attempts distinctly so they are visible on the dashboard.
- Keep legitimate flows working: live owners unlock, and **enrollment photo uploads are
  not rejected** (uploading a still image is intentional).
- Be tunable, because the low-resolution ESP32-CAM may occasionally false-reject a real
  person.

## Non-goals

- Challenge-response liveness (blink/turn) — rejected: needs multiple frames + prompting,
  awkward at a screenless door, and a phone *video* defeats it.
- A separate liveness sidecar process — was considered when an in-process segfault
  appeared, but the segfault is solved (see below), so in-process is simpler and chosen.
- Hardware changes (IR/depth camera).
- Firmware changes.

## Approach

Use DeepFace's bundled **Fasnet** model (MiniFASNet, via PyTorch), run **in-process** in
`server.py`. A small helper `check_liveness(img)` calls
`DeepFace.extract_faces(img, detector_backend=DETECTOR, anti_spoofing=True,
enforce_detection=True)` and returns `(is_real, score)` for the first detected face.

**Empty-doorway safety:** `check_liveness` uses `enforce_detection=True` and catches the
no-face/error case (via the existing `_is_no_face` chain check), returning `(None, None)`
when no real face is present. This is essential: a frame with no face must NOT be flagged
as a spoof (that would buzz at an empty doorway). On `(None, None)`, `/verify` skips the
spoof verdict and falls through to `match_face`, which then reports `no_face` as it does
today — keeping the buzzer silent at an empty doorway.

### Critical platform requirement: thread pinning

On this Intel-Mac env, the numpy-MKL / torch OpenMP thread pools conflict and **segfault
in the convolution path**. Setting `OMP_NUM_THREADS=1` and `MKL_NUM_THREADS=1` **before
any heavy import** eliminates the crash, verified deterministically (3/3 runs). These are
set via `os.environ[...]` at the very top of `server.py`, above all other imports.

Validation already performed:
- `owner.jpg` (a clean face photo) → `is_real=True, score=0.9999` — so enrollment uploads
  and the `owner.jpg` smoke test are not false-rejected. Fasnet only flags photos *of
  screens*.
- torch 2.2.2 (last x86-64 macOS build) is installed in the `smartdoor` conda env.

## Spoof decision rule (pure, unit-testable)

Add to `matching.py` (no DeepFace import, so it stays unit-testable):

```python
def is_spoof(is_real, score, min_score):
    """True when the frame should be rejected as a spoof: the model says it's fake AND
    is at least min_score confident. min_score=0.0 trusts the model's decision; raising
    it blocks only confident spoofs (fewer false-rejects of real people)."""
    return (is_real is False) and (score is not None) and (score >= min_score)
```

## Configuration (env)

- `ANTISPOOF` — master toggle. Default **on** (`"1"`). Parsed truthy/falsy.
- `ANTISPOOF_MIN_SCORE` — float, default `0.0`. Block only when at least this confident
  the face is fake. Raise (e.g. `0.9`) to cut false-rejects if the camera trips it.

## Integration (`server.py`)

`match_face`'s 4-tuple signature is **unchanged**; liveness is a separate gate so the
viewer overlay and `/verify` both reuse it without rippling the tuple.

- `check_liveness(img)` → `(is_real, score)`; returns `(None, None)` if anti-spoofing is
  disabled, no face is detectable, or the check errors (callers then fall through to
  normal handling, which reports `no_face` if truly no face). Uses `enforce_detection=
  True` + `_is_no_face` so an empty doorway is never treated as a spoof.
- **`/verify`**: after writing the temp JPEG, if `ANTISPOOF` is on, call `check_liveness`.
  If `is_spoof(...)` → return `reason:"spoof"`, log a spoof event (with the score), and
  skip recognition entirely (don't reveal/log who it resembled). Otherwise run the
  existing `match_face` flow unchanged.
- **Live viewer recognizer (`_recognizer_loop`)**: same gate before `match_face`; on a
  spoof set `_view_verdict = ("spoof", None, None, None)` so the overlay shows it. Add a
  `"spoof"` entry to `_VERDICT_STYLE` (a distinct color + "SPOOF" label).
- **Enrollment (`_validate_face_and_embed`)**: untouched — no liveness check.
- **Startup warm-up**: best-effort `check_liveness(OWNER_IMG)` during init (when enabled
  and `owner.jpg` exists) so the first door check isn't slowed by lazy model loading.

## Response shape

```json
{"verified": false, "reason": "spoof"}
```

`verified` remains the only boolean field. The firmware buzzes anything that is not
verified and not `no_face`, so a spoof denies with **no firmware change**.

## Logging / DB

- New nullable column `events.antispoof_score DOUBLE`, added via the same
  `information_schema` idempotent migration pattern used for `events.person`.
- `_VERDICT_MAP` gains `spoof -> "spoof"`.
- `_maybe_log(reason, distance, threshold, person, jpeg_bytes, antispoof_score=None)` —
  threads the score through; debounced on `(verdict, person)` like the rest (a phone held
  up posts repeatedly → one row per debounce window). `db.log_event` gains an
  `antispoof_score=None` parameter and stores it.
- `query_events` returns `antispoof_score`.

## Dashboard

- Events table: a `spoof` verdict renders a distinct badge (amber, vs. red for `denied`).
  The antispoof score is shown for spoof rows (in the Distance cell, which is otherwise
  empty for spoofs, or appended to the badge).
- Stat cards (Granted/Denied today) and the chart stay granted/denied; spoof attempts are
  visible in the events list. (A dedicated spoof counter is a possible later addition.)

## Testing

- **Unit (pure):** `is_spoof(is_real, score, min_score)` truth table in
  `tests/test_matching.py` — fake+high score blocks, fake+low score passes when threshold
  raised, real never blocks, None score safe.
- **Integration:** with `ANTISPOOF=1`, posting `owner.jpg` to `/verify` still returns
  `verified:true, user:"Owner"` (the clean photo scores real); blank frame → `no_face`.
  The existing `simulate_esp32.py` / smoke test stay green.
- **Manual:** a phone showing the owner's photo in front of the camera → `reason:"spoof"`
  / BUZZER, and a `spoof` row appears on the dashboard. (Requires the hardware.)

## Risks / notes

- **Not bulletproof.** Fasnet dramatically raises the bar against casual phone/print
  attacks but a high-quality display under good lighting can sometimes still pass.
- **False-reject risk.** Low ESP32-CAM image quality could occasionally flag a real
  person; `ANTISPOOF_MIN_SCORE` (raise it) and the `ANTISPOOF` off-switch are the
  mitigations. Log the score so a threshold can be chosen from real data.
- **Latency.** Adds a Fasnet pass (and an extra opencv detection) to `/verify`; with
  single-threaded math, still well within the ~1.5s door posting cadence.
- **Dependency.** Requires `torch==2.2.2` in the `smartdoor` env (already installed).
