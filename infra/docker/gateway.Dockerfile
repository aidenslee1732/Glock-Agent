# Glock Gateway Service Dockerfile
# Model B Architecture - Stateless LLM Proxy

FROM python:3.12-slim

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PIP_NO_CACHE_DIR=1

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user
RUN groupadd -r glock && useradd -r -g glock -d /app -s /bin/bash glock

# Set working directory
WORKDIR /app

# Copy dependency files first for caching
COPY apps/server/pyproject.toml ./apps/server/
COPY packages/shared-protocol/pyproject.toml ./packages/shared-protocol/

# Install shared protocol first
COPY packages/shared-protocol ./packages/shared-protocol
RUN pip install --upgrade pip && \
    pip install -e ./packages/shared-protocol

# Install server dependencies
COPY apps/server ./apps/server
RUN pip install -e ./apps/server

# Set ownership
RUN chown -R glock:glock /app

# Switch to non-root user
USER glock

# Expose port
EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# Default command - run the gateway server
CMD ["uvicorn", "apps.server.src.main:app", "--host", "0.0.0.0", "--port", "8000"]
