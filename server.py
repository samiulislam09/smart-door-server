from flask import Flask, request, jsonify, Response
from deepface import DeepFace
import io, tempfile, os, time, threading, urllib.request
import cv2
import numpy as np
from PIL import Image

app = Flask(__name__)
OWNER_IMG = "owner.jpg"

# SFace is the fastest of the DeepFace models (~half VGG-Face's time at similar
# accuracy). Override with FACE_MODEL if you want a different one.
MODEL_NAME = os.environ.get("FACE_MODEL", "SFace")
DETECTOR = "opencv"  # fast (~30ms) Haar-based detector

# Optional stricter threshold. If unset, the model's own cosine threshold is used.
# Lower = stricter (a smaller distance is required to count as a match).
_env_threshold = os.environ.get("MATCH_THRESHOLD")
MATCH_THRESHOLD = float(_env_threshold) if _env_threshold else None

# Owner's face embedding is computed once at startup so each /verify only has to embed
# the incoming frame, not re-process owner.jpg every call.
_OWNER_EMBEDDING = None
_THRESHOLD = None

_FACE_CASCADE = cv2.CascadeClassifier(
    cv2.data.haarcascades + "haarcascade_frontalface_default.xml")

# ESP32-CAM MJPEG stream the viewer proxies. Overridable per-request via ?src=.
ESP_STREAM_URL = os.environ.get("ESP_STREAM_URL", "http://192.168.1.102:81/stream")


def _is_no_face(exc):
    """True if exc (or any chained cause/context) is DeepFace's no-face error."""
    seen = set()
    cur = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if type(cur).__name__ == "FaceNotDetected" \
                or "could not be detected" in str(cur).lower():
            return True
        cur = cur.__cause__ or cur.__context__
    return False


def _embed(img):
    """Embedding of the first detected face. Raises (FaceNotDetected) if no face."""
    reps = DeepFace.represent(img, model_name=MODEL_NAME, detector_backend=DETECTOR,
                              enforce_detection=True)
    return np.asarray(reps[0]["embedding"], dtype=np.float32)


def _cosine_distance(a, b):
    return 1.0 - float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b)))


def init_owner():
    """Build the model, capture its threshold, and cache the owner embedding."""
    global _OWNER_EMBEDDING, _THRESHOLD
    res = DeepFace.verify(OWNER_IMG, OWNER_IMG, model_name=MODEL_NAME,
                          detector_backend=DETECTOR, enforce_detection=False)
    _THRESHOLD = float(MATCH_THRESHOLD) if MATCH_THRESHOLD is not None \
        else float(res["threshold"])
    _OWNER_EMBEDDING = _embed(OWNER_IMG)
    print(f"[init] model={MODEL_NAME} threshold={_THRESHOLD:.4f} owner embedding cached")


def match_face(img):
    """Compare img against the cached owner embedding. img = file path or BGR frame.

    Returns (reason, distance, threshold). distance/threshold are None when no face is
    present or on error.
    """
    try:
        emb = _embed(img)
    except Exception as e:
        if _is_no_face(e):
            return "no_face", None, None
        return "error", None, None
    distance = _cosine_distance(_OWNER_EMBEDDING, emb)
    reason = "match" if distance <= _THRESHOLD else "no_match"
    return reason, round(distance, 4), round(_THRESHOLD, 4)


@app.route('/verify', methods=['POST'])
def verify():
    # Validate the body is a real image; a bad/empty body is a client error, not a
    # face mismatch.
    raw = request.data
    try:
        img = Image.open(io.BytesIO(raw))
        img.load()
    except Exception as e:
        return jsonify({"verified": False, "reason": "error",
                        "error": f"cannot read image: {e}"})

    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(suffix='.jpg')
        os.close(fd)
        # The ESP32 sends JPEG already, so write the original bytes verbatim. A
        # default-quality re-encode silently wrecks the face embedding (owner-vs-owner
        # distance jumps from 0.0 to 0.62), so only re-encode non-JPEG inputs.
        if (img.format or "").upper() == "JPEG":
            with open(tmp_path, "wb") as f:
                f.write(raw)
        else:
            img.convert("RGB").save(tmp_path, format="JPEG", quality=95)

        reason, distance, threshold = match_face(tmp_path)

        if reason in ("match", "no_match"):
            return jsonify({"verified": reason == "match", "reason": reason,
                            "distance": distance, "threshold": threshold})
        if reason == "no_face":
            return jsonify({"verified": False, "reason": "no_face"})
        return jsonify({"verified": False, "reason": "error",
                        "error": "verification failed"})
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)


# ---- Live viewer (diagnostic) ---------------------------------------------------
# Proxies the ESP32's MJPEG stream and overlays a face box every frame (fast Haar).
# Recognition (SFace, ~0.28s) runs in a background thread so it never stalls the video;
# the stream loop just draws the latest cached verdict on each frame.

_VERDICT_STYLE = {
    "match":    ((0, 200, 0),    "MATCH (owner)"),
    "no_match": ((0, 0, 255),    "NO MATCH"),
    "no_face":  ((0, 200, 255),  "no face"),
    "error":    ((0, 200, 255),  "verify error"),
    "warming":  ((200, 200, 200), "warming up..."),
}

