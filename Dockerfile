# Ledger - Bitemporal Point-in-Time Feature Store & Leakage Canary Engine
FROM python:3.11-slim

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# Install system build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace

# Copy build config and pinned lockfile first for layer caching
COPY pyproject.toml requirements.txt ./

# Install dependencies and dev tooling
RUN pip install --upgrade pip && \
    pip install -r requirements.txt && \
    pip install -e ".[dev]"

# Copy source code, tests, docs, and configurations
COPY ledger/ ./ledger/
COPY tests/ ./tests/
COPY docs/ ./docs/
COPY examples/ ./examples/
COPY scripts/ ./scripts/
COPY Makefile README.md WAR_LOG.md ./

# Default command: run all 10 leakage canary tests
CMD ["ledger", "canaries"]
