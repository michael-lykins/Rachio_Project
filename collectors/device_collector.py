"""
Device collector.

Polls every POLL_INTERVAL_DEVICE seconds (default: 60s).

Collects per device:
  - Full device record (status, firmware, timezone, rain delay, coordinates)
  - Current running schedule (zone ID, start time, duration)

OTel metrics emitted:
  - rachio.device.online          (gauge: 1=online, 0=offline, per device)
  - rachio.device.rain_delay      (gauge: 1=active, 0=inactive, per device)
  - rachio.device.zone_count      (gauge: number of zones, per device)
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
_device_online = meter.create_gauge(
    "rachio.device.online",
    unit="1",
    description="1 if device is online, 0 if offline",
)
_rain_delay = meter.create_gauge(
    "rachio.device.rain_delay",
    unit="1",
    description="1 if rain delay is currently active on the device",
)
_zone_count = meter.create_gauge(
    "rachio.device.zone_count",
    unit="zones",
    description="Number of zones configured on the device",
)
_schedule_active = meter.create_gauge(
    "rachio.device.schedule_active",
    unit="1",
    description="1 if a schedule is currently running, 0 if idle",
)


class DeviceCollector:
    def __init__(self, client: RachioClient, exporter: ElasticsearchExporter):
        self._client = client
        self._exporter = exporter

    def collect(self) -> None:
        """Collect device state for all devices and ship to Elasticsearch."""
        with tracer.start_as_current_span("collector.device"):
            device_ids = self._client.get_all_device_ids()
            logger.info("DeviceCollector: polling %d device(s)", len(device_ids))
            for device_id in device_ids:
                self._collect_device(device_id)

    def _collect_device(self, device_id: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with tracer.start_as_current_span("collector.device.single") as span:
            span.set_attribute("rachio.device_id", device_id)

            # ── Device record ─────────────────────────────────────────
            try:
                device = self._client.get_device(device_id)
            except Exception as exc:
                logger.error("Failed to fetch device %s: %s", device_id, exc)
                return

            is_online = device.get("status", "").upper() == "ONLINE"
            rain_delay_active = bool(device.get("rainDelayExpirationDate"))
            zones = device.get("zones", [])
            enabled_zones = [z for z in zones if z.get("enabled", False)]

            # Emit OTel metrics
            labels = {"device_id": device_id, "device_name": device.get("name", "")}
            _device_online.set(1 if is_online else 0, labels)
            _rain_delay.set(1 if rain_delay_active else 0, labels)
            _zone_count.set(len(enabled_zones), labels)

            doc = {
                "@timestamp": now,
                "device_id": device.get("id"),
                "device_name": device.get("name"),
                "model": device.get("model"),
                "serial_number": device.get("serialNumber"),
                "status": device.get("status"),
                "is_online": is_online,
                "firmware_version": device.get("firmwareVersion"),
                "timezone": device.get("timeZone"),
                "latitude": device.get("latitude"),
                "longitude": device.get("longitude"),
                "elevation": device.get("elevation"),
                "rain_delay_active": rain_delay_active,
                "rain_delay_expiration": device.get("rainDelayExpirationDate"),
                "paused": device.get("paused", False),
                "on": device.get("on", True),
                "zone_count": len(zones),
                "enabled_zone_count": len(enabled_zones),
                "mac_address": device.get("macAddress"),
            }
            self._exporter.index_document("devices", doc)

            # ── Current schedule ──────────────────────────────────────
            try:
                schedule = self._client.get_current_schedule(device_id)
                is_running = bool(schedule and schedule.get("zoneId"))
                _schedule_active.set(1 if is_running else 0, labels)

                schedule_doc = {
                    "@timestamp": now,
                    "device_id": device_id,
                    "device_name": device.get("name"),
                    "schedule_active": is_running,
                    "zone_id": schedule.get("zoneId"),
                    "zone_name": schedule.get("zoneName"),
                    "schedule_rule_id": schedule.get("scheduleRuleId"),
                    "schedule_type": schedule.get("type"),
                    "start_time": schedule.get("startDate"),
                    "duration_seconds": schedule.get("duration"),
                    "status": schedule.get("status"),
                }
                self._exporter.index_document("current-schedules", schedule_doc)
            except Exception as exc:
                logger.warning(
                    "Failed to fetch current schedule for device %s: %s", device_id, exc
                )
