"""
Rachio API client.

Thin HTTP wrapper around the Rachio REST APIs. Every public method is
traced automatically via the OTel RequestsInstrumentor (applied in
otel/setup.py). Additional span attributes are set manually for
richer context in Elastic APM.
"""

import logging
import time
from typing import Any

import requests
from opentelemetry import trace

from config import RachioConfig

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)


class RachioAPIError(Exception):
    """Raised when the Rachio API returns a non-2xx response."""

    def __init__(self, status_code: int, message: str):
        super().__init__(f"Rachio API error {status_code}: {message}")
        self.status_code = status_code


class RachioClient:
    """
    Thread-safe Rachio API client.

    Uses a persistent requests.Session with a shared Authorization header
    so connection pooling is reused across all collector threads.
    """

    def __init__(self, config: RachioConfig):
        self._cfg = config
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {config.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
        )

    # ── Internal helpers ──────────────────────────────────────────────────

    def _get(self, base_url: str, path: str, params: dict | None = None) -> Any:
        url = f"{base_url}{path}"
        with tracer.start_as_current_span(f"rachio.get {path}") as span:
            span.set_attribute("http.url", url)
            span.set_attribute("rachio.path", path)
            try:
                resp = self._session.get(url, params=params, timeout=15)
                span.set_attribute("http.status_code", resp.status_code)
                if not resp.ok:
                    span.set_status(trace.StatusCode.ERROR, resp.text[:200])
                    raise RachioAPIError(resp.status_code, resp.text[:200])
                return resp.json()
            except requests.RequestException as exc:
                span.record_exception(exc)
                span.set_status(trace.StatusCode.ERROR, str(exc))
                raise

    def _put(self, base_url: str, path: str, payload: dict) -> Any:
        url = f"{base_url}{path}"
        with tracer.start_as_current_span(f"rachio.put {path}") as span:
            span.set_attribute("http.url", url)
            span.set_attribute("rachio.path", path)
            try:
                resp = self._session.put(url, json=payload, timeout=15)
                span.set_attribute("http.status_code", resp.status_code)
                if not resp.ok:
                    span.set_status(trace.StatusCode.ERROR, resp.text[:200])
                    raise RachioAPIError(resp.status_code, resp.text[:200])
                return resp.json() if resp.content else {}
            except requests.RequestException as exc:
                span.record_exception(exc)
                span.set_status(trace.StatusCode.ERROR, str(exc))
                raise

    # ── Person / Account ──────────────────────────────────────────────────

    def get_person_info(self) -> dict:
        """Return the authenticated user's account info including device IDs."""
        return self._get(self._cfg.base_url, "/person/info")

    def get_person(self, person_id: str) -> dict:
        """Return full person record by ID."""
        return self._get(self._cfg.base_url, f"/person/{person_id}")

    # ── Device ────────────────────────────────────────────────────────────

    def get_device(self, device_id: str) -> dict:
        """Return full device record including zones and schedule rules."""
        return self._get(self._cfg.base_url, f"/device/{device_id}")

    def get_current_schedule(self, device_id: str) -> dict:
        """Return the schedule currently running on a device (empty if idle)."""
        return self._get(self._cfg.base_url, f"/device/{device_id}/current_schedule")

    def get_device_events(
        self, device_id: str, start_ms: int, end_ms: int
    ) -> list[dict]:
        """
        Return device events between two Unix timestamps (milliseconds).
        The API returns events in reverse-chronological order.
        """
        data = self._get(
            self._cfg.base_url,
            f"/device/{device_id}/event",
            params={"startTime": start_ms, "endTime": end_ms},
        )
        return data if isinstance(data, list) else data.get("items", [])

    def get_device_forecast(self, device_id: str) -> dict:
        """Return weather forecast data for the device location."""
        return self._get(self._cfg.base_url, f"/device/{device_id}/forecast")

    # ── Zone ──────────────────────────────────────────────────────────────

    def get_zone(self, zone_id: str) -> dict:
        """Return full zone record including moisture levels and last run data."""
        return self._get(self._cfg.base_url, f"/zone/{zone_id}")

    # ── Schedule Rules ────────────────────────────────────────────────────

    def get_schedule_rule(self, schedule_rule_id: str) -> dict:
        """Return a fixed schedule rule."""
        return self._get(self._cfg.base_url, f"/schedulerule/{schedule_rule_id}")

    def get_flex_schedule_rule(self, flex_rule_id: str) -> dict:
        """Return a flex schedule rule (smart watering)."""
        return self._get(self._cfg.base_url, f"/flexschedulerule/{flex_rule_id}")

    # ── Cloud REST (Smart Hose Timer — future) ────────────────────────────

    def list_base_stations(self) -> list[dict]:
        """List Smart Hose Timer base stations (requires cloud-rest endpoint)."""
        data = self._get(self._cfg.cloud_url, "/valve/listBaseStations")
        return data.get("baseStations", [])

    def get_valve_day_views(self, valve_id: str, start_date: str, end_date: str) -> dict:
        """
        Return daily watering summaries for a valve.
        Dates must be ISO-8601 strings: 'YYYY-MM-DD'.
        """
        return self._put(
            self._cfg.cloud_url,
            "/summary/getValveDayViews",
            {"valveId": valve_id, "startDate": start_date, "endDate": end_date},
        )

    # ── Convenience: resolve all device IDs for this account ─────────────

    def get_all_device_ids(self) -> list[str]:
        """Return all device IDs belonging to the authenticated account.

        The Rachio API uses a two-step pattern:
          1. GET /person/info  → returns only {"id": "<uuid>"}
          2. GET /person/{id}  → returns full record including devices[]
        """
        info = self.get_person_info()          # step 1 — only has "id"
        person_id = info.get("id")
        if not person_id:
            return []
        person = self.get_person(person_id)    # step 2 — full record
        return [d["id"] for d in person.get("devices", [])]

    def close(self) -> None:
        self._session.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
