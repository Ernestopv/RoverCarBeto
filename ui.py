#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import sys
import os
import signal
import logging
import subprocess
import threading
import json
import urllib.error
import urllib.request
from pathlib import Path
from http import server

# VIDEO AND WEB SERVER CONFIGURATION
# Values optimized for low latency. They can be overridden from Docker/.env.
WIDTH = int(os.getenv("CAMERA_WIDTH", "640"))
HEIGHT = int(os.getenv("CAMERA_HEIGHT", "480"))
FPS = int(os.getenv("CAMERA_FPS", "30"))
JPEG_QUALITY = int(os.getenv("JPEG_QUALITY", "55"))
PORT = int(os.getenv("UI_PORT", "8001"))

# CAMERA SOURCE
#   imx500     -> Raspberry Pi AI Camera
#   simulator  -> FFmpeg generated test pattern for DEV
CAMERA_SOURCE = os.getenv("CAMERA_SOURCE", "imx500").strip().lower()

if CAMERA_SOURCE not in ("imx500", "simulator"):
    print(
        f"\nERROR: Invalid CAMERA_SOURCE: {CAMERA_SOURCE}\n"
        "Available values: imx500, simulator\n"
    )
    sys.exit(1)

# CONNECTION TO API.PY (Flask backend on port 5000)
# This URL is used server-side by ui.py, so keep it as an absolute localhost URL.
# The browser calls /api/rover/* on the UI, and this process proxies those calls to api.py.
API_BASE_URL = (
    os.getenv("API_BASE_URL")
    or os.getenv("ROVER_API_URL")
    or "http://127.0.0.1:5000/api/rover"
)

# UI FILES
BASE_DIR = Path(__file__).resolve().parent
INDEX_FILE = BASE_DIR / "index.html"
STYLE_FILE = BASE_DIR / "style.css"

# IMX500 MODELS
MODELS = {
    "objects": {
        "name": "MobileNet SSD",
        "model": "/usr/share/imx500-models/imx500_network_ssd_mobilenetv2_fpnlite_320x320_pp.rpk",
        "postprocess": "/usr/share/rpi-camera-assets/imx500_mobilenet_ssd.json",
    },
    "pose": {
        "name": "PoseNet",
        "model": "/usr/share/imx500-models/imx500_network_posenet.rpk",
        "postprocess": "/usr/share/rpi-camera-assets/imx500_posenet.json",
    },
}


MODE = os.getenv("MODE", "objects").strip().lower()

if MODE not in ("objects", "pose"):
    print(f"ERROR: Invalid MODE: {MODE}")
    print("Available modes: objects, pose")
    sys.exit(1)

CONFIG = MODELS[MODE]

if CAMERA_SOURCE == "imx500":
    if not os.path.isfile(CONFIG["model"]):
        print(f"\nERROR: Model file does not exist:\n{CONFIG['model']}\n")
        sys.exit(1)

    if not os.path.isfile(CONFIG["postprocess"]):
        print(f"\nERROR: Post-processing file does not exist:\n{CONFIG['postprocess']}\n")
        sys.exit(1)

description = (
    "MobileNet SSD - objects detection"
    if MODE == "objects"
    else "PoseNet - Human Pose Estimation"
)

if CAMERA_SOURCE == "simulator":
    description += " [DEV simulator]"


