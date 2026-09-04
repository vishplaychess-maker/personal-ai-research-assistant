# ── Phase 5 — Production multi-stage build ────────────────────────────
# Stage 1 (builder): compile/download all wheels so the runtime stage needs
# no build toolchain.
FROM python:3.12-slim AS builder

WORKDIR /build

COPY requirements.txt .
RUN pip wheel --no-cache-dir --wheel-dir /wheels -r requirements.txt

# ── Stage 2 (runtime): slim image with wheels + Playwright Chromium ───
FROM python:3.12-slim

WORKDIR /app

# Shared browser location: installed as root, readable by the app user.
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PORT=8080

# Runtime Python deps from pre-built wheels.
COPY requirements.txt .
COPY --from=builder /wheels /wheels
RUN pip install --no-cache-dir --no-index --find-links=/wheels -r requirements.txt \
    && rm -rf /wheels requirements.txt

# System dependencies required by Playwright Chromium.
# We install these manually instead of using --with-deps because the
# Debian trixie base image is missing some font packages that --with-deps
# tries to install (ttf-unifont, ttf-ubuntu-font-family).
RUN apt-get update && apt-get install -y --no-install-recommends \
    libnss3 \
    libnspr4 \
    libdbus-1-3 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libpango-1.0-0 \
    libcairo2 \
    libasound2t64 \
    libatspi2.0-0 \
    fonts-liberation \
    nodejs \
    npm \
    && rm -rf /var/lib/apt/lists/*

# Install Playwright Chromium browser (deps already installed above).
RUN playwright install chromium

# Copy application source.
COPY . .

# Persistent data directories.
RUN mkdir -p /data /data/uploads \
    # Non-root user: safe because the web scraper launches Chromium with
    # --no-sandbox (backend/app/tools/web_scraper.py _CHROMIUM_ARGS), which
    # is required inside containers anyway (user-namespace sandbox does not
    # work under the default Docker seccomp profile).
    && useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app /data /ms-playwright

USER appuser

EXPOSE ${PORT}

# PORT is configurable (default 8080; the prod compose sets 8000).
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080}"]
