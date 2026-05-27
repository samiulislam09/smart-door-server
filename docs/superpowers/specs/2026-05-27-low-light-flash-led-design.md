# Low-light flash LED — design

**Date:** 2026-05-27
**Status:** Approved, pending implementation

## Goal

Turn on the ESP32-CAM's built-in white flash LED (GPIO 4) automatically when it is
too dark for the camera to see **and** a person is in front of the door — so face
recognition works at night and the visitor is illuminated. The LED stays off in
daylight and when the doorway is empty.

## Background / constraints

- The door client is a thin ESP32-CAM (`~/Documents/Arduino/smart-door/smart-door.ino`)
  that POSTs raw JPEG bytes to `POST /verify` and maps the JSON response to door
  actions. It has **no face logic of its own** — the server (DeepFace) decides whether a
  face is present.
- The built-in flash LED is wired internally to **GPIO 4** (active-HIGH, full brightness).
  No external wiring. GPIO 4 is shared with the SD card data line, but this build does
  not use the SD card, so there is no conflict.
- **Chicken-and-egg problem:** "someone is present" is decided by the server detecting a
  face, but in the dark the frame is too dim to detect a face (returns `no_face`). The
  resolution chosen is **camera-as-flash**: when the server sees a too-dark frame it tells
  the ESP32 to light up and re-capture; the now-lit frame reveals whether anyone is there.
- **Steady-while-present** behavior is desired. Because the LED itself brightens the frame,
  a naive brightness-only rule oscillates (dark → on → bright → off → dark …). The fix is a
  small state machine: the ESP32 reports its current LED state in a request header and the
  server returns the desired next state.
- **Firmware unlock check is fragile:** the firmware decides "unlock" via a substring search
  for the literal `"verified":true`, and CLAUDE.md requires that the *only* boolean `true`
  in the JSON body is the `verified` field. Therefore the LED signal MUST be a string
  (`"led":"on"` / `"led":"off"`), never a boolean.

## Protocol (ESP32 ⇄ server contract)

- **Request:** ESP32 adds header `X-LED-State: on` or `X-LED-State: off` to each `/verify`
  POST, reporting its current LED state. Absent header ⇒ treated as `off`.
- **Response:** the server adds a string field `"led": "on"` or `"led": "off"` to **every**
  `/verify` JSON response. String value only — never trips the firmware's `"verified":true`
  substring check.
- **New reason:** `"low_light"` — the dark-frame short-circuit. Body:
  `{"verified": false, "reason": "low_light", "led": "on"}`. Not logged to the DB
  (treated like `no_face`/`error` — would otherwise flood the events table).

Updated response shapes (each also carries `"led"`):

```json
{"verified": true,  "reason": "match",     "user": "Alice", "distance": 0.31, "threshold": 0.593, "led": "on"}
{"verified": false, "reason": "no_match",  "distance": 0.91, "threshold": 0.593, "led": "on"}
{"verified": false, "reason": "spoof",     "led": "on"}
{"verified": false, "reason": "no_face",   "led": "off"}
{"verified": false, "reason": "low_light", "led": "on"}
{"verified": false, "reason": "error",     "error": "...", "led": "off"}
```

## Server changes (`server.py`)

Inside `verify()`, right after `img.load()` succeeds (before the temp-file write and
DeepFace work):

1. Read `led_in = request.headers.get("X-LED-State", "off").lower() == "on"`.
2. Measure frame brightness: mean luminance of `img.convert("L")` via `PIL.ImageStat`
   (0–255 scale). Only computed when `FLASH_LED` is enabled.
3. **Dark-frame short-circuit:** if `FLASH_LED` and `not led_in` and
   `brightness < LOW_LIGHT_THRESHOLD`, return
   `{"verified": false, "reason": "low_light", "led": "on"}` immediately — skip liveness
   and `match_face` (the dark frame would only yield `no_face`). Do **not** call
   `_maybe_log`.
4. Otherwise run the existing flow (liveness → `match_face` → `_maybe_log`), then compute
   the outgoing LED state and merge `"led"` into the response body:

   ```
   present = reason in ("match", "no_match", "spoof")   # a person is there
   led_out = "on" if (FLASH_LED and led_in and present) else "off"
   ```

   - LED on + face present → `"on"` (steady).
   - LED on + empty doorway (`no_face`) → `"off"`.
   - LED off + bright (daytime) → `"off"` (never needed).
   - `error` → `"off"`.

State-machine summary:

| current LED | frame      | face?     | → next LED | reason returned |
|-------------|------------|-----------|-----------|-----------------|
| off         | dark       | (unknown) | on        | low_light       |
| off         | bright     | any       | off       | normal          |
| on          | (lit)      | present   | on        | match/no_match/spoof |
| on          | (lit)      | none      | off       | no_face         |

## Firmware changes (`smart-door.ino`)

- `#define FLASH_LED_PIN 4`; in `setup()` set `pinMode(FLASH_LED_PIN, OUTPUT)` and
  `digitalWrite(FLASH_LED_PIN, LOW)`. Track `bool ledOn = false`.
- `verifyFace()`:
  - Add header `http.addHeader("X-LED-State", ledOn ? "on" : "off")`.
  - Parse the response for `"led":"on"` / `"led":"off"` (tolerate optional space after
    colon, like the existing `verified` check) and drive `digitalWrite(FLASH_LED_PIN, …)`,
    updating `ledOn`.
  - Add a `DOOR_LOWLIGHT` decision (or reuse IDLE) for `reason:"low_light"`: no
    unlock/buzzer, but recapture **quickly** (~300 ms) so the now-lit frame is recognized
    promptly.
- **Safety:** on non-200 / unreachable server (the existing `DOOR_IDLE` error path), force
  the LED **off** so it can never stick on if the server dies.
- Net effect on an empty dark doorway: LED blinks on for ~300 ms during a probe, sees
  nothing, turns off, then stays off until the next idle poll (~1.5 s).

## Config (`.env`)

- `FLASH_LED` — master enable, default **on**. When off, the server always returns
  `"led":"off"` and skips the brightness check.
- `LOW_LIGHT_THRESHOLD` — mean-luminance threshold (0–255) below which a frame counts as
  "dark". Default **45**. Lower = only triggers in deeper darkness.

## Out of scope / accepted trade-offs

- The `/view` diagnostic viewer / recognizer loop does **not** control the LED (it doesn't
  drive GPIO; only the `/verify` path does).
- Steady-on uses plain `digitalWrite(HIGH)` at full brightness. PWM dimming (on a free LEDC
  channel/timer) is a noted future upgrade if glare is a problem — not built now.
- **Dusk edge case:** if ambient light rises to full daylight *while* the LED is on and a
  person is standing there, the LED stays on until the doorway clears, then re-evaluates.
  Acceptable for a door (people don't linger for long).

## Testing

- Unit-testable server logic (the LED-state decision) should be a pure helper so it can be
  tested without DeepFace — mirror the existing `matching.py` split. Cases: dark+off→on,
  bright+off→off, on+present→on, on+empty→off, FLASH_LED disabled→always off.
- `simulate_esp32.py` can be extended to send `X-LED-State` and assert the returned `led`
  field for a dark frame vs a normal owner/stranger/blank frame.
