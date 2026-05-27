# Telegram Alerts (DB-backed, dashboard-configured) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (inline) to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

> **Commits:** The user owns commits. Do NOT commit automatically; leave changes in the working tree.

**Goal:** Send a Telegram message with the snapshot when a spoof/denied (or opted-in granted) event is logged, configured from a new dashboard Settings page backed by a MySQL `settings` table.

**Architecture:** A key-value `settings` table + `db` helpers; a new `notify.py` (pure `parse_config`/`should_alert` + threaded sender, no new deps); a one-line hook in `_maybe_log`; a Settings nav page with save + test routes.

**Tech Stack:** Flask blueprint, PyMySQL, Jinja, stdlib `urllib`/`threading`/`json`. Pytest for the pure helpers.

**Reference spec:** `docs/superpowers/specs/2026-05-27-telegram-alerts-design.md`

---

## File structure
- **Modify** `db.py` — `settings` DDL in `init_db()`, `get_settings()`, `set_settings()`.
- **Create** `notify.py` — `parse_config`, `should_alert` (pure); `alert`, `send_test`, senders.
- **Create** `tests/test_notify.py` — unit tests for the pure helpers.
- **Modify** `server.py` — `import notify`; call `notify.alert(...)` in `_maybe_log`.
- **Modify** `dashboard.py` — `import notify`; `settings_page`/`settings_save`/`settings_test`.
- **Create** `templates/settings.html`; **modify** `templates/base.html` (nav) + `static/style.css` (form styles).

---

### Task 1: `db.py` — settings table + helpers

- [ ] **Step 1: Add the DDL constant** after `_DDL_USER_PHOTOS`:
```python
_DDL_SETTINGS = """
CREATE TABLE IF NOT EXISTS settings (
    name  VARCHAR(64) PRIMARY KEY,
    value TEXT
) ENGINE=InnoDB
"""
```

- [ ] **Step 2: Create the table in `init_db()`** — add after the `cur.execute(_DDL_USER_PHOTOS)` line:
```python
        cur.execute(_DDL_SETTINGS)
```

- [ ] **Step 3: Add the helpers** (after `init_db`/`_ensure_event_column`):
```python
def get_settings():
    """Return all rows of the settings table as a {name: value} dict."""
    with _connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT name, value FROM settings")
        return {r["name"]: r["value"] for r in cur.fetchall()}


def set_settings(mapping):
    """Upsert each {name: value} pair into the settings table."""
    if not mapping:
        return
    with _connect() as conn, conn.cursor() as cur:
        for name, value in mapping.items():
            cur.execute(
                "INSERT INTO settings (name, value) VALUES (%s, %s) "
                "ON DUPLICATE KEY UPDATE value=VALUES(value)",
                (name, value),
            )
```

- [ ] **Step 4: Verify** `python -c "import ast; ast.parse(open('db.py').read()); print('ok')"`.

---

### Task 2: `notify.py` + unit tests (TDD on the pure helpers)

- [ ] **Step 1: Write `tests/test_notify.py`** (failing first):
```python
import notify


def test_parse_config_defaults_when_empty():
    c = notify.parse_config({})
    assert c["cooldown"] == 300
    assert c["verdicts"] == {"spoof", "denied"}
    assert c["enabled"] is False
    assert c["token"] == "" and c["chat_id"] == ""


def test_parse_config_enabled_requires_toggle_token_and_chat():
    base = {"telegram_bot_token": "t", "telegram_chat_id": "c", "alerts_enabled": "1"}
    assert notify.parse_config(base)["enabled"] is True
    assert notify.parse_config({**base, "alerts_enabled": "0"})["enabled"] is False
    assert notify.parse_config({**base, "telegram_bot_token": ""})["enabled"] is False
    assert notify.parse_config({**base, "telegram_chat_id": ""})["enabled"] is False


def test_parse_config_bad_cooldown_falls_back_to_300():
    assert notify.parse_config({"alert_cooldown_s": "abc"})["cooldown"] == 300
    assert notify.parse_config({"alert_cooldown_s": "60"})["cooldown"] == 60


def test_parse_config_parses_verdicts_list():
    assert notify.parse_config({"alert_verdicts": "spoof, granted"})["verdicts"] == {"spoof", "granted"}


def test_should_alert_cooldown_and_independence():
    st = {}
    assert notify.should_alert("spoof", 1000.0, st, 300) is True   # first
    assert notify.should_alert("spoof", 1100.0, st, 300) is False  # within cooldown
    assert notify.should_alert("spoof", 1400.0, st, 300) is True   # cooldown elapsed
    assert notify.should_alert("denied", 1100.0, st, 300) is True  # different verdict
```

