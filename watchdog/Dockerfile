FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY aqms_watchdog ./aqms_watchdog

# watchdog.yaml is mounted at /app/watchdog.yaml by docker-compose; secrets come from env.
CMD ["python", "-m", "aqms_watchdog.main", "--config", "/app/watchdog.yaml"]
