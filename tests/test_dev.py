import json
import math
import os
import time
import urllib.request

# Single public entry point (nginx).
#
# Local DEV:
#   http://127.0.0.1:8000
#
# Azure DEV/QA can reuse the same tests:
#   TEST_BASE_URL=https://<container-app-fqdn> pytest -q tests/test_dev.py
BASE_URL = os.getenv(
    "TEST_BASE_URL",
    "http://127.0.0.1:8000",
).rstrip("/")

API = f"{BASE_URL}/api/rover"
SIM = f"{BASE_URL}/simulator"
UI = BASE_URL


def req(url, method="GET", payload=None, timeout=5):
    data = None
    headers = {}

    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    request = urllib.request.Request(
        url,
        data=data,
        headers=headers,
        method=method,
    )

    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read()
        content_type = response.headers.get("Content-Type", "")

        if "json" in content_type:
            return response.status, json.loads(body.decode())

        return response.status, body


def state():
    return req(f"{SIM}/state")[1]


def close(a, b, tol=1e-6):
    assert math.isclose(
        float(a),
        float(b),
        abs_tol=tol,
    ), (a, b)


def reset():
    req(f"{SIM}/reset", "POST")


def test_services():
    # Everything is intentionally tested through nginx :8000.
    assert req(f"{BASE_URL}/health")[0] == 200
    assert req(f"{UI}/")[0] == 200
    assert req(f"{SIM}/health")[0] == 200
    assert req(f"{API}/status")[0] == 200


def test_forward_command_and_motion():
    reset()
    before = state()

    req(
        f"{API}/forward",
        "POST",
        {"speed": 0.30},
    )

    command_state = state()
    close(command_state["left"], 0.30)
    close(command_state["right"], 0.30)

    time.sleep(0.35)

    after = state()
    assert after["x"] > before["x"], (
        before["x"],
        after["x"],
    )

    req(f"{API}/stop", "POST")


def test_left_turn():
    reset()

    req(
        f"{API}/left",
        "POST",
        {"speed": 0.20},
    )

    command_state = state()
    close(command_state["left"], 0.20)
    close(command_state["right"], -0.20)

    time.sleep(0.25)

    after = state()
    assert after["heading_degrees"] > 0, after["heading_degrees"]

    req(f"{API}/stop", "POST")


def test_right_turn():
    reset()

    req(
        f"{API}/right",
        "POST",
        {"speed": 0.20},
    )

    command_state = state()
    close(command_state["left"], -0.20)
    close(command_state["right"], 0.20)

    time.sleep(0.25)

    after = state()
    assert after["heading_degrees"] < 0, after["heading_degrees"]

    req(f"{API}/stop", "POST")


def test_stop():
    req(f"{API}/stop", "POST")

    current = state()
    close(current["left"], 0)
    close(current["right"], 0)


def test_battery_scenario_and_endpoint():
    req(
        f"{SIM}/scenario",
        "POST",
        {"voltage": 10.4},
    )

    _, body = req(f"{API}/battery")

    assert body["ok"] is True
    close(body["voltage"], 10.4, 0.02)

    req(
        f"{SIM}/scenario",
        "POST",
        {"voltage": 12.2},
    )


def test_simulated_latency():
    req(
        f"{SIM}/scenario",
        "POST",
        {"latency_ms": 250},
    )

    start = time.monotonic()
    req(f"{API}/status")
    elapsed = time.monotonic() - start

    assert elapsed >= 0.20, elapsed

    req(
        f"{SIM}/scenario",
        "POST",
        {"latency_ms": 0},
    )


def test_stream():
    response = urllib.request.urlopen(
        f"{UI}/stream.mjpg",
        timeout=8,
    )

    assert (
        "multipart/x-mixed-replace"
        in response.headers.get("Content-Type", "")
    )

    chunk = response.read(4096)
    assert b"--FRAME" in chunk or b"\xff\xd8" in chunk

    response.close()
