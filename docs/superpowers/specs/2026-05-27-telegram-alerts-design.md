# Telegram alerts on spoof/denied — design

**Date:** 2026-05-27
**Status:** Approved, pending implementation

## Goal

Send a Telegram message (with the event snapshot attached) whenever a `spoof` or `denied`
event is logged, so the owner is notified of suspicious activity in real time. All
configuration (bot token, chat ID, cooldown, which verdicts, enable toggle) is stored in
MySQL and edited from a new **Settings** page in the dashboard — not in `.env`.

## Background

`server.py`'s `_maybe_log(reason, distance, threshold, person, jpeg_bytes, antispoof_score)`
is the single choke point for access events: it maps `match/no_match/spoof` →
`granted/denied/spoof`, debounces repeats of the same `(verdict, person)` within
`LOG_DEBOUNCE_S` (10s), and writes a row via `db.log_event(...)` with the JPEG. Logging
failures are swallowed so they never affect the door. The dashboard is a Flask blueprint
(`dashboard.py`) with a shared `base.html` (topbar + nav) extended by `dashboard.html`
(Overview) and `users.html` (Users). `db.py` owns the schema (a DDL string per table run in
`init_db()`) and all queries.

## Components

### 1. Storage — `db.py`

Add a key-value settings table and helpers:

```sql
CREATE TABLE IF NOT EXISTS settings (
    name  VARCHAR(64) PRIMARY KEY,
    value TEXT
) ENGINE=InnoDB
```

- Created in `init_db()` (alongside the existing tables).
- `get_settings()` → `{name: value}` dict of all rows.
- `set_settings(mapping)` → upsert each pair via
  `INSERT INTO settings (name, value) VALUES (%s,%s) ON DUPLICATE KEY UPDATE value=VALUES(value)`.

Stored keys (all stored as strings):
- `telegram_bot_token`
- `telegram_chat_id`
- `alert_cooldown_s` (default `300`)
- `alert_verdicts` (default `spoof,denied`)
- `alerts_enabled` (`1`/`0`, default `0`)

### 2. `notify.py` (new module)

No new dependencies — uses `urllib.request`, `threading`, `json` (all already used in
`server.py`).

- `parse_config(settings_dict)` — **pure**. Normalizes a raw settings dict into:
  `{"token": str, "chat_id": str, "cooldown": int, "verdicts": set[str], "enabled": bool}`.
  Defaults: cooldown `300` (non-int / missing falls back to 300), verdicts parsed from a
  comma list (default `{"spoof","denied"}`), `enabled` is True only when `alerts_enabled`
  is truthy AND both token and chat_id are non-empty. No DB or network access.
- `should_alert(verdict, now, state, cooldown)` — **pure**. `state` is a dict
  `{verdict: last_ts}`. Returns True (and updates `state[verdict]=now`) if the verdict has
  not alerted within `cooldown` seconds; otherwise False. Per-verdict independent.
- `alert(verdict, person, distance, antispoof_score, jpeg_bytes)` — orchestrator. Reads
  `db.get_settings()`, `parse_config(...)`; returns immediately if not `enabled`, if
  `verdict not in verdicts`, or if `should_alert(...)` is False (using a module-level
  `_state` dict). Otherwise builds a caption and spawns a **daemon thread** that calls
  `_send_photo(token, chat_id, caption, jpeg_bytes)`. All exceptions caught and printed;
  never raises to the caller.
- `send_test(token, chat_id)` — **synchronous** (the UI needs the result), short timeout
  (~10s). Sends a fixed test message via `_send_message(...)`. Returns `(ok: bool,
  message: str)`.
- `_send_photo(token, chat_id, caption, jpeg_bytes)` / `_send_message(token, chat_id, text)`
  — POST to `https://api.telegram.org/bot<token>/sendPhoto` (multipart/form-data built
  manually with `urllib.request`, photo as a file part) / `/sendMessage`. Raise on non-OK
  so callers can report/swallow.

