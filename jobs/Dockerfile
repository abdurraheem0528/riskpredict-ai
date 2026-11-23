# Public-feed jobs. Build from the repository root:
#   docker build -f jobs/Dockerfile -t riskpredict-jobs .
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY jobs/requirements.txt jobs/requirements.txt
RUN pip install -r jobs/requirements.txt

COPY jobs/ jobs/
COPY infra/migrations/ infra/migrations/
RUN useradd --system --uid 10001 --no-create-home jobs && chmod +x jobs/entrypoint.sh
USER jobs

# Applies migrations, then runs once (or every RUN_EVERY_SECONDS if set).
ENTRYPOINT ["/app/jobs/entrypoint.sh"]
