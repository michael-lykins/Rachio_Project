"""
Rachio API client.

Thin HTTP wrapper around the Rachio REST APIs. Every public method is
traced automatically via the OTel RequestsInstrumentor (applied in
otel/setup.py). Additional span attributes are set manually for
richer context in Elastic APM.

Rate limiting
-------------
Rachio enforces a hard limit of 3 500 API calls per day per account.
This module tracks calls in a file-backed counter that resets at midnight
and raises RuntimeError before the limit is hit.  An in-process TTL cache
reduces redundant calls for data that changes infrequently.
"""

import json
import logging
import os
import time
from datetime import date
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from opentelemetry import trace

from config import RachioConfig

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)

# ── Rate-limit constants ──────────────────────────────────────────────────────
DAILY_CALL_LIMIT = 3_500
DAILY_CALL_WARN_THRESHOLD = int(DAILY_CALL_LIMIT * 0.90)   # warn at 90 %
_COUNTER_FILE = os.getenv("RACHIO_CALL_COUNTER_FILE", "/tmp/rachio_daily_calls.json")

# ── In-process TTL response cache: url → (expires_monotonic, data) ───────────
_cache: dict[str, tuple[float, Any]] = {}


# ── Daily call counter (file-backed, resets at midnight) ─────────────────────

def _load_counter() -> dict:
    today = str(date.today())
    if os.path.exists(_COUNTER_FILE):
        try:
            with open(_COUNTER_FILE) as f:
                data = json.load(f)
            if data.get("date") == today:
                return data
        except (json.JSONDecodeError, OSError):
            pass
    return {"date": today, "count": 0}


def _save_counter(counter: dict) -> None:
    try:
        with open(_COUNTER_FILE, "w") as f:
            json.dump(counter, f)
    except OSError as exc:
        logger.warning("Could not persist Rachio call counter: %s", exc)


def _check_and_increment() -> int:
    """Increment the daily counter; raise RuntimeError if the limit is reached."""
    counter = _load_counter()
    if counter["count"] >= DAILY_CALL_LIMIT:
        raise RuntimeError(
            f"Rachio daily API limit reached ({DAILY_CALL_LIMIT} calls). "
            "Counter resets at midnight."
        )
    counter["count"] += 1
    _save_counter(counter)
    remaining = DAILY_CALL_LIMIT - counter["count"]
    if counter["count"] >= DAILY_CALL_WARN_THRESHOLD:
        logger.warning(
            "Rachio API budget low: %d/%d calls used (%d remaining today).",
            counter["count"], DAILY_CALL_LIMIT, remaining,
        )
    return counter["count"]


class RachioAPIError(Exception):
    """Raised when the Rachio API returns a non-2xx response."""

    def __init__(self, status_code: int, message: str):
        super().__init__(f"Rachio API error {status_code}: {message}")
        self.status_code = status_code


def _make_retry_session(api_key: str) -> requests.Session:
    """
    Build a requests.Session with automatic retry-on-stale-connection handling.

    The Rachio API server silently closes idle TCP connections after ~60 s.
    Because every collector polls on a fixed interval the connection pool will
    often hold a dead socket.  Mounting a Retry adapter with ``read=3`` causes
    urllib3 to transparently re-open the connection and replay the request
    instead of surfacing a RemoteDisconnected / ConnectionError to the caller.
    """
    session = requests.Session()
    session.headers.update(
        {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            # Force a fresh TCP connection on every request.
            # The Rachio API silently closes idle keep-alive connections after
            # ~60 s, which causes RemoteDisconnected / ProtocolError on the
            # next poll.  urllib3's read-retry logic does not cover ProtocolError
            # (only ReadTimeoutError), so disabling keep-alive is the cleanest fix.
            "Connection": "close",
        }
    )
    retry = Retry(
        total=3,
        connect=3,          # retry on failed connection attempts
        read=3,             # retry on RemoteDisconnected / read timeouts
        backoff_factor=0.5, # wait 0.5 s, 1 s, 2 s between retries
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "PUT"],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


class RachioClient:
    """
    Thread-safe Rachio API client.

    Uses a persistent requests.Session with connection-level retry so that
    stale keep-alive connections are transparently recovered without raising
    errors to callers.
    """

    def __init__(self, config: RachioConfig):
        self._cfg = config
        self._session = _make_retry_session(config.api_key)

    # ── Internal helpers ──────────────────────────────────────────────────

    def _get(self, base_url: str, path: str, params: dict | None = None, ttl: int = 0) -> Any:
        """
        GET with optional TTL caching and daily rate-limit enforcement.

        ttl=0  (default) → no cache, always hits the API.
        ttl>0            → serve from in-process cache if fresh; otherwise fetch.
        params are included in the cache key so different queries are stored separately.
        """
        url = f"{base_url}{path}"
        cache_key = f"{url}?{params}" if params else url

        if ttl > 0:
            entry = _cache.get(cache_key)
            if entry and time.monotonic() < entry[0]:
                logger.debug("Cache hit for %s", cache_key)
                return entry[1]

        with tracer.start_as_current_span(f"rachio.get {path}") as span:
            span.set_attribute("http.url", url)
            span.set_attribute("rachio.path", path)
            try:
                call_n = _check_and_increment()
                span.set_attribute("rachio.daily_call_count", call_n)
                resp = self._session.get(url, params=params, timeout=15)
                span.set_attribute("http.status_code", resp.status_code)
                if not resp.ok:
                    span.set_status(trace.StatusCode.ERROR, resp.text[:200])
                    raise RachioAPIError(resp.status_code, resp.text[:200])
                data = resp.json()
                if ttl > 0:
                    _cache[cache_key] = (time.monotonic() + ttl, data)
                return data
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
        return self._get(self._cfg.base_url, "/person/info", ttl=300)

    def get_person(self, person_id: str) -> dict:
        """Return full person record by ID."""
        return self._get(self._cfg.base_url, f"/person/{person_id}", ttl=300)

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
