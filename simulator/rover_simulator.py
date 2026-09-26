#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import math
import os
import threading
import time

from flask import Flask, jsonify, request, Response


app = Flask(__name__)
lock = threading.RLock()


# ============================================================
# SIMULATOR CONFIGURATION
# ============================================================

SIM_PORT = int(os.getenv("SIM_PORT", "80"))
UPDATE_HZ = float(os.getenv("SIM_UPDATE_HZ", "30"))

if UPDATE_HZ <= 0:
    raise RuntimeError("SIM_UPDATE_HZ must be greater than 0")


# APPLICATION ENVIRONMENT
#   dev  -> DEVELOPMENT
#   qa   -> QA
#   prod -> PRODUCTION

ENVIRONMENT = os.getenv("ENVIRONMENT", "dev").strip().lower()

ENVIRONMENT_LABELS = {
    "dev": "DEVELOPMENT",
    "qa": "QA",
    "prod": "PRODUCTION",
}

if ENVIRONMENT not in ENVIRONMENT_LABELS:
    raise RuntimeError(
        f"Invalid ENVIRONMENT={ENVIRONMENT!r}. "
        "Available values: dev, qa, prod"
    )

ENVIRONMENT_LABEL = ENVIRONMENT_LABELS[ENVIRONMENT]


# Simulated world dimensions in meters

WORLD_WIDTH = float(os.getenv("SIM_WORLD_WIDTH", "6.0"))
WORLD_HEIGHT = float(os.getenv("SIM_WORLD_HEIGHT", "4.0"))


# Approximate rover kinematics.
# WAVE ROVER T=1 physically accepts L/R in -0.5 .. +0.5.
# We convert that physical range back to -1 .. +1 internally so that
# a command of 0.5 corresponds to 100% simulated wheel speed.

ROVER_COMMAND_MAX = float(os.getenv("SIM_ROVER_COMMAND_MAX", "0.5"))
MAX_LINEAR_SPEED = float(os.getenv("SIM_MAX_LINEAR_SPEED", "1.25"))  # m/s
WHEEL_BASE = float(os.getenv("SIM_WHEEL_BASE", "0.32"))              # m

if ROVER_COMMAND_MAX <= 0:
    raise RuntimeError("SIM_ROVER_COMMAND_MAX must be greater than 0")

if WHEEL_BASE <= 0:
    raise RuntimeError("SIM_WHEEL_BASE must be greater than 0")


# ============================================================
# BATTERY MODEL
# ============================================================
#
# Battery pack:
#   3 x Li-ion cells in series (3S)
#   3000 mAh
#   3.7 V nominal/cell
#
# Nominal pack energy:
#   3 * 3.7 V * 3.0 Ah = 33.3 Wh
#
# T=130 returns the same battery fields as the modified firmware:
#   v   -> volts
#   c   -> milliamps
#   pwr -> milliwatts
#   ov  -> INA219 overflow flag
#
# This simulator models battery consumption only. It does not attempt
# to infer charging/discharging state.

BATTERY_CELLS = int(os.getenv("SIM_BATTERY_CELLS", "3"))
BATTERY_AH = float(os.getenv("SIM_BATTERY_AH", "3.0"))
BATTERY_CELL_NOMINAL_V = float(
    os.getenv("SIM_BATTERY_CELL_NOMINAL_V", "3.7")
)

BATTERY_TOTAL_WH = (
    BATTERY_CELLS * BATTERY_AH * BATTERY_CELL_NOMINAL_V
)

DEFAULT_VOLTAGE = float(
    os.getenv("SIM_BATTERY_VOLTAGE", "12.20")
)

# Approximate electrical consumption.
BASE_POWER_W = float(
    os.getenv("SIM_BASE_POWER_W", "7.0")
)

MOTOR_EXTRA_POWER_W = float(
    os.getenv("SIM_MOTOR_EXTRA_POWER_W", "24.0")
)

# Effective pack/wiring resistance, only for simulated voltage sag.
PACK_INTERNAL_RESISTANCE_OHM = float(
    os.getenv("SIM_PACK_INTERNAL_RESISTANCE_OHM", "0.06")
)

MIN_PACK_VOLTAGE = float(
    os.getenv("SIM_MIN_PACK_VOLTAGE", "9.90")
)

