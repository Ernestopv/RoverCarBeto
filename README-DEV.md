# Wave Rover - DEV integrado

Este entorno ejecuta:

- `ui.py` REAL en `http://localhost:8000`
- `api.py` REAL en `http://localhost:5000`
- Wave Rover 2D simulado en `http://localhost:8081`

## Arquitectura

```text
Browser
  |
  v
ui.py REAL :8000
  |              \
  |               \ camera source = simulator (FFmpeg)
  v
api.py REAL :5000
  |
  | protocolo /js?json=...
  v
Wave Rover Simulator :8081
  |
  +-- mapa 2D
  +-- física diferencial
  +-- batería
  +-- latencia
  +-- desconexiones/fallos
```

## Arrancar

```bash
cp .env.dev.example .env.dev

docker compose \
  --env-file .env.dev \
  -f docker-compose.dev.yml \
  up --build
```

Primera vez: ejecuta sin `-d` para ver los logs.

Después abre:

- Aplicación real: http://localhost:8000
- API real: http://localhost:5000
- Simulador 2D: http://localhost:8081

Abre `8000` y `8081` en dos pestañas. Los botones de `ui.py` deben mover el rover del mapa 2D.

## Ejecutar en background

```bash
docker compose \
  --env-file .env.dev \
  -f docker-compose.dev.yml \
  up -d --build
```

## Ver logs

```bash
docker compose \
  --env-file .env.dev \
  -f docker-compose.dev.yml \
  logs -f
```

Solo UI:

```bash
docker compose --env-file .env.dev -f docker-compose.dev.yml logs -f rover-ui
```

## Tests

```bash
python3 -m pip install pytest
pytest -q tests/test_dev.py
```

Se comprueba:
- servicios
- forward + movimiento físico
- left + giro visual a la izquierda
- right + giro visual a la derecha
- stop
- batería simulada
- latencia simulada
- stream MJPEG de cámara falsa

## Escenarios

Batería baja:

```bash
curl -X POST http://localhost:8081/scenario \
  -H 'Content-Type: application/json' \
  -d '{"voltage":10.4}'
```

Desconectar hardware:

```bash
curl -X POST http://localhost:8081/scenario \
  -H 'Content-Type: application/json' \
  -d '{"connected":false}'
```

Reconectar:

```bash
curl -X POST http://localhost:8081/scenario \
  -H 'Content-Type: application/json' \
  -d '{"connected":true}'
```

Latencia:

```bash
curl -X POST http://localhost:8081/scenario \
  -H 'Content-Type: application/json' \
  -d '{"latency_ms":500}'
```

## MODE

Puedes probar:

```env
MODE=pose
```

o:

```env
MODE=objects
```

En DEV la cámara es simulada. Todavía no estamos ejecutando la inferencia IMX500 real; eso se incorporará como un simulador de resultados AI en una siguiente fase.
