# API service. Build from the repository root:
#   docker build -f api/Dockerfile -t riskpredict-api .
# The engine/ package (session C) is copied in with its requirements; without it
# the API still runs (scenarios report that the engine is not installed).
FROM python:3.12-slim AS source
WORKDIR /src
COPY . .
# worker/: the API runs the same passes inside requests (api/work.py → worker/passes.py).
RUN mkdir /out && cp -r api jobs worker /out/ && \
    if [ -d engine ]; then cp -r engine /out/; fi && \
    find /out -name '__pycache__' -prune -exec rm -rf {} +

FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PORT=8000 \
    FORWARDED_ALLOW_IPS=127.0.0.1
WORKDIR /app
COPY api/requirements.txt api/requirements.txt
RUN pip install -r api/requirements.txt
COPY --from=source /out/ /app/
RUN if [ -f engine/requirements.txt ]; then pip install -r engine/requirements.txt; fi && \
    useradd --system --uid 10001 --no-create-home app
USER app
EXPOSE 8000
# No uvicorn access log: URLs can carry one-time secrets; api/main.py logs route templates only.
# FORWARDED_ALLOW_IPS: set it to the platform proxy's addresses (or "*" if only the
# platform's ingress can reach the container) so rate limits see the client IP.
CMD ["sh", "-c", "exec uvicorn api.main:app --host 0.0.0.0 --port \"$PORT\" --no-access-log --proxy-headers --timeout-graceful-shutdown 5"]