# Approximate Li-ion SOC curve PER CELL.
# Kept aligned with the improved api.py battery calculation.
BATTERY_SOC_CURVE = [
    (3.30, 0),
    (3.50, 5),
    (3.61, 10),
    (3.69, 15),
    (3.71, 20),
    (3.73, 25),
    (3.75, 30),
    (3.77, 35),
    (3.79, 40),
    (3.80, 45),
    (3.82, 50),
    (3.85, 55),
    (3.87, 60),
    (3.91, 65),
    (3.95, 70),
    (3.98, 75),
    (4.02, 80),
    (4.08, 85),
    (4.11, 90),
    (4.15, 95),
    (4.20, 100),
]


# Safety timeout: stop if no MOVEMENT command arrives for X seconds.
# T=130 telemetry requests do not refresh this timer.

COMMAND_TIMEOUT = float(os.getenv("SIM_COMMAND_TIMEOUT", "1.5"))


# ============================================================
# UTILITIES
# ============================================================

def clamp(value, minimum=-1.0, maximum=1.0):
    return max(minimum, min(maximum, value))


def normalize_angle(angle):
    while angle > math.pi:
        angle -= 2 * math.pi

    while angle < -math.pi:
        angle += 2 * math.pi

    return angle


def classify_direction(left, right):
    eps = 0.01

    if abs(left) < eps and abs(right) < eps:
        return "stop"

    if left > eps and right > eps:
        return "forward"

    if left < -eps and right < -eps:
        return "backward"

    if left < -eps and right > eps:
        return "left"

    if left > eps and right < -eps:
        return "right"

    return "custom"


def soc_from_pack_voltage(pack_voltage):
    """Approximate SOC percentage from pack voltage."""
    cell_voltage = float(pack_voltage) / BATTERY_CELLS

    if cell_voltage <= BATTERY_SOC_CURVE[0][0]:
        return 0.0

    if cell_voltage >= BATTERY_SOC_CURVE[-1][0]:
        return 100.0

    for (v1, p1), (v2, p2) in zip(
        BATTERY_SOC_CURVE,
        BATTERY_SOC_CURVE[1:]
    ):
        if v1 <= cell_voltage <= v2:
            ratio = (cell_voltage - v1) / (v2 - v1)
            return p1 + ratio * (p2 - p1)

    return 0.0


def pack_voltage_from_soc(soc_percent):
    """Approximate open-circuit pack voltage from SOC percentage."""
    soc_percent = clamp(float(soc_percent), 0.0, 100.0)

    if soc_percent <= BATTERY_SOC_CURVE[0][1]:
        return BATTERY_SOC_CURVE[0][0] * BATTERY_CELLS

    if soc_percent >= BATTERY_SOC_CURVE[-1][1]:
        return BATTERY_SOC_CURVE[-1][0] * BATTERY_CELLS

    for (v1, p1), (v2, p2) in zip(
        BATTERY_SOC_CURVE,
        BATTERY_SOC_CURVE[1:]
    ):
        if p1 <= soc_percent <= p2:
            if p2 == p1:
                cell_voltage = v1
            else:
                ratio = (soc_percent - p1) / (p2 - p1)
                cell_voltage = v1 + ratio * (v2 - v1)

            return cell_voltage * BATTERY_CELLS

    return BATTERY_SOC_CURVE[0][0] * BATTERY_CELLS


def battery_energy_from_soc(soc_percent):
    return (
        BATTERY_TOTAL_WH
        * clamp(soc_percent, 0.0, 100.0)
        / 100.0
    )


def state_copy():
    with lock:
        return dict(state)


# ============================================================
# INITIAL STATE
# ============================================================

_initial_soc = soc_from_pack_voltage(DEFAULT_VOLTAGE)
_initial_energy_wh = battery_energy_from_soc(_initial_soc)
_initial_current_a = BASE_POWER_W / max(DEFAULT_VOLTAGE, 0.1)

state = {
    # Physical WAVE ROVER command values: -0.5 .. +0.5
    "left": 0.0,
    "right": 0.0,

    # Battery telemetry
    "voltage": DEFAULT_VOLTAGE,
    "open_circuit_voltage": DEFAULT_VOLTAGE,
    "soc_percent": _initial_soc,
    "energy_wh": _initial_energy_wh,
    "current_ma": _initial_current_a * 1000.0,
    "power_mw": BASE_POWER_W * 1000.0,
    "temperature_c": 50.0,
    "ina219_overflow": False,

    # Pose
    "x": WORLD_WIDTH / 2.0,
    "y": WORLD_HEIGHT / 2.0,
    "heading": 0.0,

    # Motion
    "linear_velocity": 0.0,
    "angular_velocity": 0.0,
    "direction": "stop",

    # Commands / diagnostics
    "last_command": None,
    "last_update": time.time(),
    "last_command_time": 0.0,

    "connected": True,
    "latency_ms": 0,
    "fail_next": False,
}


