"""
main.py — BASt Traffic Demo FastAPI Backend

Serves traffic data from local Parquet files or S3 via DuckDB.
Set PARQUET_S3_PATH env var to switch to S3 mode (used on ECS Fargate).
Also exposes Apache Iceberg table metadata via PyIceberg (local mode only).
"""

from pathlib import Path
from typing import Optional
import os

import duckdb
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

# ── Paths ──────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent.parent
ICEBERG_CATALOG_DB = str(BASE_DIR / "data" / "iceberg_catalog.db")
ICEBERG_LOCAL_WAREHOUSE = str(BASE_DIR / "data" / "iceberg_local")

# When running on ECS Fargate, PARQUET_S3_PATH points to S3.
# Locally, falls back to the parquet directory.
_S3_PATH = os.getenv("PARQUET_S3_PATH")
_LOCAL_GLOB = str(BASE_DIR / "data" / "parquet" / "**" / "*.parquet")
PARQUET_GLOB = _S3_PATH if _S3_PATH else _LOCAL_GLOB
USE_S3 = bool(_S3_PATH)
AWS_REGION = os.getenv("AWS_REGION", "eu-central-1")

# ── App setup ─────────────────────────────────────────────────────────────────
app = FastAPI(
    title="BASt Traffic Demo API",
    description="German Federal Highway traffic counting data (BASt) — January 2026",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def get_con() -> duckdb.DuckDBPyConnection:
    """Return a DuckDB connection configured for local or S3 access."""
    con = duckdb.connect(database=":memory:")
    if USE_S3:
        con.execute("INSTALL httpfs; LOAD httpfs")
        # On ECS Fargate the task role is picked up automatically via the
        # credential chain (ECS container metadata endpoint).
        try:
            con.execute(f"""
                CREATE OR REPLACE SECRET aws_s3 (
                    TYPE S3,
                    PROVIDER CREDENTIAL_CHAIN,
                    REGION '{AWS_REGION}'
                )
            """)
        except Exception:
            con.execute(f"SET s3_region='{AWS_REGION}'")
            con.execute("SET s3_use_credential_chain=true")
    return con


def parquet_source() -> str:
    """Return DuckDB read_parquet expression."""
    return f"read_parquet('{PARQUET_GLOB}', hive_partitioning=false)"


# ── Health ─────────────────────────────────────────────────────────────────────
@app.get("/health")
def health():
    return {"status": "ok"}


# ── Stations ──────────────────────────────────────────────────────────────────
@app.get("/api/stations")
def get_stations():
    """List all stations with metadata."""
    con = get_con()
    try:
        df = con.execute(f"""
            SELECT
                station_id,
                station_name,
                state,
                road_class,
                road_number,
                ROUND(AVG(lat), 6)  AS lat,
                ROUND(AVG(lon), 6)  AS lon,
                SUM(kfz_total)      AS total_kfz
            FROM {parquet_source()}
            GROUP BY station_id, station_name, state, road_class, road_number
            ORDER BY total_kfz DESC
        """).df()
        return df.to_dict(orient="records")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        con.close()


# ── Daily traffic per station ──────────────────────────────────────────────────
@app.get("/api/traffic/daily")
def get_traffic_daily(
    station_id: str = Query(..., description="Station ID, e.g. BB3592"),
    start: str = Query("2026-01-01", description="Start date YYYY-MM-DD"),
    end: str = Query("2026-01-31", description="End date YYYY-MM-DD"),
):
    """Daily aggregated traffic for a single station."""
    con = get_con()
    try:
        df = con.execute(f"""
            SELECT
                date::DATE            AS date,
                SUM(kfz_r1)           AS kfz_r1,
                SUM(kfz_r2)           AS kfz_r2,
                SUM(kfz_total)        AS kfz_total
            FROM {parquet_source()}
            WHERE station_id = '{station_id}'
              AND date::DATE BETWEEN '{start}'::DATE AND '{end}'::DATE
            GROUP BY date::DATE
            ORDER BY date::DATE
        """).df()

        if df.empty:
            raise HTTPException(
                status_code=404,
                detail=f"No data for station_id={station_id} in range {start}–{end}"
            )

        df["date"] = df["date"].astype(str)
        return df.to_dict(orient="records")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        con.close()


# ── Hourly traffic per station ─────────────────────────────────────────────────
@app.get("/api/traffic/hourly")
def get_traffic_hourly(
    station_id: str = Query(..., description="Station ID, e.g. BB3592"),
    date: str = Query("2026-01-15", description="Date YYYY-MM-DD"),
):
    """Hourly traffic for a single station on a given date."""
    con = get_con()
    try:
        df = con.execute(f"""
            SELECT
                hour,
                SUM(kfz_r1)    AS kfz_r1,
                SUM(kfz_r2)    AS kfz_r2,
                SUM(kfz_total) AS kfz_total
            FROM {parquet_source()}
            WHERE station_id = '{station_id}'
              AND date::DATE = '{date}'::DATE
            GROUP BY hour
            ORDER BY hour
        """).df()

        if df.empty:
            raise HTTPException(
                status_code=404,
                detail=f"No data for station_id={station_id} on {date}"
            )

        return df.to_dict(orient="records")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        con.close()


# ── Overview ──────────────────────────────────────────────────────────────────
@app.get("/api/traffic/overview")
def get_traffic_overview():
    """
    Total traffic per day (all stations) + top 10 stations by volume
    + summary KPIs.
    """
    con = get_con()
    try:
        daily_df = con.execute(f"""
            SELECT
                date::DATE        AS date,
                SUM(kfz_total)    AS kfz_total
            FROM {parquet_source()}
            GROUP BY date::DATE
            ORDER BY date::DATE
        """).df()

        top10_df = con.execute(f"""
            SELECT
                station_id,
                station_name,
                state,
                road_class || road_number AS road,
                SUM(kfz_total)            AS total_kfz
            FROM {parquet_source()}
            GROUP BY station_id, station_name, state, road_class, road_number
            ORDER BY total_kfz DESC
            LIMIT 10
        """).df()

        kpis = con.execute(f"""
            SELECT
                SUM(kfz_total)                                   AS total_vehicles,
                COUNT(DISTINCT station_id)                        AS num_stations,
                COUNT(DISTINCT date::DATE)                        AS num_days,
                ROUND(SUM(kfz_total)::DOUBLE / NULLIF(COUNT(DISTINCT date::DATE), 0), 0)
                                                                  AS avg_daily_all_stations
            FROM {parquet_source()}
        """).df().to_dict(orient="records")[0]

        daily_df["date"] = daily_df["date"].astype(str)

        return {
            "kpis": kpis,
            "daily_totals": daily_df.to_dict(orient="records"),
            "top10_stations": top10_df.to_dict(orient="records"),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        con.close()


# ── State breakdown ────────────────────────────────────────────────────────────
@app.get("/api/traffic/states")
def get_traffic_states():
    """Traffic aggregated by German federal state (Landeskuerzel)."""
    con = get_con()
    try:
        df = con.execute(f"""
            SELECT
                state,
                COUNT(DISTINCT station_id) AS num_stations,
                SUM(kfz_total)             AS total_kfz
            FROM {parquet_source()}
            WHERE state IS NOT NULL AND state != ''
            GROUP BY state
            ORDER BY total_kfz DESC
        """).df()
        return df.to_dict(orient="records")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        con.close()


# ── Iceberg Metadata ──────────────────────────────────────────────────────────

@app.get("/api/iceberg/info")
def iceberg_info():
    """
    Return Apache Iceberg table metadata: snapshot_id, schema, location, etc.
    Uses the local SQLite-backed PyIceberg catalog.
    """
    import os as _os
    if not Path(ICEBERG_CATALOG_DB).exists():
        raise HTTPException(
            status_code=404,
            detail="Iceberg catalog not found. Run scripts/create_iceberg.py first."
        )

    try:
        from pyiceberg.catalog.sql import SqlCatalog

        catalog = SqlCatalog(
            "bast_local",
            **{
                "uri": f"sqlite:///{ICEBERG_CATALOG_DB}",
                "warehouse": f"file://{ICEBERG_LOCAL_WAREHOUSE}",
            },
        )
        table = catalog.load_table("bast.traffic")
        snap = table.current_snapshot()

        # Get latest metadata file path
        meta_dir = Path(ICEBERG_LOCAL_WAREHOUSE) / "bast" / "traffic" / "metadata"
        meta_files = sorted(meta_dir.glob("*.metadata.json"))
        latest_metadata = meta_files[-1].name if meta_files else None

        # Count data files
        data_dir = Path(ICEBERG_LOCAL_WAREHOUSE) / "bast" / "traffic" / "data"
        num_data_files = len(list(data_dir.glob("*.parquet"))) if data_dir.exists() else 0

        return {
            "format": "Apache Iceberg v2",
            "table": "bast.traffic",
            "snapshot_id": snap.snapshot_id if snap else None,
            "snapshot_timestamp_ms": snap.timestamp_ms if snap else None,
            "location": table.location(),
            "s3_location": "s3://bast-traffic-demo-112220711619/iceberg/bast/traffic",
            "num_snapshots": len(list(table.snapshots())),
            "num_data_files": num_data_files,
            "latest_metadata_file": latest_metadata,
            "schema": str(table.schema()),
            "partition_spec": str(table.spec()),
            "properties": {
                "format-version": "2",
                "engine": "PyIceberg + DuckDB",
                "source": "BASt Federal Highway Traffic Data 2026-01",
            },
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/iceberg/snapshots")
def iceberg_snapshots():
    """List all Iceberg snapshots (version history)."""
    if not Path(ICEBERG_CATALOG_DB).exists():
        raise HTTPException(status_code=404, detail="Iceberg catalog not found.")
    try:
        from pyiceberg.catalog.sql import SqlCatalog

        catalog = SqlCatalog(
            "bast_local",
            **{
                "uri": f"sqlite:///{ICEBERG_CATALOG_DB}",
                "warehouse": f"file://{ICEBERG_LOCAL_WAREHOUSE}",
            },
        )
        table = catalog.load_table("bast.traffic")
        snapshots = [
            {
                "snapshot_id": s.snapshot_id,
                "timestamp_ms": s.timestamp_ms,
                "operation": str(s.summary.operation) if s.summary and s.summary.operation else "unknown",
                "summary": {k: v for k, v in s.summary.additional_properties.items()} if s.summary else {},
            }
            for s in table.snapshots()
        ]
        return {"table": "bast.traffic", "snapshots": snapshots}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
