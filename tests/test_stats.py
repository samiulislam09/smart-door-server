import db


def test_stats_reports_spoof_today():
    db.init_db()
    before = db.stats()["spoof_today"]          # key must exist
    db.log_event("spoof", None, None, antispoof_score=0.95)
    after = db.stats()["spoof_today"]
    assert after == before + 1


def test_stats_still_has_granted_denied_keys():
    s = db.stats()
    assert "granted_today" in s and "denied_today" in s and "last_seen" in s and "series" in s
