#!/usr/bin/env python3
"""
Generate Kibana saved-objects NDJSON for all Rachio irrigation dashboards.

Usage:
    python3 kibana/generate_dashboards.py

Output:
    kibana/rachio-dashboards.ndjson

Import:
    Kibana → Stack Management → Saved Objects → Import
    Tick "Overwrite existing objects" when re-importing after changes.

Dashboards generated:
    1. 🌱 Zone Health Overview
    2. 🌧️  Weather & Watering Intelligence
    3. 📋 Watering Activity Log
    4. 📡 Device Status
    5. 💧 Water Budget & Schedule Health
"""

import json
import pathlib

OUT_DIR = pathlib.Path(__file__).parent
OUT_FILE = OUT_DIR / "rachio-dashboards.ndjson"


# ═══════════════════════════════════════════════════════════════════════════
# Low-level helpers
# ═══════════════════════════════════════════════════════════════════════════

def saved_object(type_, id_, attributes, references=None, migration_version=None):
    obj = {
        "type": type_,
        "id": id_,
        "attributes": attributes,
        "references": references or [],
    }
    # Kibana 9.x requires typeMigrationVersion on lens/dashboard objects or it
    # tries to apply all migrations from scratch and throws a 500.
    if migration_version:
        obj["typeMigrationVersion"] = migration_version
    return obj


def data_view(id_, title):
    return saved_object("index-pattern", id_, {
        "title": title,
        "timeFieldName": "@timestamp",
        "allowNoIndex": True,
    })


# ═══════════════════════════════════════════════════════════════════════════
# Lens column builders
# ═══════════════════════════════════════════════════════════════════════════

def col_date_hist(id_, interval="auto"):
    return id_, {
        "label": "@timestamp",
        "dataType": "date",
        "operationType": "date_histogram",
        "sourceField": "@timestamp",
        "isBucketed": True,
        "params": {"interval": interval, "includeEmptyRows": True, "dropPartials": False},
    }


def col_terms(id_, label, field, size=10, order="count"):
    order_by = (
        {"type": order} if order in ("count", "alphabetical")
        else {"type": "column", "columnId": order}
    )
    return id_, {
        "label": label,
        "dataType": "string",
        "operationType": "terms",
        "sourceField": field,
        "isBucketed": True,
        "params": {
            "size": size,
            "orderBy": order_by,
            "orderDirection": "desc",
            "otherBucket": False,
            "missingBucket": False,
        },
    }


def col_last_value(id_, label, field, dtype="number"):
    return id_, {
        "label": label,
        "dataType": dtype,
        "operationType": "last_value",
        "sourceField": field,
        "isBucketed": False,
        "params": {"sortField": "@timestamp", "showArrayValues": False},
    }


def col_avg(id_, label, field):
    return id_, {
        "label": label,
        "dataType": "number",
        "operationType": "avg",
        "sourceField": field,
        "isBucketed": False,
        "params": {},
    }


def col_count(id_, label="Count"):
    return id_, {
        "label": label,
        "dataType": "number",
        "operationType": "count",
        "isBucketed": False,
        "params": {},
        "sourceField": "___records___",
    }


def make_layer(layer_id, *col_pairs):
    cols = dict(col_pairs)
    return {layer_id: {
        "columns": cols,
        "columnOrder": list(cols.keys()),
        "incompleteColumns": {},
        "sampling": 1,          # required by Kibana 9.x
    }}


# ═══════════════════════════════════════════════════════════════════════════
# Visualization type builders
# ═══════════════════════════════════════════════════════════════════════════

def viz_xy(layer_id, x_col, y_cols, series="line", split=None, colors=None):
    y_cfg = []
    for i, y in enumerate(y_cols):
        c = {"forAccessor": y}
        if colors and i < len(colors):
            c["color"] = colors[i]
        y_cfg.append(c)
    layer = {
        "layerId": layer_id,
        "accessors": list(y_cols),
        "position": "top",
        "seriesType": series,
        "showGridlines": False,
        "layerType": "data",
        "xAccessor": x_col,
        "yConfig": y_cfg,
    }
    if split:
        layer["splitAccessor"] = split
    return {
        "legend": {"isVisible": True, "position": "bottom"},
        "valueLabels": "hide",
        "fittingFunction": "None",
        "axisTitlesVisibilitySettings": {"x": False, "yLeft": True, "yRight": True},
        "tickLabelsVisibilitySettings": {"x": True, "yLeft": True, "yRight": True},
        "labelsOrientation": {"x": 0, "yLeft": 0, "yRight": 0},
        "gridlinesVisibilitySettings": {"x": True, "yLeft": True, "yRight": False},
        "preferredSeriesType": series,
        "layers": [layer],
    }


