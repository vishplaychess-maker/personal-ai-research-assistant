FROM python:3.12-slim

WORKDIR /app

# Create persistent data directories with write permissions
RUN mkdir -p /data /data/uploads

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install system dependencies required by Playwright Chromium.
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
    && rm -rf /var/lib/apt/lists/*

RUN apt-get update && apt-get install -y nodejs npm && rm -rf /var/lib/apt/lists/*

# Install Playwright Chromium browser (deps already installed above)
RUN playwright install chromium

COPY . .

EXPOSE 8080

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
