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