def viz_table(layer_id, col_ids, sort_col=None):
    return {
        "columns": [{"columnId": c, "isTransposed": False} for c in col_ids],
        "layerId": layer_id,
        "layerType": "data",
        "rowHeight": "single",
        "rowHeightLines": 1,
        **({"sorting": {"columnId": sort_col, "direction": "desc"}} if sort_col else {}),
    }



def viz_pie(layer_id, group_col, size_col):
    return {
        "shape": "donut",
        "layers": [{
            "layerId": layer_id,
            "layerType": "data",
            "groups": [group_col],
            "metrics": [size_col],
            "colorMapping": {},
            "numberDisplay": "percent",
            "categoryDisplay": "default",
            "legendDisplay": "default",
            "nestedLegend": False,
        }],
    }


def lens_state(layers, visualization, query="", filters=None):
    return {
        "datasourceStates": {"formBased": {"layers": layers}},
        "visualization": visualization,
        "query": {"query": query, "language": "kuery"},
        "filters": filters or [],
        "internalReferences": [],
        "adHocDataViews": {},
    }


def lens_viz(id_, title, viz_type, layers, visualization, dv_id):
    layer_id = list(layers.keys())[0]
    refs = [{"type": "index-pattern", "id": dv_id,
             "name": f"indexpattern-datasource-layer-{layer_id}"}]
    return saved_object("lens", id_, {
        "title": title,
        "description": "",
        "visualizationType": viz_type,
        "state": lens_state(layers, visualization),
    }, refs, migration_version="8.9.0")


# ═══════════════════════════════════════════════════════════════════════════
# Data Views
# ═══════════════════════════════════════════════════════════════════════════

# Use the existing data views already in Kibana (created automatically by Elastic).
# The metrics-rachio* view covers all metrics streams.
# A separate logs view is created here for the events stream.
DATA_VIEWS = [
    data_view("dv-rachio-logs", "logs-rachio.events-default"),
]

# Existing data view ID for metrics-rachio* (auto-created by Elastic Serverless)
METRICS_DV = "ea1e8007-1435-404f-ae34-cfd52b5a5d87"
LOGS_DV    = "dv-rachio-logs"

ZN = METRICS_DV
DV = METRICS_DV
CS = METRICS_DV
WX = METRICS_DV
SC = METRICS_DV
EV = LOGS_DV


# ═══════════════════════════════════════════════════════════════════════════
# Dashboard 1 — Zone Health Overview
# ═══════════════════════════════════════════════════════════════════════════

viz_zone_moisture = lens_viz(
    "viz-zone-moisture", "Zone Moisture % (Latest)", "lnsXY",
    make_layer("L",
        col_terms("c-zone", "Zone", "zone_name", size=20, order="alphabetical"),
        col_last_value("c-moist", "Moisture %", "moisture_percent"),
    ),
    viz_xy("L", "c-zone", ["c-moist"], series="bar_horizontal", colors=["#00BFB3"]),
    ZN,
)

viz_zone_duration = lens_viz(
    "viz-zone-duration", "Last Watering Duration (Latest, seconds)", "lnsXY",
    make_layer("L",
        col_terms("c-zone", "Zone", "zone_name", size=20, order="alphabetical"),
        col_last_value("c-dur", "Duration (s)", "last_water_duration_seconds"),
    ),
    viz_xy("L", "c-zone", ["c-dur"], series="bar_horizontal", colors=["#1BA9F5"]),
    ZN,
)

viz_zone_moisture_trend = lens_viz(
    "viz-zone-moisture-trend", "Zone Moisture % Over Time", "lnsXY",
    make_layer("L",
        col_date_hist("c-ts"),
        col_terms("c-zone", "Zone", "zone_name", size=6, order="alphabetical"),
        col_avg("c-moist", "Moisture %", "moisture_percent"),
    ),
    viz_xy("L", "c-ts", ["c-moist"], series="line", split="c-zone"),
    ZN,
)

