# RFID offline login + servo lock — design

**Date:** 2026-05-27
**Status:** Planned (future work — not yet implemented)

## Goal

The **servo lock already works today** — the firmware drives a servo on `SERVO_PIN`
(GPIO 12): on a `match` verdict `unlockDoor()` rotates to 90°, holds 5 s, and returns to
0°. The remaining gap is resilience: entry currently depends on WiFi + the Flask server
being up.

This design adds:

1. **RFID offline login** (the genuinely new feature) — an RFID reader on the ESP32 so an
   authorized tag can unlock the door **even when WiFi or the server is unreachable**.
   Face verification stays the primary path; RFID is a resilient offline fallback.
2. **Servo integration for the RFID path** — reuse the existing servo so an offline RFID
   hit drives the same `unlockDoor()` actuation. No new actuator; the work here is wiring
   the RFID decision into the existing unlock routine. (See Part 2 — it's mostly "don't
   regress what already works.")

## Background

Today the flow is: ESP32-CAM captures a frame → `POST /verify` (raw JPEG) → the server
returns a JSON verdict (`match`/`no_match`/`spoof`/`no_face`/`low_light`/`error`) → the
firmware maps `match` → unlock, `no_match`/`spoof` → buzzer, else idle. The server caches
all enrolled face embeddings at boot (`init_owners()`), logs `granted`/`denied`/`spoof`
to MySQL via the single `_maybe_log()` choke point (debounced 10 s, snapshot saved), and
the dashboard (`dashboard.py` blueprint) manages users/photos and shows events.

Current firmware actuators and pins (AI-Thinker ESP32-CAM, from `smart-door.ino`):
- **Servo** on **GPIO 12** (`SERVO_PIN`) — `unlockDoor()` writes 90°, `delay(5000)`, then
  0°. ⚠️ GPIO 12 is the MTDI boot-strapping pin; the firmware already warns a servo signal
  here at power-on can block boot (fallback: GPIO 13).
- **Buzzer** on **GPIO 14** (`BUZZER_PIN`) — 5 beeps on deny.
- **Flash LED** on **GPIO 4** (`FLASH_LED_PIN`) — low-light flash, active-HIGH.
- Polling cadence: 1.5 s idle, 10 s after grant, 8 s after deny, 300 ms low-light recheck.

The gap this design fills:
- **Server is a single point of failure for entry.** If WiFi or the Flask server is down,
  the servo never gets an unlock decision and there is no way in. RFID provides a local,
  server-independent unlock path that reuses the existing servo.

## Hardware

| Part | Suggested module | Interface | Notes |
|------|------------------|-----------|-------|
| RFID reader | MFRC522 (RC522), 13.56 MHz | SPI | Reads MIFARE tag UIDs. Cheap, well-supported on ESP32. |
| RFID tags/cards | MIFARE Classic 1K fobs/cards | — | One or more per resident. |
| Servo | SG90 (light) or MG996R (metal-gear, stronger) | PWM | Drives the deadbolt/latch arm. MG996R needs its own 5 V supply. |
| Power | External 5 V regulated for servo | — | Do **not** drive a servo off the ESP32-CAM's 3V3; brownouts reset the cam. |

### GPIO budget caution (ESP32-CAM) — this is the hard part

The ESP32-CAM (AI-Thinker) has very few free pins, and the project **already uses three**:
GPIO 4 (flash LED), GPIO 12 (servo), GPIO 14 (buzzer). The camera + PSRAM occupy most of
the rest, and GPIO 0 is the boot-mode strap. RC522 needs **five** more lines (SPI
SCK/MISO/MOSI/SS + RST), which the AI-Thinker board almost certainly cannot spare cleanly.

- Remaining candidate pins (GPIO 2, 13, 15, 16) are few and several are flash/strap-
  sensitive; pin choice must be validated on the bench.
- **Likely conclusion (open question to confirm):** the AI-Thinker ESP32-CAM cannot host
  camera + servo + buzzer + flash LED **and** an SPI RFID reader at once. Preferred options:
  (a) move to a board with more usable GPIO (e.g. **ESP32-S3-CAM**), or (b) split the lock
  into a **second microcontroller** dedicated to RFID + servo, talking to the camera node
  (or directly to the server) over WiFi/serial. Document the final wiring map in the
  firmware repo once decided.

## Part 1 — RFID offline login

### Principle: fail-operational, locally

The whole point is to keep working when the server doesn't. So the ESP32 holds a **local
allowlist of authorized tag UIDs in NVS (non-volatile flash)** and decides RFID unlocks
**on-device**, with no network call required.

### Offline unlock flow (firmware, no server)

1. RC522 reports a tag UID.
2. Firmware checks the UID against the locally cached allowlist in NVS.
3. **Hit** → drive the servo to unlock (see Part 2), short confirmation beep, auto-relock
   after the timeout. **Optionally** queue an `rfid_granted` event to POST to the server
   when connectivity returns (best-effort; never blocks the unlock).
4. **Miss** → buzzer, optionally queue an `rfid_denied` event.

Because this path touches neither WiFi nor `/verify`, it works during an outage.

### Online sync — provisioning the allowlist

When the ESP32 is online it periodically fetches the current authorized UID list from the
server so the local NVS cache stays in sync with dashboard changes.

- **New server endpoint:** `GET /rfid/allowlist` → `{"version": <int>, "uids": ["AABBCCDD", ...]}`.
  Returns the set of enabled tag UIDs (hex, uppercase, normalized).
  - **Auth:** this endpoint is for the door device, not the browser. Like `/verify` it has
    no dashboard session, so protect it with a shared device token (`X-Device-Token`
    header, value in `.env`) rather than leaving it fully open. (`/verify` is open today;
    the allowlist is more sensitive, so it gets a token.)
- Firmware stores the `version` and re-downloads only when it changes (or on a periodic
  poll, e.g. every few minutes / on boot). On a successful fetch it rewrites NVS.
- If the fetch fails, the firmware keeps the **last good** cached allowlist — that is the
  resilience guarantee.

### Decision: where the allowlist is authoritative

The **server DB is the source of truth**; the NVS cache is a replica. Adding/revoking a
tag in the dashboard changes the DB; the device converges on its next sync. Revocation is
therefore *eventually* offline-effective (a revoked tag still works during an outage until
the device next syncs) — an accepted trade-off for a home lock, called out explicitly
here. A future hardening step could push a revoke immediately when online.

## Part 2 — Servo lock (reuse the existing actuator)

**This already exists** — `unlockDoor()` in `smart-door.ino` drives the servo on GPIO 12
(90° → hold 5 s → 0°) and is called on `DOOR_GRANT`. The only work here is making the
**RFID offline path call the same routine**, plus two small robustness tweaks; do not
rebuild what works.

### What stays the same

- The servo is driven by **firmware PWM**, not the server. The server emits the *decision*
  (the `match` verdict over `/verify`); the firmware translates it to servo motion. This
  mirrors the existing split where the firmware already owns buzzer/LED actuation, and it
  honors the "door never blocks on a server round-trip" principle.
- Unlock = rotate to 90°, hold 5 s (`delay(5000)` today), return to 0°.

### What changes / to verify

- **Shared unlock path:** an offline RFID hit (decided on-device, Part 1) calls
  `unlockDoor()` directly — same motion, no network. Factor the grant cooldown so an RFID
  unlock and a face unlock don't fight.
- **Fail-secure on boot (verify):** `setup()` already does `doorServo.write(0)` (locked) at
  start — good. Confirm a brownout/reset can't leave it open; keep locked as the rest state.
- **Optional config constants:** lift the hard-coded `90` / `0` angles and `5000` ms hold
  into named constants (`UNLOCKED_ANGLE` / `LOCKED_ANGLE` / `RELOCK_AFTER_S`) for clarity.
- **GPIO 12 strap caveat** stands (see the GPIO caution) — if RFID forces a board change,
  re-pin the servo there too.

## Data model additions

A new table linking tags to users (mirrors the `user_photos` pattern — one user, many
credentials):

```sql
CREATE TABLE IF NOT EXISTS rfid_tags (
    id         INT AUTO_INCREMENT PRIMARY KEY,
    user_id    INT NOT NULL,
    uid        VARCHAR(32) NOT NULL UNIQUE,   -- hex, uppercase, normalized
    label      VARCHAR(64),                   -- e.g. "Alice's blue fob"
    enabled    TINYINT NOT NULL DEFAULT 1,
    created_at DOUBLE NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
    INDEX idx_rfid_user (user_id)
) ENGINE=InnoDB
```

- Created in `db.init_db()` alongside the existing tables (same idempotent pattern).
- New `db.py` helpers: `add_tag(user_id, uid, label)`, `list_tags()` /
  `tags_for_user(user_id)`, `delete_tag(id)`, `set_tag_enabled(id, bool)`, and
  `enabled_uids()` (used by `/rfid/allowlist`).
- Revoking = `enabled=0` or delete. The allowlist endpoint returns only enabled UIDs.
- Deleting a user cascades to their tags (same as photos).

### Event logging

Extend the existing logging so RFID entries appear in the same history. Either:
- reuse `events.verdict` with new values `rfid_granted` / `rfid_denied`, or
- keep `granted`/`denied` and add an `events.method` column (`face` / `rfid`).

**Recommendation:** add `events.method VARCHAR(8)` (default `face`) via the existing
`_ensure_event_column()` idempotent migration helper — it keeps the verdict vocabulary
stable (the chart/stats code already groups by `verdict`) while distinguishing the source.
RFID events the device queues offline are POSTed on reconnect to a small new endpoint
(e.g. `POST /rfid/event`, device-token auth) that funnels through the same `_maybe_log()`
choke point so debouncing, snapshots-absence, retention, and Telegram alerts all apply
uniformly.

## Dashboard changes

On the **Users** page (or a small new **RFID** section), per user:

- List their tags (label + masked/last-4 of UID + enabled toggle).
- **Add a tag.** Two options:
  - *Manual:* type the UID (printed on the fob / read with a desktop reader).
  - *Scan-to-enroll (nicer):* a short-lived "present a card now" mode where the door
    device reports the next-seen UID to the server for assignment. (Optional; manual entry
    is the minimum viable path.)
- Enable/disable or delete a tag.

No changes to the matching/face logic.

## Configuration additions

| Where | Key | Purpose |
|-------|-----|---------|
| `.env` | `DEVICE_TOKEN` | Shared secret for device-only endpoints (`/rfid/allowlist`, `/rfid/event`). |
| Firmware | `RELOCK_AFTER_S` | Seconds the servo stays unlocked before auto-relock. |
| Firmware | servo `LOCKED_ANGLE` / `UNLOCKED_ANGLE` | Servo positions. |
| Firmware | RC522 + servo pin map | Validated on the bench (see GPIO caution). |
| Firmware | allowlist poll interval | How often to re-sync the UID list when online. |

## Security considerations

- **UID cloning.** MIFARE Classic UIDs can be cloned; UID-only auth is weak against a
  determined attacker. Acceptable for a home/demo lock as a *convenience fallback* behind
  the primary face path. A hardening step (future): use the card's authenticated sectors /
  a challenge-response (DESFire) instead of bare UID. Document this as a known limitation.
- **Device endpoints** (`/rfid/allowlist`, `/rfid/event`) require `X-Device-Token`; they
  are not behind the dashboard session (the device can't log in), but unlike `/verify`
  they are not fully open.
- **Fail-secure servo.** Locked is the default/rest state; loss of power or an unknown
  state must not open the door.
- **Offline revocation lag.** A revoked tag can still open the door during a network
  outage until the device next syncs — stated trade-off (see "where the allowlist is
  authoritative").
- **No secrets to the browser.** Same as the Telegram token: device token lives in `.env`,
  never echoed to the dashboard.

## Error handling (consistent with the existing philosophy)

- Allowlist fetch fails → keep last-good NVS cache; door keeps working offline.
- Server unreachable → RFID still unlocks locally; queued events flush on reconnect
  (best-effort, capped queue; dropping the oldest is fine — the door action already
  happened).
- Servo/PWM error → fail to the locked position; buzz to signal a fault.
- Any server-side RFID logging failure is swallowed like face logging (door unaffected).

## Testing

- **Unit (pure, no hardware):**
  - UID normalization (case/whitespace/format) and allowlist membership.
  - `enabled_uids()` returns only enabled tags; revoked/deleted excluded.
  - Event-method tagging round-trips through `_maybe_log`.
- **Integration (server):** `/rfid/allowlist` returns the right set + version and rejects a
  missing/bad device token; `/rfid/event` funnels through `_maybe_log` (debounce/retention
  apply).
- **Bench (hardware):** scan an authorized tag with WiFi off → servo unlocks, auto-relocks;
  scan an unknown tag → buzzer; revoke a tag in the dashboard, re-sync, confirm it no
  longer opens; power-cycle mid-unlock → returns to locked.

## Build order

1. `rfid_tags` table + `db.py` helpers + `events.method` migration.
2. Dashboard tag management (manual UID entry first).
3. `/rfid/allowlist` + `/rfid/event` endpoints with device-token auth.
4. Firmware: RC522 read + NVS allowlist cache + offline decision + sync.
5. Firmware: wire the RFID hit into the existing `unlockDoor()`; share the grant cooldown;
   confirm fail-secure on boot/brownout. (Servo actuation itself already exists.)
6. Optional: scan-to-enroll, DESFire challenge-response hardening.

## Out of scope

- No biometrics-on-card or PIN keypad (separate future feature).
- No multi-door / multi-lock coordination.
- No change to the face-matching, anti-spoofing, or low-light logic.
- Strong card cryptography (DESFire) is noted as a hardening path, not part of v1.
