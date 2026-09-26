#!/usr/bin/env python3

# -*- coding: utf-8 -*-

import json

import os

import logging

import threading

import time
from collections import deque
from statistics import median

from flask import Flask, jsonify, request

import requests


# ============================================================

# CONFIGURATION AND LOGGING

# ============================================================



app = Flask(__name__)

ROVER_IP = os.environ.get("ROVER_IP", "192.168.4.1")

ROVER_URL = f"http://{ROVER_IP}/js"

API_HOST = "0.0.0.0"

API_PORT = 5000

MAX_SPEED = 1.0  # Normalized API/UI speed: 0.0 .. 1.0



# WAVE ROVER CMD_SPEED_CTRL (T=1) accepts -0.5 .. +0.5.

# 0.5 is 100% motor command, so normalized UI/API values are scaled by 0.5.

ROVER_COMMAND_MAX = 0.5

HEARTBEAT_INTERVAL = 0.5

WATCHDOG_TIMEOUT = 2.0

REQUEST_TIMEOUT = 1.0

# ============================================================
# BATTERY ESTIMATION (3S Li-ion, 3000 mAh cells)
# ============================================================

BATTERY_CELLS = int(os.environ.get("BATTERY_CELLS", "3"))
BATTERY_AH = float(os.environ.get("BATTERY_AH", "3.0"))
BATTERY_CELL_NOMINAL_V = float(os.environ.get("BATTERY_CELL_NOMINAL_V", "3.7"))
BATTERY_TOTAL_WH = BATTERY_CELLS * BATTERY_AH * BATTERY_CELL_NOMINAL_V
BATTERY_SAMPLE_WINDOW = float(os.environ.get("BATTERY_SAMPLE_WINDOW", "60"))
BATTERY_MIN_POWER_W = float(os.environ.get("BATTERY_MIN_POWER_W", "0.5"))

# Approximate open-circuit voltage curve for a typical Li-ion cell.
# The rover is a 3S pack, so the code converts pack voltage to per-cell voltage.
# This is intentionally an estimate: motor/load sag and cell chemistry affect SOC.
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





# ============================================================

# MOTOR CALIBRATION

# ============================================================

#

# If the rover drifts:

#

#   - If it drifts to the RIGHT:

#       the LEFT motor is usually pushing harder.

#       Lower LEFT_TRIM, for example to 0.95.

#

#   - If it drifts to the LEFT:

#       the RIGHT motor is usually pushing harder.

#       Lower RIGHT_TRIM, for example to 0.95.

#

# Recommended values: 0.80 .. 1.00

#

LEFT_TRIM = float(os.environ.get("LEFT_TRIM", "1.0"))

RIGHT_TRIM = float(os.environ.get("RIGHT_TRIM", "1.0"))



# Avoid dangerous or unreasonable values.

LEFT_TRIM = max(0.0, min(1.0, LEFT_TRIM))

RIGHT_TRIM = max(0.0, min(1.0, RIGHT_TRIM))

logging.basicConfig(

    level=logging.INFO,

    format="%(asctime)s [%(levelname)s] %(message)s"

)

log = logging.getLogger("rover-api")





# ============================================================

# ROVER STATE

# ============================================================



state_lock = threading.RLock()

command_lock = threading.Lock()

current_left = 0.0

current_right = 0.0

current_speed = 0.0

selected_speed = 0.3

current_direction = "stop"

heartbeat_enabled = False

last_rover_response = None

last_error = None

last_command_time = 0.0

# Recent battery samples used to smooth voltage and power estimates.
battery_lock = threading.Lock()
battery_samples = deque()





# ============================================================

# UTILITIES

# ============================================================





def clamp(value, minimum=-1.0, maximum=1.0):

    """Clamp a numeric value to the specified range."""

    return max(minimum, min(maximum, value))





def set_state(left, right, speed, direction, selected=None):

    """Safely update the controller internal state."""

    global current_left, current_right, current_speed

    global selected_speed, current_direction, last_command_time

    with state_lock:

        current_left = left

        current_right = right

        current_speed = speed

        if selected is not None:

            selected_speed = selected

        current_direction = direction

        last_command_time = time.time()





def get_state():

    """Return a copy of the current state."""

    with state_lock:

        return {

            "left": current_left,

            "right": current_right,

            "speed": current_speed,

            "selected_speed": selected_speed,

            "direction": current_direction,

            "heartbeat": heartbeat_enabled,

            "watchdog_timeout": WATCHDOG_TIMEOUT,

            "left_trim": LEFT_TRIM,

            "right_trim": RIGHT_TRIM,

            "last_command_time": last_command_time,

            "last_rover_response": last_rover_response,

            "last_error": last_error

        }





