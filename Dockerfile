# ── Thunder AI — Render backend image ───────────────────────────────────
# Render builds this Dockerfile with the repository root as the context
# (dockerContext: .). The React frontend is built and served SEPARATELY by
# Render as a static site (see render.yaml), so this image is backend-only.
FROM python:3.11-slim

# PYTHONPATH=/app/backend lets the app's own `from app.* import ...` statements
# resolve while uvicorn imports the ASGI app as `backend.app.main:app`.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app/backend \
    PORT=10000

WORKDIR /app

# Compiler toolchain for the few dependencies without wheels (e.g.
# chroma-hnswlib, pulled in by chromadb) and curl for ad-hoc health checks.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential curl \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps first so the layer caches across source-only changes.
COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install --upgrade pip \
    && pip install -r /app/backend/requirements.txt

# Backend source. `backend/` is a namespace package (no __init__.py at its
# root), which is what allows `backend.app.main:app` to import from /app.
COPY backend /app/backend

# Runtime data directory (uploads / SQLite fallback). Render's filesystem is
# ephemeral; with DATABASE_URL set the managed Postgres is authoritative.
RUN mkdir -p /data/uploads

# Run as a non-root user: /app and /data are the only writable paths, system
# Python packages stay read-only, and port 10000 needs no privileged bind.
RUN useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app /data
USER appuser

EXPOSE 10000

CMD ["sh", "-c", "uvicorn backend.app.main:app --host 0.0.0.0 --port ${PORT:-10000}"]
