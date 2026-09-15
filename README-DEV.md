# RoverCarBeto

This environment runs:

- REAL `ui.py` at [`http://localhost:8000`](http://localhost:8000)

- REAL `api.py` at [`http://localhost:5000`](http://localhost:5000)

- Simulated 2D Wave Rover at [`http://localhost:8081`](http://localhost:8081)

## Architecture

```text
Browser
  |
  v
REAL ui.py :8000
  |              \
  |               \ camera source = simulator (FFmpeg)
  v
REAL api.py :5000
  |
  | protocol /js?json=...
  v
Wave Rover Simulator :8081
  |
  +-- 2D map
  +-- differential-drive physics
  +-- battery
  +-- latency
  +-- disconnections/failures
Start
cp .env.dev.example .env.dev

docker compose \
  --env-file .env.dev \
  -f docker-compose.dev.yml \
  up --build

The first time, run it without -d so you can see the logs.

Then open:

Real application: http://localhost:8000
Real API: http://localhost:5000
2D simulator: http://localhost:8081

Open 8000 and 8081 in two browser tabs. The buttons in ui.py should move the rover on the 2D map.

Run in the background
docker compose \
  --env-file .env.dev \
  -f docker-compose.dev.yml \
  up -d --build
View logs
docker compose \
  --env-file .env.dev \
  -f docker-compose.dev.yml \
  logs -f

UI only:

docker compose --env-file .env.dev -f docker-compose.dev.yml logs -f rover-ui
Tests
python3 -m pip install pytest

pytest -q tests/test_dev.py

The following are verified:

services
forward + physical movement
left + visual left turn
right + visual right turn
stop
simulated battery
simulated latency
fake camera MJPEG stream
Scenarios

Low battery:

curl -X POST http://localhost:8081/scenario \
  -H 'Content-Type: application/json' \
  -d '{"voltage":10.4}'

Disconnect hardware:

curl -X POST http://localhost:8081/scenario \
  -H 'Content-Type: application/json' \
  -d '{"connected":false}'

Reconnect:

curl -X POST http://localhost:8081/scenario \
  -H 'Content-Type: application/json' \
  -d '{"connected":true}'

Latency:

curl -X POST http://localhost:8081/scenario \
  -H 'Content-Type: application/json' \
  -d '{"latency_ms":500}'
MODE

You can test:

MODE=pose

or:

MODE=objects

In DEV, the camera is simulated. We are not yet running real IMX500 inference; that will be added in a later phase as a simulator for AI results.
