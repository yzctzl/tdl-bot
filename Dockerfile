# syntax=docker/dockerfile:1
FROM ghcr.io/astral-sh/uv:0.13.0 AS uv

FROM python:3.12-slim AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --python /usr/local/bin/python --locked --no-dev --no-editable \
    && /opt/venv/bin/tdl-bot --help > /dev/null

FROM python:3.12-slim AS runtime
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TDL_BOT_ENV_FILE=/workspace/.env \
    RUNTIME_DIR=/run/tdl-bot \
    HOME=/data/tdl-home
RUN groupadd --gid 1000 app \
    && useradd --uid 1000 --gid app --no-create-home app \
    && mkdir -p /workspace /data /tdl-bin /run/tdl-bot \
    && chown app:app /workspace /data /tdl-bin /run/tdl-bot
COPY --from=build /opt/venv /opt/venv
USER app
WORKDIR /workspace
HEALTHCHECK --interval=30s --timeout=10s --start-period=90s --retries=3 \
    CMD ["tdl-bot", "health"]
ENTRYPOINT ["tdl-bot"]
CMD ["container"]
