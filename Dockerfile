FROM node:26.10.0-alpine@sha256:0b36e8c136b94cd4fcf02188228e76c31ad5872eef3fec8cbd2eee500cfd9e80 AS web-build
WORKDIR /web
RUN npm install --global npm@12.2.0
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

FROM ghcr.io/astral-sh/uv:0.12.21@sha256:a7aed3216253ee804de3e2d8afa5073baa1a177335345d43845cd4165e43b711 AS dependency-tools

FROM python:3.14.8-slim-trixie@sha256:c3e521df8b2b498a7a682e7e18676771cb80c6b75b8699af886b2d554ce40151 AS runtime-build
# Debian's reviewed PCRE2 security fix postdates this immutable base image.
RUN apt-get update -qq \
    && apt-get install -y --no-install-recommends libpcre2-8-0=10.46-1~deb13u3 \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE NOTICE ./
COPY src ./src
ARG KNOWLEDGE_RUNTIME_EXTRAS=""
RUN --mount=from=dependency-tools,source=/uv,target=/usr/local/bin/uv \
    --mount=type=cache,target=/root/.cache/uv \
    set -eu; set --; \
    for extra in $KNOWLEDGE_RUNTIME_EXTRAS; do \
        case "$extra" in iceberg|objects|clickhouse|snowflake|bigquery|drive|search|semantic-validation) ;; \
            *) echo "Unknown runtime extra" >&2; exit 1 ;; esac; \
        set -- "$@" --extra "$extra"; \
    done; \
    uv sync --frozen --no-dev --link-mode=copy "$@"
COPY --from=web-build /web/dist ./web/dist
COPY scripts/build_runtime_root.py /tmp/build_runtime_root.py
COPY scripts/apply_runtime_patches.py /tmp/apply_runtime_patches.py
COPY third_party/runtime-patches /tmp/runtime-patches
RUN python /tmp/build_runtime_root.py /runtime-root

# Keep the same glibc/CPython ABI and real package evidence, without OS tools.
FROM scratch AS runtime
COPY --from=runtime-build /runtime-root /
WORKDIR /app
ENV PATH="/app/.venv/bin:/usr/local/bin" PYTHONPATH="/app/src" KNOWLEDGE_WEB_DIST="/app/web/dist" PYTHONDONTWRITEBYTECODE="1" LANG="C.UTF-8"
USER 10001:10001
EXPOSE 8000
CMD ["uvicorn", "knowledge_layer.platform:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log", "--limit-concurrency", "64", "--timeout-keep-alive", "5"]