# ============================================================
# BATTERY / TELEMETRY HELPERS
# ============================================================

def update_battery_locked(dt, activity):
    """
    Update simulated battery.

    activity:
        0.0 -> motors stopped
        1.0 -> full motor command
    """
    activity = clamp(activity, 0.0, 1.0)

    requested_power_w = (
        BASE_POWER_W
        + MOTOR_EXTRA_POWER_W * activity
    )

    energy_used_wh = (
        requested_power_w
        * max(dt, 0.0)
        / 3600.0
    )

    state["energy_wh"] = max(
        0.0,
        state["energy_wh"] - energy_used_wh
    )

    if BATTERY_TOTAL_WH > 0:
        state["soc_percent"] = clamp(
            (
                state["energy_wh"]
                / BATTERY_TOTAL_WH
            ) * 100.0,
            0.0,
            100.0
        )
    else:
        state["soc_percent"] = 0.0

    open_v = pack_voltage_from_soc(
        state["soc_percent"]
    )

    current_a = (
        requested_power_w
        / max(open_v, 0.1)
    )

    loaded_v = (
        open_v
        - current_a * PACK_INTERNAL_RESISTANCE_OHM
    )

    loaded_v = max(
        MIN_PACK_VOLTAGE,
        loaded_v
    )

    measured_power_w = loaded_v * current_a

    state["open_circuit_voltage"] = open_v
    state["voltage"] = loaded_v
    state["current_ma"] = current_a * 1000.0
    state["power_mw"] = measured_power_w * 1000.0
    state["ina219_overflow"] = False

    # Simple temperature model for telemetry/UI testing.
    state["temperature_c"] = 50.0 + 8.0 * activity


def rover_telemetry_locked():
    """
    Telemetry compatible with the modified WAVE ROVER firmware.
    """
    yaw = math.degrees(state["heading"]) % 360.0

    return {
        "T": 1001,
        "L": round(state["left"], 5),
        "R": round(state["right"], 5),

        # Simplified IMU telemetry
        "r": 0.0,
        "p": 0.0,
        "y": round(yaw, 4),
        "temp": round(state["temperature_c"], 5),

        # INA219-compatible telemetry
        "v": round(state["voltage"], 5),
        "c": round(state["current_ma"], 4),
        "pwr": int(round(state["power_mw"])),
        "ov": bool(state["ina219_overflow"]),
    }


# ============================================================
# SIMULATOR PHYSICS ENGINE
# ============================================================

def physics_loop():
    period = 1.0 / UPDATE_HZ
    previous = time.monotonic()

    while True:
        loop_started = time.monotonic()

        dt = loop_started - previous
        previous = loop_started

        # Prevent huge jumps after debugger pauses/sleep.
        dt = min(max(dt, 0.0), 0.25)

        with lock:
            now_wall = time.time()

            # Watchdog applies only to movement commands.
            if (
                state["last_command_time"] > 0
                and (
                    now_wall
                    - state["last_command_time"]
                ) > COMMAND_TIMEOUT
            ):
                state["left"] = 0.0
                state["right"] = 0.0
                state["direction"] = "stop"

            physical_left = state["left"]
            physical_right = state["right"]

            # Convert real rover command range (-0.5..0.5)
            # back to normalized wheel range (-1..1).
            norm_left = clamp(
                physical_left / ROVER_COMMAND_MAX,
                -1.0,
                1.0
            )

            norm_right = clamp(
                physical_right / ROVER_COMMAND_MAX,
                -1.0,
                1.0
            )

            v_left = norm_left * MAX_LINEAR_SPEED
            v_right = norm_right * MAX_LINEAR_SPEED

            linear = (v_right + v_left) / 2.0

            # Differential-drive convention:
            # LEFT  -> L negative, R positive
            # RIGHT -> L positive, R negative
            angular = (
                v_right - v_left
            ) / WHEEL_BASE

            state["linear_velocity"] = linear
            state["angular_velocity"] = angular

            state["heading"] = normalize_angle(
                state["heading"] + angular * dt
            )

            state["x"] += (
                linear
                * math.cos(state["heading"])
                * dt
            )

            state["y"] += (
                linear
                * math.sin(state["heading"])
                * dt
            )

            state["x"] = clamp(
                state["x"],
                0.0,
                WORLD_WIDTH
            )

            state["y"] = clamp(
                state["y"],
                0.0,
                WORLD_HEIGHT
            )

            activity = (
                abs(norm_left)
                + abs(norm_right)
            ) / 2.0

            update_battery_locked(
                dt,
                activity
            )

            state["last_update"] = now_wall

        elapsed = time.monotonic() - loop_started

        time.sleep(
            max(0.0, period - elapsed)
        )


