# Public-feed jobs. Build from the repository root:
#   docker build -f jobs/Dockerfile -t riskpredict-jobs .
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HF_HOME=/tmp/huggingface

WORKDIR /app
COPY jobs/requirements*.txt jobs/
# Spike detection is always installed. The GLiNER2 tagging stack (CPU torch
# and the model libraries) only with:  --build-arg WITH_MODEL=true
ARG WITH_MODEL=false
RUN pip install -r jobs/requirements-analyse.txt && \
    if [ "$WITH_MODEL" = "true" ]; then \
      pip install --index-url https://download.pytorch.org/whl/cpu torch==2.15.0 && \
      pip install -r jobs/requirements-model.txt; \
    fi

COPY jobs/ jobs/
COPY infra/migrations/ infra/migrations/
RUN useradd --system --uid 10001 --no-create-home jobs && chmod +x jobs/entrypoint.sh
USER jobs

# Applies migrations, then runs once (or every RUN_EVERY_SECONDS if set).
ENTRYPOINT ["/app/jobs/entrypoint.sh"]
