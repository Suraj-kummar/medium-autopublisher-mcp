# ─── Medium Auto-Publisher MCP Server ─────────────────────────────────────────
# Build:  docker build -t medium-autopublisher .
# Run:    docker run --env-file .env -p 8080:8080 -v ./drafts:/app/drafts medium-autopublisher

FROM python:3.14-slim AS base

# Prevent Python from buffering stdout/stderr
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Install dependencies first (cached layer)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source
COPY . .

# Create directories for persistent data
RUN mkdir -p drafts logs

# Expose SSE HTTP port
EXPOSE 8080

# Health check — hits the /health endpoint
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/health')" || exit 1

# Default: run in SSE mode for cloud deployment
CMD ["python", "server.py", "--transport", "sse", "--port", "8080"]
