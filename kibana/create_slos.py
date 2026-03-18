#!/usr/bin/env python3
"""
Create Kibana SLOs for the Rachio irrigation collector service.

Usage:
    python3 kibana/create_slos.py

Reads credentials from .env in the project root.

SLOs created:
    1. Controller Availability        — device online 99% of polled intervals
    2. Rachio API Reliability         — 99.5% of API calls succeed (OTel spans)
    3. Collector Operation Success    — 99% of collector spans succeed
    4. Rain-Delay-Free Windows        — informational: track rain delay frequency
    5. Weather Intelligence Active    — schedules using weather intelligence
"""

import json
import os
import sys
import urllib.request
import urllib.error
from pathlib import Path

# ── Load credentials from .env ────────────────────────────────────────────
env_file = Path(__file__).parent.parent / ".env"
env = {}
if env_file.exists():
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip()

ES_ENDPOINT = env.get("ES_ENDPOINT", "")
API_KEY     = env.get("ES_API_KEY", "")

# Kibana URL derived from ES URL (replace .es. with .kb.)
KB_URL = ES_ENDPOINT.replace(".es.", ".kb.")

if not KB_URL or not API_KEY:
    print("ERROR: ES_ENDPOINT and ES_API_KEY must be set in .env")
    sys.exit(1)

print(f"Kibana: {KB_URL}")
print(f"API Key: {API_KEY[:8]}…")
print()


# ── HTTP helpers ──────────────────────────────────────────────────────────

def kb_request(method, path, body=None):
    url = f"{KB_URL}{path}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={
            "Authorization": f"ApiKey {API_KEY}",
            "kbn-xsrf":      "true",
            "Content-Type":  "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        try:
            return json.loads(body)
        except Exception:
            return {"error": body, "statusCode": e.code}


def create_slo(slo_def):
    name = slo_def["name"]
    # Check if SLO with this name already exists
    existing = kb_request("GET", "/api/observability/slos?perPage=100")
    for s in existing.get("results", []):
        if s.get("name") == name:
            print(f"  ↩  Already exists — skipping: {name}")
            return s

    result = kb_request("POST", "/api/observability/slos", slo_def)
    if "id" in result:
        print(f"  ✓  Created: {name}  (id: {result['id']})")
    else:
        print(f"  ✗  Failed : {name}")
        print(f"     {json.dumps(result, indent=4)}")
    return result


# ═══════════════════════════════════════════════════════════════════════════
# SLO Definitions
# ═══════════════════════════════════════════════════════════════════════════

