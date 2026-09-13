#!/usr/bin/env bash
set -euo pipefail

# -------------------------------------------------------------------
# Runtime configuration
# -------------------------------------------------------------------
export ENVIRONMENT="${ENVIRONMENT:-prod}"
export SIMULATOR_ENABLED="${SIMULATOR_ENABLED:-false}"
export CAMERA_SOURCE="${CAMERA_SOURCE:-imx500}"
export MODE="${MODE:-objects}"

export SIM_PORT="${SIM_PORT:-8081}"
export UI_PORT="${UI_PORT:-8001}"

export API_BASE_URL="${API_BASE_URL:-http://127.0.0.1:5000/api/rover}"
export ROVER_IP="${ROVER_IP:-192.168.4.1}"

API_PID=""
SIM_PID=""
UI_PID=""
NGINX_PID=""

cleanup() {
  echo
  echo "Stopping rover services..."

  for pid in "${NGINX_PID}" "${UI_PID}" "${API_PID}" "${SIM_PID}"; do
    if [ -n "${pid}" ] && kill -0 "${pid}" 2>/dev/null; then
      kill "${pid}" 2>/dev/null || true
    fi
  done

  # Give processes a moment to stop cleanly.
  sleep 1

  for pid in "${NGINX_PID}" "${UI_PID}" "${API_PID}" "${SIM_PID}"; do
    if [ -n "${pid}" ]; then
      wait "${pid}" 2>/dev/null || true
    fi
  done
}

shutdown() {
  exit 0
}

trap shutdown INT TERM
trap cleanup EXIT

echo "============================================================"
echo " Rover AI"
echo "============================================================"
echo "Environment       : ${ENVIRONMENT}"
echo "Simulator enabled : ${SIMULATOR_ENABLED}"
echo "Camera source     : ${CAMERA_SOURCE}"
echo "Mode              : ${MODE}"
echo "Rover endpoint    : ${ROVER_IP}"
echo "API URL           : ${API_BASE_URL}"
echo "Simulator port    : ${SIM_PORT}"
echo "UI internal port  : ${UI_PORT}"
echo "Public proxy port : 8000"
echo "============================================================"

# -------------------------------------------------------------------
# Wave Rover simulator (DEV / QA only)
# -------------------------------------------------------------------
if [ "${SIMULATOR_ENABLED}" = "true" ]; then
  echo "Starting Wave Rover simulator on port ${SIM_PORT}..."
  python3 /app/simulator/rover_simulator.py &
  SIM_PID=$!

  for i in $(seq 1 30); do
    if curl -fsS "http://127.0.0.1:${SIM_PORT}/health" >/dev/null 2>&1; then
      echo "Wave Rover simulator is ready."
      break
    fi

    if [ "$i" -eq 30 ]; then
      echo "ERROR: Wave Rover simulator did not become ready."
      exit 1
    fi

    sleep 1
  done
fi

# -------------------------------------------------------------------
# Flask API
# -------------------------------------------------------------------
echo "Starting Rover API on port 5000..."
python3 /app/api.py &
API_PID=$!

for i in $(seq 1 30); do
  if curl -fsS "http://127.0.0.1:5000/health" >/dev/null 2>&1; then
    echo "Rover API is ready."
    break
  fi

  if [ "$i" -eq 30 ]; then
    echo "ERROR: Rover API did not become ready."
    exit 1
  fi

  sleep 1
done

# -------------------------------------------------------------------
# Rover UI (internal only)
# -------------------------------------------------------------------
echo "Starting Rover UI on internal port ${UI_PORT}..."
python3 /app/ui.py "${MODE}" &
UI_PID=$!

for i in $(seq 1 30); do
  if curl -fsS "http://127.0.0.1:${UI_PORT}/" >/dev/null 2>&1; then
    echo "Rover UI is ready."
    break
  fi

  if [ "$i" -eq 30 ]; then
    echo "ERROR: Rover UI did not become ready."
    exit 1
  fi

  sleep 1
done

# -------------------------------------------------------------------
# nginx reverse proxy
#   /            -> UI        :8001
#   /api/        -> API       :5000
#   /simulator/  -> Simulator :8081
# -------------------------------------------------------------------
echo "Validating nginx configuration..."
nginx -t

echo "Starting nginx reverse proxy on public port 8000..."
nginx -g 'daemon off;' &
NGINX_PID=$!

for i in $(seq 1 30); do
  if curl -fsS "http://127.0.0.1:8000/health" >/dev/null 2>&1; then
    echo "nginx reverse proxy is ready."
    break
  fi

  if [ "$i" -eq 30 ]; then
    echo "ERROR: nginx did not become ready."
    exit 1
  fi

  sleep 1
done

echo "============================================================"
echo " Rover AI is ready"
echo " UI        : http://0.0.0.0:8000/"
echo " API       : http://0.0.0.0:8000/api/rover/"
if [ "${SIMULATOR_ENABLED}" = "true" ]; then
  echo " Simulator : http://0.0.0.0:8000/simulator/"
fi
echo " Health    : http://0.0.0.0:8000/health"
echo "============================================================"

PIDS=("${NGINX_PID}" "${UI_PID}" "${API_PID}")
if [ -n "${SIM_PID}" ]; then
  PIDS+=("${SIM_PID}")
fi

set +e
wait -n "${PIDS[@]}"
STATUS=$?
set -e

echo "ERROR: One of the rover services exited unexpectedly (status=${STATUS})."
exit "${STATUS}"
