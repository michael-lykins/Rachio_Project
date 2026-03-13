"""
Elasticsearch Serverless exporter — Elastic Streams edition.

Writes to ECS-aligned named streams following the convention:
  {type}-{dataset}-{namespace}

Stream names used:
  logs-rachio.events-default            ← event history (log-type data)
  metrics-rachio.devices-default
  metrics-rachio.current_schedules-default
  metrics-rachio.zones-default
  metrics-rachio.weather-default
  metrics-rachio.schedules-default

Key requirements for Elastic Streams / data streams vs. concrete indices:
  - Bulk operations MUST use op_type "create" (not "index")
  - Single-document indexing uses client.index() which is fine for streams
  - All documents must carry @timestamp (enforced in _enrich())
  - Elastic Serverless auto-manages rollover, retention, and sharding

OTel metrics emitted:
  rachio.es.docs_indexed   (counter)
  rachio.es.index_errors   (counter)
"""

import logging
from datetime import datetime, timezone
from typing import Any

from elasticsearch import Elasticsearch, helpers
from opentelemetry import metrics, trace

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)
meter = metrics.get_meter(__name__)

_docs_indexed = meter.create_counter(
    "rachio.es.docs_indexed",
    unit="documents",
    description="Total documents successfully written to Elastic streams",
)
_index_errors = meter.create_counter(
    "rachio.es.index_errors",
    unit="errors",
    description="Total Elasticsearch stream write errors",
)

# Maps logical data_type names → ECS-aligned stream names.
# "logs-*" for event/audit data; "metrics-*" for numeric/state data.
_STREAM_MAP: dict[str, str] = {
    "events":            "logs-rachio.events-default",
    "devices":           "metrics-rachio.devices-default",
    "current-schedules": "metrics-rachio.current_schedules-default",
    "zones":             "metrics-rachio.zones-default",
    "weather":           "metrics-rachio.weather-default",
    "schedules":         "metrics-rachio.schedules-default",
}


class ElasticsearchExporter:
    """
    Thread-safe exporter that writes Rachio data documents to
    Elastic Streams on an Elasticsearch Serverless cluster.
    """

    def __init__(self, endpoint: str, api_key: str, namespace: str = "default"):
        self._namespace = namespace
        self._client = Elasticsearch(
            hosts=[endpoint],
            api_key=api_key,
            retry_on_timeout=True,
            max_retries=3,
            request_timeout=30,
        )
        logger.info("ElasticsearchExporter initialised → %s", endpoint)

    # ── Public API ────────────────────────────────────────────────────────

    def index_document(self, data_type: str, document: dict[str, Any]) -> None:
        """
        Write a single document to the appropriate Elastic stream.

        Args:
            data_type: Logical name matching a key in _STREAM_MAP,
                       e.g. 'devices', 'zones', 'events'.
            document:  The payload dict — @timestamp added automatically.
        """
        stream = self._stream_name(data_type)
        doc = self._enrich(document, data_type)
        with tracer.start_as_current_span("es.index") as span:
            span.set_attribute("es.stream", stream)
            span.set_attribute("es.data_type", data_type)
            try:
                self._client.index(index=stream, document=doc)
                _docs_indexed.add(1, {"data_type": data_type})
                logger.debug("Wrote document to stream %s", stream)
            except Exception as exc:
                _index_errors.add(1, {"data_type": data_type})
                span.record_exception(exc)
                span.set_status(trace.StatusCode.ERROR, str(exc))
                logger.error("Failed to write to stream %s: %s", stream, exc)
                raise

    def bulk_index(self, data_type: str, documents: list[dict[str, Any]]) -> None:
        """
        Bulk-write a list of documents to the appropriate Elastic stream.

        Data streams / Elastic Streams require op_type "create" — using
        "index" is rejected with HTTP 400. The "_op_type" key in each
        action dict overrides the helpers.bulk() default.
        """
        if not documents:
            return

        stream = self._stream_name(data_type)
        actions = [
            {
                "_op_type": "create",   # required for data streams / Elastic Streams
                "_index": stream,
                **self._enrich(doc, data_type),
            }
            for doc in documents
        ]

        with tracer.start_as_current_span("es.bulk") as span:
            span.set_attribute("es.stream", stream)
            span.set_attribute("es.bulk_size", len(actions))
            try:
                success, errors = helpers.bulk(
                    self._client, actions, raise_on_error=False, stats_only=False
                )
                _docs_indexed.add(success, {"data_type": data_type})
                if errors:
                    _index_errors.add(len(errors), {"data_type": data_type})
                    logger.warning(
                        "Bulk write to %s: %d ok, %d errors", stream, success, len(errors)
                    )
                    for err in errors[:5]:
                        logger.error("Bulk error: %s", err)
                else:
                    logger.debug("Bulk wrote %d documents to %s", success, stream)
            except Exception as exc:
                _index_errors.add(len(actions), {"data_type": data_type})
                span.record_exception(exc)
                span.set_status(trace.StatusCode.ERROR, str(exc))
                logger.error("Bulk write failed for %s: %s", stream, exc)
                raise

    def health_check(self) -> bool:
        """Return True if the cluster is reachable."""
        try:
            info = self._client.info()
            logger.info("Elasticsearch cluster: %s", info.get("cluster_name", "unknown"))
            return True
        except Exception as exc:
            logger.error("Elasticsearch health check failed: %s", exc)
            return False

    def close(self) -> None:
        self._client.close()

    # ── Internal helpers ──────────────────────────────────────────────────

    def _stream_name(self, data_type: str) -> str:
        """Resolve a logical data_type to its full ECS stream name."""
        if data_type in _STREAM_MAP:
            return _STREAM_MAP[data_type]
        # Fallback for any unrecognised types — still ECS-shaped
        logger.warning("No stream mapping for data_type '%s' — using fallback", data_type)
        return f"metrics-rachio.{data_type.replace('-', '_')}-{self._namespace}"

    @staticmethod
    def _enrich(document: dict[str, Any], data_type: str) -> dict[str, Any]:
        """Add standard ECS metadata fields to every document."""
        now = datetime.now(timezone.utc).isoformat()
        return {
            **document,
            "@timestamp": document.get("@timestamp", now),
            "rachio": {
                **document.get("rachio", {}),
                "collected_at": now,
                "data_type": data_type,
            },
        }
