"""
ontology.py — Domain object layer for the BASt Traffic Demo.

Defines Station and Corridor as structured dicts hydrated from DuckDB queries.
Functions accept (con, src) — a DuckDB connection and the table expression
returned by parquet_source() — so they stay decoupled from main.py's config.

Object IDs:
  station  → station_id string, e.g. "NW5048"
  corridor → "{road_class}{road_number}/{state}", e.g. "A1/NW", "B10/RP"
"""

from __future__ import annotations
import json


def get_station(con, src: str, station_id: str) -> dict:
    """Return a hydrated Station object for the given station_id."""
    # Core attributes + H1 2026 aggregates in one scan
    row = con.execute(f"""
        WITH base AS (
            SELECT
                station_id,
                ANY_VALUE(station_name)   AS station_name,
                ANY_VALUE(state)          AS state,
                ANY_VALUE(road_class)     AS road_class,
                ANY_VALUE(road_number)    AS road_number,
                ROUND(AVG(lat), 6)        AS lat,
                ROUND(AVG(lon), 6)        AS lon,
                SUM(kfz_r1)              AS total_kfz,
                COUNT(DISTINCT date)      AS active_days,
                SUM(COALESCE(sv_r1, 0))  AS total_sv,
                SUM(CASE WHEN COALESCE(pkw_r1, 0) > 0 THEN 1 ELSE 0 END) AS hours_with_pkw
            FROM {src}
            WHERE station_id = '{station_id}'
              AND YEAR(date) = 2026 AND MONTH(date) BETWEEN 1 AND 6
            GROUP BY station_id
        )
        SELECT
            *,
            ROUND(total_kfz::DOUBLE / NULLIF(active_days, 0), 0) AS avg_daily_kfz,
            ROUND(total_sv * 100.0 / NULLIF(total_kfz, 0), 1)    AS sv_share_pct
        FROM base
    """).fetchone()

    if row is None:
        return {"error": f"Station '{station_id}' not found in H1 2026 data."}

    cols = [
        "station_id", "station_name", "state", "road_class", "road_number",
        "lat", "lon", "total_kfz", "active_days", "total_sv",
        "hours_with_pkw", "avg_daily_kfz", "sv_share_pct",
    ]
    d = dict(zip(cols, row))

    # Congestion percentile: rank this station among all stations by H1 2026 volume
    pct_row = con.execute(f"""
        WITH totals AS (
            SELECT station_id, SUM(kfz_r1) AS total_kfz
            FROM {src}
            WHERE YEAR(date) = 2026 AND MONTH(date) BETWEEN 1 AND 6
            GROUP BY station_id
        )
        SELECT ROUND(PERCENT_RANK() OVER (ORDER BY total_kfz) * 100, 1) AS pct
        FROM totals
        WHERE station_id = '{station_id}'
    """).fetchone()
    congestion_pct = pct_row[0] if pct_row else None

    # Maintenance priority: heavier traffic mix + higher congestion = higher score (0-10)
    sv_share = d.get("sv_share_pct") or 0.0
    maintenance_score = round(sv_share * (congestion_pct or 0) / 1000, 1) if congestion_pct else None

    return {
        "type":                    "Station",
        "station_id":              d["station_id"],
        "station_name":            d["station_name"],
        "state":                   d["state"],
        "road":                    f"{d['road_class']}{d['road_number']}",
        "lat":                     d["lat"],
        "lon":                     d["lon"],
        "h1_2026": {
            "avg_daily_kfz":       int(d["avg_daily_kfz"]) if d["avg_daily_kfz"] else None,
            "total_kfz":           int(d["total_kfz"]) if d["total_kfz"] else None,
            "sv_share_pct":        d["sv_share_pct"],
            "congestion_percentile": congestion_pct,
        },
        "maintenance_priority_score": maintenance_score,
        "note": (
            "congestion_percentile: 0=quietest station, 100=busiest. "
            "maintenance_priority_score: 0-10, higher = more urgent (SV share × congestion)."
        ),
    }


