"""
Event collector.

Polls every POLL_INTERVAL_EVENTS seconds (default: 120s / 2 min).

Fetches device event history for a configurable lookback window and
deduplicates against already-seen event IDs to avoid re-indexing.
Events are kept in an in-memory set; on restart the dedup window
resets — this is acceptable since Elasticsearch documents are
idempotent (same event ID will just be a duplicate document that
Kibana will show once if filtered by event ID).

Event categories include:
  DEVICE, SCHEDULE, RAIN_DELAY, WATER_BUDGET, ZONE, SKIP, WEATHER

OTel metrics emitted:
  - rachio.events.total     (counter: total events seen, per category/type)
  - rachio.events.errors    (counter: error-category events specifically)
"""

import logging
from datetime import datetime, timezone

from opentelemetry import metrics, trace

from exporters.elasticsearch import ElasticsearchExporter
from rachio_client import RachioClient

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)
meter = metrics.get_meter(__name__)

# ── OTel Metrics ──────────────────────────────────────────────────────────
_events_total = meter.create_counter(
    "rachio.events.total",
    unit="events",
    description="Total Rachio device events collected",
)
_events_errors = meter.create_counter(
    "rachio.events.errors",
    unit="events",
    description="Total Rachio error/fault events collected",
)

# Event categories that indicate a problem
_ERROR_CATEGORIES = {"FAULT", "ERROR", "OFFLINE"}


class EventCollector:
    def __init__(
        self,
        client: RachioClient,
        exporter: ElasticsearchExporter,
        lookback_seconds: int = 300,
    ):
        self._client = client
        self._exporter = exporter
        self._lookback_seconds = lookback_seconds
        # In-memory dedup: set of event IDs seen this session
        self._seen_event_ids: set[str] = set()

    def collect(self) -> None:
        """Collect recent events for all devices."""
        with tracer.start_as_current_span("collector.events"):
            now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
            start_ms = now_ms - (self._lookback_seconds * 1000)
            device_ids = self._client.get_all_device_ids()
            for device_id in device_ids:
                self._collect_device_events(device_id, start_ms, now_ms)

    def _collect_device_events(
        self, device_id: str, start_ms: int, end_ms: int
    ) -> None:
        with tracer.start_as_current_span("collector.events.device") as span:
            span.set_attribute("rachio.device_id", device_id)
            try:
                events = self._client.get_device_events(device_id, start_ms, end_ms)
            except Exception as exc:
                logger.error(
                    "EventCollector: failed to fetch events for device %s: %s",
                    device_id,
                    exc,
                )
                return

            new_events = [e for e in events if e.get("id") not in self._seen_event_ids]
            if not new_events:
                logger.debug("EventCollector: no new events for device %s", device_id)
                return

            logger.info(
                "EventCollector: %d new event(s) for device %s",
                len(new_events),
                device_id,
            )

            documents = []
            for event in new_events:
                event_id = event.get("id", "")
                self._seen_event_ids.add(event_id)

                category = event.get("category", "UNKNOWN").upper()
                event_type = event.get("type", "UNKNOWN")

                _events_total.add(1, {"category": category, "type": event_type})
                if category in _ERROR_CATEGORIES:
                    _events_errors.add(1, {"category": category, "type": event_type})

                # Convert Rachio epoch-ms timestamp to ISO-8601
                event_ms = event.get("createDate") or event.get("eventDate")
                timestamp = (
                    datetime.fromtimestamp(event_ms / 1000, tz=timezone.utc).isoformat()
                    if event_ms
                    else datetime.now(timezone.utc).isoformat()
                )

                documents.append(
                    {
                        "@timestamp": timestamp,
                        "event_id": event_id,
                        "device_id": device_id,
                        "category": category,
                        "type": event_type,
                        "subtype": event.get("subType"),
                        "summary": event.get("summary"),
                        "topic": event.get("topic"),
                        "icon_url": event.get("iconUrl"),
                        "action": event.get("action"),
                        "hidden": event.get("hidden", False),
                        "data": event.get("data"),
                    }
                )

            self._exporter.bulk_index("events", documents)
