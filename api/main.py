"""
main.py — BASt Traffic Demo FastAPI Backend

Serves traffic data from local Parquet files or S3 via DuckDB.
Set PARQUET_S3_PATH env var to switch to S3 mode (used on ECS Fargate).
Also exposes Apache Iceberg table metadata via PyIceberg (local mode only).
"""

from pathlib import Path
from typing import Optional
import os
import json

import boto3
import duckdb
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

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


# ── Average hourly pattern ────────────────────────────────────────────────────

@app.get("/api/traffic/hourly-pattern")
def get_hourly_pattern(
    month: str = Query("2026-06", description="Month as YYYY-MM, e.g. 2026-06"),
):
    """Average vehicles per hour-of-day across all stations for a given month."""
    try:
        year, mon = month.split("-")
        int(year); int(mon)
    except Exception:
        raise HTTPException(status_code=400, detail="month must be YYYY-MM")
    con = get_con()
    try:
        df = con.execute(f"""
            SELECT
                hour,
                ROUND(AVG(kfz_total), 0) AS avg_kfz
            FROM {parquet_source()}
            WHERE YEAR(date::DATE) = {int(year)}
              AND MONTH(date::DATE) = {int(mon)}
            GROUP BY hour
            ORDER BY hour
        """).df()
        if df.empty:
            raise HTTPException(status_code=404, detail=f"No data for {month}")
        return df.to_dict(orient="records")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        con.close()


# ── Bedrock chat ──────────────────────────────────────────────────────────────

_PARQUET_EXPR = f"read_parquet('{PARQUET_GLOB}', hive_partitioning=false)"

_BEDROCK_MODEL = "qwen.qwen3-32b-v1:0"

_BEDROCK_SYSTEM = f"""You are a data analyst assistant for BASt (German Federal Highway Research Institute).
Help users explore German highway traffic data from January 2026.
1,832 counting stations, ~569 million vehicles, hourly granularity.

DuckDB SQL data source — use this exact expression in FROM clauses:
  {_PARQUET_EXPR}

Columns:
  station_id VARCHAR      -- unique ID e.g. 'BB3592'
  station_name VARCHAR    -- human-readable name
  state VARCHAR           -- 2-letter federal state: NW BY HE NI BW RP ST TH SN SH SL BB BE MV HH HB
  road_class VARCHAR      -- 'A' (Autobahn) or 'B' (Bundesstrasse)
  road_number VARCHAR     -- e.g. '1', '3', '61'
  lat DOUBLE, lon DOUBLE
  date DATE               -- 2026-01-01 to 2026-01-31
  hour INTEGER            -- 0-23
  kfz_r1 INTEGER          -- vehicles direction 1 per hour
  kfz_r2 INTEGER          -- vehicles direction 2 per hour
  kfz_total INTEGER       -- total both directions

Always call execute_sql to fetch real data before answering.
Add LIMIT 20 unless the user asks for more. Be concise and data-driven."""

_BEDROCK_TOOLS = [
    {
        "toolSpec": {
            "name": "execute_sql",
            "description": "Run a DuckDB SQL query against the BASt traffic Parquet data. Returns up to 50 rows as JSON.",
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": f"DuckDB SQL. Use FROM {_PARQUET_EXPR} as the data source.",
                        }
                    },
                    "required": ["query"],
                }
            },
        }
    }
]


def _ask_stream(question: str):
    """Sync generator that drives the Bedrock converse_stream agentic loop."""
    try:
        bedrock = boto3.client("bedrock-runtime", region_name=AWS_REGION)
    except Exception as exc:
        yield f'data: {json.dumps({"type": "error", "message": f"Bedrock client init failed: {exc}"})}\n\n'
        yield f'data: {json.dumps({"type": "done"})}\n\n'
        return

    messages = [{"role": "user", "content": [{"text": question}]}]

    while True:
        try:
            response = bedrock.converse_stream(
                modelId=_BEDROCK_MODEL,
                system=[{"text": _BEDROCK_SYSTEM}],
                messages=messages,
                toolConfig={"tools": _BEDROCK_TOOLS},
            )
        except Exception as exc:
            yield f'data: {json.dumps({"type": "error", "message": str(exc)})}\n\n'
            yield f'data: {json.dumps({"type": "done"})}\n\n'
            return

        assistant_blocks = []
        current_text = ""
        pending_tool = None
        tool_input_raw = ""
        stop_reason = None

        for event in response["stream"]:
            if "contentBlockStart" in event:
                start = event["contentBlockStart"].get("start", {})
                if "toolUse" in start:
                    if current_text:
                        assistant_blocks.append({"text": current_text})
                        current_text = ""
                    pending_tool = {
                        "toolUseId": start["toolUse"]["toolUseId"],
                        "name": start["toolUse"]["name"],
                    }
                    tool_input_raw = ""
                    yield f'data: {json.dumps({"type": "tool_start", "name": pending_tool["name"]})}\n\n'

            elif "contentBlockDelta" in event:
                delta = event["contentBlockDelta"]["delta"]
                if "text" in delta:
                    current_text += delta["text"]
                    yield f'data: {json.dumps({"type": "text", "delta": delta["text"]})}\n\n'
                elif "toolUse" in delta:
                    tool_input_raw += delta["toolUse"].get("input", "")

            elif "contentBlockStop" in event:
                if current_text:
                    assistant_blocks.append({"text": current_text})
                    current_text = ""
                if pending_tool is not None:
                    try:
                        parsed = json.loads(tool_input_raw) if tool_input_raw else {}
                    except Exception:
                        parsed = {}
                    assistant_blocks.append({
                        "toolUse": {
                            "toolUseId": pending_tool["toolUseId"],
                            "name": pending_tool["name"],
                            "input": parsed,
                        }
                    })
                    pending_tool = None

            elif "messageStop" in event:
                stop_reason = event["messageStop"]["stopReason"]

        messages.append({"role": "assistant", "content": assistant_blocks})

        if stop_reason == "tool_use":
            tool_results = []
            for block in assistant_blocks:
                if "toolUse" not in block:
                    continue
                tu = block["toolUse"]
                if tu["name"] == "execute_sql":
                    query = tu["input"].get("query", "")
                    yield f'data: {json.dumps({"type": "tool_running", "query": query[:300]})}\n\n'
                    try:
                        con = get_con()
                        df = con.execute(query).df()
                        con.close()
                        rows = json.loads(df.head(50).to_json(orient="records", date_format="iso"))
                        result_text = json.dumps(rows)
                    except Exception as exc:
                        result_text = f"Error: {exc}"
                    tool_results.append({
                        "toolUseId": tu["toolUseId"],
                        "content": [{"text": result_text}],
                    })
            messages.append({
                "role": "user",
                "content": [{"toolResult": tr} for tr in tool_results],
            })
        else:
            yield f'data: {json.dumps({"type": "done"})}\n\n'
            break


@app.get("/api/ask")
def ask(q: str = Query(..., description="Natural language question about BASt traffic data")):
    return StreamingResponse(
        _ask_stream(q),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


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
