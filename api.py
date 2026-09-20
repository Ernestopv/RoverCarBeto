#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import os
import logging
import threading
import time

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

MAX_SPEED = 1.0
HEARTBEAT_INTERVAL = 0.5
WATCHDOG_TIMEOUT = 2.0
REQUEST_TIMEOUT = 1.0

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
    """Send HTTP requests to the Wave Rover with L and R between -1 and 1."""
    global last_rover_response, last_error

    left = clamp(float(left))
    right = clamp(float(right))

    payload = {
        "T": 1,
        "L": left,
        "R": right
    }

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


# ============================================================
# API ROUTES
# ============================================================

@app.route("/", methods=["GET"])
def index():
    return jsonify({
        "ok": True,
        "service": "Wave Rover API",
        "version": "1.1",
        "rover_ip": ROVER_IP,
        "calibration": {
            "left_trim": LEFT_TRIM,
            "right_trim": RIGHT_TRIM
        },
        "endpoints": {
            "status": "GET /api/rover/status",
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
        response = requests.get(ROVER_URL, params={"json": json.dumps(payload)}, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        data = response.json()
        raw_voltage = data.get("v")
        if raw_voltage is None:
            return jsonify({"ok": False, "error": "The rover did not return the v field", "raw": data}), 502
        voltage = float(raw_voltage)
        if voltage > 100:
            voltage /= 100.0
        return jsonify({"ok": True, "voltage": round(voltage, 2), "raw_voltage": raw_voltage})
    except Exception as e:
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
      left     -> L + / R -
      right    -> L - / R +
    """

    directions = {
        "forward": (1, 1),
        "backward": (-1, -1),

        # Corrected for your rover's physical orientation.
        "left": (1, -1),
        "right": (-1, 1)
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

    with command_lock:
        with state_lock:
            heartbeat_enabled = False

        response = send_rover_command(0, 0)

        # Stop the motors without forgetting the user's selected speed.
        set_state(0, 0, 0, "stop")

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
    log.info("WATCHDOG:   %.1f seconds", WATCHDOG_TIMEOUT)

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