viz_zone_table = lens_viz(
    "viz-zone-table", "Zone Summary Table", "lnsDatatable",
    make_layer("L",
        col_terms("c-zone", "Zone", "zone_name", size=20, order="alphabetical"),
        col_last_value("c-moist",    "Moisture %",        "moisture_percent"),
        col_last_value("c-dur",      "Last Duration (s)", "last_water_duration_seconds"),
        col_last_value("c-last",     "Last Watered",      "last_water_date",    dtype="string"),
        col_last_value("c-next",     "Next Run",          "next_run_date",      dtype="string"),
        col_last_value("c-soil",     "Soil Type",         "soil_type",          dtype="string"),
        col_last_value("c-nozzle",   "Nozzle",            "nozzle_name",        dtype="string"),
    ),
    viz_table("L", ["c-zone", "c-moist", "c-dur", "c-last", "c-next", "c-soil", "c-nozzle"],
              sort_col="c-moist"),
    ZN,
)


# ═══════════════════════════════════════════════════════════════════════════
# Dashboard 2 — Weather & Watering Intelligence
# ═══════════════════════════════════════════════════════════════════════════

viz_weather_temp = lens_viz(
    "viz-weather-temp", "Temperature Forecast °F", "lnsXY",
    make_layer("L",
        col_date_hist("c-ts"),
        col_avg("c-hi", "High °F",  "temperature_high_f"),
        col_avg("c-lo", "Low °F",   "temperature_low_f"),
    ),
    viz_xy("L", "c-ts", ["c-hi", "c-lo"], series="line", colors=["#FF6B6B", "#1BA9F5"]),
    WX,
)

viz_weather_precip = lens_viz(
    "viz-weather-precip", "Precipitation Probability & Accumulation", "lnsXY",
    make_layer("L",
        col_date_hist("c-ts"),
        col_avg("c-prob", "Precip Probability (0-1)", "precip_probability"),
        col_avg("c-acc",  "Accumulation (in)",        "precip_accumulation_inches"),
    ),
    viz_xy("L", "c-ts", ["c-prob", "c-acc"], series="bar", colors=["#006BB4", "#54B399"]),
    WX,
)

viz_weather_et = lens_viz(
    "viz-weather-et", "Evapotranspiration Rate (mm/day)", "lnsXY",
    make_layer("L",
        col_date_hist("c-ts"),
        col_avg("c-et", "ET mm/day", "evapotranspiration_mm"),
    ),
    viz_xy("L", "c-ts", ["c-et"], series="area", colors=["#00BFB3"]),
    WX,
)

viz_weather_humidity = lens_viz(
    "viz-weather-humidity", "Humidity %", "lnsXY",
    make_layer("L",
        col_date_hist("c-ts"),
        col_avg("c-hum", "Humidity %", "humidity_pct"),
    ),
    viz_xy("L", "c-ts", ["c-hum"], series="line", colors=["#54B399"]),
    WX,
)

viz_weather_today = lens_viz(
    "viz-weather-today", "Current Conditions", "lnsDatatable",
    make_layer("L",
        col_last_value("c-hi",   "Temp High °F",    "temperature_high_f"),
        col_last_value("c-lo",   "Temp Low °F",     "temperature_low_f"),
        col_last_value("c-hum",  "Humidity %",      "humidity_pct"),
        col_last_value("c-prob", "Precip Prob",     "precip_probability"),
        col_last_value("c-acc",  "Precip (in)",     "precip_accumulation_inches"),
        col_last_value("c-et",   "ET mm/day",       "evapotranspiration_mm"),
        col_last_value("c-wind", "Wind mph",        "wind_speed_mph"),
        col_last_value("c-uv",   "UV Index",        "uv_index"),
        col_last_value("c-dew",  "Dew Point °F",    "dew_point_f"),
        col_last_value("c-sum",  "Summary",         "weather_summary", dtype="string"),
    ),
    viz_table("L", ["c-hi","c-lo","c-hum","c-prob","c-acc","c-et","c-wind","c-uv","c-dew","c-sum"]),
    WX,
)


# ═══════════════════════════════════════════════════════════════════════════
# Dashboard 3 — Watering Activity Log
# ═══════════════════════════════════════════════════════════════════════════

viz_events_timeline = lens_viz(
    "viz-events-timeline", "Events Over Time by Category", "lnsXY",
    make_layer("L",
        col_date_hist("c-ts"),
        col_terms("c-cat", "Category", "category", size=10),
        col_count("c-cnt", "Events"),
    ),
    viz_xy("L", "c-ts", ["c-cnt"], series="bar_stacked", split="c-cat"),
    EV,
)

