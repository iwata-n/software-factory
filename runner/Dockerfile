FROM python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY entry.py /opt/factory/entry.py
USER 65532:65532
WORKDIR /work
ENTRYPOINT ["python", "/opt/factory/entry.py"]