- [ ] **Step 2: Run to confirm failure** — `conda run -n smartdoor python -m pytest tests/test_notify.py -v` → FAIL (no module `notify`).

- [ ] **Step 3: Create `notify.py`**:
```python
"""Telegram alerting for spoof/denied (and opt-in granted) door events.

Config comes from the DB `settings` table (edited via the dashboard), not env. Sending is
fire-and-forget on a daemon thread and never affects the door. No third-party deps — stdlib
urllib/threading/json only.
"""
import json
import threading
import time
import urllib.request
import uuid

import db

_API = "https://api.telegram.org/bot{token}/{method}"
_TRUE = ("1", "true", "True", "on", "yes")
_state = {}   # {verdict: last_alert_ts}, process-lifetime cooldown tracking


def parse_config(settings):
    """Normalize the raw {name: value} settings dict into a typed config. Pure (no DB/net).
    `enabled` is True only when the toggle is on AND both token and chat_id are non-empty."""
    token = (settings.get("telegram_bot_token") or "").strip()
    chat_id = (settings.get("telegram_chat_id") or "").strip()
    try:
        cooldown = int(settings.get("alert_cooldown_s") or 300)
    except (TypeError, ValueError):
        cooldown = 300
    raw = settings.get("alert_verdicts")
    verdicts = {"spoof", "denied"} if raw is None else {v.strip() for v in raw.split(",") if v.strip()}
    enabled = (settings.get("alerts_enabled") in _TRUE) and bool(token) and bool(chat_id)
    return {"token": token, "chat_id": chat_id, "cooldown": cooldown,
            "verdicts": verdicts, "enabled": enabled}


def should_alert(verdict, now, state, cooldown):
    """True if `verdict` hasn't alerted within `cooldown` seconds; records `now` if so.
    Mutates `state` (a {verdict: last_ts} dict); otherwise pure."""
    last = state.get(verdict)
    if last is not None and now - last < cooldown:
        return False
    state[verdict] = now
    return True


def _caption(verdict, person, distance, antispoof_score):
    when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    if verdict == "spoof":
        head = "🛡 Spoof blocked at the door"
        detail = f"Anti-spoof score: {antispoof_score:.2f}" if antispoof_score is not None else ""
    elif verdict == "denied":
        head = "🚫 Denied — unrecognized face"
        detail = f"Closest distance: {distance:.3f}" if distance is not None else ""
    else:
        head = f"✅ {verdict.capitalize()} at the door"
        detail = ""
    who = f"\nWho: {person}" if person else ""
    tail = f"\n{detail}" if detail else ""
    return f"{head}{who}\nWhen: {when}{tail}"


def _multipart(fields, file_field, file_bytes, filename="snapshot.jpg"):
    boundary = uuid.uuid4().hex
    parts = []
    for name, value in fields.items():
        parts.append(b"--" + boundary.encode())
        parts.append(f'Content-Disposition: form-data; name="{name}"'.encode())
        parts.append(b"")
        parts.append(str(value).encode())
    if file_bytes is not None:
        parts.append(b"--" + boundary.encode())
        parts.append(
            f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"'.encode())
        parts.append(b"Content-Type: image/jpeg")
        parts.append(b"")
        parts.append(file_bytes)
    parts.append(b"--" + boundary.encode() + b"--")
    parts.append(b"")
    return f"multipart/form-data; boundary={boundary}", b"\r\n".join(parts)


def _post(token, method, fields, file_bytes=None, file_field="photo", timeout=10):
    ctype, body = _multipart(fields, file_field, file_bytes)
    req = urllib.request.Request(_API.format(token=token, method=method),
                                 data=body, headers={"Content-Type": ctype}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode())
    if not payload.get("ok"):
        raise RuntimeError(payload.get("description", "Telegram API error"))
    return payload


def _send(token, chat_id, caption, jpeg_bytes):
    if jpeg_bytes:
        _post(token, "sendPhoto", {"chat_id": chat_id, "caption": caption}, file_bytes=jpeg_bytes)
    else:
        _post(token, "sendMessage", {"chat_id": chat_id, "text": caption})


def _safe_send(token, chat_id, caption, jpeg_bytes):
    try:
        _send(token, chat_id, caption, jpeg_bytes)
    except Exception as ex:
        print("[notify] send failed (door unaffected):", ex)


def send_test(token, chat_id):
    """Synchronous test send for the dashboard button. Returns (ok, message)."""
    if not token or not chat_id:
        return False, "Set a bot token and chat ID first."
    try:
        _post(token, "sendMessage",
              {"chat_id": chat_id,
               "text": "✅ Smart Door test alert — Telegram is configured correctly."})
        return True, "Test message sent."
    except Exception as ex:
        return False, f"Test failed: {ex}"


def alert(verdict, person, distance, antispoof_score, jpeg_bytes):
    """Fire-and-forget alert for a just-logged event. Reads config from the DB, applies the
    per-verdict cooldown, and sends on a daemon thread. Never raises to the caller."""
    try:
        cfg = parse_config(db.get_settings())
        if not cfg["enabled"] or verdict not in cfg["verdicts"]:
            return
        if not should_alert(verdict, time.time(), _state, cfg["cooldown"]):
            return
        caption = _caption(verdict, person, distance, antispoof_score)
        threading.Thread(target=_safe_send,
                         args=(cfg["token"], cfg["chat_id"], caption, jpeg_bytes),
                         daemon=True).start()
    except Exception as ex:
        print("[notify] alert skipped (door unaffected):", ex)
```

