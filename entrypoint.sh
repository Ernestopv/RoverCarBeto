#!/usr/bin/env bash
set -euo pipefail

ENVIRONMENT="${ENVIRONMENT:-prod}"
SIMULATOR_ENABLED="${SIMULATOR_ENABLED:-false}"
CAMERA_SOURCE="${CAMERA_SOURCE:-imx500}"
MODE="${MODE:-objects}"

API_PID=""
SIM_PID=""

cleanup() {
  echo "Stopping rover services..."

  if [ -n "${API_PID}" ] && kill -0 "${API_PID}" 2>/dev/null; then
    kill "${API_PID}" 2>/dev/null || true
  fi

  if [ -n "${SIM_PID}" ] && kill -0 "${SIM_PID}" 2>/dev/null; then
    kill "${SIM_PID}" 2>/dev/null || true
  fi
}

trap cleanup EXIT INT TERM

echo "============================================================"
echo " Rover AI"
echo "============================================================"
echo "Environment       : ${ENVIRONMENT}"
echo "Simulator enabled : ${SIMULATOR_ENABLED}"
echo "Camera source     : ${CAMERA_SOURCE}"
echo "Mode              : ${MODE}"
echo "Rover endpoint    : ${ROVER_IP:-192.168.4.1}"
echo "============================================================"

if [ "${SIMULATOR_ENABLED}" = "true" ]; then
  echo "Starting Wave Rover simulator on port ${SIM_PORT:-8081}..."
  python3 /app/simulator/rover_simulator.py &
  SIM_PID=$!

  # Wait until the simulator is available before starting api.py.
  for i in $(seq 1 30); do
    if curl -fsS "http://127.0.0.1:${SIM_PORT:-8081}/health" >/dev/null 2>&1; then
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

echo "Starting Rover UI on port ${UI_PORT:-8000}..."
exec python3 /app/ui.py "${MODE}"
