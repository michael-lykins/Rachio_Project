# ── Build stage ───────────────────────────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /app

# Build deps needed to compile psutil from source on linux/aarch64.
# psutil is a transitive dependency of elastic-opentelemetry.
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libffi-dev \
    python3-dev \
    linux-libc-dev \
    && rm -rf /var/lib/apt/lists/*

# Create a virtual environment — the correct way to do multi-stage Python
# builds. A venv is fully self-contained and copies cleanly between stages,
# unlike --prefix installs which embed absolute paths in dist-info metadata.
RUN python -m venv /venv
ENV PATH="/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt \
    # Let EDOT discover installed packages (requests, APScheduler, etc.) and
    # install any matching OTel instrumentation libraries automatically.
    && edot-bootstrap --action=install


# ── Runtime stage ─────────────────────────────────────────────────────────
FROM python:3.12-slim

WORKDIR /app

# Copy the fully built virtual environment from the builder
COPY --from=builder /venv /venv
ENV PATH="/venv/bin:$PATH"

# Copy application source
COPY otel/        ./otel/
COPY collectors/  ./collectors/
COPY exporters/   ./exporters/
COPY config.py        .
COPY rachio_client.py .
COPY scheduler.py     .

# Non-root user for security
RUN useradd -m -u 1000 rachio
USER rachio

# Health check — verifies the process is alive
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "import scheduler" || exit 1

# opentelemetry-instrument (provided by EDOT) bootstraps all OTel providers
# and auto-instrumentation before handing off to scheduler.py.
ENTRYPOINT ["opentelemetry-instrument", "python", "-u", "scheduler.py"]
