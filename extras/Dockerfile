FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SENTINEL_STORAGE_BACKEND=sqlite \
    SENTINEL_DB_PATH=/var/lib/sentinel/sentinel.db \
    SENTINEL_BACKUP_DIR=/var/lib/sentinel/.reset-backups \
    SENTINEL_SEED_LEGACY_DATA=true \
    SENTINEL_WEBHOOK_HOST=0.0.0.0

WORKDIR /app

COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN mkdir -p /var/lib/sentinel && chown -R 10001:10001 /var/lib/sentinel

USER 10001:10001
EXPOSE 8501 8090

HEALTHCHECK --interval=20s --timeout=4s --start-period=45s --retries=3 \
    CMD ["python", "extras/ops/healthcheck.py", "ui"]

ENTRYPOINT ["streamlit", "run", "apps/sentinel/ui.py", "--server.address=0.0.0.0", "--server.port=8501", "--server.headless=true", "--browser.gatherUsageStats=false"]
