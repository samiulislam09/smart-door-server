# Low-light Flash LED Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

> **Commits:** The user owns commits. Each task ends with a suggested commit command, but do NOT run it automatically — pause and let the user commit (or run it only when they ask).

**Goal:** Light the ESP32-CAM's built-in GPIO 4 flash LED automatically when a frame is too dark to recognize a face and a person is present, then turn it off when the doorway is empty or in daylight.

**Architecture:** Server-driven, stateless-per-request. The ESP32 reports its current LED state in an `X-LED-State` request header; `/verify` measures frame brightness, short-circuits a too-dark frame to `reason:"low_light"` (telling the LED to turn on and re-capture), and returns a string `"led":"on"|"off"` field on every response. The firmware drives GPIO 4 from that field. Pure decision logic lives in `matching.py` (unit-tested); brightness measurement and wiring live in `server.py`.

**Tech Stack:** Python 3.10 (Flask, Pillow/ImageStat, pytest), Arduino C++ (ESP32-CAM firmware).

**Reference spec:** `docs/superpowers/specs/2026-05-27-low-light-flash-led-design.md`

---

## File structure

- **Modify** `matching.py` — add two pure helpers: `is_low_light()`, `next_led_state()`.
- **Modify** `tests/test_matching.py` — unit tests for the two helpers.
- **Modify** `server.py` — add `FLASH_LED`/`LOW_LIGHT_THRESHOLD` config, a `_mean_brightness()` helper, the `ImageStat` import, and wire the LED decision into `verify()`.
- **Modify** `simulate_esp32.py` — send `X-LED-State` and report the returned `led` field; add a `--dark` case.
- **Modify** `~/Documents/Arduino/smart-door/smart-door.ino` — drive GPIO 4 from the response; send the header; handle `low_light`; force LED off on server error.

---

### Task 1: Pure LED-decision helpers in `matching.py`

**Files:**
- Modify: `matching.py` (append after `is_spoof`, around line 65)
- Test: `tests/test_matching.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_matching.py`:

```python
def test_is_low_light_below_threshold_is_dark():
    assert matching.is_low_light(20.0, 45.0) is True


def test_is_low_light_at_or_above_threshold_is_not_dark():
    assert matching.is_low_light(45.0, 45.0) is False   # boundary: not strictly below
    assert matching.is_low_light(100.0, 45.0) is False


def test_next_led_state_stays_on_while_person_present():
    assert matching.next_led_state(True, True, True) == "on"


def test_next_led_state_off_when_doorway_empty():
    assert matching.next_led_state(True, True, False) == "off"


def test_next_led_state_off_in_daylight_led_was_off():
    # bright frame, LED off, person present -> no light needed
    assert matching.next_led_state(True, False, True) == "off"


def test_next_led_state_off_when_flash_disabled():
    assert matching.next_led_state(False, True, True) == "off"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_matching.py -k "led_state or low_light" -v`
Expected: FAIL — `AttributeError: module 'matching' has no attribute 'is_low_light'`

- [ ] **Step 3: Implement the helpers**

Append to `matching.py`:

```python
def is_low_light(brightness, threshold):
    """True when a frame's mean luminance (0-255) is below `threshold` — too dark to
    reliably detect a face, so the camera flash LED should be turned on to probe."""
    return brightness < threshold


def next_led_state(flash_enabled, led_currently_on, person_present):
    """Desired LED state ('on'/'off') for the next capture, AFTER recognition ran.

    Steady-while-present: the LED stays on only when it is already lit AND a person is
    present; it turns off on an empty (but lit) doorway, in daylight (LED was never on),
    and whenever the flash feature is disabled. The dark-frame probe (off -> on) is decided
    separately via is_low_light, because presence is unknown until the frame is lit."""
    if flash_enabled and led_currently_on and person_present:
        return "on"
    return "off"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_matching.py -k "led_state or low_light" -v`
Expected: PASS (6 passed)

- [ ] **Step 5: Suggested commit (user runs)**

```bash
git add matching.py tests/test_matching.py
git commit -m "feat: pure LED-decision helpers for low-light flash"
```

---

### Task 2: Config + brightness measurement + wire into `verify()` (`server.py`)

**Files:**
- Modify: `server.py` line 9 (import), config block (~line 36), `verify()` (lines 251–295)