def get_corridor(con, src: str, road_class: str, road_number: str, state: str) -> dict:
    """Return a hydrated Corridor object for road_class + road_number + state."""
    row = con.execute(f"""
        SELECT
            ANY_VALUE(road_class)         AS road_class,
            ANY_VALUE(road_number)        AS road_number,
            ANY_VALUE(state)              AS state,
            COUNT(DISTINCT station_id)    AS num_stations,
            SUM(kfz_r1)                  AS total_kfz,
            COUNT(DISTINCT date)          AS active_days,
            ROUND(SUM(kfz_r1)::DOUBLE / NULLIF(COUNT(DISTINCT date), 0), 0) AS avg_daily_kfz,
            ROUND(SUM(COALESCE(sv_r1, 0)) * 100.0 / NULLIF(SUM(kfz_r1), 0), 1) AS sv_share_pct
        FROM {src}
        WHERE road_class = '{road_class}'
          AND road_number = '{road_number}'
          AND state = '{state}'
          AND YEAR(date) = 2026 AND MONTH(date) BETWEEN 1 AND 6
    """).fetchone()

    if row is None or row[3] == 0:
        return {"error": f"Corridor '{road_class}{road_number}/{state}' not found in H1 2026 data."}

    cols = ["road_class", "road_number", "state", "num_stations",
            "total_kfz", "active_days", "avg_daily_kfz", "sv_share_pct"]
    d = dict(zip(cols, row))

    # Peak hour across the corridor
    peak_row = con.execute(f"""
        SELECT hour, SUM(kfz_r1) AS vol
        FROM {src}
        WHERE road_class = '{road_class}'
          AND road_number = '{road_number}'
          AND state = '{state}'
          AND YEAR(date) = 2026 AND MONTH(date) BETWEEN 1 AND 6
        GROUP BY hour
        ORDER BY vol DESC
        LIMIT 1
    """).fetchone()
    peak_hour = peak_row[0] if peak_row else None

    # Top 3 stations by volume
    top_stations = con.execute(f"""
        SELECT station_id, ANY_VALUE(station_name) AS name, SUM(kfz_r1) AS vol
        FROM {src}
        WHERE road_class = '{road_class}'
          AND road_number = '{road_number}'
          AND state = '{state}'
          AND YEAR(date) = 2026 AND MONTH(date) BETWEEN 1 AND 6
        GROUP BY station_id
        ORDER BY vol DESC
        LIMIT 3
    """).df().to_dict(orient="records")

    return {
        "type":        "Corridor",
        "corridor_id": f"{road_class}{road_number}/{state}",
        "road":        f"{road_class}{road_number}",
        "state":       d["state"],
        "h1_2026": {
            "num_stations":  d["num_stations"],
            "total_kfz":     int(d["total_kfz"]) if d["total_kfz"] else None,
            "avg_daily_kfz": int(d["avg_daily_kfz"]) if d["avg_daily_kfz"] else None,
            "sv_share_pct":  d["sv_share_pct"],
            "peak_hour":     peak_hour,
        },
        "top_stations": top_stations,
        "note": (
            "peak_hour uses BASt convention: hour 1 = 00:00-01:00, hour 24 = 23:00-00:00. "
            "top_stations ranked by H1 2026 kfz_r1 volume."
        ),
    }


def get_object(con, src: str, object_type: str, object_id: str) -> dict:
    """Dispatcher: resolve object_type + object_id to a hydrated domain object.

    object_type: "station" | "corridor"
    object_id:
      station  → station_id, e.g. "NW5048"
      corridor → "{road_class}{road_number}/{state}", e.g. "A1/NW"
    """
    ot = object_type.lower().strip()

    if ot == "station":
        return get_station(con, src, object_id.strip())

    if ot == "corridor":
        # Parse "A1/NW" or "B10/RP" → road_class, road_number, state
        if "/" not in object_id:
            return {
                "error": (
                    f"Corridor id must be '{{road_class}}{{road_number}}/{{state}}', "
                    f"e.g. 'A1/NW'. Got: '{object_id}'"
                )
            }
        road_part, state = object_id.split("/", 1)
        road_class = road_part[0].upper()          # "A" or "B"
        road_number = road_part[1:]                # "1", "61", etc.
        return get_corridor(con, src, road_class, road_number, state.upper())

    return {"error": f"Unknown object_type '{object_type}'. Valid values: 'station', 'corridor'."}
