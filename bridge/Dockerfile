FROM python:3.12-slim

WORKDIR /app
COPY bridge.py .

RUN groupadd -r bridge && useradd -r -g bridge bridge -m && \
    chown bridge:bridge /app/bridge.py && \
    mkdir -p /data /remux-db && \
    chown bridge:bridge /data /remux-db

VOLUME /data
VOLUME /remux-db

USER bridge
CMD ["python3", "/app/bridge.py"]