- [ ] **Step 1: Add the `ImageStat` import**

In `server.py` line 9, change:

```python
from PIL import Image
```

to:

```python
from PIL import Image, ImageStat
```

- [ ] **Step 2: Add config constants**

In `server.py`, immediately after the `ANTISPOOF_MIN_SCORE` line (~line 36), add:

```python
# Low-light flash LED. The ESP32-CAM's built-in GPIO 4 LED is lit when a frame is too dark
# to recognize a face AND a person is present; the server tells the firmware via the
# response "led" field. FLASH_LED toggles the feature; LOW_LIGHT_THRESHOLD is the mean
# luminance (0-255) below which a frame counts as "dark".
FLASH_LED = os.environ.get("FLASH_LED", "1").lower() not in ("0", "false", "no", "off", "")
LOW_LIGHT_THRESHOLD = float(os.environ.get("LOW_LIGHT_THRESHOLD", "45"))
```

- [ ] **Step 3: Add the brightness helper**

In `server.py`, just above `def verify():` (line 251), add:

```python
def _mean_brightness(img):
    """Mean luminance (0-255) of a PIL image — a cheap proxy for 'too dark to see a face'."""
    return ImageStat.Stat(img.convert("L")).mean[0]
```

- [ ] **Step 4: Add `"led":"off"` to the image-read error return**

In `verify()`, change the `except` block (lines 258–260) from:

```python
    except Exception as e:
        return jsonify({"verified": False, "reason": "error",
                        "error": f"cannot read image: {e}"})
```

to:

```python
    except Exception as e:
        return jsonify({"verified": False, "reason": "error",
                        "error": f"cannot read image: {e}", "led": "off"})
```

- [ ] **Step 5: Add the dark-frame short-circuit**

In `verify()`, immediately after the `except` block above and before `tmp_path = None` (line 262), insert:

```python
    # Read the firmware's current LED state and short-circuit a too-dark frame: tell the
    # ESP32 to light up and re-capture, skipping the expensive embed/liveness on a frame
    # that would only yield no_face. Presence is confirmed on the next (lit) frame.
    led_in = request.headers.get("X-LED-State", "off").strip().lower() == "on"
    if FLASH_LED and not led_in and matching.is_low_light(
            _mean_brightness(img), LOW_LIGHT_THRESHOLD):
        return jsonify({"verified": False, "reason": "low_light", "led": "on"})
```

- [ ] **Step 6: Add `"led"` to the spoof return**

In `verify()`, change the spoof block (lines 276–278) from:

```python
        if matching.is_spoof(is_real, antispoof_score, ANTISPOOF_MIN_SCORE):
            _maybe_log("spoof", None, None, None, raw, antispoof_score=antispoof_score)
            return jsonify({"verified": False, "reason": "spoof"})
```

to:

```python
        if matching.is_spoof(is_real, antispoof_score, ANTISPOOF_MIN_SCORE):
            _maybe_log("spoof", None, None, None, raw, antispoof_score=antispoof_score)
            return jsonify({"verified": False, "reason": "spoof",
                            "led": matching.next_led_state(FLASH_LED, led_in, True)})
```

- [ ] **Step 7: Add `"led"` to the match/no_match/no_face/error returns**

In `verify()`, change the block (lines 280–292) from:

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

to:

```python
        reason, distance, threshold, person = match_face(tmp_path)
        _maybe_log(reason, distance, threshold, person, raw)

        present = reason in ("match", "no_match")          # a person is in frame
        led_out = matching.next_led_state(FLASH_LED, led_in, present)

        if reason in ("match", "no_match"):
            body = {"verified": reason == "match", "reason": reason,
                    "distance": distance, "threshold": threshold, "led": led_out}
            if person:                       # string field; never a second boolean true
                body["user"] = person
            return jsonify(body)
        if reason == "no_face":
            return jsonify({"verified": False, "reason": "no_face", "led": led_out})
        return jsonify({"verified": False, "reason": "error",
                        "error": "verification failed", "led": "off"})
```

- [ ] **Step 8: Verify existing unit tests still pass**

Run: `python -m pytest tests/ -v`
Expected: PASS (all existing tests + the 6 from Task 1)

- [ ] **Step 9: Manual smoke test (server must be running with an enrolled owner)**

Run a normal owner check and confirm a `"led"` field is present:

