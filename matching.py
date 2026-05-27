"""Pure, dependency-light helpers for face matching and enrollment.

No DeepFace or MySQL imports here on purpose: this module is safe to import in unit
tests and has no side effects. server.py composes these with the heavy embedding step.
"""
import numpy as np


def cosine_distance(a, b):
    """Cosine distance in [0, 2]; 0 = identical direction. A zero-norm vector has no
    direction, so it's treated as maximally distant (1.0) rather than yielding nan."""
    a = np.asarray(a, dtype=np.float32)
    b = np.asarray(b, dtype=np.float32)
    norm_a, norm_b = np.linalg.norm(a), np.linalg.norm(b)
    if norm_a == 0 or norm_b == 0:
        return 1.0
    return 1.0 - float(np.dot(a, b) / (norm_a * norm_b))


def best_match(embedding, owners, threshold):
    """Find the closest enrolled face.

    owners: list of (name, embedding_np). Returns (name_or_None, distance_or_None):
      - name is set only when the closest owner is within `threshold`
      - distance is the closest distance found (None when there are no owners)
    """
    best_name, best_dist = None, None
    for name, owner_emb in owners:
        d = cosine_distance(embedding, owner_emb)
        if best_dist is None or d < best_dist:
            best_name, best_dist = name, d
    if best_dist is None:
        return None, None
    if best_dist <= threshold:
        return best_name, best_dist
    return None, best_dist


def embedding_to_bytes(emb):
    """Serialize an embedding to raw float32 bytes for DB storage."""
    return np.asarray(emb, dtype=np.float32).tobytes()


def bytes_to_embedding(blob):
    """Inverse of embedding_to_bytes. Returns a writable copy (np.frombuffer alone is
    read-only, which would break any later in-place use)."""
    return np.frombuffer(blob, dtype=np.float32).copy()


def validate_name(raw):
    """Validate a user-supplied name. Returns (True, cleaned) or (False, error)."""
    name = (raw or "").strip()
    if not name:
        return False, "Name is required."
    if len(name) > 64:
        return False, "Name must be 64 characters or fewer."
    return True, name


def is_spoof(is_real, score, min_score):
    """True when a frame should be rejected as a spoof: the model judged it fake AND is at
    least `min_score` confident. min_score=0.0 trusts the model's decision; raising it blocks
    only confident spoofs (fewer false-rejects of real people). Safe on None inputs."""
    return (is_real is False) and (score is not None) and (score >= min_score)


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