# Keyboard support injected into index.html at runtime.
# Arrow keys and WASD reuse the movement buttons already present in the UI.
KEYBOARD_SCRIPT = r"""
<script>
(() => {
    const KEY_TO_DIRECTION = {
        ArrowUp: "up",
        ArrowDown: "down",
        ArrowLeft: "left",
        ArrowRight: "right",
        w: "up",
        W: "up",
        s: "down",
        S: "down",
        a: "left",
        A: "left",
        d: "right",
        D: "right",
    };

    const DIRECTION_HINTS = {
        up: ["up", "forward", "forwards", "ahead", "adelante", "arriba", "north", "\u25b2", "\u2191"],
        down: ["down", "back", "backward", "backwards", "reverse", "atras", "atr\u00e1s", "abajo", "south", "\u25bc", "\u2193"],
        left: ["left", "izquierda", "west", "\u25c0", "\u2190"],
        right: ["right", "derecha", "east", "\u25b6", "\u2192"],
        stop: ["stop", "halt", "parar", "detener", "\u25a0", "\u23f9"],
    };

    const pressed = new Set();

    function isTypingTarget(element) {
        if (!element) return false;
        const tag = (element.tagName || "").toLowerCase();
        return tag === "input" || tag === "textarea" || tag === "select" || element.isContentEditable;
    }

    function controlText(element) {
        return [
            element.id,
            element.getAttribute("name"),
            element.getAttribute("data-action"),
            element.getAttribute("data-command"),
            element.getAttribute("data-direction"),
            element.getAttribute("aria-label"),
            element.getAttribute("title"),
            element.textContent,
        ]
            .filter(Boolean)
            .join(" " )
            .trim()
            .toLowerCase();
    }

    function findControl(direction) {
        const hints = DIRECTION_HINTS[direction];
        const controls = Array.from(document.querySelectorAll(
            "button, [role='button'], [data-action], [data-command], [data-direction], a"
        ));

        let best = null;
        let bestScore = 0;

        for (const control of controls) {
            if (control.disabled || control.getAttribute("aria-disabled") === "true") continue;

            const text = controlText(control);
            let score = 0;

            for (const hint of hints) {
                const h = hint.toLowerCase();
                if (text === h) score = Math.max(score, 100);
                else if (text.split(/[^a-z\u00e1\u00e9\u00ed\u00f3\u00fa\u00fc\u00f10-9]+/i).includes(h)) score = Math.max(score, 80);
                else if (text.includes(h)) score = Math.max(score, 40);
            }

            if (score > bestScore) {
                best = control;
                bestScore = score;
            }
        }

        return best;
    }

    function dispatchPress(control) {
        if (!control) return false;

        try {
            control.dispatchEvent(new PointerEvent("pointerdown", { bubbles: true, pointerType: "mouse", isPrimary: true }));
        } catch (_) {}
        control.dispatchEvent(new MouseEvent("mousedown", { bubbles: true, button: 0 }));
        control.click();
        return true;
    }

    function dispatchRelease(control) {
        if (!control) return;

        try {
            control.dispatchEvent(new PointerEvent("pointerup", { bubbles: true, pointerType: "mouse", isPrimary: true }));
        } catch (_) {}
        control.dispatchEvent(new MouseEvent("mouseup", { bubbles: true, button: 0 }));
    }

    function stopRover() {
        const stopControl = findControl("stop");
        if (stopControl) stopControl.click();
    }

    document.addEventListener("keydown", (event) => {
        const direction = KEY_TO_DIRECTION[event.key];
        if (!direction || isTypingTarget(event.target)) return;

        event.preventDefault();
        if (pressed.has(event.key) || event.repeat) return;

        const control = findControl(direction);
        if (!control) {
            console.warn(`[keyboard] No UI control found for: ${direction}`);
            return;
        }

        pressed.add(event.key);
        dispatchPress(control);
    });

    document.addEventListener("keyup", (event) => {
        const direction = KEY_TO_DIRECTION[event.key];
        if (!direction || isTypingTarget(event.target)) return;

        event.preventDefault();
        pressed.delete(event.key);

        dispatchRelease(findControl(direction));
        stopRover();
    });

    window.addEventListener("blur", () => {
        if (pressed.size > 0) {
            pressed.clear();
            stopRover();
        }
    });
})();
</script>
"""


def load_index_page():
    """Load index.html and inject runtime camera information."""
    try:
        page = INDEX_FILE.read_text(encoding="utf-8")
    except OSError as exc:
        logging.error("Could not read %s: %s", INDEX_FILE, exc)
        return (
            "<!DOCTYPE html><html><body>"
            "<h1>UI error</h1>"
            "<p>Could not load index.html.</p>"
            "</body></html>"
        )

    page = (
        page
        .replace("__DESCRIPTION__", description)
        .replace("__WIDTH__", str(WIDTH))
        .replace("__HEIGHT__", str(HEIGHT))
        .replace("__FPS__", str(FPS))
    )

    # Add keyboard controls without requiring changes to index.html.
    if "</body>" in page.lower():
        lower_page = page.lower()
        body_pos = lower_page.rfind("</body>")
        page = page[:body_pos] + KEYBOARD_SCRIPT + page[body_pos:]
    else:
        page += KEYBOARD_SCRIPT

    return page


def load_stylesheet():
    """Load the external CSS stylesheet."""
    try:
        return STYLE_FILE.read_bytes()
    except OSError as exc:
        logging.error("Could not read %s: %s", STYLE_FILE, exc)
        return b""



