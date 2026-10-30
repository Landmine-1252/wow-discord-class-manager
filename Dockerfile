# syntax=docker/dockerfile:1.7@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e
FROM ghcr.io/astral-sh/uv:0.13.6@sha256:af22a52f5eafef3d78e551599d405c02df61919d18028f0cea8eda93bc9986d0 AS uv
FROM python:3.13.17-slim-bookworm@sha256:e32be0e55814a08977501ca863271d9bd02443710db8f2afaf8372e2c4508530 AS build
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_COMPILE_BYTECODE=1
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project --no-build

FROM python:3.13.17-slim-bookworm@sha256:e32be0e55814a08977501ca863271d9bd02443710db8f2afaf8372e2c4508530 AS runtime
ARG VERSION=1.1.0
ARG REVISION=unknown
LABEL org.opencontainers.image.title="WoW Discord Class Manager" \
      org.opencontainers.image.description="Self-hosted World of Warcraft character rosters and main-class colors for Discord" \
      org.opencontainers.image.source="https://github.com/landmine-1252/wow-discord-class-manager" \
      org.opencontainers.image.url="https://github.com/landmine-1252/wow-discord-class-manager" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${REVISION}"
RUN groupadd --gid 10001 bot \
    && useradd --uid 10001 --gid bot --no-create-home --shell /usr/sbin/nologin bot \
    && mkdir /data \
    && chown bot:bot /data \
    && chmod 700 /data
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
COPY src /app/src
COPY LICENSE /app/LICENSE
ENV PATH="/app/.venv/bin:$PATH" PYTHONPATH=/app/src \
    PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
USER 10001:10001
STOPSIGNAL SIGTERM
HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
    CMD ["python", "-m", "wow_class_manager.health"]
CMD ["python", "-m", "wow_class_manager"]