viz_events_table = lens_viz(
    "viz-events-table", "Recent Events", "lnsDatatable",
    make_layer("L",
        col_date_hist("c-ts", interval="1m"),
        col_last_value("c-cat",    "Category", "category", dtype="string"),
        col_last_value("c-type",   "Type",     "type",     dtype="string"),
        col_last_value("c-sub",    "Subtype",  "subtype",  dtype="string"),
        col_last_value("c-sum",    "Summary",  "summary",  dtype="string"),
    ),
    viz_table("L", ["c-ts", "c-cat", "c-type", "c-sub", "c-sum"], sort_col="c-ts"),
    EV,
)

viz_events_donut = lens_viz(
    "viz-events-donut", "Event Category Breakdown", "lnsPie",
    make_layer("L",
        col_terms("c-cat", "Category", "category", size=10),
        col_count("c-cnt", "Events"),
    ),
    viz_pie("L", "c-cat", "c-cnt"),
    EV,
)

viz_events_errors = lens_viz(
    "viz-events-errors", "Total Events (Selected Period)", "lnsDatatable",
    make_layer("L",
        col_terms("c-cat", "Category", "category", size=10),
        col_count("c-cnt", "Events"),
    ),
    viz_table("L", ["c-cat", "c-cnt"], sort_col="c-cnt"),
    EV,
)


# ═══════════════════════════════════════════════════════════════════════════
# Dashboard 4 — Device Status
# ═══════════════════════════════════════════════════════════════════════════

viz_device_online = lens_viz(
    "viz-device-online", "Device Online Over Time", "lnsXY",
    make_layer("L",
        col_date_hist("c-ts"),
        col_avg("c-online", "Online (1=yes, 0=no)", "is_online"),
    ),
    viz_xy("L", "c-ts", ["c-online"], series="area", colors=["#00BFB3"]),
    DV,
)

viz_device_rain_delay = lens_viz(
    "viz-device-rain-delay", "Rain Delay Over Time", "lnsXY",
    make_layer("L",
        col_date_hist("c-ts"),
        col_avg("c-rd", "Rain Delay Active (1=yes)", "rain_delay_active"),
    ),
    viz_xy("L", "c-ts", ["c-rd"], series="area", colors=["#006BB4"]),
    DV,
)

viz_device_details = lens_viz(
    "viz-device-details", "Device Details", "lnsDatatable",
    make_layer("L",
        col_terms("c-dev",    "Device",        "device_name",       size=5),
        col_last_value("c-status",  "Status",       "status",           dtype="string"),
        col_last_value("c-model",   "Model",        "model",            dtype="string"),
        col_last_value("c-fw",      "Firmware",     "firmware_version", dtype="string"),
        col_last_value("c-tz",      "Timezone",     "timezone",         dtype="string"),
        col_last_value("c-zones",   "Zones",        "zone_count"),
        col_last_value("c-online",  "Online",       "is_online"),
        col_last_value("c-paused",  "Paused",       "paused"),
    ),
    viz_table("L", ["c-dev","c-status","c-model","c-fw","c-tz","c-zones","c-online","c-paused"]),
    DV,
)

viz_device_curr_sched = lens_viz(
    "viz-device-curr-sched", "Current Active Schedule", "lnsDatatable",
    make_layer("L",
        col_last_value("c-dev",    "Device",          "device_name",    dtype="string"),
        col_last_value("c-active", "Active",          "schedule_active"),
        col_last_value("c-zone",   "Zone Running",    "zone_name",      dtype="string"),
        col_last_value("c-type",   "Schedule Type",   "schedule_type",  dtype="string"),
        col_last_value("c-dur",    "Duration (s)",    "duration_seconds"),
        col_last_value("c-start",  "Started",         "start_time",     dtype="string"),
    ),
    viz_table("L", ["c-dev","c-active","c-zone","c-type","c-dur","c-start"]),
    CS,
)

viz_device_zone_count = lens_viz(
    "viz-device-zone-count", "Zone Count", "lnsDatatable",
    make_layer("L",
        col_terms("c-dev", "Device", "device_name", size=5),
        col_last_value("c-zones", "Zone Count", "zone_count"),
        col_last_value("c-online", "Online", "is_online"),
    ),
    viz_table("L", ["c-dev", "c-zones", "c-online"]),
    DV,
)


