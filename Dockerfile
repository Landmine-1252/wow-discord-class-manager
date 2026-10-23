# syntax=docker/dockerfile:1.7@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e
FROM ghcr.io/astral-sh/uv:0.13.5@sha256:32a7bb39b496babbd997b0a39a4d66c126bdaf5661b302bf6f06abda8deca6ff AS uv
FROM python:3.14.7-slim-bookworm@sha256:82bc3c539b8813ada9d68c63b40158fa002f7f33de9bf3312a3dfdc0620dff56 AS build
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_COMPILE_BYTECODE=1
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project --no-build

FROM python:3.14.7-slim-bookworm@sha256:82bc3c539b8813ada9d68c63b40158fa002f7f33de9bf3312a3dfdc0620dff56 AS runtime
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
