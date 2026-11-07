FROM python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates nodejs \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir pytest==9.2.0
COPY entry.py /opt/factory/entry.py
USER 65532:65532
WORKDIR /work
ENTRYPOINT ["python", "/opt/factory/entry.py"]
