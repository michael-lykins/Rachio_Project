"""
Schedule collector.

Polls every POLL_INTERVAL_SCHEDULES seconds (default: 900s / 15 min).

Collects both fixed schedule rules and flex (smart) schedule rules for
all devices. Flex schedules contain the richest data for irrigation
intelligence — water budget, seasonal adjustment, ET-driven scheduling.

OTel metrics emitted:
  - rachio.schedule.water_budget_pct    (gauge: water budget efficiency %)
  - rachio.schedule.seasonal_adjust_pct (gauge: seasonal adjustment %)
  - rachio.schedule.total_duration_s    (gauge: total run time in seconds)
  - rachio.schedule.enabled             (gauge: 1=enabled, 0=disabled)
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
_water_budget = meter.create_gauge(
    "rachio.schedule.water_budget_pct",
    unit="%",
    description="Water budget percentage for the schedule (efficiency vs baseline)",
)
_seasonal_adjust = meter.create_gauge(
    "rachio.schedule.seasonal_adjust_pct",
    unit="%",
    description="Seasonal adjustment percentage applied to the schedule",
)
_total_duration = meter.create_gauge(
    "rachio.schedule.total_duration_seconds",
    unit="s",
    description="Total run duration of the schedule in seconds",
)
_schedule_enabled = meter.create_gauge(
    "rachio.schedule.enabled",
    unit="1",
    description="1 if schedule is enabled, 0 if disabled",
)


class ScheduleCollector:
    def __init__(self, client: RachioClient, exporter: ElasticsearchExporter):
        self._client = client
        self._exporter = exporter

    def collect(self) -> None:
        """Collect all schedule rules for all devices."""
        with tracer.start_as_current_span("collector.schedules"):
            device_ids = self._client.get_all_device_ids()
            for device_id in device_ids:
                self._collect_device_schedules(device_id)

    def _collect_device_schedules(self, device_id: str) -> None:
        try:
            device = self._client.get_device(device_id)
        except Exception as exc:
            logger.error(
                "ScheduleCollector: failed to fetch device %s: %s", device_id, exc
            )
            return

        device_name = device.get("name", device_id)
        schedule_rules = device.get("scheduleRules", [])
        flex_rules = device.get("flexScheduleRules", [])

        logger.info(
            "ScheduleCollector: device %s has %d fixed, %d flex schedules",
            device_name,
            len(schedule_rules),
            len(flex_rules),
        )

        documents = []

        for rule_stub in schedule_rules:
            doc = self._collect_fixed_schedule(
                rule_stub.get("id"), device_id, device_name
            )
            if doc:
                documents.append(doc)

        for rule_stub in flex_rules:
            doc = self._collect_flex_schedule(
                rule_stub.get("id"), device_id, device_name
            )
            if doc:
                documents.append(doc)

        if documents:
            self._exporter.bulk_index("schedules", documents)

    def _collect_fixed_schedule(
        self, rule_id: str, device_id: str, device_name: str
    ) -> dict | None:
        if not rule_id:
            return None
        now = datetime.now(timezone.utc).isoformat()
        try:
            rule = self._client.get_schedule_rule(rule_id)
        except Exception as exc:
            logger.error("Failed to fetch schedule rule %s: %s", rule_id, exc)
            return None

        is_enabled = rule.get("enabled", False)
        water_budget = rule.get("wateringAdjustment") or rule.get("waterBudget", 100)
        seasonal = rule.get("seasonalAdjustment", 100)
        duration = rule.get("totalDuration") or rule.get("duration", 0)

        labels = {
            "schedule_id": rule_id,
            "schedule_name": rule.get("name", ""),
            "device_id": device_id,
            "schedule_type": "fixed",
        }
        _schedule_enabled.set(1 if is_enabled else 0, labels)
        _water_budget.set(float(water_budget), labels)
        _seasonal_adjust.set(float(seasonal), labels)
        _total_duration.set(float(duration), labels)

        return {
            "@timestamp": now,
            "schedule_id": rule.get("id"),
            "schedule_name": rule.get("name"),
            "schedule_type": "fixed",
            "device_id": device_id,
            "device_name": device_name,
            "enabled": is_enabled,
            "start_time_local": rule.get("startTime"),
            "days_of_week": rule.get("days"),
            "total_duration_seconds": duration,
            "water_budget_pct": water_budget,
            "seasonal_adjustment_pct": seasonal,
            "rain_delay": rule.get("rainDelay", False),
            "use_weather_intelligence": rule.get("useWeatherIntelligence", False),
            "skip_evapotranspiration": rule.get("skipEvapotranspiration", False),
            "skip_freeze": rule.get("skipFreeze", False),
            "skip_wind": rule.get("skipWind", False),
            "skip_percent": rule.get("skipPercent"),
            "summary": rule.get("summary"),
            "zones": [
                {
                    "zone_id": z.get("id"),
                    "zone_number": z.get("zoneNumber"),
                    "duration_seconds": z.get("duration"),
                    "sort_order": z.get("sortOrder"),
                }
                for z in rule.get("zones", [])
            ],
        }

    def _collect_flex_schedule(
        self, rule_id: str, device_id: str, device_name: str
    ) -> dict | None:
        if not rule_id:
            return None
        now = datetime.now(timezone.utc).isoformat()
        try:
            rule = self._client.get_flex_schedule_rule(rule_id)
        except Exception as exc:
            logger.error("Failed to fetch flex schedule rule %s: %s", rule_id, exc)
            return None

        is_enabled = rule.get("enabled", False)
        water_budget = rule.get("wateringAdjustment") or rule.get("waterBudget", 100)
        seasonal = rule.get("seasonalAdjustment", 100)
        duration = rule.get("totalDuration") or rule.get("duration", 0)

        labels = {
            "schedule_id": rule_id,
            "schedule_name": rule.get("name", ""),
            "device_id": device_id,
            "schedule_type": "flex",
        }
        _schedule_enabled.set(1 if is_enabled else 0, labels)
        _water_budget.set(float(water_budget), labels)
        _seasonal_adjust.set(float(seasonal), labels)
        _total_duration.set(float(duration), labels)

        return {
            "@timestamp": now,
            "schedule_id": rule.get("id"),
            "schedule_name": rule.get("name"),
            "schedule_type": "flex",
            "device_id": device_id,
            "device_name": device_name,
            "enabled": is_enabled,
            "total_duration_seconds": duration,
            "water_budget_pct": water_budget,
            "seasonal_adjustment_pct": seasonal,
            "next_run_date": rule.get("nextRunDate"),
            "last_run_date": rule.get("lastRunDate"),
            "summary": rule.get("summary"),
            "zones": [
                {
                    "zone_id": z.get("id"),
                    "zone_number": z.get("zoneNumber"),
                    "duration_seconds": z.get("duration"),
                    "sort_order": z.get("sortOrder"),
                }
                for z in rule.get("zones", [])
            ],
        }
