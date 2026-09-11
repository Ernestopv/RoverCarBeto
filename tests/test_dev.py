import json
import math
import time
import urllib.request
import urllib.error

API = "http://127.0.0.1:5000"
SIM = "http://127.0.0.1:8081"
UI = "http://127.0.0.1:8000"


def req(url, method="GET", payload=None, timeout=5):
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    r = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(r, timeout=timeout) as res:
        body = res.read()
        ct = res.headers.get("Content-Type", "")
        if "json" in ct:
            return res.status, json.loads(body.decode())
        return res.status, body


def state():
    return req(f"{SIM}/state")[1]


def close(a, b, tol=1e-6):
    assert math.isclose(float(a), float(b), abs_tol=tol), (a, b)


def reset():
    req(f"{SIM}/reset", "POST")


def test_services():
    assert req(f"{SIM}/health")[0] == 200
    assert req(f"{API}/health")[0] == 200
    assert req(f"{UI}/")[0] == 200


def test_forward_command_and_motion():
    reset()
    before = state()
    req(f"{API}/api/rover/forward", "POST", {"speed": 0.30})
    command_state = state()
    close(command_state["left"], 0.30)
    close(command_state["right"], 0.30)

    time.sleep(0.35)
    after = state()
    assert after["x"] > before["x"], (before["x"], after["x"])

    req(f"{API}/api/rover/stop", "POST")


def test_left_turn():
    reset()
    req(f"{API}/api/rover/left", "POST", {"speed": 0.20})
    command_state = state()
    close(command_state["left"], 0.20)
    close(command_state["right"], -0.20)

    time.sleep(0.25)
    after = state()
    assert after["heading_degrees"] > 0, after["heading_degrees"]

    req(f"{API}/api/rover/stop", "POST")


def test_right_turn():
    reset()
    req(f"{API}/api/rover/right", "POST", {"speed": 0.20})
    command_state = state()
    close(command_state["left"], -0.20)
    close(command_state["right"], 0.20)

    time.sleep(0.25)
    after = state()
    assert after["heading_degrees"] < 0, after["heading_degrees"]

    req(f"{API}/api/rover/stop", "POST")


def test_stop():
    req(f"{API}/api/rover/stop", "POST")
    s = state()
    close(s["left"], 0)
    close(s["right"], 0)


def test_battery_scenario_and_endpoint():
    req(f"{SIM}/scenario", "POST", {"voltage": 10.4})
    _, body = req(f"{API}/api/rover/battery")
    assert body["ok"] is True
    close(body["voltage"], 10.4, 0.02)

    req(f"{SIM}/scenario", "POST", {"voltage": 12.2})


def test_simulated_latency():
    req(f"{SIM}/scenario", "POST", {"latency_ms": 250})
    start = time.monotonic()
    req(f"{API}/api/rover/status")
    elapsed = time.monotonic() - start
    assert elapsed >= 0.20, elapsed
    req(f"{SIM}/scenario", "POST", {"latency_ms": 0})


def test_stream():
    r = urllib.request.urlopen(f"{UI}/stream.mjpg", timeout=8)
    assert "multipart/x-mixed-replace" in r.headers.get("Content-Type", "")
    chunk = r.read(4096)
    assert b"--FRAME" in chunk or b"\xff\xd8" in chunk
    r.close()