def normalize_rover_voltage(raw_voltage):
    """Normalize known WAVE ROVER voltage formats to volts."""
    voltage = float(raw_voltage)

    # Modified firmware returns volts directly (for example 12.04).
    if 8.0 <= voltage <= 20.0:
        return voltage

    # Some firmware variants report hundredths of a volt (for example 1204).
    if 800.0 <= voltage <= 2000.0:
        return voltage / 100.0

    # Also accept millivolt-style values if encountered.
    if 8000.0 <= voltage <= 20000.0:
        return voltage / 1000.0

    # Backward-compatible fallback with the previous endpoint behavior.
    if voltage > 100.0:
        return voltage / 100.0

    return voltage


def battery_percent_from_voltage(pack_voltage):
    """Estimate 3S Li-ion state of charge using piecewise interpolation."""
    cell_voltage = pack_voltage / BATTERY_CELLS

    if cell_voltage <= BATTERY_SOC_CURVE[0][0]:
        return 0.0
    if cell_voltage >= BATTERY_SOC_CURVE[-1][0]:
        return 100.0

    for (v1, p1), (v2, p2) in zip(BATTERY_SOC_CURVE, BATTERY_SOC_CURVE[1:]):
        if v1 <= cell_voltage <= v2:
            ratio = (cell_voltage - v1) / (v2 - v1)
            return p1 + ratio * (p2 - p1)

    return 0.0


def battery_level_name(percent):
    """Human-friendly level name without trying to infer charging state."""
    if percent >= 95:
        return "full"
    if percent >= 70:
        return "high"
    if percent >= 40:
        return "medium"
    if percent >= 20:
        return "low"
    return "critical"


def format_remaining_time(hours):
    if hours is None:
        return None

    total_minutes = max(0, int(round(hours * 60)))
    h, m = divmod(total_minutes, 60)

    if h:
        return f"{h}h {m:02d}m"
    return f"{m}m"


def update_battery_samples(voltage, power_w):
    """Keep roughly the last BATTERY_SAMPLE_WINDOW seconds of readings."""
    now = time.time()

    with battery_lock:
        battery_samples.append((now, voltage, power_w))

        cutoff = now - BATTERY_SAMPLE_WINDOW
        while battery_samples and battery_samples[0][0] < cutoff:
            battery_samples.popleft()

        voltages = [sample[1] for sample in battery_samples]
        powers = [sample[2] for sample in battery_samples if sample[2] is not None]

        # Median voltage is less sensitive to short motor-induced voltage dips.
        filtered_voltage = median(voltages) if voltages else voltage
        average_power_w = (sum(powers) / len(powers)) if powers else None

        return filtered_voltage, average_power_w, len(battery_samples)


def parse_speed_param(data_dict):

    """Extract and validate the 'speed' parameter from HTTP requests."""

    if not isinstance(data_dict, dict):

        return None, "Invalid JSON"

    with state_lock:

        default_speed = selected_speed

    speed_val = data_dict.get("speed", default_speed)

    try:

        return clamp(float(speed_val), 0.0, MAX_SPEED), None

    except (TypeError, ValueError):

        return None, "Parameter 'speed' must be a valid number"





def apply_motor_trim(left, right):

    """

    Apply independent compensation to each side.

    It is applied after calculating direction and speed so it works

    the same for forward, backward, left, right, and /move.

    """

    left = clamp(left * LEFT_TRIM)

    right = clamp(right * RIGHT_TRIM)

    return left, right





# ============================================================

# COMMUNICATION WITH WAVE ROVER

# ============================================================





def send_rover_command(left, right):

    """Send normalized -1..1 values using WAVE ROVER's -0.5..0.5 T=1 range."""

    global last_rover_response, last_error

    # Public API/UI values are normalized: -1.0 .. +1.0.

    # The physical WAVE ROVER expects T=1 L/R in -0.5 .. +0.5.

    normalized_left = clamp(float(left))

    normalized_right = clamp(float(right))

    rover_left = clamp(

        normalized_left * ROVER_COMMAND_MAX,

        -ROVER_COMMAND_MAX,

        ROVER_COMMAND_MAX,

    )

    rover_right = clamp(

        normalized_right * ROVER_COMMAND_MAX,

        -ROVER_COMMAND_MAX,

        ROVER_COMMAND_MAX,

    )

    payload = {

        "T": 1,

        "L": rover_left,

        "R": rover_right,

    }

    log.debug(

        "Rover command: normalized L=%.3f R=%.3f -> physical L=%.3f R=%.3f",

        normalized_left,

        normalized_right,

        rover_left,

        rover_right,

    )

    try:

        response = requests.get(

            ROVER_URL,

            params={"json": json.dumps(payload)},

            timeout=REQUEST_TIMEOUT

        )

        response.raise_for_status()

        rover_response = response.text

        with state_lock:

            last_rover_response = rover_response

            last_error = None

        return rover_response

    except requests.RequestException as e:

        error = str(e)

        with state_lock:

            last_error = error

        log.warning("Error communicating with Wave Rover: %s", error)

        return None