SLOS = [

    # ── 1. Controller Availability ─────────────────────────────────────────
    # Every minute the device collector polls the device status. This SLO
    # measures what % of those polls found the controller ONLINE.
    # Target: 99% over a rolling 30-day window.
    {
        "name":        "Rachio Controller Availability",
        "description": (
            "Measures the percentage of device polling intervals in which "
            "the Rachio-3C6768 controller reports ONLINE status. "
            "A dip below 99% indicates connectivity or firmware issues."
        ),
        "indicator": {
            "type": "sli.kql.custom",
            "params": {
                "index":          "metrics-rachio.devices-default",
                "filter":         "device_id: *",
                "good":           "is_online: true",
                "total":          "*",
                "timestampField": "@timestamp",
            },
        },
        "timeWindow":      {"duration": "30d", "type": "rolling"},
        "budgetingMethod": "occurrences",
        "objective":       {"target": 0.99},
    },

    # ── 2. Rachio API Reliability ──────────────────────────────────────────
    # Every HTTP call the collector makes to api.rach.io is traced via OTel.
    # This SLO measures what % of those spans complete with outcome=success.
    # Target: 99.5% over a rolling 30-day window.
    {
        "name":        "Rachio API Reliability",
        "description": (
            "Percentage of outbound HTTP calls to the Rachio API (api.rach.io) "
            "that complete with a successful outcome. Tracked via OpenTelemetry "
            "span data. Failures indicate API downtime or auth issues."
        ),
        "indicator": {
            "type": "sli.kql.custom",
            "params": {
                "index":          "traces-*.otel-*",
                "filter":         "attributes.service.target.name: api.rach.io",
                "good":           "attributes.event.outcome: success",
                "total":          "*",
                "timestampField": "@timestamp",
            },
        },
        "timeWindow":      {"duration": "30d", "type": "rolling"},
        "budgetingMethod": "occurrences",
        "objective":       {"target": 0.995},
    },

    # ── 3. Collector Operation Success Rate ────────────────────────────────
    # Each scheduled collector run (device, zone, weather, etc.) produces
    # an OTel span. This SLO measures the overall success rate of all
    # collector operations. Target: 99% over a rolling 7-day window.
    {
        "name":        "Rachio Collector Operation Success",
        "description": (
            "Overall success rate of all rachio-collector operation spans "
            "(device polling, zone checks, weather, events, schedules). "
            "A sustained drop indicates a systemic collection failure."
        ),
        "indicator": {
            "type": "sli.kql.custom",
            "params": {
                "index":          "traces-*.otel-*",
                "filter":         "resource.attributes.service.name: rachio-collector",
                "good":           "attributes.event.outcome: success",
                "total":          "*",
                "timestampField": "@timestamp",
            },
        },
        "timeWindow":      {"duration": "7d", "type": "rolling"},
        "budgetingMethod": "occurrences",
        "objective":       {"target": 0.99},
    },

    # ── 4. Rain-Delay-Free Irrigation Windows ──────────────────────────────
    # Informational SLO tracking what % of polling intervals have NO active
    # rain delay. A low % means the controller is suppressing watering
    # frequently — useful to correlate with precipitation data.
    # Target: 70% (deliberately lenient — rain delays are expected).
    {
        "name":        "Irrigation Rain-Delay-Free Rate",
        "description": (
            "Percentage of device polling intervals where no rain delay is "
            "active on the controller. A value below 70% over 30 days suggests "
            "unusually frequent rainfall or overly aggressive rain sensor config. "
            "Correlate with the Weather dashboard for context."
        ),
        "indicator": {
            "type": "sli.kql.custom",
            "params": {
                "index":          "metrics-rachio.devices-default",
                "filter":         "device_id: *",
                "good":           "rain_delay_active: false",
                "total":          "*",
                "timestampField": "@timestamp",
            },
        },
        "timeWindow":      {"duration": "30d", "type": "rolling"},
        "budgetingMethod": "occurrences",
        "objective":       {"target": 0.70},
    },

    # ── 5. Zone Data Collection Coverage ──────────────────────────────────
    # Every 5 minutes the zone collector polls all 4 zones.  This SLO
    # measures what % of zone documents belong to enabled zones —
    # a proxy for whether all expected zones are being collected.
    # Target: 99% over a rolling 7-day window.
    {
        "name":        "Zone Data Collection Coverage",
        "description": (
            "Percentage of zone polling documents that belong to an enabled "
            "zone. A drop below 99% indicates a zone was unexpectedly disabled "
            "in the Rachio app, or zone data collection is failing for one or "
            "more zones on Rachio-3C6768."
        ),
        "indicator": {
            "type": "sli.kql.custom",
            "params": {
                "index":          "metrics-rachio.zones-default",
                "filter":         "device_id: *",
                "good":           "enabled: true",
                "total":          "*",
                "timestampField": "@timestamp",
            },
        },
        "timeWindow":      {"duration": "7d", "type": "rolling"},
        "budgetingMethod": "occurrences",
        "objective":       {"target": 0.99},
    },
]


# ═══════════════════════════════════════════════════════════════════════════
# Create all SLOs
# ═══════════════════════════════════════════════════════════════════════════

print(f"Creating {len(SLOS)} SLOs...\n")
results = []
for slo in SLOS:
    r = create_slo(slo)
    results.append(r)

print()
created = [r for r in results if "id" in r]
print(f"Done — {len(created)}/{len(SLOS)} SLOs created successfully.")
print()
print("View in Kibana: Observability → SLOs")
