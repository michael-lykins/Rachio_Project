# Rachio Irrigation Telemetry Collector

Polls the Rachio smart irrigation REST API and ships telemetry to Elastic Cloud Observability (Elasticsearch + Kibana).

## What it does

Five collectors run on tiered polling intervals and write to ECS-aligned data streams:

| Collector | Interval | Data streams |
|-----------|----------|-------------|
| Device | 60 s | `metrics-rachio.devices-default` |
| Zone | 300 s | `metrics-rachio.zones-default` |
| Event | 120 s | `logs-rachio.events-default` |
| Weather | 1800 s | `metrics-rachio.weather-default` |
| Schedule | 900 s | `metrics-rachio.schedules-default`, `metrics-rachio.current_schedules-default` |

Elastic EDOT OpenTelemetry auto-instrumentation ships traces and metrics to APM.

## Project layout

```
rachio/
├── collectors/              # 5 collector modules (device, zone, event, weather, schedule)
├── exporters/
│   └── elasticsearch.py     # Bulk writes to ECS data streams
├── kibana/
│   ├── generate_dashboards.py   # Dashboard generation script
│   ├── create_slos.py           # SLO creation script
│   └── rachio-dashboards.ndjson # Saved objects export
├── otel/
│   └── setup.py             # EDOT OTel SDK initialization
├── config.py                # RachioConfig, ElasticsearchConfig, PollingConfig
├── rachio_client.py         # HTTP client — rate limiting, TTL cache, retry, OTel spans
├── scheduler.py             # Entry point — APScheduler orchestration
├── Dockerfile
├── docker-compose.yml
└── .env.example
```

## Setup

```bash
cp .env.example .env
# Fill in RACHIO_API_KEY, Elasticsearch credentials, and OTel endpoint
```

Run with Docker:

```bash
docker compose up -d
```

Run locally:

```bash
pip install -r requirements.txt
python scheduler.py
```

## Rate limiting

Rachio enforces a **3,500 calls/day** hard limit per account. The client tracks usage in a file-backed counter at `/tmp/rachio_daily_calls.json` (configurable via `RACHIO_CALL_COUNTER_FILE`) that resets at midnight. Warnings fire at 90% usage; a `RuntimeError` is raised if the limit is hit.

Frequently-polled endpoints (`/person/info`, `/person/{id}`) are cached for 5 minutes in-process to reduce redundant calls.

## Configuration

All settings are loaded from environment variables (`.env`). See `.env.example` for the full list:

| Variable | Description |
|----------|-------------|
| `RACHIO_API_KEY` | Rachio account API key |
| `ELASTIC_APM_SERVER_URL` | OTel/APM endpoint |
| `ELASTIC_APM_API_KEY` | APM ingest API key |
| `ELASTICSEARCH_URL` | Elasticsearch endpoint |
| `ELASTICSEARCH_API_KEY` | Elasticsearch ingest API key |