class StreamOutput:
    """Keeps only the most recent JPEG frame."""

    def __init__(self):
        self.condition = threading.Condition()
        self.frame = None
        self.sequence = 0
        self.running = True

    def set_frame(self, frame):
        with self.condition:
            self.frame = frame
            self.sequence += 1
            self.condition.notify_all()

    def get_frame(self, last_sequence):
        with self.condition:
            while self.running and self.sequence == last_sequence:
                self.condition.wait()

            if not self.running:
                return None, last_sequence

            return self.frame, self.sequence

    def stop(self):
        with self.condition:
            self.running = False
            self.condition.notify_all()


output = StreamOutput()


def call_api(endpoint, method="GET", payload=None):
    url = f"{API_BASE_URL}{endpoint}"

    data = None
    headers = {}

    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(
        url,
        data=data,
        headers=headers,
        method=method,
    )

    try:
        with urllib.request.urlopen(req, timeout=2) as res:
            raw = res.read().decode("utf-8")
            return res.status, json.loads(raw)

    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")

        try:
            return exc.code, json.loads(body)
        except Exception:
            return exc.code, {
                "ok": False,
                "error": body,
            }

    except Exception as exc:
        return 503, {
            "ok": False,
            "error": str(exc),
        }


class StreamingHandler(server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        return

    def send_json(self, data, status=200):
        content = json.dumps(data).encode("utf-8")

        self.send_response(status)
        self.send_header(
            "Content-Type",
            "application/json; charset=utf-8",
        )
        self.send_header(
            "Content-Length",
            str(len(content)),
        )
        self.send_header(
            "Cache-Control",
            "no-cache",
        )
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            content = load_index_page().encode("utf-8")

            self.send_response(200)
            self.send_header(
                "Content-Type",
                "text/html; charset=utf-8",
            )
            self.send_header(
                "Content-Length",
                str(len(content)),
            )
            self.send_header(
                "Cache-Control",
                "no-cache",
            )
            self.end_headers()
            self.wfile.write(content)
            return

        if self.path == "/style.css":
            content = load_stylesheet()

            if not content:
                self.send_error(404, "style.css not found")
                return

            self.send_response(200)
            self.send_header(
                "Content-Type",
                "text/css; charset=utf-8",
            )
            self.send_header(
                "Content-Length",
                str(len(content)),
            )
            self.send_header(
                "Cache-Control",
                "no-cache",
            )
            self.end_headers()
            self.wfile.write(content)
            return

        if self.path == "/api/rover/status":
            status_code, response = call_api(
                "/status",
                "GET",
            )
            self.send_json(response, status_code)
            return

        if self.path == "/api/rover/battery":
            status_code, response = call_api(
                "/battery",
                "GET",
            )
            self.send_json(response, status_code)
            return

        if self.path == "/stream.mjpg":
            self.send_response(200)
            self.send_header("Age", "0")
            self.send_header(
                "Cache-Control",
                "no-cache, private",
            )
            self.send_header(
                "Pragma",
                "no-cache",
            )
            self.send_header(
                "Content-Type",
                "multipart/x-mixed-replace; boundary=FRAME",
            )
            self.end_headers()

            try:
                last_sequence = -1

                while True:
                    frame, last_sequence = output.get_frame(
                        last_sequence
                    )

                    if frame is None:
                        break

                    self.wfile.write(b"--FRAME\r\n")
                    self.wfile.write(
                        b"Content-Type: image/jpeg\r\n"
                    )
                    self.wfile.write(
                        (
                            f"Content-Length: {len(frame)}\r\n\r\n"
                        ).encode("ascii")
                    )
                    self.wfile.write(frame)
                    self.wfile.write(b"\r\n")
                    self.wfile.flush()

            except (
                BrokenPipeError,
                ConnectionResetError,
                ConnectionAbortedError,
            ):
                logging.info("Client disconnected")

            except Exception as exc:
                logging.warning(
                    "Stream error: %s",
                    exc,
                )

            return

        self.send_error(404)

    def do_POST(self):
        if self.path.startswith("/api/rover/"):
            endpoint = self.path[len("/api/rover"):]

            try:
                length = int(
                    self.headers.get(
                        "Content-Length",
                        "0",
                    )
                )

                body = (
                    self.rfile.read(length)
                    if length > 0
                    else None
                )

                payload = (
                    json.loads(
                        body.decode("utf-8")
                    )
                    if body
                    else None
                )

            except (
                ValueError,
                UnicodeDecodeError,
                json.JSONDecodeError,
            ) as exc:
                self.send_json(
                    {
                        "ok": False,
                        "error": f"Invalid request body: {exc}",
                    },
                    400,
                )
                return

            status_code, response = call_api(
                endpoint,
                "POST",
                payload,
            )
            self.send_json(
                response,
                status_code,
            )
            return

        self.send_error(404)


class StreamingServer(server.ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


camera_process = None


def start_camera():
    global camera_process

    if CAMERA_SOURCE == "simulator":
        command = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel", "warning",
            "-re",
            "-f", "lavfi",
            "-i", f"testsrc2=size={WIDTH}x{HEIGHT}:rate={FPS}",
            "-an",
            "-c:v", "mjpeg",
            "-q:v", "5",
            "-f", "mjpeg",
            "-"
        ]

        logging.info(
            "Starting simulated camera with FFmpeg: %sx%s @ %s FPS",
            WIDTH,
            HEIGHT,
            FPS,
        )

    else:
        command = [
            "rpicam-vid",
            "--timeout", "0",
            "--nopreview",
            "--width", str(WIDTH),
            "--height", str(HEIGHT),
            "--framerate", str(FPS),
            "--codec", "mjpeg",
            "--quality", str(JPEG_QUALITY),
            "--flush",
            "--denoise", "cdn_off",
            "--post-process-file", CONFIG["postprocess"],
            "--vflip",
            "--output", "-"
        ]

        logging.info(
            "Starting IMX500 camera: model=%s, postprocess=%s",
            CONFIG["model"],
            CONFIG["postprocess"],
        )

    try:
        camera_process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=None,
            bufsize=0,
        )
    except FileNotFoundError as exc:
        if CAMERA_SOURCE == "simulator":
            raise RuntimeError(
                "FFmpeg is not installed inside the DEV container. "
                "Install package 'ffmpeg' in Dockerfile.dev."
            ) from exc
        raise

    return camera_process


