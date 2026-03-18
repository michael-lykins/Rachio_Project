"""
Main application entry point and scheduler orchestrator.

Starts OTel, validates config, performs a startup health check against
Elasticsearch, then launches APScheduler with one job per collector
at its configured polling interval.

Each collector runs in its own thread (max_instances=1 prevents overlapping
runs if a collection cycle takes longer than the interval).
"""

import logging
import signal
import sys
import time

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from dotenv import load_dotenv

# Load .env before any other imports so env vars are available
load_dotenv()

from collectors.device_collector import DeviceCollector
from collectors.event_collector import EventCollector
from collectors.schedule_collector import ScheduleCollector
from collectors.weather_collector import WeatherCollector
from collectors.zone_collector import ZoneCollector
from config import load_config
from exporters.elasticsearch import ElasticsearchExporter
from rachio_client import RachioClient

logger = logging.getLogger(__name__)


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )


def _safe_collect(collector_name: str, collect_fn) -> None:
    """Wrapper that catches and logs all exceptions from a collector job."""
    try:
        collect_fn()
    except Exception as exc:
        logger.error("Collector '%s' raised an unhandled exception: %s", collector_name, exc, exc_info=True)


def main() -> None:
    # ── 1. Bootstrap logging ──────────────────────────────────────────────
    # OTel providers are initialised automatically by `opentelemetry-instrument`
    # (EDOT) before Python reaches this point — no manual setup required.
    _configure_logging("INFO")

    # ── 2. Load and validate config ───────────────────────────────────────
    try:
        cfg = load_config()
    except EnvironmentError as exc:
        logger.critical("Configuration error: %s", exc)
        sys.exit(1)

    # Re-configure logging now that config is loaded (may change level)
    _configure_logging(cfg.log_level)

    logger.info("=" * 60)
    logger.info("Rachio Data Collector starting up")
    logger.info("Environment : %s", cfg.environment)
    logger.info("Log level   : %s", cfg.log_level)
    logger.info("=" * 60)

    # ── 4. Initialise shared services ─────────────────────────────────────
    client = RachioClient(cfg.rachio)
    exporter = ElasticsearchExporter(
        endpoint=cfg.elasticsearch.endpoint,
        api_key=cfg.elasticsearch.api_key,
        namespace=cfg.elasticsearch.namespace,
    )

    # ── 5. Startup health checks ──────────────────────────────────────────
    logger.info("Running startup health checks...")

    if not exporter.health_check():
        logger.critical("Elasticsearch is unreachable — check ES_ENDPOINT and ES_API_KEY")
        sys.exit(1)

    try:
        # Step 1: /person/info returns only {"id": "..."}
        info = client.get_person_info()
        person_id = info.get("id", "unknown")
        # Step 2: /person/{id} returns full record with devices, username, etc.
        person = client.get_person(person_id)
        device_count = len(person.get("devices", []))
        logger.info(
            "Rachio API connected — account: %s (id: %s), devices: %d",
            person.get("username", person.get("email", "unknown")),
            person_id,
            device_count,
        )
        if device_count == 0:
            logger.warning("No devices found on this Rachio account!")
    except Exception as exc:
        logger.critical("Rachio API health check failed: %s", exc)
        sys.exit(1)

    # ── 6. Instantiate collectors ─────────────────────────────────────────
    event_lookback = cfg.rachio.event_lookback_seconds
    collectors = {
        "device":   DeviceCollector(client, exporter),
        "zones":    ZoneCollector(client, exporter),
        "events":   EventCollector(client, exporter, lookback_seconds=event_lookback),
        "weather":  WeatherCollector(client, exporter),
        "schedules": ScheduleCollector(client, exporter),
    }

    intervals = {
        "device":    cfg.polling.device,
        "zones":     cfg.polling.zones,
        "events":    cfg.polling.events,
        "weather":   cfg.polling.weather,
        "schedules": cfg.polling.schedules,
    }

    # ── 7. Run initial collection immediately on startup ──────────────────
    logger.info("Running initial data collection on all collectors...")
    for name, collector in collectors.items():
        logger.info("  → %s", name)
        _safe_collect(name, collector.collect)

    # ── 8. Schedule recurring jobs ────────────────────────────────────────
    scheduler = BackgroundScheduler(timezone="UTC")
    for name, collector in collectors.items():
        interval_seconds = intervals[name]
        scheduler.add_job(
            func=lambda c=collector, n=name: _safe_collect(n, c.collect),
            trigger=IntervalTrigger(seconds=interval_seconds),
            id=f"collector_{name}",
            name=f"{name.capitalize()} Collector",
            max_instances=1,
            misfire_grace_time=30,
        )
        logger.info(
            "Scheduled '%s' collector every %ds", name, interval_seconds
        )

    scheduler.start()
    logger.info("Scheduler started — all collectors running. Press Ctrl+C to stop.")

    # ── 9. Graceful shutdown on SIGINT / SIGTERM ──────────────────────────
    def _shutdown(signum, frame):
        logger.info("Shutdown signal received — stopping scheduler...")
        scheduler.shutdown(wait=True)
        client.close()
        exporter.close()
        logger.info("Shutdown complete.")
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    # Keep the main thread alive
    while True:
        time.sleep(60)


if __name__ == "__main__":
    main()