- [ ] **Step 4: Run tests** — `conda run -n smartdoor python -m pytest tests/test_notify.py -v` → 5 passed.

---

### Task 3: `server.py` — hook into `_maybe_log`

- [ ] **Step 1: Import notify** — add `import notify` next to `import matching` near the top.

- [ ] **Step 2: Call alert after a successful log.** In `_maybe_log`, change:
```python
        db.log_event(verdict, distance, threshold, person=person, jpeg_bytes=jpeg_bytes,
                     antispoof_score=antispoof_score)
        _log_count += 1
        if _log_count % 50 == 0:
            db.prune()
```
to:
```python
        db.log_event(verdict, distance, threshold, person=person, jpeg_bytes=jpeg_bytes,
                     antispoof_score=antispoof_score)
        notify.alert(verdict, person, distance, antispoof_score, jpeg_bytes)
        _log_count += 1
        if _log_count % 50 == 0:
            db.prune()
```
(`notify.alert` decides which verdicts actually send, per the saved config — so granted is included only if the user ticks it. It never raises.)

- [ ] **Step 3: Verify** `conda run -n smartdoor python -c "import ast; ast.parse(open('server.py').read()); print('ok')"`.

---

### Task 4: `dashboard.py` — Settings routes

- [ ] **Step 1: Import notify** — add `import notify` below `import db`.

- [ ] **Step 2: Add the three routes** (after `home()`/`users_page()`):
```python
@bp.route("/dashboard/settings")
@login_required
def settings_page():
    return render_template("settings.html", settings=db.get_settings())


@bp.route("/settings", methods=["POST"])
@login_required
def settings_save():
    form = request.form
    updates = {
        "telegram_chat_id": form.get("telegram_chat_id", "").strip(),
        "alert_cooldown_s": form.get("alert_cooldown_s", "300").strip() or "300",
        "alert_verdicts": ",".join(form.getlist("alert_verdicts")),
        "alerts_enabled": "1" if form.get("alerts_enabled") else "0",
    }
    token = form.get("telegram_bot_token", "").strip()
    if token:                                 # blank submission keeps the saved token
        updates["telegram_bot_token"] = token
    db.set_settings(updates)
    return redirect(url_for("dashboard.settings_page", msg="Settings saved."))


@bp.route("/settings/test", methods=["POST"])
@login_required
def settings_test():
    s = db.get_settings()
    ok, message = notify.send_test(s.get("telegram_bot_token", ""), s.get("telegram_chat_id", ""))
    return redirect(url_for("dashboard.settings_page", msg=message))
```

- [ ] **Step 3: Verify** `conda run -n smartdoor python -c "import ast; ast.parse(open('dashboard.py').read()); print('ok')"`.

---

### Task 5: Templates + nav + CSS

- [ ] **Step 1: Add the Settings nav link** in `templates/base.html`, after the Users `<a>`:
```html
      <a href="{{ url_for('dashboard.settings_page') }}"
         class="{{ 'active' if request.endpoint == 'dashboard.settings_page' else '' }}">Settings</a>
```