def camera_reader():
    buffer = b""

    while True:
        if (
            camera_process is None
            or camera_process.poll() is not None
        ):
            break

        try:
            data = camera_process.stdout.read(4096)
        except Exception:
            break

        if not data:
            break

        buffer += data

        while True:
            start = buffer.find(b"\xff\xd8")

            if start < 0:
                if len(buffer) > 1024 * 1024:
                    buffer = buffer[-65536:]
                break

            end = buffer.find(
                b"\xff\xd9",
                start + 2,
            )

            if end < 0:
                if start > 0:
                    buffer = buffer[start:]
                break

            end += 2
            frame = buffer[start:end]
            buffer = buffer[end:]

            output.set_frame(frame)

    output.stop()


def stop_camera():
    global camera_process

    call_api(
        "/stop",
        "POST",
    )

    output.stop()

    if (
        camera_process
        and camera_process.poll() is None
    ):
        try:
            camera_process.send_signal(
                signal.SIGINT
            )
            camera_process.wait(
                timeout=5
            )

        except Exception:
            try:
                camera_process.kill()
            except Exception:
                pass

    camera_process = None


def main():
    start_camera()

    threading.Thread(
        target=camera_reader,
        daemon=True,
    ).start()

    http_server = StreamingServer(
        ("", PORT),
        StreamingHandler,
    )

    print(
        f"HTTP server running at "
        f"http://0.0.0.0:{PORT}"
    )
    print(
        f"Video: {WIDTH}x{HEIGHT} @ {FPS} FPS, "
        f"JPEG quality={JPEG_QUALITY}, "
        f"mode={MODE}, "
        f"camera={CAMERA_SOURCE}"
    )
    print(f"API backend: {API_BASE_URL}")
    print(
        "Checking connection to Flask API "
        "(api.py)..."
    )

    code, res = call_api(
        "/status",
        "GET",
    )

    if code == 200:
        print(
            "Connection to api.py successful:"
        )
        print(
            json.dumps(
                res,
                indent=2,
            )
        )
    else:
        print(
            "WARNING: Could not connect "
            f"to api.py at {API_BASE_URL}. "
            "Make sure it is running first."
        )

    try:
        http_server.serve_forever()

    except KeyboardInterrupt:
        print("\nStopping...")

    finally:
        stop_camera()
        http_server.server_close()


if __name__ == "__main__":
    main()
