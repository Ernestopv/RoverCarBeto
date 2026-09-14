#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import math
import os
import threading
import time
from flask import Flask, jsonify, request, Response

app = Flask(__name__)
lock = threading.Lock()

# ============================================================
# SIMULATOR CONFIGURATION
# ============================================================

SIM_PORT = int(os.getenv("SIM_PORT", "80"))
UPDATE_HZ = float(os.getenv("SIM_UPDATE_HZ", "30"))

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

# Approximate kinematics
MAX_LINEAR_SPEED = float(os.getenv("SIM_MAX_LINEAR_SPEED", "1.0"))   # m/s
WHEEL_BASE = float(os.getenv("SIM_WHEEL_BASE", "0.32"))             # m

# Battery
DEFAULT_VOLTAGE = float(os.getenv("SIM_BATTERY_VOLTAGE", "12.20"))
BATTERY_DRAIN_PER_SEC = float(os.getenv("SIM_BATTERY_DRAIN_PER_SEC", "0.002"))

# Safety timeout: stop the rover if no commands arrive for X seconds
COMMAND_TIMEOUT = float(os.getenv("SIM_COMMAND_TIMEOUT", "1.5"))

state = {
    "left": 0.0,
    "right": 0.0,
    "voltage": DEFAULT_VOLTAGE,

    "x": WORLD_WIDTH / 2.0,
    "y": WORLD_HEIGHT / 2.0,
    "heading": 0.0,  # radians. 0 = right

    "linear_velocity": 0.0,
    "angular_velocity": 0.0,

    "direction": "stop",
    "last_command": None,
    "last_update": time.time(),
    "last_command_time": 0.0,

    "connected": True,
    "latency_ms": 0,
    "fail_next": False,
}


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
    eps = 0.02

    if abs(left) < eps and abs(right) < eps:
        return "stop"

    if left > eps and right > eps:
        return "forward"

    if left < -eps and right < -eps:
        return "backward"

    if left > eps and right < -eps:
        return "left"

    if left < -eps and right > eps:
        return "right"

    return "custom"


def state_copy():
    with lock:
        return dict(state)


# ============================================================
# SIMULATOR PHYSICS ENGINE
# ============================================================

def physics_loop():
    period = 1.0 / UPDATE_HZ
    previous = time.time()

    while True:
        now = time.time()
        dt = now - previous
        previous = now

        with lock:
            # Watchdog: stop if the controller stops sending commands
            if (
                state["last_command_time"] > 0
                and (now - state["last_command_time"]) > COMMAND_TIMEOUT
            ):
                state["left"] = 0.0
                state["right"] = 0.0
                state["direction"] = "stop"

            left = state["left"]
            right = state["right"]

            # Convert [-1, 1] into approximate linear speed for each wheel
            v_left = left * MAX_LINEAR_SPEED
            v_right = right * MAX_LINEAR_SPEED

            linear = (v_right + v_left) / 2.0
            angular = (v_left - v_right) / WHEEL_BASE

            state["linear_velocity"] = linear
            state["angular_velocity"] = angular

            # Kinematic integration
            state["heading"] = normalize_angle(
                state["heading"] + angular * dt
            )

            state["x"] += linear * math.cos(state["heading"]) * dt
            state["y"] += linear * math.sin(state["heading"]) * dt

            # Keep the rover inside the simulated world
            state["x"] = max(0.0, min(WORLD_WIDTH, state["x"]))
            state["y"] = max(0.0, min(WORLD_HEIGHT, state["y"]))

            # Simple battery drain simulation
            activity = max(abs(left), abs(right))
            if activity > 0.01:
                state["voltage"] = max(
                    9.0,
                    state["voltage"] - BATTERY_DRAIN_PER_SEC * activity * dt
                )

            state["last_update"] = now

        time.sleep(period)


# ============================================================
# WAVE ROVER-COMPATIBLE API
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
            "connected": False
        }), 503

    return jsonify({
        "ok": True,
        "service": "wave-rover-simulator",
        "environment": ENVIRONMENT,
        "environment_label": ENVIRONMENT_LABEL,
        "connected": True
    })


