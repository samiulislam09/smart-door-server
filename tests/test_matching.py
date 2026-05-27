import numpy as np
import pytest

import matching


def test_cosine_distance_identical_is_zero():
    v = np.array([1.0, 2.0, 3.0], dtype=np.float32)
    assert matching.cosine_distance(v, v) == pytest.approx(0.0, abs=1e-6)


def test_embedding_roundtrip_preserves_values():
    v = np.array([0.1, -0.2, 0.3, 0.4], dtype=np.float32)
    restored = matching.bytes_to_embedding(matching.embedding_to_bytes(v))
    assert restored.dtype == np.float32
    assert np.allclose(restored, v)


def test_best_match_picks_closest_within_threshold():
    target = np.array([1.0, 0.0], dtype=np.float32)
    owners = [
        ("Alice", np.array([1.0, 0.0], dtype=np.float32)),   # distance 0
        ("Bob",   np.array([0.0, 1.0], dtype=np.float32)),   # distance 1
    ]
    name, dist = matching.best_match(target, owners, threshold=0.5)
    assert name == "Alice"
    assert dist == pytest.approx(0.0, abs=1e-6)


def test_best_match_returns_none_but_reports_distance_when_all_above_threshold():
    target = np.array([1.0, 0.0], dtype=np.float32)
    owners = [("Bob", np.array([0.0, 1.0], dtype=np.float32))]  # distance 1
    name, dist = matching.best_match(target, owners, threshold=0.5)
    assert name is None
    assert dist == pytest.approx(1.0, abs=1e-6)


def test_best_match_empty_owners_returns_none_none():
    target = np.array([1.0, 0.0], dtype=np.float32)
    assert matching.best_match(target, [], threshold=0.5) == (None, None)


def test_validate_name_trims_and_accepts():
    assert matching.validate_name("  Alice  ") == (True, "Alice")


def test_validate_name_rejects_empty():
    ok, msg = matching.validate_name("   ")
    assert ok is False
    assert "required" in msg.lower()


def test_validate_name_rejects_too_long():
    ok, msg = matching.validate_name("x" * 65)
    assert ok is False
    assert "64" in msg


def test_cosine_distance_zero_vector_is_not_nan():
    zero = np.zeros(3, dtype=np.float32)
    other = np.array([1.0, 0.0, 0.0], dtype=np.float32)
    d = matching.cosine_distance(zero, other)
    assert not np.isnan(d)
    assert d == pytest.approx(1.0, abs=1e-6)


def test_bytes_to_embedding_is_writable():
    v = np.array([0.1, 0.2], dtype=np.float32)
    restored = matching.bytes_to_embedding(matching.embedding_to_bytes(v))
    assert restored.flags.writeable is True


def test_is_spoof_blocks_confident_fake():
    assert matching.is_spoof(False, 0.95, 0.0) is True
    assert matching.is_spoof(False, 0.95, 0.9) is True


def test_is_spoof_passes_low_confidence_fake_when_threshold_raised():
    # model says fake but only 0.5 confident; with min_score 0.9 we don't block it
    assert matching.is_spoof(False, 0.5, 0.9) is False


def test_is_spoof_never_blocks_real():
    assert matching.is_spoof(True, 0.99, 0.0) is False


def test_is_spoof_safe_on_none():
    assert matching.is_spoof(None, None, 0.0) is False
    assert matching.is_spoof(False, None, 0.0) is False


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
    # LED was already off; next_led_state never turns it on (is_low_light does that probe)
    assert matching.next_led_state(True, False, True) == "off"


def test_next_led_state_off_when_flash_disabled():
    assert matching.next_led_state(False, True, True) == "off"
