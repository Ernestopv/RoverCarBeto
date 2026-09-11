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
# CONFIGURACION Y LOGGING
# ============================================================

app = Flask(__name__)

ROVER_IP = os.environ.get("ROVER_IP", "192.168.4.1")
ROVER_URL = f"http://{ROVER_IP}/js"
API_HOST = "0.0.0.0"
API_PORT = 5000

MAX_SPEED = 1.0
HEARTBEAT_INTERVAL = 0.5
REQUEST_TIMEOUT = 1.0

# ============================================================
# CALIBRACION DE MOTORES
# ============================================================
#
# Si el rover se desvía:
#
#   - Si se va hacia la DERECHA:
#       normalmente el motor IZQUIERDO empuja más.
#       Baja LEFT_TRIM, por ejemplo a 0.95.
#
#   - Si se va hacia la IZQUIERDA:
#       normalmente el motor DERECHO empuja más.
#       Baja RIGHT_TRIM, por ejemplo a 0.95.
#
# Valores recomendados: 0.80 .. 1.00
#
LEFT_TRIM = float(os.environ.get("LEFT_TRIM", "1.0"))
RIGHT_TRIM = float(os.environ.get("RIGHT_TRIM", "1.0"))

# Evita valores peligrosos o absurdos.
LEFT_TRIM = max(0.0, min(1.0, LEFT_TRIM))
RIGHT_TRIM = max(0.0, min(1.0, RIGHT_TRIM))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger("rover-api")


# ============================================================
# ESTADO DEL ROVER
# ============================================================

state_lock = threading.Lock()

current_left = 0.0
current_right = 0.0
current_speed = 0.0
current_direction = "stop"
heartbeat_enabled = False

last_rover_response = None
last_error = None
last_command_time = 0.0


# ============================================================
# UTILIDADES
# ============================================================

def clamp(value, minimum=-1.0, maximum=1.0):
    """Limita un valor numerico al rango indicado."""
    return max(minimum, min(maximum, value))


def set_state(left, right, speed, direction):
    """Actualiza el estado interno del controlador de forma segura."""
    global current_left, current_right, current_speed
    global current_direction, last_command_time

    with state_lock:
        current_left = left
        current_right = right
        current_speed = speed
        current_direction = direction
        last_command_time = time.time()


def get_state():
    """Devuelve una copia del estado actual."""
    with state_lock:
        return {
            "left": current_left,
            "right": current_right,
            "speed": current_speed,
            "direction": current_direction,
            "heartbeat": heartbeat_enabled,
            "left_trim": LEFT_TRIM,
            "right_trim": RIGHT_TRIM,
            "last_command_time": last_command_time,
            "last_rover_response": last_rover_response,
            "last_error": last_error
        }


def parse_speed_param(data_dict):
    """Extrae y valida el parametro 'speed' de las peticiones HTTP."""
    if not isinstance(data_dict, dict):
        return None, "JSON invalido"

    speed_val = data_dict.get("speed", current_speed or 0.3)

    try:
        return clamp(float(speed_val), 0.0, MAX_SPEED), None
    except (TypeError, ValueError):
        return None, "Parametro 'speed' debe ser un numero valido"


def apply_motor_trim(left, right):
    """
    Aplica compensacion independiente a cada lado.

    Se aplica despues de calcular direccion y velocidad para que funcione
    igual en forward, backward, left, right y /move.
    """
    left = clamp(left * LEFT_TRIM)
    right = clamp(right * RIGHT_TRIM)
    return left, right


# ============================================================
# COMUNICACION CON WAVE ROVER
# ============================================================

def send_rover_command(left, right):
    """Envia peticiones HTTP al Wave Rover con L y R entre -1 y 1."""
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

        log.warning("Error comunicando con Wave Rover: %s", error)
        return None


