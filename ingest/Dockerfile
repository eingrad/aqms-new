FROM python:3.12-slim
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY aqms_ingest ./aqms_ingest

# config.yaml is mounted at /app/config.yaml by docker-compose; secrets come from env.
CMD ["python", "-m", "aqms_ingest.main", "--config", "/app/config.yaml"]