```bash
curl -s -X POST -H "Content-Type: application/octet-stream" \
     --data-binary @owner.jpg http://127.0.0.1:8080/verify
```
Expected: JSON includes `"verified": true` and `"led": "off"` (owner.jpg is well-lit, LED was off).

Then simulate a dark frame with the LED reported off:

```bash
python - <<'PY'
import io, json, urllib.request
from PIL import Image
buf = io.BytesIO(); Image.new("RGB",(640,480),(5,5,5)).save(buf,format="JPEG")
req = urllib.request.Request("http://127.0.0.1:8080/verify", data=buf.getvalue(),
    method="POST", headers={"Content-Type":"application/octet-stream","X-LED-State":"off"})
print(urllib.request.urlopen(req,timeout=60).read().decode())
PY
```
Expected: `{"verified": false, "reason": "low_light", "led": "on"}`

- [ ] **Step 10: Suggested commit (user runs)**

```bash
git add server.py
git commit -m "feat: low-light flash LED decision in /verify"
```

---

### Task 3: Extend `simulate_esp32.py` to exercise the LED path

**Files:**
- Modify: `simulate_esp32.py`

- [ ] **Step 1: Send the LED state header and report the `led` field**

In `simulate_esp32.py`, change `post_image` (lines 22–28) from:

```python
def post_image(url, data):
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/octet-stream"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode())
```

to:

```python
def post_image(url, data, led_state="off"):
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/octet-stream",
                 "X-LED-State": led_state},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode())
```

- [ ] **Step 2: Thread `led_state` through `run_case`**

Change `run_case` (lines 49–55) from:

```python
def run_case(name, data, url):
    try:
        resp = post_image(url, data)
    except Exception as e:
        print(f"[{name}] request FAILED: {e}")
        return
    print(f"[{name}] response={json.dumps(resp)}  ->  door action: {door_action(resp)}")
```

to:

```python
def run_case(name, data, url, led_state="off"):
    try:
        resp = post_image(url, data, led_state)
    except Exception as e:
        print(f"[{name}] request FAILED: {e}")
        return
    led = resp.get("led", "-")
    print(f"[{name}] response={json.dumps(resp)}  ->  door action: {door_action(resp)}"
          f"  |  LED: {led}")
```

- [ ] **Step 3: Add a `dark_image_bytes` helper and a `--dark` case**

In `simulate_esp32.py`, add after `blank_image_bytes` (line 46):

```python
def dark_image_bytes():
    """A near-black 640x480 JPEG: a valid image too dark to detect a face."""
    from PIL import Image
    img = Image.new("RGB", (640, 480), (5, 5, 5))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()
```

Then in `main`, just before the no-face case (line 83), add:

```python
    run_case("dark frame (LED off, expect low_light -> LED on)",
             dark_image_bytes(), args.url, led_state="off")
```

- [ ] **Step 4: Run it (server must be running)**

Run: `python simulate_esp32.py`
Expected: the owner line shows `LED: off`, the dark-frame line shows
`reason":"low_light"` and `LED: on`, and the no-face line shows `LED: off`.

- [ ] **Step 5: Suggested commit (user runs)**

```bash
git add simulate_esp32.py
git commit -m "test: simulate_esp32 exercises X-LED-State and led field"
```

---

### Task 4: Firmware — drive GPIO 4 from the response (`smart-door.ino`)

**Files:**
- Modify: `~/Documents/Arduino/smart-door/smart-door.ino`

> No unit tests for firmware; verification is the on-device behavior described in Step 7.

- [ ] **Step 1: Add the LED pin, decision, and timing defines**

After the `#define BUZZER_PIN 14` line, add:

```cpp
#define FLASH_LED_PIN 4   // AI-Thinker ESP32-CAM built-in white flash LED (active HIGH)
```

In the door-decision defines block (after `#define DOOR_IDLE (-1)`), add:

```cpp
#define DOOR_LOWLIGHT 2   // frame too dark: LED just turned on, re-capture the lit frame
```

In the verification-timing block (after `#define DENY_COOLDOWN_MS 8000`), add:

```cpp
#define LOWLIGHT_RECHECK_MS  300   // after lighting up, re-capture quickly to recognize
```

- [ ] **Step 2: Track LED state globally**

After `unsigned long nextCheckTime = 0;`, add:

