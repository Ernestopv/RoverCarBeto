FROM python:3.12-slim-bookworm

ARG TARGETARCH

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg \
        curl \
        nginx \
        ca-certificates \
        gnupg \
        wget \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir flask requests


# ------------------------------------------------------------
# Raspberry Pi camera runtime
# Only installed in the ARM64 image used by Raspberry Pi PROD.
# DEV / QA amd64 skip this section.
# ------------------------------------------------------------
RUN if [ "${TARGETARCH}" = "arm64" ]; then \
        echo "Installing Raspberry Pi camera runtime for ARM64..." && \
        mkdir -p /usr/share/keyrings && \
        wget -qO- https://archive.raspberrypi.com/debian/raspberrypi.gpg.key \
          | gpg --dearmor -o /usr/share/keyrings/raspberrypi-archive-keyring.gpg && \
        echo "deb [signed-by=/usr/share/keyrings/raspberrypi-archive-keyring.gpg] https://archive.raspberrypi.com/debian/ bookworm main" \
          > /etc/apt/sources.list.d/raspberrypi.list && \
        apt-get update && \
        apt-get install -y --no-install-recommends \
          rpicam-apps-lite && \
        rm -rf /var/lib/apt/lists/*; \
    else \
        echo "Skipping Raspberry Pi camera runtime for ${TARGETARCH}"; \
    fi


COPY api.py ui.py index.html style.css /app/

COPY simulator/ /app/simulator/

COPY nginx.conf /etc/nginx/nginx.conf

COPY entrypoint.sh /app/entrypoint.sh

RUN chmod +x /app/entrypoint.sh

EXPOSE 5000 8000 8081

ENTRYPOINT ["/app/entrypoint.sh"]