def move_motors(left, right, speed=None, direction="custom"):
    """Control centralizado del movimiento y calibracion de motores."""
    global heartbeat_enabled

    left = clamp(left)
    right = clamp(right)

    if speed is not None:
        speed = clamp(speed, 0.0, MAX_SPEED)
        left *= speed
        right *= speed
    else:
        speed = max(abs(left), abs(right))

    # Compensacion para que el rover vaya recto.
    left, right = apply_motor_trim(left, right)

    heartbeat_enabled = True
    set_state(left, right, speed, direction)

    log.debug(
        "Movimiento %s: L=%.3f R=%.3f speed=%.3f",
        direction,
        left,
        right,
        speed
    )

    return send_rover_command(left, right)


def heartbeat_loop():
    """Reenvia periodicamente la ultima orden al Rover."""
    log.info("Heartbeat iniciado.")

    while True:
        try:
            if heartbeat_enabled:
                with state_lock:
                    l_val = current_left
                    r_val = current_right

                # Importante: aquí NO se aplica trim otra vez.
                send_rover_command(l_val, r_val)

            time.sleep(HEARTBEAT_INTERVAL)

        except Exception as e:
            log.exception("Error en heartbeat: %s", e)
            time.sleep(HEARTBEAT_INTERVAL)


# ============================================================
# RUTAS API
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
            return jsonify({"ok": False, "error": "El rover no devolvio el campo v", "raw": data}), 502
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
            "error": "JSON invalido"
        }), 400

    try:
        left = clamp(float(data.get("left", 0)))
        right = clamp(float(data.get("right", 0)))
    except (TypeError, ValueError):
        return jsonify({
            "ok": False,
            "error": "left y right deben ser numeros"
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
    global current_speed

    data = request.get_json(silent=True) or {}

    new_speed, error = parse_speed_param(data)

    if error:
        return jsonify({
            "ok": False,
            "error": error
        }), 400

    with state_lock:
        direction = current_direction
        old_left = current_left
        old_right = current_right

    if old_left == 0 and old_right == 0:
        with state_lock:
            current_speed = new_speed

        return jsonify({
            "ok": True,
            "speed": new_speed,
            "direction": "stop"
        })

    base_left = 1 if old_left > 0 else -1
    base_right = 1 if old_right > 0 else -1

    response = move_motors(
        base_left,
        base_right,
        speed=new_speed,
        direction=direction
    )

    return jsonify({
        "ok": True,
        "speed": new_speed,
        "direction": direction,
        "controller": get_state(),
        "rover_response": response
    })


@app.route("/api/rover/<direction_name>", methods=["POST"])
def directional_move(direction_name):
    """
    Direcciones estandar.

    CORRECCION:
      Antes LEFT y RIGHT estaban intercambiados para tu rover.

      forward  -> L + / R +
      backward -> L - / R -
      left     -> L + / R -
      right    -> L - / R +
    """

    directions = {
        "forward": (1, 1),
        "backward": (-1, -1),

        # Corregidos para tu orientación física.
        "left": (1, -1),
        "right": (-1, 1)
    }

    if direction_name not in directions:
        return jsonify({
            "ok": False,
            "error": "Endpoint no encontrado"
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

    heartbeat_enabled = False

    response = send_rover_command(0, 0)
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
# MANEJO DE ERRORES Y MAIN
# ============================================================

@app.errorhandler(404)
def not_found(error):
    return jsonify({
        "ok": False,
        "error": "Endpoint no encontrado"
    }), 404


@app.errorhandler(500)
def internal_error(error):
    return jsonify({
        "ok": False,
        "error": "Error interno del servidor"
    }), 500


if __name__ == "__main__":
    log.info("==========================================")
    log.info("         WAVE ROVER API")
    log.info("==========================================")
    log.info("Rover: %s", ROVER_URL)
    log.info("API:   http://0.0.0.0:%d", API_PORT)
    log.info("LEFT_TRIM:  %.3f", LEFT_TRIM)
    log.info("RIGHT_TRIM: %.3f", RIGHT_TRIM)

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