# ============================================================
# SIMULATOR CONTROL API
# ============================================================

@app.get("/health")
def health():
    with lock:
        connected = state["connected"]

    if not connected:
        return jsonify({
            "ok": False,
            "service": "wave-rover-simulator",
            "environment": ENVIRONMENT,
            "environment_label": ENVIRONMENT_LABEL,
            "connected": False,
        }), 503

    return jsonify({
        "ok": True,
        "service": "wave-rover-simulator",
        "environment": ENVIRONMENT,
        "environment_label": ENVIRONMENT_LABEL,
        "connected": True,
    })


@app.get("/state")
def get_state():
    data = state_copy()

    data["heading_degrees"] = round(
        math.degrees(data["heading"]),
        2
    )

    power_w = data["power_mw"] / 1000.0

    if power_w > 0.05:
        data["estimated_remaining_hours"] = (
            data["energy_wh"] / power_w
        )
    else:
        data["estimated_remaining_hours"] = None

    return jsonify({
        "ok": True,
        "environment": ENVIRONMENT,
        "environment_label": ENVIRONMENT_LABEL,
        "world": {
            "width": WORLD_WIDTH,
            "height": WORLD_HEIGHT,
        },
        "battery_config": {
            "cells": BATTERY_CELLS,
            "capacity_ah": BATTERY_AH,
            "nominal_voltage": (
                BATTERY_CELLS
                * BATTERY_CELL_NOMINAL_V
            ),
            "total_energy_wh": BATTERY_TOTAL_WH,
            "base_power_w": BASE_POWER_W,
            "motor_extra_power_w": MOTOR_EXTRA_POWER_W,
        },
        **data,
    })


@app.post("/reset")
def reset():
    """
    Reset pose/motion only.

    Battery state is intentionally preserved.
    """
    with lock:
        state["left"] = 0.0
        state["right"] = 0.0

        state["x"] = WORLD_WIDTH / 2.0
        state["y"] = WORLD_HEIGHT / 2.0
        state["heading"] = 0.0

        state["linear_velocity"] = 0.0
        state["angular_velocity"] = 0.0

        state["direction"] = "stop"
        state["last_command"] = None
        state["last_command_time"] = 0.0

    return jsonify({
        "ok": True,
        **state_copy()
    })


@app.post("/scenario")
def scenario():
    """
    Change simulation conditions during testing.

    Examples:

      {"voltage": 12.0}
      {"battery_percent": 75}
      {"connected": false}
      {"latency_ms": 500}
      {"fail_next": true}
      {"x": 1.2, "y": 2.4, "heading_degrees": 90}
    """
    data = request.get_json(silent=True) or {}

    try:
        with lock:
            if "voltage" in data:
                voltage = float(data["voltage"])
                soc = soc_from_pack_voltage(
                    voltage
                )

                state["soc_percent"] = soc
                state["energy_wh"] = (
                    battery_energy_from_soc(soc)
                )

                state["open_circuit_voltage"] = voltage
                state["voltage"] = voltage

            if "battery_percent" in data:
                soc = clamp(
                    float(data["battery_percent"]),
                    0.0,
                    100.0
                )

                state["soc_percent"] = soc
                state["energy_wh"] = (
                    battery_energy_from_soc(soc)
                )

                voltage = pack_voltage_from_soc(soc)

                state["open_circuit_voltage"] = voltage
                state["voltage"] = voltage

            if "connected" in data:
                state["connected"] = bool(
                    data["connected"]
                )

            if "latency_ms" in data:
                state["latency_ms"] = max(
                    0,
                    int(data["latency_ms"])
                )

            if "fail_next" in data:
                state["fail_next"] = bool(
                    data["fail_next"]
                )

            if "x" in data:
                state["x"] = clamp(
                    float(data["x"]),
                    0.0,
                    WORLD_WIDTH
                )

            if "y" in data:
                state["y"] = clamp(
                    float(data["y"]),
                    0.0,
                    WORLD_HEIGHT
                )

            if "heading_degrees" in data:
                state["heading"] = normalize_angle(
                    math.radians(
                        float(
                            data["heading_degrees"]
                        )
                    )
                )

    except (TypeError, ValueError) as exc:
        return jsonify({
            "ok": False,
            "error": (
                f"invalid scenario value: {exc}"
            ),
        }), 400

    return jsonify({
        "ok": True,
        **state_copy()
    })


