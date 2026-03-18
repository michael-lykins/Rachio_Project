"""
otel/setup.py — DEPRECATED (kept as a no-op for backwards compatibility).

OTel providers, exporters, and auto-instrumentation are now configured
entirely by the Elastic Distribution of OpenTelemetry (EDOT) Python via the
`opentelemetry-instrument` entry-point that wraps the container process.

  pip install elastic-opentelemetry
  edot-bootstrap --action=install          # installs per-lib instrumentors
  opentelemetry-instrument python scheduler.py

All OTEL_* environment variables in .env are picked up automatically —
no manual TracerProvider / MeterProvider / LoggerProvider setup needed.
"""


def setup_otel(*args, **kwargs) -> None:  # noqa: ANN002, ANN003
    """No-op — EDOT handles initialisation via opentelemetry-instrument."""
