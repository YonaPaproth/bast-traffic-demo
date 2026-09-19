"""
ontology.py — Domain object layer for the BASt Traffic Demo.

Functions accept (con, src, snapshot_id) — a DuckDB connection, the table
expression from parquet_source(), and the active Iceberg snapshot_id string
— so they stay decoupled from main.py configuration.

Object IDs:
  station  → station_id string, e.g. "NW5048"
  corridor → "{road_class}{road_number}/{state}", e.g. "A1/NW", "B10/RP"

Metric definitions:
  traffic_volume_percentile  — PERCENT_RANK() of this station/corridor by
    H1 2026 kfz_r1 (direction 1) among all stations in the same dataset.
    0 = lowest volume, 100 = highest. Vehicle counts, not speed or delay,
    so this measures relative throughput exposure, not congestion.

  traffic_exposure_heuristic — experimental score (0-10) combining SV share
    (proportion of heavy vehicles) and volume percentile, as a proxy for
    road-surface stress. Formula: sv_share_pct × volume_percentile / 1000.
    Not validated against maintenance inspection data. Use as a triage
    signal only, not a maintenance decision.

  sv_share_pct — SUM(sv_r1) / SUM(kfz_r1) × 100. sv_r1 is the pre-computed
    Schwerverkehr aggregate from BASt (trucks + buses, direction 1). Stations
    with format S02-06 report sv_r1 = 0 (no vehicle-type breakdown available);
    this depresses the share for those stations. NULL sv_r1 values are treated
    as 0 via COALESCE — they do not mean zero heavy traffic.

  Corridor totals are summed station observations. The same vehicle can pass
    multiple counting stations on a corridor, so totals cannot be interpreted
    as unique vehicle counts.
"""

from __future__ import annotations
import re


# ── Input validation ───────────────────────────────────────────────────────────

_STATION_ID_RE  = re.compile(r'^[A-Z]{1,2}[A-Z0-9]{1,8}$', re.IGNORECASE)
_ROAD_NUMBER_RE = re.compile(r'^\d{1,4}$')
_STATE_RE       = re.compile(r'^[A-Z]{2}$', re.IGNORECASE)
_ROAD_CLASS_RE  = re.compile(r'^[AB]$', re.IGNORECASE)

VALID_STATES = {
    "NW", "BY", "HE", "NI", "BW", "RP", "ST", "TH", "SN",
    "SH", "SL", "BB", "BE", "MV", "HH", "HB",
}

_REPORTING_PERIOD = "H1 2026 (2026-01-01 to 2026-06-30)"
_DIRECTION        = "direction 1 (kfz_r1 / sv_r1)"
_METRIC_VERSION   = "1.0"


def _validate_station_id(station_id: str) -> str | None:
    """Return error string if invalid, else None."""
    s = station_id.strip()
    if not s or not _STATION_ID_RE.match(s):
        return (
            f"Invalid station_id '{station_id}'. "
            "Expected 2-10 alphanumeric characters, e.g. 'NW5048'."
        )
    return None


def _validate_corridor(road_class: str, road_number: str, state: str) -> str | None:
    if not _ROAD_CLASS_RE.match(road_class):
        return f"road_class must be 'A' or 'B', got '{road_class}'."
    if not _ROAD_NUMBER_RE.match(road_number):
        return f"road_number must be 1-4 digits, got '{road_number}'."
    if state.upper() not in VALID_STATES:
        return f"state '{state}' not recognised. Valid values: {sorted(VALID_STATES)}."
    return None


def _provenance(snapshot_id: str | None) -> dict:
    return {
        "snapshot_id":      snapshot_id,
        "reporting_period": _REPORTING_PERIOD,
        "direction":        _DIRECTION,
        "metric_version":   _METRIC_VERSION,
    }


# ── Station ────────────────────────────────────────────────────────────────────