# ============================================================
# WAVE ROVER-COMPATIBLE /js API
# ============================================================

@app.get("/js")
def wave_rover_js():
    with lock:
        connected = state["connected"]
        latency_ms = state["latency_ms"]
        fail_next = state["fail_next"]

        if fail_next:
            state["fail_next"] = False

    if not connected:
        return jsonify({
            "ok": False,
            "error": "simulated rover disconnected",
        }), 503

    if latency_ms:
        time.sleep(latency_ms / 1000.0)

    if fail_next:
        return jsonify({
            "ok": False,
            "error": "simulated transient failure",
        }), 500

    raw = request.args.get("json")

    if not raw:
        return jsonify({
            "ok": False,
            "error": "missing json query parameter",
        }), 400

    try:
        cmd = json.loads(raw)
    except json.JSONDecodeError:
        return jsonify({
            "ok": False,
            "error": "invalid json",
        }), 400

    if not isinstance(cmd, dict):
        return jsonify({
            "ok": False,
            "error": "json command must be an object",
        }), 400

    cmd_type = cmd.get("T")

    try:
        with lock:
            state["last_command"] = cmd

            if cmd_type == 1:
                # Match physical WAVE ROVER range.
                left = clamp(
                    float(cmd.get("L", 0.0)),
                    -ROVER_COMMAND_MAX,
                    ROVER_COMMAND_MAX
                )

                right = clamp(
                    float(cmd.get("R", 0.0)),
                    -ROVER_COMMAND_MAX,
                    ROVER_COMMAND_MAX
                )

                state["left"] = left
                state["right"] = right

                state["direction"] = classify_direction(
                    left,
                    right
                )

                # Only movement refreshes watchdog.
                state["last_command_time"] = time.time()

            elif cmd_type == 130:
                # Telemetry request only.
                pass

            else:
                return jsonify({
                    "ok": False,
                    "error": (
                        f"unsupported T={cmd_type}"
                    ),
                }), 400

            return jsonify(
                rover_telemetry_locked()
            )

    except (TypeError, ValueError) as exc:
        return jsonify({
            "ok": False,
            "error": (
                f"invalid rover command: {exc}"
            ),
        }), 400


# ============================================================
# 2D SIMULATOR UI
# ============================================================

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Wave Rover Simulator - __ENVIRONMENT_LABEL__</title>