def move_motors(left, right, speed=None, direction="custom"):

    """Centralized movement control and motor calibration."""

    global heartbeat_enabled

    left = clamp(left)

    right = clamp(right)

    if speed is not None:

        speed = clamp(speed, 0.0, MAX_SPEED)

        left *= speed

        right *= speed

    else:

        speed = max(abs(left), abs(right))

    # Compensation to keep the rover driving straight.

    left, right = apply_motor_trim(left, right)

    with command_lock:

        with state_lock:

            heartbeat_enabled = True

        # Remember the requested speed independently from the physical state.

        set_state(left, right, speed, direction, selected=speed)

        log.debug(

            "Movement %s: L=%.3f R=%.3f speed=%.3f",

            direction,

            left,

            right,

            speed

        )

        return send_rover_command(left, right)





def heartbeat_loop():

    """Resend active commands and stop if control messages go stale."""

    global current_left, current_right, current_speed

    global current_direction, heartbeat_enabled

    log.info("Heartbeat started.")

    while True:

        try:

            command = None

            watchdog_stop = False

            with command_lock:

                with state_lock:

                    if heartbeat_enabled:

                        elapsed = time.time() - last_command_time

                        if elapsed >= WATCHDOG_TIMEOUT:

                            heartbeat_enabled = False

                            current_left = 0.0

                            current_right = 0.0

                            current_speed = 0.0

                            current_direction = "stop"

                            command = (0.0, 0.0)

                            watchdog_stop = True

                        else:

                            command = (current_left, current_right)

                if command is not None:

                    # Important: trim is NOT applied again here.

                    send_rover_command(*command)

            if watchdog_stop:

                log.warning(

                    "Watchdog stopped the rover after %.1f seconds without commands.",

                    WATCHDOG_TIMEOUT

                )

            time.sleep(HEARTBEAT_INTERVAL)

        except Exception as e:

            log.exception("Heartbeat error: %s", e)

            time.sleep(HEARTBEAT_INTERVAL)





def confirm_stop_after_pending_commands():

    """Send a final stop after any command that was already in progress."""

    with command_lock:

        with state_lock:

            still_stopped = not heartbeat_enabled and current_direction == "stop"

        if still_stopped:

            send_rover_command(0, 0)





# ============================================================

# API ROUTES

# ============================================================





@app.route("/", methods=["GET"])

def index():

    return jsonify({

        "ok": True,

        "service": "Wave Rover API",

        "version": "1.2",

        "rover_ip": ROVER_IP,

        "calibration": {

            "left_trim": LEFT_TRIM,

            "right_trim": RIGHT_TRIM

        },

        "endpoints": {

            "status": "GET /api/rover/status",
            "battery": "GET /api/rover/battery",

            "move": "POST /api/rover/move",

            "speed": "POST /api/rover/speed",

            "forward": "POST /api/rover/forward",

            "backward": "POST /api/rover/backward",

            "left": "POST /api/rover/left",

            "right": "POST /api/rover/right",

            "stop": "POST /api/rover/stop"

        }

    })





@app.route("/health", methods=["GET"])

def health():

    return jsonify({"ok": True, "service": "rover-api"}), 200





