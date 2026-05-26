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