<style>
    :root {
        color-scheme: dark;
    }

    body {
        margin: 0;
        font-family: Arial, sans-serif;
        background: #0d1117;
        color: #e6edf3;
    }

    .page {
        max-width: 1180px;
        margin: 0 auto;
        padding: 22px;
    }

    h1 {
        margin: 0 0 4px;
        font-size: 24px;
    }

    .subtitle {
        color: #8b949e;
        margin-bottom: 18px;
    }

    .layout {
        display: grid;
        grid-template-columns: minmax(0, 1fr) 330px;
        gap: 16px;
    }

    .panel {
        background: #161b22;
        border: 1px solid #30363d;
        border-radius: 12px;
        padding: 14px;
    }

    canvas {
        width: 100%;
        height: auto;
        background: #0d1117;
        border: 1px solid #30363d;
        border-radius: 8px;
    }

    .stats {
        font-family: monospace;
        line-height: 1.6;
        white-space: pre;
    }

    button {
        background: #238636;
        border: 0;
        color: white;
        border-radius: 8px;
        padding: 9px 12px;
        cursor: pointer;
        font-weight: bold;
    }

    button.secondary {
        background: #30363d;
    }

    .badge {
        display: inline-block;
        border-radius: 999px;
        padding: 4px 9px;
        font-size: 12px;
        background: #30363d;
        margin-bottom: 10px;
    }

    .ok { background: #1f6f3d; }
    .bad { background: #8e2d2d; }

    .help {
        margin-top: 12px;
        color: #8b949e;
        font-size: 13px;
        line-height: 1.5;
    }

    .scenario-row {
        display: flex;
        gap: 6px;
        flex-wrap: wrap;
        margin-top: 10px;
    }

    @media (max-width: 800px) {
        .layout {
            grid-template-columns: 1fr;
        }
    }
</style>
</head>

<body>
<div class="page">
    <h1>Wave Rover Simulator - __ENVIRONMENT_LABEL__</h1>

    <div class="subtitle">
        __ENVIRONMENT_LABEL__ diagnostic view.
        Control the rover from your real api.py / UI.
    </div>

    <div class="layout">
        <div class="panel">
            <canvas id="world" width="800" height="530"></canvas>

            <div class="help">
                The triangle indicates rover orientation.
                The simulator now emits v, c, pwr and ov
                like the modified ESP32 firmware.
            </div>
        </div>

        <div class="panel">
            <div id="connection" class="badge">
                Connecting...
            </div>

            <div class="stats" id="stats">
                Waiting for state...
            </div>

            <div class="scenario-row">
                <button
                    class="secondary"
                    onclick="resetRover()"
                >
                    Reset position
                </button>
            </div>

            <hr style="border-color:#30363d;margin:16px 0">

            <strong>__ENVIRONMENT_LABEL__ Scenarios</strong>

            <div class="scenario-row">
                <button onclick="setBatteryPercent(100)">
                    100%
                </button>

                <button onclick="setBatteryPercent(75)">
                    75%
                </button>

                <button onclick="setBatteryPercent(50)">
                    50%
                </button>

                <button onclick="setBatteryPercent(20)">
                    20%
                </button>
            </div>

            <div class="scenario-row">
                <button onclick="setConnected(true)">
                    Connect
                </button>

                <button
                    class="secondary"
                    onclick="setConnected(false)"
                >
                    Disconnect
                </button>
            </div>

            <div class="scenario-row">
                <button
                    class="secondary"
                    onclick="setLatency(500)"
                >
                    500ms latency
                </button>

                <button
                    class="secondary"
                    onclick="setLatency(0)"
                >
                    No latency
                </button>
            </div>

            <div class="scenario-row">
                <button
                    class="secondary"
                    onclick="failNext()"
                >
                    Fail next request
                </button>
            </div>
        </div>
    </div>
</div>

<script>
const canvas = document.getElementById('world');
const ctx = canvas.getContext('2d');

const statsEl = document.getElementById('stats');
const connectionEl = document.getElementById('connection');

let latest = null;

function drawGrid(world) {
    ctx.clearRect(
        0,
        0,
        canvas.width,
        canvas.height
    );

    ctx.fillStyle = '#0d1117';

    ctx.fillRect(
        0,
        0,
        canvas.width,
        canvas.height
    );

    ctx.strokeStyle = '#21262d';
    ctx.lineWidth = 1;

    const cols = 8;
    const rows = 6;

    for (let i = 0; i <= cols; i++) {
        const x = (
            i * canvas.width / cols
        );

        ctx.beginPath();
        ctx.moveTo(x, 0);
        ctx.lineTo(x, canvas.height);
        ctx.stroke();
    }

    for (let i = 0; i <= rows; i++) {
        const y = (
            i * canvas.height / rows
        );

        ctx.beginPath();
        ctx.moveTo(0, y);
        ctx.lineTo(canvas.width, y);
        ctx.stroke();
    }
}

function drawRover(s) {
    if (!s) return;

    const px = (
        s.x
        / s.world.width
    ) * canvas.width;

    const py = (
        canvas.height
        - (
            s.y
            / s.world.height
        ) * canvas.height
    );

    ctx.save();
    ctx.translate(px, py);
    ctx.rotate(-s.heading);

    ctx.fillStyle = '#f0f6fc';

    ctx.fillRect(
        -20,
        -12,
        40,
        24
    );

    ctx.fillStyle = '#2f81f7';
    ctx.beginPath();
    ctx.moveTo(28, 0);
    ctx.lineTo(12, -8);
    ctx.lineTo(12, 8);
    ctx.closePath();
    ctx.fill();

    ctx.fillStyle = '#8b949e';
    ctx.fillRect(-22, -16, 10, 4);
    ctx.fillRect(-22, 12, 10, 4);
    ctx.fillRect(12, -16, 10, 4);
    ctx.fillRect(12, 12, 10, 4);

    ctx.restore();
}

function formatHours(hours) {
    if (!Number.isFinite(hours)) {
        return '--';
    }

    const totalMinutes = Math.max(
        0,
        Math.round(hours * 60)
    );

    const h = Math.floor(
        totalMinutes / 60
    );

    const m = totalMinutes % 60;

    if (h > 0) {
        return (
            `${h}h `
            + `${String(m).padStart(2, '0')}m`
        );
    }

    return `${m}m`;
}

function render(s) {
    drawGrid(s.world);
    drawRover(s);

    const currentA = (
        s.current_ma / 1000
    );

    const powerW = (
        s.power_mw / 1000
    );

    statsEl.textContent =
`x: ${s.x.toFixed(2)} m
y: ${s.y.toFixed(2)} m
heading: ${s.heading_degrees.toFixed(1)}°
left cmd: ${s.left.toFixed(3)}
right cmd: ${s.right.toFixed(3)}
direction: ${s.direction}
speed: ${s.linear_velocity.toFixed(2)} m/s
turn rate: ${s.angular_velocity.toFixed(2)} rad/s

battery: ${s.voltage.toFixed(2)} V
SOC: ${s.soc_percent.toFixed(1)} %
current: ${currentA.toFixed(2)} A
power: ${powerW.toFixed(2)} W
energy: ${s.energy_wh.toFixed(2)} Wh
estimated: ${formatHours(
    s.estimated_remaining_hours
)}

latency: ${s.latency_ms} ms`;

    if (s.connected) {
        connectionEl.textContent =
            'ROVER CONNECTED';

        connectionEl.className =
            'badge ok';
    } else {
        connectionEl.textContent =
            'ROVER DISCONNECTED';

        connectionEl.className =
            'badge bad';
    }
}

async function refresh() {
    try {
        const r = await fetch(
            './state',
            {
                cache: 'no-store'
            }
        );

        const data = await r.json();

        latest = data;
        render(data);

    } catch (e) {
        connectionEl.textContent =
            'SIMULATOR ERROR';

        connectionEl.className =
            'badge bad';
    }
}

async function scenario(payload) {
    await fetch(
        './scenario',
        {
            method: 'POST',
            headers: {
                'Content-Type':
                    'application/json'
            },
            body: JSON.stringify(payload)
        }
    );

    await refresh();
}

function setBatteryPercent(v) {
    scenario({
        battery_percent: v
    });
}

function setConnected(v) {
    scenario({
        connected: v
    });
}

function setLatency(v) {
    scenario({
        latency_ms: v
    });
}

function failNext() {
    scenario({
        fail_next: true
    });
}

async function resetRover() {
    await fetch(
        './reset',
        {
            method: 'POST'
        }
    );

    await refresh();
}

setInterval(
    refresh,
    100
);

refresh();
</script>
</body>
</html>
"""


@app.get("/")
def index():
    page = (
        PAGE
        .replace(
            "__ENVIRONMENT_LABEL__",
            ENVIRONMENT_LABEL
        )
        .replace(
            "__ENVIRONMENT_NAME__",
            ENVIRONMENT
        )
    )

    return Response(
        page,
        mimetype="text/html"
    )


if __name__ == "__main__":
    threading.Thread(
        target=physics_loop,
        daemon=True
    ).start()

    print("==========================================")
    print(
        f"       WAVE ROVER {ENVIRONMENT_LABEL} SIMULATOR"
    )
    print("==========================================")

    print(
        f"Environment: {ENVIRONMENT} "
        f"({ENVIRONMENT_LABEL})"
    )

    print(
        f"World: {WORLD_WIDTH}m x "
        f"{WORLD_HEIGHT}m"
    )

    print(
        f"Update: {UPDATE_HZ} Hz"
    )

    print(
        f"Battery: {BATTERY_CELLS}S "
        f"{BATTERY_AH:.1f}Ah "
        f"({BATTERY_TOTAL_WH:.1f}Wh)"
    )

    print(
        f"Power model: "
        f"{BASE_POWER_W:.1f}W idle + "
        f"{MOTOR_EXTRA_POWER_W:.1f}W motors"
    )

    print(
        f"Port: {SIM_PORT}"
    )

    app.run(
        host="0.0.0.0",
        port=SIM_PORT,
        debug=False,
        threaded=True
    )