@app.get("/state")
def get_state():
    data = state_copy()
    data["heading_degrees"] = round(math.degrees(data["heading"]), 2)

    return jsonify({
        "ok": True,
        "environment": ENVIRONMENT,
        "environment_label": ENVIRONMENT_LABEL,
        "world": {
            "width": WORLD_WIDTH,
            "height": WORLD_HEIGHT
        },
        **data
    })


@app.post("/reset")
def reset():
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

    return jsonify({"ok": True, **state_copy()})


@app.post("/scenario")
def scenario():
    """
    Allows simulation conditions to be changed during DEV testing.

    Examples:
      {"voltage": 10.5}
      {"connected": false}
      {"latency_ms": 500}
      {"fail_next": true}
      {"x": 1.2, "y": 2.4, "heading_degrees": 90}
    """
    data = request.get_json(silent=True) or {}

    with lock:
        if "voltage" in data:
            state["voltage"] = float(data["voltage"])

        if "connected" in data:
            state["connected"] = bool(data["connected"])

        if "latency_ms" in data:
            state["latency_ms"] = max(0, int(data["latency_ms"]))

        if "fail_next" in data:
            state["fail_next"] = bool(data["fail_next"])

        if "x" in data:
            state["x"] = max(0.0, min(WORLD_WIDTH, float(data["x"])))

        if "y" in data:
            state["y"] = max(0.0, min(WORLD_HEIGHT, float(data["y"])))

        if "heading_degrees" in data:
            state["heading"] = normalize_angle(
                math.radians(float(data["heading_degrees"]))
            )

    return jsonify({"ok": True, **state_copy()})


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
            "error": "simulated rover disconnected"
        }), 503

    if latency_ms:
        time.sleep(latency_ms / 1000.0)

    if fail_next:
        return jsonify({
            "ok": False,
            "error": "simulated transient failure"
        }), 500

    raw = request.args.get("json")

    if not raw:
        return jsonify({
            "ok": False,
            "error": "missing json query parameter"
        }), 400

    try:
        cmd = json.loads(raw)
    except json.JSONDecodeError:
        return jsonify({
            "ok": False,
            "error": "invalid json"
        }), 400

    cmd_type = cmd.get("T")

    with lock:
        state["last_command"] = cmd
        state["last_command_time"] = time.time()

        if cmd_type == 1:
            left = clamp(float(cmd.get("L", 0.0)))
            right = clamp(float(cmd.get("R", 0.0)))

            state["left"] = left
            state["right"] = right
            state["direction"] = classify_direction(left, right)

        elif cmd_type != 130:
            return jsonify({
                "ok": False,
                "error": f"unsupported T={cmd_type}"
            }), 400

        return jsonify({
            "T": 1001,
            "L": state["left"],
            "R": state["right"],
            "v": state["voltage"]
        })


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
        grid-template-columns: minmax(0, 1fr) 300px;
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
        __ENVIRONMENT_LABEL__ environment diagnostic view. Control the rover from your real ui.py.
    </div>

    <div class="layout">
        <div class="panel">
            <canvas id="world" width="800" height="530"></canvas>
            <div class="help">
                The triangle indicates the rover orientation.
                This page does not send movement commands; it only visualizes
                what it receives from api.py.
            </div>
        </div>

        <div class="panel">
            <div id="connection" class="badge">Connecting...</div>

            <div class="stats" id="stats">Waiting for state...</div>

            <div class="scenario-row">
                <button class="secondary" onclick="resetRover()">Reset position</button>
            </div>

            <hr style="border-color:#30363d;margin:16px 0">

            <strong>__ENVIRONMENT_LABEL__ Scenarios</strong>

            <div class="scenario-row">
                <button onclick="setVoltage(12.4)">High battery</button>
                <button onclick="setVoltage(10.4)">Low battery</button>
            </div>

            <div class="scenario-row">
                <button onclick="setConnected(true)">Connect</button>
                <button class="secondary" onclick="setConnected(false)">Disconnect</button>
            </div>

            <div class="scenario-row">
                <button class="secondary" onclick="setLatency(500)">500ms latency</button>
                <button class="secondary" onclick="setLatency(0)">No latency</button>
            </div>

            <div class="scenario-row">
                <button class="secondary" onclick="failNext()">Fail next request</button>
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
    ctx.clearRect(0,0,canvas.width,canvas.height);

    ctx.fillStyle = '#0d1117';
    ctx.fillRect(0,0,canvas.width,canvas.height);

    ctx.strokeStyle = '#21262d';
    ctx.lineWidth = 1;

    const cols = 8;
    const rows = 6;

    for (let i=0;i<=cols;i++) {
        const x = i * canvas.width / cols;
        ctx.beginPath();
        ctx.moveTo(x,0);
        ctx.lineTo(x,canvas.height);
        ctx.stroke();
    }

    for (let i=0;i<=rows;i++) {
        const y = i * canvas.height / rows;
        ctx.beginPath();
        ctx.moveTo(0,y);
        ctx.lineTo(canvas.width,y);
        ctx.stroke();
    }
}

