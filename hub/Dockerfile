# Hub image (API + built web GUI). Build context is the repository root:
#   docker build -f hub/Dockerfile .
FROM node:22-alpine AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

FROM python:3.12-slim

# tzdata: the hub's TZ setting (deploy/docker-compose.yml) needs the zone files.
RUN apt-get update && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.8 /uv /usr/local/bin/uv
ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1 PYTHONUNBUFFERED=1

WORKDIR /app/hub
COPY hub/pyproject.toml hub/uv.lock hub/README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY proto/ /app/proto/
COPY hub/ ./
RUN uv sync --frozen --no-dev
COPY --from=web /web/dist /app/web/dist

# the commit this image is built from: shown by GET /api/health (build_info.py)
ARG TOPCOAT_BUILD=unknown
ENV PATH=/app/hub/.venv/bin:$PATH \
    TOPCOAT_PROTO_ROOT=/app/proto \
    TOPCOAT_WEB_DIST=/app/web/dist \
    TOPCOAT_TLS_DIR=/data/tls \
    TOPCOAT_BUILD=${TOPCOAT_BUILD}

RUN useradd --system --uid 10001 topcoat && mkdir -p /data/tls /data/updates && chown -R topcoat /data
USER topcoat
# /data/tls keeps the hub CA (TLS mode "auto", ARCHITECTURE D11): mount a volume there, or
# every new container gets a new CA and all nodes have to pair again.
VOLUME ["/data/tls", "/data/updates"]
EXPOSE 8443
# Runs migrations, prepares TLS (auto | files | off) and serves HTTPS on :8443.
CMD ["python", "-m", "topcoat_hub.serve"]