def get_station(con, src: str, station_id: str, snapshot_id: str | None = None) -> dict:
    """Return a hydrated Station object for the given station_id."""
    err = _validate_station_id(station_id)
    if err:
        return {"error": err}

    sid = station_id.strip().upper()

    # Core attributes + H1 2026 aggregates
    row = con.execute(f"""
        WITH base AS (
            SELECT
                station_id,
                ANY_VALUE(station_name)               AS station_name,
                ANY_VALUE(state)                       AS state,
                ANY_VALUE(road_class)                  AS road_class,
                ANY_VALUE(road_number)                 AS road_number,
                ROUND(AVG(lat), 6)                     AS lat,
                ROUND(AVG(lon), 6)                     AS lon,
                SUM(kfz_r1)                            AS total_kfz_r1,
                COUNT(DISTINCT date)                   AS active_days,
                SUM(COALESCE(sv_r1, 0))               AS total_sv_r1,
                COUNT(CASE WHEN sv_r1 IS NULL THEN 1 END) AS sv_null_hours
            FROM {src}
            WHERE station_id = ?
              AND YEAR(date) = 2026 AND MONTH(date) BETWEEN 1 AND 6
            GROUP BY station_id
        )
        SELECT
            *,
            ROUND(total_kfz_r1::DOUBLE / NULLIF(active_days, 0), 0) AS avg_daily_kfz_r1,
            ROUND(total_sv_r1 * 100.0 / NULLIF(total_kfz_r1, 0), 1) AS sv_share_pct
        FROM base
    """, [sid]).fetchone()

    if row is None:
        return {"error": f"Station '{sid}' not found in H1 2026 data."}

    cols = [
        "station_id", "station_name", "state", "road_class", "road_number",
        "lat", "lon", "total_kfz_r1", "active_days", "total_sv_r1",
        "sv_null_hours", "avg_daily_kfz_r1", "sv_share_pct",
    ]
    d = dict(zip(cols, row))

    # Traffic volume percentile — rank ALL stations first, then select this one.
    # WHERE must be OUTSIDE the window function scope.
    pct_row = con.execute(f"""
        WITH totals AS (
            SELECT station_id, SUM(kfz_r1) AS vol
            FROM {src}
            WHERE YEAR(date) = 2026 AND MONTH(date) BETWEEN 1 AND 6
            GROUP BY station_id
        ),
        ranked AS (
            SELECT
                station_id,
                ROUND(PERCENT_RANK() OVER (ORDER BY vol) * 100, 1) AS pct
            FROM totals
        )
        SELECT pct FROM ranked WHERE station_id = ?
    """, [sid]).fetchone()
    volume_pct = pct_row[0] if pct_row else None

    sv_null = d["sv_null_hours"] or 0
    coverage_note = (
        f"{sv_null} of this station's hourly rows have NULL sv_r1 "
        "(no vehicle-type breakdown in source data for those hours). "
        "Classification availability and missing observation hours are not assessed."
        if sv_null > 0
        else (
            "No NULL sv_r1 values detected; classification availability "
            "and missing observation hours are not assessed."
        )
    )

    # Traffic exposure heuristic: suppressed when NULL sv_r1 rows are present,
    # because COALESCE(sv_r1, 0) depresses sv_share_pct in ways that cannot be
    # distinguished from genuinely low heavy-vehicle presence.
    sv_share = d["sv_share_pct"] or 0.0
    exposure_score = (
        round(sv_share * volume_pct / 1000, 1)
        if volume_pct is not None and sv_null == 0
        else None
    )

    return {
        "type":         "Station",
        "station_id":   d["station_id"],
        "station_name": d["station_name"],
        "state":        d["state"],
        "road":         f"{d['road_class']}{d['road_number']}",
        "lat":          d["lat"],
        "lon":          d["lon"],
        "h1_2026": {
            "avg_daily_kfz_r1":        int(d["avg_daily_kfz_r1"]) if d["avg_daily_kfz_r1"] else None,
            "total_kfz_r1":            int(d["total_kfz_r1"]) if d["total_kfz_r1"] else None,
            "sv_share_pct":            d["sv_share_pct"],
            "traffic_volume_percentile": volume_pct,
            "traffic_exposure_heuristic": exposure_score,
        },
        "coverage": coverage_note,
        "metric_notes": {
            "traffic_volume_percentile": (
                "Relative volume rank among all stations in H1 2026 by kfz_r1 (dir 1). "
                "Measures throughput exposure, not speed or delay."
            ),
            "traffic_exposure_heuristic": (
                "Experimental. Formula: sv_share_pct × traffic_volume_percentile / 1000. "
                "Range 0-10. Not validated against maintenance inspection records."
            ),
        },
        "provenance": _provenance(snapshot_id),
    }


# ── Corridor ───────────────────────────────────────────────────────────────────

