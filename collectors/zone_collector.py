"""
Zone collector.

Polls every POLL_INTERVAL_ZONES seconds (default: 300s / 5 min).

Collects per zone (across all devices):
  - Zone configuration (nozzle type, area, root depth, allowed depletion)
  - Moisture state (current level %, field capacity, wilting point)
  - Last/next watering data

OTel metrics emitted:
  - rachio.zone.moisture_percent    (gauge: soil moisture %, per zone)
  - rachio.zone.last_run_seconds    (gauge: last watering duration, per zone)
  - rachio.zone.enabled             (gauge: 1=enabled, 0=disabled, per zone)
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
_moisture_pct = meter.create_gauge(
    "rachio.zone.moisture_percent",
    unit="%",
    description="Current soil moisture percentage for a zone (Flex schedules only)",
)
_last_run_seconds = meter.create_gauge(
    "rachio.zone.last_run_seconds",
    unit="s",
    description="Duration of the last completed watering run for this zone",
)
_zone_enabled = meter.create_gauge(
    "rachio.zone.enabled",
    unit="1",
    description="1 if zone is enabled, 0 if disabled",
)
_available_water = meter.create_gauge(
    "rachio.zone.available_water",
    unit="in",
    description="Available water content in the soil (inches)",
)


class ZoneCollector:
    def __init__(self, client: RachioClient, exporter: ElasticsearchExporter):
        self._client = client
        self._exporter = exporter

    def collect(self) -> None:
        """Collect zone state for all zones on all devices."""
        with tracer.start_as_current_span("collector.zones"):
            device_ids = self._client.get_all_device_ids()
            for device_id in device_ids:
                self._collect_device_zones(device_id)

    def _collect_device_zones(self, device_id: str) -> None:
        try:
            device = self._client.get_device(device_id)
        except Exception as exc:
            logger.error("ZoneCollector: failed to fetch device %s: %s", device_id, exc)
            return

        zones = device.get("zones", [])
        logger.info(
            "ZoneCollector: collecting %d zones for device %s",
            len(zones),
            device.get("name", device_id),
        )

        documents = []
        for zone_stub in zones:
            zone_id = zone_stub.get("id")
            if not zone_id:
                continue
            doc = self._collect_zone(zone_id, device_id, device.get("name", ""))
            if doc:
                documents.append(doc)

        if documents:
            self._exporter.bulk_index("zones", documents)

    def _collect_zone(self, zone_id: str, device_id: str, device_name: str) -> dict | None:
        now = datetime.now(timezone.utc).isoformat()
        with tracer.start_as_current_span("collector.zone.single") as span:
            span.set_attribute("rachio.zone_id", zone_id)
            try:
                zone = self._client.get_zone(zone_id)
            except Exception as exc:
                logger.error("Failed to fetch zone %s: %s", zone_id, exc)
                return None

        is_enabled = zone.get("enabled", False)
        moisture_pct = zone.get("lastKnownMoisturePercent")
        last_duration = zone.get("lastWaterDuration")

        labels = {
            "zone_id": zone_id,
            "zone_name": zone.get("name", ""),
            "device_id": device_id,
            "device_name": device_name,
        }

        _zone_enabled.set(1 if is_enabled else 0, labels)
        if moisture_pct is not None:
            _moisture_pct.set(float(moisture_pct), labels)
        if last_duration is not None:
            _last_run_seconds.set(float(last_duration), labels)
        if zone.get("availableWater") is not None:
            _available_water.set(float(zone["availableWater"]), labels)

        nozzle = zone.get("customNozzle") or zone.get("nozzle") or {}
        return {
            "@timestamp": now,
            "zone_id": zone.get("id"),
            "zone_number": zone.get("zoneNumber"),
            "zone_name": zone.get("name"),
            "device_id": device_id,
            "device_name": device_name,
            "enabled": is_enabled,
            "nozzle_name": nozzle.get("name"),
            "nozzle_inches_per_hour": nozzle.get("inchesPerHour"),
            "yard_area_sq_ft": zone.get("yardAreaSquareFeet"),
            "root_zone_depth_inches": zone.get("rootZoneDepth"),
            "allowed_depletion_fraction": zone.get("managementAllowedDepletion"),
            "efficiency": zone.get("efficiency"),
            "soil_type": (zone.get("customSoil") or zone.get("soil") or {}).get("name"),
            "shade_type": (zone.get("customShade") or zone.get("shade") or {}).get("name"),
            "crop_coefficient": zone.get("cropCoefficient"),
            "available_water_inches": zone.get("availableWater"),
            "root_zone_depth_inches": zone.get("rootZoneDepth"),
            "moisture_percent": moisture_pct,
            "field_capacity": zone.get("fieldCapacity"),
            "wilting_point": zone.get("wiltingPoint"),
            "last_water_duration_seconds": last_duration,
            "last_water_date": zone.get("lastWaterDate"),
            "next_run_date": zone.get("nextRunDate"),
            "runtime_seconds": zone.get("runtime"),
        }