function drawRover(s) {
    if (!s) return;

    const px = (s.x / s.world.width) * canvas.width;
    const py = canvas.height - (s.y / s.world.height) * canvas.height;

    ctx.save();
    ctx.translate(px, py);
    ctx.rotate(-s.heading);

    ctx.fillStyle = '#f0f6fc';
    ctx.fillRect(-20, -12, 40, 24);

    ctx.fillStyle = '#2f81f7';
    ctx.beginPath();
    ctx.moveTo(28,0);
    ctx.lineTo(12,-8);
    ctx.lineTo(12,8);
    ctx.closePath();
    ctx.fill();

    ctx.fillStyle = '#8b949e';
    ctx.fillRect(-22,-16,10,4);
    ctx.fillRect(-22,12,10,4);
    ctx.fillRect(12,-16,10,4);
    ctx.fillRect(12,12,10,4);

    ctx.restore();
}

function render(s) {
    drawGrid(s.world);
    drawRover(s);

    statsEl.textContent =
`x: ${s.x.toFixed(2)} m
y: ${s.y.toFixed(2)} m
heading: ${s.heading_degrees.toFixed(1)}°
left: ${s.left.toFixed(2)}
right: ${s.right.toFixed(2)}
direction: ${s.direction}
speed: ${s.linear_velocity.toFixed(2)} m/s
turn rate: ${s.angular_velocity.toFixed(2)} rad/s
battery: ${s.voltage.toFixed(2)} V
latency: ${s.latency_ms} ms`;

    if (s.connected) {
        connectionEl.textContent = 'ROVER CONNECTED';
        connectionEl.className = 'badge ok';
    } else {
        connectionEl.textContent = 'ROVER DISCONNECTED';
        connectionEl.className = 'badge bad';
    }
}

async function refresh() {
    try {
        const r = await fetch('./state', {cache:'no-store'});
        const data = await r.json();
        latest = data;
        render(data);
    } catch(e) {
        connectionEl.textContent = 'SIMULATOR ERROR';
        connectionEl.className = 'badge bad';
    }
}

async function scenario(payload) {
    await fetch('./scenario', {
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify(payload)
    });
    await refresh();
}

function setVoltage(v) {
    scenario({voltage:v});
}

function setConnected(v) {
    scenario({connected:v});
}

function setLatency(v) {
    scenario({latency_ms:v});
}

function failNext() {
    scenario({fail_next:true});
}

async function resetRover() {
    await fetch('./reset', {method:'POST'});
    await refresh();
}

setInterval(refresh, 100);
refresh();
</script>
</body>
</html>
"""


@app.get("/")
def index():
    page = (
        PAGE
        .replace("__ENVIRONMENT_LABEL__", ENVIRONMENT_LABEL)
        .replace("__ENVIRONMENT_NAME__", ENVIRONMENT)
    )
    return Response(page, mimetype="text/html")


if __name__ == "__main__":
    threading.Thread(target=physics_loop, daemon=True).start()

    print("==========================================")
    print(f"       WAVE ROVER {ENVIRONMENT_LABEL} SIMULATOR")
    print("==========================================")
    print(f"Environment: {ENVIRONMENT} ({ENVIRONMENT_LABEL})")
    print(f"World: {WORLD_WIDTH}m x {WORLD_HEIGHT}m")
    print(f"Update: {UPDATE_HZ} Hz")
    print(f"Port: {SIM_PORT}")

    app.run(
        host="0.0.0.0",
        port=SIM_PORT,
        debug=False,
        threaded=True
    )
