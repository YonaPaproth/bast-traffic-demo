"""
source_catalog.py — registry of data sources available to the cockpit agent.

Each entry describes one logical source: what it contains, what fields it exposes,
its known limitations, and how the backend queries it. The agent receives only
entries backed by an implemented adapter.
"""

SOURCE_CATALOG: dict[str, dict] = {
    "bast_traffic": {
        "id": "bast_traffic",
        "name": "BASt Federal Highway Traffic Counts",
        "description": (
            "Hourly vehicle counts at ~1,943 counting stations on German Autobahns (A) "
            "and Bundesstrassen (B). Covers H1 2025 (Jan–Jun 2025) and H1 2026 "
            "(Jan–Jun 2026). ~15M station-hour observations."
        ),
        "fields": [
            "station_id", "station_name", "state", "road_class", "road_number",
            "lat", "lon", "date", "hour",
            "kfz_r1", "kfz_r2", "kfz_total", "pkw_r1", "sv_r1",
        ],
        "date_coverage": "2025-01-01 to 2026-06-30",
        "update_frequency": "static (Iceberg snapshot, updated monthly at most)",
        "attribution": "Bundesanstalt für Straßenwesen (BASt), open data",
        "limitations": [
            "2026-01: sv_r1 and pkw_r1 are NULL (old schema). Use COALESCE(sv_r1, 0).",
            "2026-01: kfz_r2 unreliable — exclude or treat as NULL.",
            "hour is BASt format: 1 = 00:00–01:00, 24 = 23:00–00:00.",
            "sv_r1 = Schwerverkehr (trucks + buses) direction 1 only.",
            "Stations with S02-06 format report pkw_r1 = 0 (no per-type breakdown).",
            "kfz_r1/kfz_r2 are directional counts, not unique vehicle counts.",
        ],
        "adapter": "duckdb_iceberg",
    },
}


def catalog_description() -> str:
    """Short multi-line description of all catalog sources for inclusion in agent system prompt."""
    lines: list[str] = []
    for s in SOURCE_CATALOG.values():
        lines.append(f"Source: {s['id']} — {s['name']}")
        lines.append(f"  {s['description']}")
        lines.append(f"  Fields: {', '.join(s['fields'])}")
        lines.append(f"  Coverage: {s['date_coverage']}")
        for lim in s["limitations"]:
            lines.append(f"  ⚠ {lim}")
    return "\n".join(lines)
