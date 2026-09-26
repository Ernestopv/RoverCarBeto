import json
import math
import os
import time
import urllib.parse
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
ROVER_COMMAND_MAX = float(
    os.getenv(
        "TEST_ROVER_COMMAND_MAX",
        "0.5",
    )
)

# Battery configuration used by api.py and rover_simulator.py.
BATTERY_CELLS = int(
    os.getenv(
        "TEST_BATTERY_CELLS",
        "3",
    )
)

BATTERY_AH = float(
    os.getenv(
        "TEST_BATTERY_AH",
        "3.0",
    )
)

BATTERY_CELL_NOMINAL_V = float(
    os.getenv(
        "TEST_BATTERY_CELL_NOMINAL_V",
        "3.7",
    )
)

BATTERY_TOTAL_WH = (
    BATTERY_CELLS
    * BATTERY_AH
    * BATTERY_CELL_NOMINAL_V
)


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

    with urllib.request.urlopen(
        request,
        timeout=timeout,
    ) as response:
        body = response.read()
        content_type = response.headers.get(
            "Content-Type",
            "",
        )

        if "json" in content_type:
            return (
                response.status,
                json.loads(body.decode()),
            )

        return response.status, body


def state():
    return req(f"{SIM}/state")[1]


def simulator_command(payload):
    """
    Send a raw WAVE ROVER-compatible command directly to the simulator.

    This bypasses api.py and is useful for checking the simulator contract
    itself, especially T=130 telemetry.
    """
    query = urllib.parse.urlencode({
        "json": json.dumps(
            payload,
            separators=(",", ":"),
        )
    })

    return req(
        f"{SIM}/js?{query}"
    )


def close(a, b, tol=1e-6):
    assert math.isclose(
        float(a),
        float(b),
        abs_tol=tol,
    ), (a, b)


def expected_motor(speed, direction=1):
    """Convert normalized API speed to raw WAVE ROVER T=1 motor value."""
    return (
        float(direction)
        * float(speed)
        * ROVER_COMMAND_MAX
    )


def reset():
    req(
        f"{SIM}/reset",
        "POST",
    )


def stop():
    req(
        f"{API}/stop",
        "POST",
    )


def set_battery_percent(percent):
    req(
        f"{SIM}/scenario",
        "POST",
        {
            "battery_percent": percent,
        },
    )


def wait_for_state(
    predicate,
    timeout=2.0,
    interval=0.05,
):
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


def wait_for_battery_samples(
    minimum_samples=5,
    timeout=3.0,
    interval=0.08,
):
    """
    Poll /api/rover/battery until the API has enough samples for
    estimate_quality == stable.

    api.py smooths battery telemetry over a rolling window, so tests should
    not assume the first reading is already stable.
    """
    deadline = time.monotonic() + timeout
    last_body = None

    while time.monotonic() < deadline:
        status, last_body = req(
            f"{API}/battery"
        )

        assert status == 200
        assert last_body["ok"] is True

        if (
            int(last_body.get("samples", 0))
            >= minimum_samples
        ):
            return last_body

        time.sleep(interval)

    return last_body


def test_services():
    assert req(
        f"{BASE_URL}/health"
    )[0] == 200

    assert req(
        f"{UI}/"
    )[0] == 200

    assert req(
        f"{SIM}/health"
    )[0] == 200

    assert req(
        f"{API}/status"
    )[0] == 200

    # The new battery endpoint should also be healthy.
    status, body = req(
        f"{API}/battery"
    )

    assert status == 200
    assert body["ok"] is True


def test_forward_command_and_motion():
    reset()
    before = state()

    try:
        req(
            f"{API}/forward",
            "POST",
            {
                "speed": 0.30,
            },
        )

        command_state = state()

        close(
            command_state["left"],
            expected_motor(0.30),
        )

        close(
            command_state["right"],
            expected_motor(0.30),
        )

        after = wait_for_state(
            lambda s: (
                float(s["x"])
                > float(before["x"])
            ),
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
            {
                "speed": 0.20,
            },
        )

        command_state = state()

        close(
            command_state["left"],
            expected_motor(
                0.20,
                -1,
            ),
        )

        close(
            command_state["right"],
            expected_motor(
                0.20,
                1,
            ),
        )

        after = wait_for_state(
            lambda s: (
                float(
                    s["heading_degrees"]
                ) > 0
            ),
            timeout=2.0,
        )

        assert after is not None
        assert (
            after["heading_degrees"] > 0
        ), after

    finally:
        stop()


