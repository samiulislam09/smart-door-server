#!/usr/bin/env python3
"""Stand-in for the ESP32-CAM door client.

Posts images to the Flask /verify endpoint exactly as the firmware does
(raw bytes, Content-Type: application/octet-stream) and prints the door action
each response would trigger, using the same verified/reason logic as the firmware.

Usage:
    python simulate_esp32.py                       # owner + blank frame
    python simulate_esp32.py --stranger face.jpg   # also test a non-owner face
    python simulate_esp32.py --url http://192.168.1.103:8080/verify
"""
import io
import os
import json
import argparse
import urllib.request

DEFAULT_URL = os.environ.get("VERIFY_URL", "http://127.0.0.1:8080/verify")


def post_image(url, data, led_state="off"):
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/octet-stream",
                 "X-LED-State": led_state},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode())


def door_action(resp):
    """Mirror the firmware decision: grant -> UNLOCK, no_face -> IDLE, else BUZZER."""
    if resp.get("verified") is True:
        return "UNLOCK"
    if resp.get("reason") == "no_face":
        return "IDLE (silent)"
    return "BUZZER"


def blank_image_bytes():
    """A flat gray 640x480 JPEG: a valid image with no detectable face."""
    from PIL import Image
    img = Image.new("RGB", (640, 480), (127, 127, 127))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


def dark_image_bytes():
    """A near-black 640x480 JPEG: a valid image too dark to detect a face."""
    from PIL import Image
    img = Image.new("RGB", (640, 480), (5, 5, 5))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


def run_case(name, data, url, led_state="off"):
    try:
        resp = post_image(url, data, led_state)
    except Exception as e:
        print(f"[{name}] request FAILED: {e}")
        return
    led = resp.get("led", "-")
    print(f"[{name}] response={json.dumps(resp)}  ->  door action: {door_action(resp)}"
          f"  |  LED: {led}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--owner", default="owner.jpg")
    ap.add_argument("--user", help="path to a second enrolled person's face (expect UNLOCK)")
    ap.add_argument("--stranger", help="path to a non-enrolled face image (expect BUZZER)")
    args = ap.parse_args()

    print(f"Target: {args.url}\n")

    with open(args.owner, "rb") as f:
        run_case("owner    (expect UNLOCK)", f.read(), args.url)

    if args.user:
        with open(args.user, "rb") as f:
            run_case("2nd user (expect UNLOCK)", f.read(), args.url)
    else:
        print("[2nd user (expect UNLOCK)] skipped - pass --user <path> to test a second enrolled person")

    if args.stranger:
        with open(args.stranger, "rb") as f:
            run_case("stranger (expect BUZZER)", f.read(), args.url)
    else:
        print("[stranger (expect BUZZER)] skipped - pass --stranger <path> to test a non-enrolled face")

    run_case("dark frame (LED off, expect low_light -> LED on)",
             dark_image_bytes(), args.url, led_state="off")

    run_case("no-face  (expect IDLE)  ", blank_image_bytes(), args.url)


if __name__ == "__main__":
    main()
