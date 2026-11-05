# API, worker, mock systems and the bootstrap task share one image; the command picks the role.
FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.12.22 /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --no-install-project
COPY src ./src
COPY migrations ./migrations
COPY alembic.ini ./
RUN uv sync --locked --no-dev

FROM python:3.12-slim
RUN useradd --create-home --uid 10001 recoup
WORKDIR /app
COPY --from=build --chown=recoup /app /app
COPY --chown=recoup datasets ./datasets
COPY --chown=recoup evals ./evals
ENV PATH="/app/.venv/bin:$PATH" RECOUP_HOME=/home/recoup/.recoup PYTHONUNBUFFERED=1
USER recoup
# The eval report the UI's Evals page serves: computed at build time, deterministic, $0.
RUN recoup eval --report evals/latest.json > /dev/null
EXPOSE 8000 8001
CMD ["recoup", "serve", "--host", "0.0.0.0", "--port", "8000"]