@app.route("/api/rover/battery", methods=["GET"])
def battery():
    payload = {"T": 130}

    try:
        response = requests.get(
            ROVER_URL,
            params={"json": json.dumps(payload)},
            timeout=REQUEST_TIMEOUT
        )
        response.raise_for_status()
        data = response.json()

        raw_voltage = data.get("v")
        if raw_voltage is None:
            return jsonify({
                "ok": False,
                "error": "The rover did not return the v field",
                "raw": data
            }), 502

        voltage = normalize_rover_voltage(raw_voltage)

        # Sanity check for a 3S Li-ion pack. This catches malformed telemetry
        # without rejecting normal loaded/full values.
        if not 8.0 <= voltage <= 13.5:
            return jsonify({
                "ok": False,
                "error": f"Unreasonable 3S battery voltage: {voltage:.3f} V",
                "raw": data
            }), 502

        raw_current_ma = data.get("c")
        raw_power_mw = data.get("pwr")

        current_ma = None
        current_a = None
        if raw_current_ma is not None:
            current_ma = float(raw_current_ma)
            current_a = current_ma / 1000.0

        power_w = None
        if raw_power_mw is not None:
            power_w = abs(float(raw_power_mw)) / 1000.0
        elif current_a is not None:
            # Fallback for firmware that returns current but not pwr.
            power_w = abs(voltage * current_a)

        filtered_voltage, average_power_w, sample_count = update_battery_samples(
            voltage,
            power_w
        )

        level_percent = battery_percent_from_voltage(filtered_voltage)
        level_percent = max(0.0, min(100.0, level_percent))
        level_name = battery_level_name(level_percent)

        remaining_wh = BATTERY_TOTAL_WH * (level_percent / 100.0)

        estimated_hours = None
        estimated_minutes = None
        estimated_human = None

        if average_power_w is not None and average_power_w >= BATTERY_MIN_POWER_W:
            estimated_hours = remaining_wh / average_power_w
            estimated_minutes = int(round(estimated_hours * 60))
            estimated_human = format_remaining_time(estimated_hours)

        estimate_quality = "warming_up" if sample_count < 5 else "stable"

        result = {
            "ok": True,
            "voltage": round(voltage, 3),
            "filtered_voltage": round(filtered_voltage, 3),
            "level_percent": round(level_percent, 1),
            "level": level_name,
            "current_ma": round(current_ma, 1) if current_ma is not None else None,
            "current_a": round(current_a, 3) if current_a is not None else None,
            "power_w": round(power_w, 3) if power_w is not None else None,
            "average_power_w": round(average_power_w, 3) if average_power_w is not None else None,
            "energy_remaining_wh": round(remaining_wh, 2),
            "estimated_remaining_hours": round(estimated_hours, 2) if estimated_hours is not None else None,
            "estimated_remaining_minutes": estimated_minutes,
            "estimated_remaining": estimated_human,
            "samples": sample_count,
            "estimate_quality": estimate_quality,
            "battery_config": {
                "cells": BATTERY_CELLS,
                "capacity_ah": BATTERY_AH,
                "nominal_voltage": round(BATTERY_CELLS * BATTERY_CELL_NOMINAL_V, 2),
                "total_energy_wh": round(BATTERY_TOTAL_WH, 2),
                "smoothing_window_seconds": BATTERY_SAMPLE_WINDOW
            },
            "estimate_note": (
                "Battery percentage is estimated from 3S Li-ion voltage; "
                "remaining time uses the rover-reported average power and may vary with motor load."
            ),
            "raw_voltage": raw_voltage,
            "raw_current_ma": raw_current_ma,
            "raw_power_mw": raw_power_mw
        }

        return jsonify(result)

    except (TypeError, ValueError) as e:
        return jsonify({
            "ok": False,
            "error": f"Invalid battery telemetry: {e}"
        }), 502
    except requests.RequestException as e:
        return jsonify({
            "ok": False,
            "error": f"Unable to contact Wave Rover: {e}"
        }), 502
    except Exception as e:
        log.exception("Battery endpoint error: %s", e)
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/rover/status", methods=["GET"])

def status():

    payload = {"T": 130}

    try:

        response = requests.get(

            ROVER_URL,

            params={"json": json.dumps(payload)},

            timeout=REQUEST_TIMEOUT

        )

        response.raise_for_status()

        return jsonify({

            "ok": True,

            "rover": response.json(),

            "controller": get_state()

        })

    except Exception as e:

        return jsonify({

            "ok": False,

            "error": str(e),

            "controller": get_state()

        }), 500





@app.route("/api/rover/move", methods=["POST"])

def move():

    data = request.get_json(silent=True)

    if not isinstance(data, dict):

        return jsonify({

            "ok": False,

            "error": "Invalid JSON"

        }), 400

    try:

        left = clamp(float(data.get("left", 0)))

        right = clamp(float(data.get("right", 0)))

    except (TypeError, ValueError):

        return jsonify({

            "ok": False,

            "error": "left and right must be numbers"

        }), 400

    response = move_motors(

        left,

        right,

        direction="custom"

    )

    state = get_state()

    return jsonify({

        "ok": True,

        "left": state["left"],

        "right": state["right"],

        "rover_response": response

    })





@app.route("/api/rover/speed", methods=["POST"])

