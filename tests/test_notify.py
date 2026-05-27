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
