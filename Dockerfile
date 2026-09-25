FROM python:3.12-slim-bookworm

ARG TARGETARCH

WORKDIR /app

# ------------------------------------------------------------
# Base runtime
# Used by all environments: DEV / QA / PROD
# ------------------------------------------------------------
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg \
        curl \
        nginx \
        ca-certificates \
        gnupg \
        wget \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir \
        flask \
        requests


# ------------------------------------------------------------
# Raspberry Pi AI Camera runtime
#
# Installed ONLY in linux/arm64 image for Raspberry Pi PROD.
#
# DEV / QA:
#   CAMERA_SOURCE=simulator
#   Uses FFmpeg only.
#
# PROD:
#   CAMERA_SOURCE=imx500
#   Uses rpicam-vid + IMX500 post-processing stages.
# ------------------------------------------------------------
RUN if [ "${TARGETARCH}" = "arm64" ]; then \
        echo "Installing Raspberry Pi AI Camera runtime for ARM64..." && \
        mkdir -p /usr/share/keyrings && \
        wget -qO- https://archive.raspberrypi.com/debian/raspberrypi.gpg.key \
          | gpg --dearmor -o /usr/share/keyrings/raspberrypi-archive-keyring.gpg && \
        echo "deb [arch=arm64 signed-by=/usr/share/keyrings/raspberrypi-archive-keyring.gpg] https://archive.raspberrypi.com/debian/ bookworm main" \
          > /etc/apt/sources.list.d/raspberrypi.list && \
        apt-get update && \
        apt-get install -y --no-install-recommends \
          rpicam-apps-lite \
          rpicam-apps-imx500-postprocess && \
        rm -rf /var/lib/apt/lists/*; \
    else \
        echo "Skipping Raspberry Pi AI Camera runtime for ${TARGETARCH}"; \
    fi


# ------------------------------------------------------------
# Application
# ------------------------------------------------------------
COPY api.py ui.py /app/

COPY static/ /app/static/

COPY simulator/ /app/simulator/

COPY nginx.conf /etc/nginx/nginx.conf

COPY entrypoint.sh /app/entrypoint.sh

RUN chmod +x /app/entrypoint.sh


# ------------------------------------------------------------
# Ports
#
# 5000 -> Flask API
# 8000 -> nginx public endpoint
# 8001 -> internal UI, not normally exposed externally
# 8081 -> rover simulator
# ------------------------------------------------------------
EXPOSE 5000 8000 8001 8081

ENTRYPOINT ["/app/entrypoint.sh"]