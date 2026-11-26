# API + worker in ONE container, for Render's free web service (background workers are
# not free there). Build from the repository root:
#   docker build -f infra/render/Dockerfile -t riskpredict-render .
# docker compose keeps using api/Dockerfile and worker/Dockerfile (two containers).
# start.sh runs the worker in the background and execs uvicorn on $PORT; if either
# process stops, the container exits and Render restarts it. The container has one
# public port, so /internal/* answers only the worker on 127.0.0.1 (INTERNAL_LOOPBACK_ONLY).
FROM python:3.12-slim AS source
WORKDIR /src
COPY . .
RUN mkdir /out && cp -r api worker jobs /out/ && \
    if [ -d engine ]; then cp -r engine /out/; fi && \
    find /out -name '__pycache__' -prune -exec rm -rf {} +

FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PORT=10000 \
    FORWARDED_ALLOW_IPS=127.0.0.1 \
    INTERNAL_LOOPBACK_ONLY=true \
    MALLOC_ARENA_MAX=2
WORKDIR /app
# api/requirements.txt is a superset of worker/requirements.txt (same pins).
COPY api/requirements.txt api/requirements.txt
COPY worker/requirements.txt worker/requirements.txt
RUN pip install -r api/requirements.txt -r worker/requirements.txt
COPY --from=source /out/ /app/
COPY infra/render/start.sh /app/start.sh
RUN if [ -f engine/requirements.txt ]; then pip install -r engine/requirements.txt; fi && \
    chmod 0755 /app/start.sh && \
    useradd --system --uid 10001 --no-create-home app
USER app
EXPOSE 10000
CMD ["/app/start.sh"]
