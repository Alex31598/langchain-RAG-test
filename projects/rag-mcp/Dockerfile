# RAG MCP server image (skeleton). server.py is a stub until M4.7.
#
# SECURITY:
# - No secrets are baked into the image. MCP_AUTH_TOKEN and NVIDIA_NIM_API_KEY
#   are injected at runtime via docker-compose environment. QDRANT_URL and
#   OLLAMA_URL point at internal-network services (no host port binding).
# - Base image is a pinned tag (python:3.11-slim), not :latest.
# - Container runs as a non-root user; only port 8000 is exposed (compose maps
#   it to 127.0.0.1:8101). See tasks/PLAN.md §6.1, §8.

FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    UV_CACHE_DIR=/tmp/uv-cache \
    PATH=/app/.venv/bin:$PATH

# Install pinned uv by copying the binary from the official distroless image
# (avoids needing curl/wget, which python:3.11-slim does not ship).
COPY --from=ghcr.io/astral-sh/uv:0.9.18 /uv /uvx /usr/local/bin/
RUN uv --version

# Non-root runtime user.
RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --home-dir /app --shell /usr/sbin/nologin app

WORKDIR /app
RUN chown app:app /app

# Copy project manifest + source (build context is projects/rag-mcp).
COPY --chown=app:app pyproject.toml ./pyproject.toml
COPY --chown=app:app src ./src

# Sync the project's exact-pinned dependencies into the venv as the non-root user.
USER app
RUN uv sync --no-dev

EXPOSE 8000
CMD ["python", "-m", "rag_mcp.server"]