- [ ] **Step 2: Create `templates/settings.html`**:
```html
{% extends "base.html" %}
{% block content %}
  <main class="wrap">
    <section class="card settings-card">
      <div class="card-title">Telegram alerts</div>
      <form class="settings-form" method="post" action="{{ url_for('dashboard.settings_save') }}">
        <label>Bot token
          <input type="password" name="telegram_bot_token" autocomplete="off"
                 placeholder="{{ 'saved ✓ — leave blank to keep' if settings.get('telegram_bot_token') else 'paste token from @BotFather' }}">
        </label>
        <label>Chat ID
          <input type="text" name="telegram_chat_id" placeholder="e.g. 123456789"
                 value="{{ settings.get('telegram_chat_id', '') }}">
        </label>
        <label>Cooldown (seconds)
          <input type="number" name="alert_cooldown_s" min="0"
                 value="{{ settings.get('alert_cooldown_s', '300') }}">
        </label>
        {% set sel = (settings.get('alert_verdicts', 'spoof,denied')).split(',') %}
        <fieldset class="verdicts">
          <legend>Alert on</legend>
          <label class="chk"><input type="checkbox" name="alert_verdicts" value="spoof" {{ 'checked' if 'spoof' in sel else '' }}> Spoof</label>
          <label class="chk"><input type="checkbox" name="alert_verdicts" value="denied" {{ 'checked' if 'denied' in sel else '' }}> Denied</label>
          <label class="chk"><input type="checkbox" name="alert_verdicts" value="granted" {{ 'checked' if 'granted' in sel else '' }}> Granted</label>
        </fieldset>
        <label class="chk">
          <input type="checkbox" name="alerts_enabled" value="1" {{ 'checked' if settings.get('alerts_enabled') == '1' else '' }}> Enable alerts
        </label>
        <button class="btn-save" type="submit">Save</button>
      </form>
      <form class="settings-test" method="post" action="{{ url_for('dashboard.settings_test') }}">
        <button class="btn-test" type="submit">Send test message</button>
      </form>
    </section>
  </main>
{% endblock %}
```

- [ ] **Step 3: Append settings CSS** to `static/style.css`:
```css

/* ---- settings ---- */
.settings-card { max-width: 520px; }
.settings-form { display: flex; flex-direction: column; gap: 14px; }
.settings-form > label { display: flex; flex-direction: column; gap: 6px;
  font-size: 13px; color: var(--muted); }
.settings-form input[type=text], .settings-form input[type=password],
.settings-form input[type=number] {
  background: var(--panel-2); color: var(--text); border: 1px solid var(--border);
  border-radius: 8px; padding: 9px 12px; font-size: 14px; }
.settings-form .verdicts {
  border: 1px solid var(--border); border-radius: 8px; padding: 10px 12px;
  display: flex; gap: 18px; flex-wrap: wrap; margin: 0; }
.settings-form .verdicts legend { color: var(--muted); font-size: 12px; padding: 0 4px; }
.settings-form label.chk { flex-direction: row; align-items: center; gap: 8px; color: var(--text); }
.btn-save { align-self: flex-start; background: var(--accent); color: #fff; border: none;
  border-radius: 8px; padding: 9px 18px; font-size: 14px; font-weight: 600; cursor: pointer; }
.btn-save:hover { filter: brightness(1.1); }
.settings-test { margin-top: 14px; }
.btn-test { background: var(--panel-2); color: var(--text); border: 1px solid var(--border);
  border-radius: 8px; padding: 9px 16px; font-size: 14px; cursor: pointer; }
.btn-test:hover { filter: brightness(1.15); }
```

- [ ] **Step 4: Verify templates compile** —
`conda run -n smartdoor python -c "from jinja2 import Environment, FileSystemLoader as L; e=Environment(loader=L('templates')); [e.get_template(t) for t in ('base.html','settings.html')]; print('ok')"`
and CSS braces balance.

---

## Manual verification (requires a server RESTART — `init_db` must create the `settings` table)

1. Restart the server (the running process predates the new table/code). Open `/dashboard/settings` — the **Settings** nav item appears and is highlighted.
2. Enter a real bot token + chat ID, tick **Spoof**, **Denied**, **Enable alerts**, set cooldown, **Save** → toast "Settings saved." Reopen: chat ID persists, token field is blank but placeholder says "saved ✓".
3. Click **Send test message** → toast "Test message sent." and the message arrives in Telegram. (Or a clear failure toast if token/chat is wrong.)
4. Trigger a spoof/denied (`simulate_esp32.py --stranger <face>` or a photo of an enrolled face) → a Telegram alert with the snapshot arrives; a second spoof within the cooldown produces no second alert.
5. Turn **Enable alerts** off, Save → no more alerts fire.

## Notes
- `notify.alert` reads config per call (alerts are rare/debounced), so changes take effect without a restart once the table exists.
- The bot token is stored plaintext in MySQL behind the dashboard login (documented trade-off); the form never echoes it back.