# ═══════════════════════════════════════════════════════════════════════════
# Dashboard 5 — Water Budget & Schedule Health
# ═══════════════════════════════════════════════════════════════════════════

viz_sched_budget = lens_viz(
    "viz-sched-budget", "Water Budget % by Schedule", "lnsXY",
    make_layer("L",
        col_terms("c-name", "Schedule", "schedule_name", size=10, order="alphabetical"),
        col_last_value("c-budget", "Water Budget %", "water_budget_pct"),
    ),
    viz_xy("L", "c-name", ["c-budget"], series="bar_horizontal", colors=["#006BB4"]),
    SC,
)

viz_sched_adjustment = lens_viz(
    "viz-sched-adjustment", "Water Budget % Trend Over Time", "lnsXY",
    make_layer("L",
        col_date_hist("c-ts"),
        col_terms("c-name", "Schedule", "schedule_name", size=5),
        col_avg("c-budget", "Water Budget %", "water_budget_pct"),
    ),
    viz_xy("L", "c-ts", ["c-budget"], series="line", split="c-name"),
    SC,
)

viz_sched_table = lens_viz(
    "viz-sched-table", "Schedule Details", "lnsDatatable",
    make_layer("L",
        col_terms("c-name",    "Schedule",      "schedule_name",        size=10),
        col_last_value("c-type",     "Type",          "schedule_type",    dtype="string"),
        col_last_value("c-enabled",  "Enabled",       "enabled"),
        col_last_value("c-budget",   "Water Budget %","water_budget_pct"),
        col_last_value("c-adj",      "Seasonal Adj %","seasonal_adjustment"),
        col_last_value("c-zones",    "Zones",         "zone_count"),
        col_last_value("c-dur",      "Total Dur (s)", "total_duration_seconds"),
    ),
    viz_table("L", ["c-name","c-type","c-enabled","c-budget","c-adj","c-zones","c-dur"]),
    SC,
)

viz_sched_seasonal_trend = lens_viz(
    "viz-sched-seasonal-trend", "Seasonal Adjustment % Trend", "lnsXY",
    make_layer("L",
        col_date_hist("c-ts"),
        col_terms("c-name", "Schedule", "schedule_name", size=5),
        col_avg("c-adj", "Seasonal Adj %", "seasonal_adjustment"),
    ),
    viz_xy("L", "c-ts", ["c-adj"], series="line", split="c-name", colors=["#E7664C"]),
    SC,
)


# ═══════════════════════════════════════════════════════════════════════════
# Dashboard builder
# ═══════════════════════════════════════════════════════════════════════════

def make_dashboard(id_, title, description, panels):
    """
    panels = list of dicts: {id, x, y, w, h}
    Kibana grid is 48 columns wide; heights in rows (~120px each).
    """
    panel_list = []
    refs = []
    for i, p in enumerate(panels):
        pref = f"panel_{i}"
        refs.append({"type": "lens", "id": p["id"], "name": pref})
        panel_list.append({
            "type": "lens",
            "gridData": {"x": p["x"], "y": p["y"], "w": p["w"], "h": p["h"], "i": str(i)},
            "panelIndex": str(i),
            "embeddableConfig": {"hidePanelTitles": False},
            "title": "",
            "panelRefName": pref,
        })
    return saved_object("dashboard", id_, {
        "title": title,
        "description": description,
        "hits": 0,
        "kibanaSavedObjectMeta": {
            "searchSourceJSON": json.dumps({
                "query": {"query": "", "language": "kuery"},
                "filter": [],
            })
        },
        "optionsJSON": json.dumps({
            "useMargins": True,
            "syncColors": True,
            "syncCursor": True,
            "syncTooltips": False,
            "hidePanelTitles": False,
        }),
        "panelsJSON": json.dumps(panel_list),
        "timeRestore": False,
        "version": 1,
    }, refs, migration_version="8.9.0")


