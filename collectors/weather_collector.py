"""
Weather / forecast collector.

Polls every POLL_INTERVAL_WEATHER seconds (default: 1800s / 30 min).

Collects the weather forecast for each device location including:
  - Current conditions (temperature, humidity, precipitation)
  - Daily forecast (high/low temp, precip probability, ET rate)
  - Wind speed, UV index, dew point

OTel metrics emitted:
  - rachio.weather.temp_high_f        (gauge: forecast high temperature)
  - rachio.weather.precip_probability (gauge: 0–1, per device)
  - rachio.weather.et_mm              (gauge: evapotranspiration mm/day)
  - rachio.weather.wind_speed_mph     (gauge: current wind speed)
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
_temp_high = meter.create_gauge(
    "rachio.weather.temp_high_f",
    unit="degF",
    description="Forecast high temperature for the device location",
)
_precip_probability = meter.create_gauge(
    "rachio.weather.precip_probability",
    unit="1",
    description="Forecasted precipitation probability (0.0–1.0)",
)
_et_rate = meter.create_gauge(
    "rachio.weather.et_mm",
    unit="mm",
    description="Evapotranspiration rate (mm/day) — key input for Flex schedules",
)
_wind_speed = meter.create_gauge(
    "rachio.weather.wind_speed_mph",
    unit="mph",
    description="Current wind speed at the device location",
)
_humidity = meter.create_gauge(
    "rachio.weather.humidity",
    unit="%",
    description="Current relative humidity at the device location",
)


class WeatherCollector:
    def __init__(self, client: RachioClient, exporter: ElasticsearchExporter):
        self._client = client
        self._exporter = exporter

    def collect(self) -> None:
        """Collect weather forecast for all device locations."""
        with tracer.start_as_current_span("collector.weather"):
            device_ids = self._client.get_all_device_ids()
            for device_id in device_ids:
                self._collect_device_weather(device_id)

    def _collect_device_weather(self, device_id: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with tracer.start_as_current_span("collector.weather.device") as span:
            span.set_attribute("rachio.device_id", device_id)
            try:
                forecast_data = self._client.get_device_forecast(device_id)
            except Exception as exc:
                logger.error(
                    "WeatherCollector: failed to fetch forecast for device %s: %s",
                    device_id,
                    exc,
                )
                return

        labels = {"device_id": device_id}

        # The API returns a list of daily forecast objects
        forecasts = forecast_data if isinstance(forecast_data, list) else [forecast_data]

        documents = []
        for entry in forecasts:
            # Current conditions are in the first entry (today)
            is_current = entry == forecasts[0]
            if is_current:
                current_temp = entry.get("temperatureMax") or entry.get("temperatureCurrent")
                precip_prob = entry.get("precipProbability", 0)
                et = entry.get("evapotranspiration") or entry.get("et", 0)
                wind = entry.get("windSpeed", 0)
                humidity_val = entry.get("humidity", 0)

                if current_temp is not None:
                    _temp_high.set(float(current_temp), labels)
                _precip_probability.set(float(precip_prob), labels)
                if et:
                    _et_rate.set(float(et), labels)
                if wind:
                    _wind_speed.set(float(wind), labels)
                if humidity_val:
                    _humidity.set(float(humidity_val) * 100 if humidity_val <= 1 else float(humidity_val), labels)

            # Forecast date — Rachio returns epoch-ms or ISO strings
            forecast_time = entry.get("time") or entry.get("date")
            if isinstance(forecast_time, (int, float)):
                forecast_ts = datetime.fromtimestamp(
                    forecast_time / 1000 if forecast_time > 1e10 else forecast_time,
                    tz=timezone.utc,
                ).isoformat()
            else:
                forecast_ts = forecast_time or now

            documents.append(
                {
                    "@timestamp": now,
                    "forecast_date": forecast_ts,
                    "device_id": device_id,
                    "is_current": is_current,
                    "temperature_high_f": entry.get("temperatureMax"),
                    "temperature_low_f": entry.get("temperatureMin"),
                    "temperature_current_f": entry.get("temperatureCurrent"),
                    "precip_probability": entry.get("precipProbability"),
                    "precip_intensity": entry.get("precipIntensity"),
                    "precip_intensity_max": entry.get("precipIntensityMax"),
                    "precip_type": entry.get("precipType"),
                    "precip_accumulation_inches": entry.get("precipAccumulation"),
                    "wind_speed_mph": entry.get("windSpeed"),
                    "wind_gust_mph": entry.get("windGust"),
                    "humidity_pct": (
                        entry.get("humidity", 0) * 100
                        if (entry.get("humidity") or 0) <= 1
                        else entry.get("humidity")
                    ),
                    "dew_point_f": entry.get("dewPoint"),
                    "cloud_cover_pct": (
                        entry.get("cloudCover", 0) * 100
                        if (entry.get("cloudCover") or 0) <= 1
                        else entry.get("cloudCover")
                    ),
                    "uv_index": entry.get("uvIndex"),
                    "visibility_miles": entry.get("visibility"),
                    "evapotranspiration_mm": entry.get("evapotranspiration") or entry.get("et"),
                    "weather_summary": entry.get("summary"),
                    "weather_icon": entry.get("icon"),
                    "sunrise_epoch": entry.get("sunriseTime"),
                    "sunset_epoch": entry.get("sunsetTime"),
                }
            )

        if documents:
            self._exporter.bulk_index("weather", documents)
            logger.info(
                "WeatherCollector: indexed %d forecast entries for device %s",
                len(documents),
                device_id,
            )
