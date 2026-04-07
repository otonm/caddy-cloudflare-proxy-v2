FROM python:3.13-slim

WORKDIR /app

# Install uv for dependency management
RUN pip install --no-cache-dir uv

# Copy dependency manifest first for layer caching
COPY pyproject.toml .

# Install all runtime dependencies (no dev extras in production image)
RUN uv sync --no-dev

# Copy application source
COPY core/ core/
COPY ui/ ui/
COPY main.py .

# Data directory for persistent JSON config (can be volume-mounted)
RUN mkdir -p /data

EXPOSE 8088

CMD ["uv", "run", "python", "main.py"]