# Shared between the stream loop (producer) and the recognizer thread (consumer).
_view_lock = threading.Lock()
_view_latest_frame = None       # most recent BGR frame seen by the viewer
_view_frame_ts = 0.0            # when it was seen (recognizer skips stale frames)
_view_verdict = ("warming", None, None)
_recognizer_started = False


def _detect_faces(bgr):
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    return _FACE_CASCADE.detectMultiScale(gray, scaleFactor=1.2,
                                           minNeighbors=5, minSize=(60, 60))


def _draw(bgr, faces, reason, distance, threshold):
    color, label = _VERDICT_STYLE.get(reason, ((200, 200, 200), reason))
    for (x, y, w, h) in faces:
        cv2.rectangle(bgr, (x, y), (x + w, y + h), color, 2)
    if distance is not None:
        label += f"  d={distance:.2f}/{threshold:.2f}"
    cv2.putText(bgr, label, (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.8, color, 2)
    return bgr


def _recognizer_loop():
    """Continuously recognize the latest viewed frame, but only while someone is
    watching (a fresh frame arrived recently). Updates _view_verdict; never blocks the
    stream."""
    global _view_verdict
    while True:
        time.sleep(0.15)
        with _view_lock:
            fresh = _view_latest_frame is not None and (time.time() - _view_frame_ts) < 2.0
            frame = _view_latest_frame.copy() if fresh else None
        if frame is None:
            continue
        _view_verdict = match_face(frame)
        time.sleep(0.4)  # cap recognition to a few per second


def _ensure_recognizer():
    global _recognizer_started
    if not _recognizer_started:
        _recognizer_started = True
        threading.Thread(target=_recognizer_loop, daemon=True).start()


def _mjpeg_frames(url):
    """Yield raw JPEG frames from an MJPEG HTTP stream by scanning SOI/EOI markers."""
    with urllib.request.urlopen(url, timeout=10) as resp:
        buf = b""
        while True:
            chunk = resp.read(8192)
            if not chunk:
                break
            buf += chunk
            while True:
                start = buf.find(b"\xff\xd8")
                end = buf.find(b"\xff\xd9", start + 2)
                if start != -1 and end != -1:
                    yield buf[start:end + 2]
                    buf = buf[end + 2:]
                else:
                    break


def _annotated_stream(src):
    global _view_latest_frame, _view_frame_ts
    _ensure_recognizer()
    try:
        for jpg in _mjpeg_frames(src):
            frame = cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_COLOR)
            if frame is None:
                continue
            with _view_lock:
                _view_latest_frame = frame
                _view_frame_ts = time.time()
            reason, distance, threshold = _view_verdict   # atomic tuple read
            _draw(frame, _detect_faces(frame), reason, distance, threshold)
            ok, out = cv2.imencode(".jpg", frame)
            if not ok:
                continue
            yield (b"--frame\r\nContent-Type: image/jpeg\r\n\r\n"
                   + out.tobytes() + b"\r\n")
    except Exception as e:
        blank = np.zeros((240, 640, 3), np.uint8)
        cv2.putText(blank, f"stream error: {str(e)[:48]}", (10, 120),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 255), 2)
        ok, out = cv2.imencode(".jpg", blank)
        if ok:
            yield (b"--frame\r\nContent-Type: image/jpeg\r\n\r\n"
                   + out.tobytes() + b"\r\n")


@app.route('/annotated_stream')
def annotated_stream():
    src = request.args.get("src", ESP_STREAM_URL)
    return Response(_annotated_stream(src),
                    mimetype="multipart/x-mixed-replace; boundary=frame")


@app.route('/view')
def view():
    src = request.args.get("src", ESP_STREAM_URL)
    return f"""<!doctype html><html><head><title>Door camera</title>
<style>body{{font-family:sans-serif;background:#111;color:#eee;text-align:center}}
img{{max-width:95vw;border:2px solid #333;margin-top:10px}}
input{{width:360px}}</style></head><body>
<h2>Smart Door &mdash; live verification</h2>
<form method="get" action="/view"><input name="src" value="{src}"><button>load</button></form>
<p>green box = owner matched &nbsp; red = not the owner &nbsp; yellow = no face</p>
<img src="/annotated_stream?src={src}">
</body></html>"""


# Warm up the model and cache the owner embedding at import time so the first /verify
# is fast (and so failures surface at startup, not on the first door check).
init_owner()


if __name__ == '__main__':
    # threaded=True so serving the viewer does not block the door's /verify.
    app.run(host='0.0.0.0', port=8080, threaded=True)


# Run with the conda env that has the dependencies (NOT the empty ./venv):
#   conda activate smartdoor
#   cd /Volumes/files/smart-door-system/server
#   python server.py
# Optional: MATCH_THRESHOLD=0.35 python server.py    # tighten the match
# Live viewer: open http://127.0.0.1:8080/view  (smooth proxied ESP32 stream + boxes).
# Set ESP_STREAM_URL or use ?src= if the ESP32's IP differs from the default.