```cpp
bool ledOn = false;   // mirrors the physical GPIO 4 state; sent to the server each poll
```

- [ ] **Step 3: Initialize the pin in `setup()`**

In `setup()`, right after `pinMode(BUZZER_PIN, OUTPUT);`, add:

```cpp
  pinMode(FLASH_LED_PIN, OUTPUT);
  digitalWrite(FLASH_LED_PIN, LOW);   // start dark
```

- [ ] **Step 4: Add an `applyLed()` helper**

Add this function above `verifyFace()`:

```cpp
// Drive GPIO 4 from the server's "led" field (tolerate an optional space after the colon).
void applyLed(const String& response) {
  if (response.indexOf("\"led\":\"on\"") >= 0 ||
      response.indexOf("\"led\": \"on\"") >= 0) {
    digitalWrite(FLASH_LED_PIN, HIGH);
    ledOn = true;
  } else if (response.indexOf("\"led\":\"off\"") >= 0 ||
             response.indexOf("\"led\": \"off\"") >= 0) {
    digitalWrite(FLASH_LED_PIN, LOW);
    ledOn = false;
  }
}
```

- [ ] **Step 5: Send `X-LED-State`, force LED off on error, and handle `low_light` in `verifyFace()`**

Replace the body of `verifyFace()` (from `http.begin(serverURL);` through the final `return DOOR_DENY;`) with:

```cpp
  http.begin(serverURL);
  http.addHeader("Content-Type", "application/octet-stream");
  http.addHeader("X-LED-State", ledOn ? "on" : "off");
  int code = http.POST(fb->buf, fb->len);

  if (code != 200) {
    Serial.println("Server error: " + String(code));
    http.end();
    digitalWrite(FLASH_LED_PIN, LOW);   // never let the LED stick on if the server dies
    ledOn = false;
    return DOOR_IDLE;
  }

  String response = http.getString();
  Serial.println("Server response: " + response);
  http.end();

  applyLed(response);   // honor the server's LED instruction before deciding the door action

  // The server guarantees the only literal "true" in the body is the verified field
  // (tolerate an optional space after the colon).
  if (response.indexOf("\"verified\":true") >= 0 ||
      response.indexOf("\"verified\": true") >= 0) {
    return DOOR_GRANT;
  }
  if (response.indexOf("low_light") >= 0) {
    return DOOR_LOWLIGHT;       // dark: LED now on, re-capture the lit frame quickly
  }
  if (response.indexOf("no_face") >= 0) {
    return DOOR_IDLE;           // empty doorway: stay silent
  }
  return DOOR_DENY;             // face present but not the owner
```

- [ ] **Step 6: Handle `DOOR_LOWLIGHT` in `loop()`**

In the `switch (decision)` block, add a case before `default:`:

```cpp
    case DOOR_LOWLIGHT:
      nextCheckTime = millis() + LOWLIGHT_RECHECK_MS;  // LED just lit; recognize the lit frame
      break;
```

- [ ] **Step 7: Flash to the board and verify on-device**

Upload via Arduino IDE. With the server running:
- Cover the camera / dim the room: the white LED turns on, and when a face appears it stays steadily lit and unlocks (or buzzes for a stranger). Expected serial: `low_light` responses, then `verified` / `no_match`.
- Step away (empty dark doorway): the LED blinks briefly (~300 ms probe) then goes off until the next idle poll.
- In daylight: the LED never turns on.
- Stop the server: the LED turns off and stays off.

- [ ] **Step 8: Suggested commit (user runs, in the firmware repo)**

```bash
cd ~/Documents/Arduino/smart-door
git add smart-door.ino
git commit -m "feat: drive built-in flash LED from server low-light signal"
```

---

## Notes for the implementer

- **Why a string, not a boolean:** the firmware decides "unlock" by substring-searching for the literal `"verified":true`. CLAUDE.md requires the only boolean `true` in the body be `verified`. `"led":"on"`/`"led":"off"` are strings, so they never trip that check.
- **Why short-circuit before DeepFace:** a dark frame yields `no_face` anyway; skipping the ~0.28s embed + torch liveness on dark frames saves work and avoids logging noise (`low_light` is intentionally not passed to `_maybe_log`).
- **`/view` is out of scope:** the diagnostic viewer doesn't drive GPIO; only `/verify` controls the LED.
