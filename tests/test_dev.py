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
# Azure DEV/QA:
#   TEST_BASE_URL=https://<container-app-fqdn> pytest -q tests/test_dev.py
BASE_URL = os.getenv(
    "TEST_BASE_URL",
    "http://127.0.0.1:8000",
).rstrip("/")

API = f"{BASE_URL}/api/rover"
SIM = f"{BASE_URL}/simulator"
UI = BASE_URL

# Public UI/API speed is normalized as 0.0 .. 1.0.
# WAVE ROVER T=1 expects motor values in -0.5 .. +0.5.
ROVER_COMMAND_MAX = float(os.getenv("TEST_ROVER_COMMAND_MAX", "0.5"))


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


def expected_motor(speed, direction=1):
    """Convert normalized API speed to raw WAVE ROVER T=1 motor value."""
    return float(direction) * float(speed) * ROVER_COMMAND_MAX


def reset():
    req(f"{SIM}/reset", "POST")


def stop():
    req(f"{API}/stop", "POST")


def wait_for_state(predicate, timeout=2.0, interval=0.05):
    """
    Poll simulator state until predicate(state) is True.

    This is more reliable than fixed time.sleep() calls,
    especially on GitHub Actions / slower CI runners.
    """
    deadline = time.monotonic() + timeout
    last_state = None

    while time.monotonic() < deadline:
        last_state = state()

        if predicate(last_state):
            return last_state

        time.sleep(interval)

    return last_state


def test_services():
    assert req(f"{BASE_URL}/health")[0] == 200
    assert req(f"{UI}/")[0] == 200
    assert req(f"{SIM}/health")[0] == 200
    assert req(f"{API}/status")[0] == 200


def test_forward_command_and_motion():
    reset()
    before = state()

    try:
        req(
            f"{API}/forward",
            "POST",
            {"speed": 0.30},
        )

        command_state = state()

        close(command_state["left"], expected_motor(0.30))
        close(command_state["right"], expected_motor(0.30))

        after = wait_for_state(
            lambda s: float(s["x"]) > float(before["x"]),
            timeout=2.0,
        )

        assert after is not None
        assert after["x"] > before["x"], {
            "before": before["x"],
            "after": after["x"],
        }

    finally:
        stop()


def test_left_turn():
    reset()

    try:
        req(
            f"{API}/left",
            "POST",
            {"speed": 0.20},
        )

        command_state = state()

        close(command_state["left"], expected_motor(0.20, -1))
        close(command_state["right"], expected_motor(0.20, 1))

        after = wait_for_state(
            lambda s: float(s["heading_degrees"]) > 0,
            timeout=2.0,
        )

        assert after is not None
        assert after["heading_degrees"] > 0, after

    finally:
        stop()


def test_right_turn():
    reset()

    try:
        req(
            f"{API}/right",
            "POST",
            {"speed": 0.20},
        )

        command_state = state()

        close(command_state["left"], expected_motor(0.20, 1))
        close(command_state["right"], expected_motor(0.20, -1))

        after = wait_for_state(
            lambda s: float(s["heading_degrees"]) < 0,
            timeout=2.0,
        )

        assert after is not None
        assert after["heading_degrees"] < 0, after

    finally:
        stop()


def test_speed_scaling_75_percent():
    """
    75% in the public API must become 0.375 in WAVE ROVER T=1:
        0.75 * 0.5 = 0.375
    """
    reset()

    try:
        req(
            f"{API}/forward",
            "POST",
            {"speed": 0.75},
        )

        command_state = state()

        close(command_state["left"], expected_motor(0.75))
        close(command_state["right"], expected_motor(0.75))

    finally:
        stop()


def test_stop():
    stop()

    current = wait_for_state(
        lambda s: (
            math.isclose(float(s["left"]), 0.0, abs_tol=1e-6)
            and math.isclose(float(s["right"]), 0.0, abs_tol=1e-6)
        ),
        timeout=1.0,
    )

    assert current is not None
    close(current["left"], 0)
    close(current["right"], 0)


def test_battery_scenario_and_endpoint():
    try:
        req(
            f"{SIM}/scenario",
            "POST",
            {"voltage": 10.4},
        )

        _, body = req(f"{API}/battery")

        assert body["ok"] is True
        close(body["voltage"], 10.4, 0.02)

    finally:
        req(
            f"{SIM}/scenario",
            "POST",
            {"voltage": 12.2},
        )


def test_simulated_latency():
    try:
        req(
            f"{SIM}/scenario",
            "POST",
            {"latency_ms": 250},
        )

        start = time.monotonic()
        req(f"{API}/status")
        elapsed = time.monotonic() - start

        assert elapsed >= 0.20, elapsed

    finally:
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

    try:
        content_type = response.headers.get("Content-Type", "")

        assert "multipart/x-mixed-replace" in content_type

        chunk = response.read(4096)

        assert (
            b"--FRAME" in chunk
            or b"\xff\xd8" in chunk
        )

    finally:
        response.close()