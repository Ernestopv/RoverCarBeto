FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir flask

COPY rover_simulator.py /app/rover_simulator.py

EXPOSE 80

CMD ["python3", "/app/rover_simulator.py"]