DASHBOARDS = [
    make_dashboard(
        "dash-zone-health",
        "🌱 Zone Health Overview",
        "Soil moisture levels, watering history, and zone status for all irrigation zones.",
        [
            {"id": "viz-zone-moisture",       "x":  0, "y":  0, "w": 24, "h": 15},
            {"id": "viz-zone-duration",       "x": 24, "y":  0, "w": 24, "h": 15},
            {"id": "viz-zone-moisture-trend", "x":  0, "y": 15, "w": 48, "h": 15},
            {"id": "viz-zone-table",          "x":  0, "y": 30, "w": 48, "h": 15},
        ],
    ),
    make_dashboard(
        "dash-weather",
        "🌧️ Weather & Watering Intelligence",
        "Forecast temperature, precipitation, evapotranspiration, and humidity trends.",
        [
            {"id": "viz-weather-today",    "x":  0, "y":  0, "w": 48, "h": 10},
            {"id": "viz-weather-temp",     "x":  0, "y": 10, "w": 24, "h": 15},
            {"id": "viz-weather-precip",   "x": 24, "y": 10, "w": 24, "h": 15},
            {"id": "viz-weather-et",       "x":  0, "y": 25, "w": 24, "h": 15},
            {"id": "viz-weather-humidity", "x": 24, "y": 25, "w": 24, "h": 15},
        ],
    ),
    make_dashboard(
        "dash-events",
        "📋 Watering Activity Log",
        "Event history: watering starts/stops, rain delays, schedule skips, and faults.",
        [
            {"id": "viz-events-errors",   "x":  0, "y":  0, "w": 12, "h": 10},
            {"id": "viz-events-donut",    "x": 12, "y":  0, "w": 18, "h": 15},
            {"id": "viz-events-timeline", "x":  0, "y": 10, "w": 12, "h": 15},
            {"id": "viz-events-table",    "x":  0, "y": 25, "w": 48, "h": 15},
        ],
    ),
    make_dashboard(
        "dash-device-status",
        "📡 Device Status",
        "Controller online/offline history, rain delay, zone count, and current running schedule.",
        [
            {"id": "viz-device-zone-count",   "x":  0, "y":  0, "w": 12, "h":  8},
            {"id": "viz-device-details",      "x": 12, "y":  0, "w": 36, "h": 10},
            {"id": "viz-device-online",       "x":  0, "y": 10, "w": 24, "h": 12},
            {"id": "viz-device-rain-delay",   "x": 24, "y": 10, "w": 24, "h": 12},
            {"id": "viz-device-curr-sched",   "x":  0, "y": 22, "w": 48, "h": 12},
        ],
    ),
    make_dashboard(
        "dash-schedule-health",
        "💧 Water Budget & Schedule Health",
        "Schedule configuration, water budget percentages, and seasonal adjustment trends.",
        [
            {"id": "viz-sched-budget",          "x":  0, "y":  0, "w": 24, "h": 15},
            {"id": "viz-sched-seasonal-trend",  "x": 24, "y":  0, "w": 24, "h": 15},
            {"id": "viz-sched-adjustment",      "x":  0, "y": 15, "w": 48, "h": 15},
            {"id": "viz-sched-table",           "x":  0, "y": 30, "w": 48, "h": 12},
        ],
    ),
]


# ═══════════════════════════════════════════════════════════════════════════
# Assemble and write
# ═══════════════════════════════════════════════════════════════════════════

VISUALIZATIONS = [
    # Zone Health
    viz_zone_moisture, viz_zone_duration, viz_zone_moisture_trend, viz_zone_table,
    # Weather
    viz_weather_temp, viz_weather_precip, viz_weather_et, viz_weather_humidity, viz_weather_today,
    # Events
    viz_events_timeline, viz_events_table, viz_events_donut, viz_events_errors,
    # Device
    viz_device_online, viz_device_rain_delay, viz_device_details,
    viz_device_curr_sched, viz_device_zone_count,
    # Schedules
    viz_sched_budget, viz_sched_adjustment, viz_sched_table, viz_sched_seasonal_trend,
]

ALL_OBJECTS = DATA_VIEWS + VISUALIZATIONS + DASHBOARDS

with OUT_FILE.open("w") as f:
    for obj_ in ALL_OBJECTS:
        f.write(json.dumps(obj_, separators=(",", ":")) + "\n")

print(f"✓  Written {len(ALL_OBJECTS)} saved objects → {OUT_FILE}")
print(f"   {len(DATA_VIEWS)} data views")
print(f"   {len(VISUALIZATIONS)} visualizations")
print(f"   {len(DASHBOARDS)} dashboards")
print()
print("Import steps:")
print("  1. Kibana → Stack Management → Saved Objects → Import")
print("  2. Choose kibana/rachio-dashboards.ndjson")
print("  3. Tick 'Overwrite existing objects'")
print("  4. Open Dashboards → filter by 'rachio'")
