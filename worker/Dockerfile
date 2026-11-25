# Worker service. Build from the repository root:
#   docker build -f worker/Dockerfile -t riskpredict-worker .
# It imports the shared code in api/core (no FastAPI needed) and the engine/ package.
FROM python:3.12-slim AS source
WORKDIR /src
COPY . .
RUN mkdir -p /out/api && cp -r worker jobs /out/ && cp api/__init__.py /out/api/ && cp -r api/core /out/api/ && \
    if [ -d engine ]; then cp -r engine /out/; fi && \
    find /out -name '__pycache__' -prune -exec rm -rf {} +

FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY worker/requirements.txt worker/requirements.txt
RUN pip install -r worker/requirements.txt
COPY --from=source /out/ /app/
RUN if [ -f engine/requirements.txt ]; then pip install -r engine/requirements.txt; fi && \
    useradd --system --uid 10001 --no-create-home worker
USER worker
CMD ["python", "-m", "worker"]
