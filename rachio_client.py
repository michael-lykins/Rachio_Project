import os
import requests
import logging

from opentelemetry import trace, metrics
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.resources import Resource

from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter

from opentelemetry.instrumentation.requests import RequestsInstrumentor
from opentelemetry.instrumentation.logging import LoggingInstrumentor
from opentelemetry.instrumentation.system_metrics import SystemMetricsInstrumentor

# ─── Configure Resource (service metadata) ──────────────────────────────
resource = Resource.create({
    "service.name":           "rachio-client",
    "service.version":        "0.1.0",
    "deployment.environment": "production",
})

# ─── Tracing Setup ──────────────────────────────────────────────────────
trace.set_tracer_provider(TracerProvider(resource=resource))
tracer = trace.get_tracer(__name__)

otlp_trace_exporter = OTLPSpanExporter(
    endpoint=os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317"),
    headers={"api-key": os.getenv("OTEL_EXPORTER_OTLP_HEADERS", "")},
)
span_processor = BatchSpanProcessor(otlp_trace_exporter)
trace.get_tracer_provider().add_span_processor(span_processor)

# ─── Metrics Setup ──────────────────────────────────────────────────────
metrics.set_meter_provider(MeterProvider(resource=resource))
meter = metrics.get_meter(__name__)

otlp_metric_exporter = OTLPMetricExporter(
    endpoint=os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317"),
    headers={"api-key": os.getenv("OTEL_EXPORTER_OTLP_HEADERS", "")},
)
metric_reader = PeriodicExportingMetricReader(otlp_metric_exporter, export_interval_millis=10000)
metrics.get_meter_provider().start_pipeline(meter, metric_reader)

# ─── Instrument HTTP, Logging, System Metrics ───────────────────────────
RequestsInstrumentor().instrument(tracer_provider=trace.get_tracer_provider())
LoggingInstrumentor().instrument(set_logging_format=True)
SystemMetricsInstrumentor().instrument(meter_provider=metrics.get_meter_provider())

# ─── Application Logic ───────────────────────────────────────────────────
API_KEY = os.getenv("RACHIO_API_KEY")
BASE_URL = "https://api.rach.io/1/public"

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

def fetch_devices():
    with tracer.start_as_current_span("fetch-devices"):
        resp = requests.get(
            f"{BASE_URL}/device",
            headers={"Authorization": f"Bearer {API_KEY}"}
        )
        resp.raise_for_status()
        return resp.json()

def main():
    logger.info("Starting Rachio client")
    devices = fetch_devices()
    logger.info(f"Found {len(devices.get('devices', []))} devices")
    for d in devices.get("devices", []):
        logger.info(f"- {d['name']} ({d['status']})")

if __name__ == "__main__":
    main()
