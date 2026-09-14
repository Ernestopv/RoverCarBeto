FROM python:3.12-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg \
        curl \
        nginx \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir flask requests


COPY api.py ui.py index.html style.css /app/
COPY simulator/ /app/simulator/

COPY nginx.conf /etc/nginx/nginx.conf

COPY entrypoint.sh /app/entrypoint.sh

RUN chmod +x /app/entrypoint.sh

EXPOSE 5000 8000 8081

ENTRYPOINT ["/app/entrypoint.sh"]