def test_right_turn():
    reset()

    try:
        req(
            f"{API}/right",
            "POST",
            {
                "speed": 0.20,
            },
        )

        command_state = state()

        close(
            command_state["left"],
            expected_motor(
                0.20,
                1,
            ),
        )

        close(
            command_state["right"],
            expected_motor(
                0.20,
                -1,
            ),
        )

        after = wait_for_state(
            lambda s: (
                float(
                    s["heading_degrees"]
                ) < 0
            ),
            timeout=2.0,
        )

        assert after is not None
        assert (
            after["heading_degrees"] < 0
        ), after

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
            {
                "speed": 0.75,
            },
        )

        command_state = state()

        close(
            command_state["left"],
            expected_motor(0.75),
        )

        close(
            command_state["right"],
            expected_motor(0.75),
        )

    finally:
        stop()


def test_stop():
    stop()

    current = wait_for_state(
        lambda s: (
            math.isclose(
                float(s["left"]),
                0.0,
                abs_tol=1e-6,
            )
            and math.isclose(
                float(s["right"]),
                0.0,
                abs_tol=1e-6,
            )
        ),
        timeout=1.0,
    )

    assert current is not None

    close(
        current["left"],
        0,
    )

    close(
        current["right"],
        0,
    )


def test_simulator_t130_battery_telemetry():
    """
    The simulator must match the modified ESP32 firmware contract.

    Required battery fields:
      v   -> volts
      c   -> milliamps
      pwr -> milliwatts
      ov  -> INA219 overflow flag
    """
    stop()
    set_battery_percent(75)

    # Allow one simulator physics tick to apply the loaded-voltage model.
    time.sleep(0.10)

    status, telemetry = simulator_command(
        {
            "T": 130,
        }
    )

    assert status == 200
    assert telemetry["T"] == 1001

    for field in (
        "v",
        "c",
        "pwr",
        "ov",
    ):
        assert field in telemetry, telemetry

    assert 9.9 <= float(
        telemetry["v"]
    ) <= 12.6

    assert float(
        telemetry["c"]
    ) > 0

    assert float(
        telemetry["pwr"]
    ) > 0

    assert isinstance(
        telemetry["ov"],
        bool,
    )


def test_simulator_battery_configuration():
    """
    Validate the configured 3S / 3000 mAh battery model.
    """
    current = state()

    config = current[
        "battery_config"
    ]

    assert int(
        config["cells"]
    ) == BATTERY_CELLS

    close(
        config["capacity_ah"],
        BATTERY_AH,
        1e-6,
    )

    close(
        config["nominal_voltage"],
        (
            BATTERY_CELLS
            * BATTERY_CELL_NOMINAL_V
        ),
        1e-6,
    )

    close(
        config["total_energy_wh"],
        BATTERY_TOTAL_WH,
        1e-6,
    )


