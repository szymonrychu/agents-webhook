FROM python:3.12-slim-bookworm
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_PROJECT_ENVIRONMENT=/app/.venv UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && uv sync --frozen --no-dev \
    && apt-get purge -y git && apt-get autoremove -y && rm -rf /var/lib/apt/lists/* /root/.cache
ENV PATH="/app/.venv/bin:$PATH"
USER 1000
ENTRYPOINT ["agents-webhook"]
