# Recruence: one container serving the dashboard, API, MeetStream webhooks and MIA tool endpoints.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/data

WORKDIR /app

# Runtime dependencies only (the test packages at the end of requirements.txt are skipped).
# The PostgreSQL driver is included, so DATABASE_URL works without rebuilding.
COPY requirements.txt requirements-postgres.txt ./
RUN sed '/^# tests/,$d' requirements.txt > requirements.runtime.txt \
    && pip install -r requirements.runtime.txt -r requirements-postgres.txt \
    && rm requirements.txt requirements.runtime.txt requirements-postgres.txt

COPY app ./app
COPY scripts ./scripts

# Run as an unprivileged user. With SQLite, interviews, transcripts and reports live in /data: mount a volume there
# (both compose files do). No VOLUME instruction: some hosts refuse it (Railway), and a cloud backend on PostgreSQL
# keeps everything in the database, so /data needs no volume there (docs/CLOUD.md).
RUN useradd --create-home --uid 10001 mia \
    && mkdir -p /data && chown mia:mia /data
USER mia

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health/ready', timeout=4)"

CMD ["uvicorn", "app.server:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