def test_battery_scenario_and_endpoint():
    """
    The public API battery endpoint should expose the richer telemetry
    used by the UI:

      - voltage / filtered_voltage
      - level_percent / level
      - current
      - instantaneous and average power
      - remaining energy
      - estimated remaining time
      - battery configuration

    Exact voltage is intentionally NOT asserted because the simulator models
    load sag and api.py applies smoothing.
    """
    stop()

    try:
        set_battery_percent(75)

        # Give the simulator time to settle at idle power.
        time.sleep(0.15)

        body = wait_for_battery_samples(
            minimum_samples=5,
            timeout=3.0,
        )

        assert body is not None
        assert body["ok"] is True

        required = (
            "voltage",
            "filtered_voltage",
            "level_percent",
            "level",
            "current_ma",
            "current_a",
            "power_w",
            "average_power_w",
            "energy_remaining_wh",
            "estimated_remaining_hours",
            "estimated_remaining_minutes",
            "estimated_remaining",
            "samples",
            "estimate_quality",
            "battery_config",
        )

        for field in required:
            assert field in body, (
                field,
                body,
            )

        # 75% scenario with the idle-voltage sag model should remain in
        # roughly the same SOC region.
        assert 60.0 <= float(
            body["level_percent"]
        ) <= 85.0, body

        assert body["level"] in {
            "medium",
            "high",
        }

        assert 9.9 <= float(
            body["voltage"]
        ) <= 12.6

        assert float(
            body["current_ma"]
        ) > 0

        assert float(
            body["current_a"]
        ) > 0

        assert float(
            body["power_w"]
        ) > 0

        assert float(
            body["average_power_w"]
        ) > 0

        assert 0 < float(
            body["energy_remaining_wh"]
        ) <= BATTERY_TOTAL_WH

        assert float(
            body["estimated_remaining_hours"]
        ) > 0

        assert int(
            body["estimated_remaining_minutes"]
        ) > 0

        assert isinstance(
            body["estimated_remaining"],
            str,
        )

        assert body[
            "estimated_remaining"
        ]

        assert int(
            body["samples"]
        ) >= 5

        assert body[
            "estimate_quality"
        ] == "stable"

        config = body[
            "battery_config"
        ]

        assert int(
            config["cells"]
        ) == BATTERY_CELLS

        close(
            config["capacity_ah"],
            BATTERY_AH,
            1e-6,
        )

        close(
            config["total_energy_wh"],
            BATTERY_TOTAL_WH,
            0.01,
        )

    finally:
        # Put the simulator back near its default state for following tests.
        req(
            f"{SIM}/scenario",
            "POST",
            {
                "voltage": 12.2,
            },
        )


def test_battery_power_increases_when_moving():
    """
    The simulator should report more electrical power when the rover moves
    than when it is stopped.
    """
    stop()
    set_battery_percent(75)

    time.sleep(0.12)

    idle = state()
    idle_power_w = (
        float(idle["power_mw"])
        / 1000.0
    )

    try:
        req(
            f"{API}/forward",
            "POST",
            {
                "speed": 1.0,
            },
        )

        moving = wait_for_state(
            lambda s: (
                float(s["power_mw"])
                > float(idle["power_mw"])
            ),
            timeout=2.0,
        )

        assert moving is not None

        moving_power_w = (
            float(moving["power_mw"])
            / 1000.0
        )

        assert (
            moving_power_w
            > idle_power_w
        ), {
            "idle_power_w": idle_power_w,
            "moving_power_w": moving_power_w,
        }

    finally:
        stop()


def test_t130_does_not_keep_simulator_motion_alive():
    """
    Regression test for the simulator watchdog.

    Telemetry T=130 must not refresh last_command_time. If movement is sent
    directly once and only T=130 queries follow, the simulator must stop after
    its COMMAND_TIMEOUT.
    """
    reset()

    # One raw movement command; no api.py heartbeat.
    simulator_command({
        "T": 1,
        "L": 0.15,
        "R": 0.15,
    })

    moving = state()

    assert abs(
        float(moving["left"])
    ) > 0

    deadline = (
        time.monotonic()
        + 2.5
    )

    while time.monotonic() < deadline:
        simulator_command({
            "T": 130,
        })

        current = state()

        if (
            math.isclose(
                float(current["left"]),
                0.0,
                abs_tol=1e-6,
            )
            and math.isclose(
                float(current["right"]),
                0.0,
                abs_tol=1e-6,
            )
        ):
            break

        time.sleep(0.10)

    current = state()

    close(
        current["left"],
        0,
    )

    close(
        current["right"],
        0,
    )

    assert (
        current["direction"]
        == "stop"
    )


def test_simulated_latency():
    try:
        req(
            f"{SIM}/scenario",
            "POST",
            {
                "latency_ms": 250,
            },
        )

        start = time.monotonic()

        req(
            f"{API}/status"
        )

        elapsed = (
            time.monotonic()
            - start
        )

        assert elapsed >= 0.20, elapsed

    finally:
        req(
            f"{SIM}/scenario",
            "POST",
            {
                "latency_ms": 0,
            },
        )


def test_stream():
    response = urllib.request.urlopen(
        f"{UI}/stream.mjpg",
        timeout=8,
    )

    try:
        content_type = response.headers.get(
            "Content-Type",
            "",
        )

        assert (
            "multipart/x-mixed-replace"
            in content_type
        )

        chunk = response.read(4096)

        assert (
            b"--FRAME" in chunk
            or b"\xff\xd8" in chunk
        )

    finally:
        response.close()