def speed():

    global current_speed, selected_speed, heartbeat_enabled

    data = request.get_json(silent=True) or {}

    new_speed, error = parse_speed_param(data)

    if error:

        return jsonify({

            "ok": False,

            "error": error

        }), 400

    with command_lock:

        with state_lock:

            direction = current_direction

            old_left = current_left

            old_right = current_right

            old_speed = current_speed

        if old_left == 0 and old_right == 0:

            with state_lock:

                selected_speed = new_speed

            return jsonify({

                "ok": True,

                "speed": new_speed,

                "direction": "stop",

                "controller": get_state()

            })

        if old_speed <= 0:

            return jsonify({

                "ok": False,

                "error": "Unable to determine current speed"

            }), 409

        ratio = new_speed / old_speed

        new_left = clamp(old_left * ratio)

        new_right = clamp(old_right * ratio)

        new_direction = direction if new_speed > 0 else "stop"

        # current_left/current_right already include motor trim, so do not apply

        # it a second time while changing speed.

        if new_speed == 0:

            with state_lock:

                heartbeat_enabled = False

        set_state(

            new_left,

            new_right,

            new_speed,

            new_direction,

            selected=new_speed

        )

        response = send_rover_command(new_left, new_right)

    return jsonify({

        "ok": True,

        "speed": new_speed,

        "direction": new_direction,

        "controller": get_state(),

        "rover_response": response

    })





@app.route("/api/rover/<direction_name>", methods=["POST"])

def directional_move(direction_name):

    """

    Standard directions.

    CORRECTION:

      Previously LEFT and RIGHT were swapped for your rover.

      forward  -> L + / R +

      backward -> L - / R -

      left     -> L - / R +

      right    -> L + / R -

    """

    directions = {

        "forward": (1, 1),

        "backward": (-1, -1),

        "left": (-1, 1),

        "right": (1, -1)

    }

    if direction_name not in directions:

        return jsonify({

            "ok": False,

            "error": "Endpoint not found"

        }), 404

    data = request.get_json(silent=True) or {}

    speed_value, error = parse_speed_param(data)

    if error:

        return jsonify({

            "ok": False,

            "error": error

        }), 400

    l_mult, r_mult = directions[direction_name]

    response = move_motors(

        l_mult,

        r_mult,

        speed=speed_value,

        direction=direction_name

    )

    state = get_state()

    return jsonify({

        "ok": True,

        "direction": direction_name,

        "speed": speed_value,

        "left": state["left"],

        "right": state["right"],

        "left_trim": LEFT_TRIM,

        "right_trim": RIGHT_TRIM,

        "rover_response": response

    })





@app.route("/api/rover/stop", methods=["POST"])

def stop():

    global heartbeat_enabled

    # Give STOP priority: update state and contact the rover immediately,

    # without waiting for a heartbeat or movement request holding command_lock.

    with state_lock:

        heartbeat_enabled = False

        set_state(0, 0, 0, "stop")

    response = send_rover_command(0, 0)

    # If an older movement command was already in progress, send a second stop

    # after it finishes so a stale command can never become the final command.

    threading.Thread(

        target=confirm_stop_after_pending_commands,

        daemon=True

    ).start()

    return jsonify({

        "ok": True,

        "direction": "stop",

        "speed": 0,

        "left": 0,

        "right": 0,

        "rover_response": response

    })





# ============================================================

# ERROR HANDLING AND MAIN

# ============================================================





@app.errorhandler(404)

def not_found(error):

    return jsonify({

        "ok": False,

        "error": "Endpoint not found"

    }), 404





@app.errorhandler(500)

def internal_error(error):

    return jsonify({

        "ok": False,

        "error": "Internal server error"

    }), 500





if __name__ == "__main__":

    log.info("==========================================")

    log.info("         WAVE ROVER API")

    log.info("==========================================")

    log.info("Rover: %s", ROVER_URL)

    log.info("API:   http://0.0.0.0:%d", API_PORT)

    log.info("LEFT_TRIM:  %.3f", LEFT_TRIM)

    log.info("RIGHT_TRIM: %.3f", RIGHT_TRIM)

    log.info("ROVER T=1 MAX: %.2f (UI/API 1.00 -> rover %.2f)", ROVER_COMMAND_MAX, ROVER_COMMAND_MAX)

    log.info("WATCHDOG:   %.1f seconds", WATCHDOG_TIMEOUT)
    log.info(
        "BATTERY:    %dS %.1fAh %.1fWh, smoothing %.0fs",
        BATTERY_CELLS,
        BATTERY_AH,
        BATTERY_TOTAL_WH,
        BATTERY_SAMPLE_WINDOW
    )

    threading.Thread(

        target=heartbeat_loop,

        daemon=True

    ).start()

    app.run(

        host=API_HOST,

        port=API_PORT,

        debug=False,

        threaded=True

    )