Caption format:
- spoof → `🛡 Spoof blocked at the door` + newline + `When: <local time>` + `Anti-spoof
  score: <score 2dp>` (when present).
- denied → `🚫 Denied — unrecognized face` + newline + `When: <local time>` + `Closest
  distance: <distance 3dp>` (when present).

### 3. Hook — `server.py`

In `_maybe_log`, after the successful `db.log_event(...)` (inside the existing `try`), call
`notify.alert(verdict, person, distance, antispoof_score, jpeg_bytes)`. Because it sits
after the 10s log debounce and only does a fast settings read + thread spawn, it adds
negligible latency. Any failure is already inside the swallowed `try`. Import `notify` at
the top of `server.py`.

### 4. Dashboard — `dashboard.py` + templates

- `base.html`: add a third nav item **Settings** → `dashboard.settings_page`, with the same
  `request.endpoint`-based active highlighting as Overview/Users.
- `GET /dashboard/settings` → `settings_page()` (login-required): renders `settings.html`
  with `db.get_settings()`. The bot token value is **never** written into the form; the
  template shows whether a token is saved (e.g. "saved ✓" vs "not set") and the input stays
  empty.
- `POST /settings` → `settings_save()` (login-required): reads form fields and upserts via
  `db.set_settings`. For `telegram_bot_token`: if the submitted value is blank, **do not
  overwrite** the stored token (lets the user save other fields without re-entering the
  secret). `alert_verdicts` is rebuilt from the checked checkboxes; `alerts_enabled` from
  the toggle; `alert_cooldown_s` from the number field. Redirect to `settings_page` with a
  `msg` toast.
- `POST /settings/test` → `settings_test()` (login-required): loads the saved token/chat,
  calls `notify.send_test(...)`, redirects to `settings_page` with the `(ok, message)`
  result as the `msg` toast.
- `settings.html` (new, extends `base.html`): a "Telegram alerts" card with a form
  (`POST /settings`) containing — bot token (`<input type="password">`, blank = keep
  existing, with a "saved ✓ / not set" indicator), chat ID (text), cooldown seconds
  (number, default 300), verdict checkboxes (Spoof, Denied, Granted), an **Enable alerts**
  checkbox, and a **Save** button — plus a separate small form (`POST /settings/test`) with
  a **Send test message** button.

## Configuration flow

All config lives in MySQL; nothing in `.env` for this feature. Until the user saves
anything, defaults apply (`alerts_enabled=0`, so alerts are off out of the box). Saving the
token/chat and enabling the toggle turns alerts on with no server restart (config is read
per alert).

## Security

The bot token is stored in MySQL **plaintext**; the Settings page is behind the existing
dashboard password login. The form uses a masked (`password`) input and never echoes the
saved token back to the browser. This is acceptable for a self-hosted local deployment and
is a conscious trade-off (documented here, not a surprise).

## Error handling

- Not enabled / missing token or chat → `alert()` is a silent no-op.
- Telegram or network failure during `alert()` → caught in the daemon thread, printed, door
  unaffected (same philosophy as the existing swallowed logging).
- `send_test()` surfaces failures to the UI as a toast (e.g. "Test failed: <reason>").

## Testing

- **Unit (no DB/network):**
  - `parse_config`: defaults when keys absent; verdict comma-list parsing; non-int cooldown
    falls back to 300; `enabled` false when toggle off or token/chat blank, true otherwise.
  - `should_alert`: first call for a verdict alerts; a repeat within cooldown does not; after
    cooldown it alerts again; two different verdicts throttle independently.
- **Manual:** in Settings, save token + chat ID, tick Enable + Spoof/Denied, Save → click
  **Send test message** and confirm the Telegram message arrives. Trigger a denied/spoof
  (`simulate_esp32.py --stranger <face>` or a dark/photo frame) → confirm a photo alert
  arrives; a second spoof within the cooldown produces no second alert.

## Out of scope

- Telegram only (no email/SMS/push providers).
- `granted`/`no_face` never alert unless Granted is ticked.
- No per-user alert routing or multiple recipients (single chat ID).
- No changes to matching/door logic.
