"""
Centralised configuration loaded from environment variables.
All sensitive values must be set in .env (never hard-coded).
"""

import os
from dataclasses import dataclass, field


def _require(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise EnvironmentError(
            f"Required environment variable '{name}' is not set. "
            "Check your .env file."
        )
    return value


def _int(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


@dataclass(frozen=True)
class RachioConfig:
    api_key: str
    base_url: str = "https://api.rach.io/1/public"
    cloud_url: str = "https://cloud-rest.rach.io"
    # How many past seconds of events to request on each event poll
    event_lookback_seconds: int = field(default_factory=lambda: _int("RACHIO_EVENT_LOOKBACK_SECONDS", 300))


@dataclass(frozen=True)
class ElasticsearchConfig:
    endpoint: str
    api_key: str
    # Namespace segment in ECS stream names: {type}-rachio.{dataset}-{namespace}
    namespace: str = field(default_factory=lambda: os.getenv("ES_NAMESPACE", "default"))
    # Number of retries on ES write failure
    max_retries: int = field(default_factory=lambda: _int("ES_MAX_RETRIES", 3))


@dataclass(frozen=True)
class PollingConfig:
    # Intervals in seconds
    device: int = field(default_factory=lambda: _int("POLL_INTERVAL_DEVICE", 60))
    zones: int = field(default_factory=lambda: _int("POLL_INTERVAL_ZONES", 300))
    events: int = field(default_factory=lambda: _int("POLL_INTERVAL_EVENTS", 120))
    weather: int = field(default_factory=lambda: _int("POLL_INTERVAL_WEATHER", 1800))
    schedules: int = field(default_factory=lambda: _int("POLL_INTERVAL_SCHEDULES", 900))


@dataclass(frozen=True)
class AppConfig:
    rachio: RachioConfig
    elasticsearch: ElasticsearchConfig
    polling: PollingConfig
    log_level: str = field(default_factory=lambda: os.getenv("LOG_LEVEL", "INFO"))
    environment: str = field(default_factory=lambda: os.getenv("ENVIRONMENT", "production"))


def load_config() -> AppConfig:
    """
    Load and validate all configuration from environment variables.
    Raises EnvironmentError immediately if any required variable is missing.
    """
    return AppConfig(
        rachio=RachioConfig(
            api_key=_require("RACHIO_API_KEY"),
        ),
        elasticsearch=ElasticsearchConfig(
            endpoint=_require("ES_ENDPOINT"),
            api_key=_require("ES_API_KEY"),
        ),
        polling=PollingConfig(),
    )
