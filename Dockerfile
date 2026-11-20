FROM python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

COPY fulla/ ./fulla/
COPY norns_app/ ./norns_app/
COPY config.yaml ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONDONTWRITEBYTECODE=1

# The commit this image was built from (CI passes it), so Fulla can say which
# of its own commits is running. Empty for a build by hand.
ARG FULLA_COMMIT=""
ENV FULLA_COMMIT=$FULLA_COMMIT

CMD ["fulla-serve"]