def get_corridor(
    con, src: str,
    road_class: str, road_number: str, state: str,
    snapshot_id: str | None = None,
) -> dict:
    """Return a hydrated Corridor object for road_class + road_number + state."""
    rc  = road_class.upper()
    rn  = road_number.strip()
    st  = state.upper()

    err = _validate_corridor(rc, rn, st)
    if err:
        return {"error": err}

    row = con.execute(f"""
        SELECT
            COUNT(DISTINCT station_id)                                      AS num_stations,
            SUM(kfz_r1)                                                     AS total_kfz_r1,
            COUNT(DISTINCT date)                                            AS active_days,
            ROUND(SUM(kfz_r1)::DOUBLE / NULLIF(COUNT(DISTINCT date), 0), 0) AS avg_daily_kfz_r1,
            ROUND(SUM(COALESCE(sv_r1, 0)) * 100.0 / NULLIF(SUM(kfz_r1), 0), 1) AS sv_share_pct,
            COUNT(CASE WHEN sv_r1 IS NULL THEN 1 END)                      AS sv_null_hours
        FROM {src}
        WHERE road_class = ? AND road_number = ? AND state = ?
          AND YEAR(date) = 2026 AND MONTH(date) BETWEEN 1 AND 6
    """, [rc, rn, st]).fetchone()

    if row is None or row[0] == 0:
        return {"error": f"Corridor '{rc}{rn}/{st}' not found in H1 2026 data."}

    num_stations, total_kfz, active_days, avg_daily, sv_share, sv_null = row

    # Peak hour (summed across all stations in the corridor)
    peak_row = con.execute(f"""
        SELECT hour, SUM(kfz_r1) AS vol
        FROM {src}
        WHERE road_class = ? AND road_number = ? AND state = ?
          AND YEAR(date) = 2026 AND MONTH(date) BETWEEN 1 AND 6
        GROUP BY hour
        ORDER BY vol DESC
        LIMIT 1
    """, [rc, rn, st]).fetchone()
    peak_hour = peak_row[0] if peak_row else None

    # Top 3 stations by H1 2026 kfz_r1 volume
    top_stations = con.execute(f"""
        SELECT station_id, ANY_VALUE(station_name) AS name, SUM(kfz_r1) AS vol
        FROM {src}
        WHERE road_class = ? AND road_number = ? AND state = ?
          AND YEAR(date) = 2026 AND MONTH(date) BETWEEN 1 AND 6
        GROUP BY station_id
        ORDER BY vol DESC
        LIMIT 3
    """, [rc, rn, st]).df().to_dict(orient="records")

    sv_null_note = (
        f"{sv_null} station-hour rows have NULL sv_r1 across this corridor. "
        "Classification availability and missing observation hours are not assessed."
        if sv_null > 0
        else (
            "No NULL sv_r1 values detected; classification availability "
            "and missing observation hours are not assessed."
        )
    )

    return {
        "type":        "Corridor",
        "corridor_id": f"{rc}{rn}/{st}",
        "road":        f"{rc}{rn}",
        "state":       st,
        "h1_2026": {
            "num_stations":  num_stations,
            "total_kfz_r1":  int(total_kfz) if total_kfz else None,
            "avg_daily_kfz_r1": int(avg_daily) if avg_daily else None,
            "sv_share_pct":  sv_share,
            "peak_hour":     peak_hour,
        },
        "top_stations": top_stations,
        "coverage": sv_null_note,
        "metric_notes": {
            "total_kfz_r1": (
                "Sum of station-level kfz_r1 observations. The same vehicle can be "
                "counted at multiple stations on this corridor — not a unique vehicle count."
            ),
            "sv_share_pct": (
                "SUM(sv_r1) / SUM(kfz_r1). Stations with format S02-06 report sv_r1=0 "
                "(no vehicle-type breakdown), which depresses the share for those stations."
            ),
            "peak_hour": (
                "Hour with the highest summed kfz_r1 across the corridor. "
                "BASt convention: hour 1 = 00:00-01:00, hour 24 = 23:00-00:00."
            ),
        },
        "provenance": _provenance(snapshot_id),
    }


# ── Dispatcher ─────────────────────────────────────────────────────────────────

def get_object(
    con, src: str,
    object_type: str, object_id: str,
    snapshot_id: str | None = None,
) -> dict:
    """Resolve object_type + object_id to a hydrated domain object.

    object_type: "station" | "corridor"
    object_id:
      station  → station_id, e.g. "NW5048"
      corridor → "{road_class}{road_number}/{state}", e.g. "A1/NW"
    """
    ot = object_type.lower().strip()

    if ot == "station":
        return get_station(con, src, object_id.strip(), snapshot_id)

    if ot == "corridor":
        if "/" not in object_id:
            return {
                "error": (
                    "Corridor id must be '{road_class}{road_number}/{state}', "
                    f"e.g. 'A1/NW'. Got: '{object_id}'"
                )
            }
        road_part, state = object_id.split("/", 1)
        if not road_part or not road_part[0].upper() in ("A", "B"):
            return {
                "error": (
                    f"Corridor road_class must be 'A' or 'B', got '{road_part[:1]}'. "
                    "Example: 'A1/NW'."
                )
            }
        road_class  = road_part[0].upper()
        road_number = road_part[1:].strip()
        return get_corridor(con, src, road_class, road_number, state.strip(), snapshot_id)

    return {
        "error": f"Unknown object_type '{object_type}'. Valid values: 'station', 'corridor'."
    }
