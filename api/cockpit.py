"""
cockpit.py — AI Cockpit endpoint: /api/cockpit/ask

Extends the existing Bedrock agentic loop with:
  - execute_sql: bounded query runner with a request-scoped result registry
  - render_chart: validated Plotly trace builder, streamed as chart_spec SSE events
  - get_object / create_action: reused from existing ontology + actions modules

Call configure() from main.py after all shared globals are available.
The router is then included in the FastAPI app via app.include_router().
"""

import json
import pandas as pd
from typing import Any, Callable, Optional

import boto3
from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse

from api.cockpit_models import ALLOWED_CHART_KINDS, MAX_CHART_POINTS
from api.query_runner import QueryError, PREVIEW_ROWS, run_bounded_query
from api.source_catalog import catalog_description

router = APIRouter()

# ── Module-level state injected by configure() ────────────────────────────────
_get_con: Optional[Callable] = None
_parquet_source: Optional[Callable] = None
_parquet_expr: str = ""
_iceberg_manifest: dict = {}
_actions_db: Any = None
_aws_region: str = "eu-central-1"
_bedrock_model: str = "eu.anthropic.claude-haiku-4-5-20251001-v1:0"
_COCKPIT_SYSTEM: str = ""


def configure(
    get_con_fn: Callable,
    parquet_source_fn: Callable,
    parquet_expr: str,
    iceberg_manifest: dict,
    actions_db: Any,
    aws_region: str,
    bedrock_model: str,
) -> None:
    """Wire shared dependencies from main.py into this module."""
    global _get_con, _parquet_source, _parquet_expr, _iceberg_manifest
    global _actions_db, _aws_region, _bedrock_model, _COCKPIT_SYSTEM
    _get_con = get_con_fn
    _parquet_source = parquet_source_fn
    _parquet_expr = parquet_expr
    _iceberg_manifest = iceberg_manifest
    _actions_db = actions_db
    _aws_region = aws_region
    _bedrock_model = bedrock_model
    _COCKPIT_SYSTEM = _build_system_prompt(parquet_expr)


# ── System prompt ─────────────────────────────────────────────────────────────

def _build_system_prompt(parquet_expr: str) -> str:
    return f"""You are an AI traffic analyst for the BASt Digital Brain cockpit.

Users ask questions about German highway traffic. You query the data, explain your
findings concisely, and always render a chart when the result has multiple rows
worth visualising. Be direct and data-driven.

## Tools
- execute_sql: Run a DuckDB SELECT query. Returns result_id + column list + preview rows.
- render_chart: Render a chart from a previous execute_sql result_id.
  Call this after every execute_sql that returns time-series, rankings or comparisons.
- get_object: Detailed computed metrics for a specific Station or Corridor.
  Use instead of execute_sql when the user names a specific station or road/state pair.
- create_action: Propose an action for human review. Only call when the user explicitly asks.

## Chart selection
- Hourly or daily time series → line (x = time column, y = metric, series = station/state if comparing)
- Rankings, top-N, per-state bar comparisons → bar (x = name, y = metric)
- Correlations or two continuous metrics → scatter
- Always set x_label and y_label with units, e.g. "Vehicles/hour", "Heavy-vehicle share (%)"
- Limit series to ≤ 10 groups; ask the user to narrow if more

## SQL source
Use this exact expression in FROM clauses:
  {parquet_expr}

## Schema
  station_id VARCHAR        — unique ID, e.g. 'NW5048'
  station_name VARCHAR      — human-readable name
  state VARCHAR             — 2-letter state: NW BY HE NI BW RP ST TH SN SH SL BB BE MV HH HB
  road_class VARCHAR        — 'A' (Autobahn) or 'B' (Bundesstrasse)
  road_number VARCHAR       — e.g. '1', '3', '61'
  lat DOUBLE, lon DOUBLE
  date DATE                 — 2025-01-01 to 2025-06-30 and 2026-01-01 to 2026-06-30
  hour INTEGER              — BASt format: 1 = 00:00–01:00, 24 = 23:00–00:00
  kfz_r1 INTEGER            — total vehicles direction 1 per hour
  kfz_r2 INTEGER            — total vehicles direction 2 (unreliable for 2026-01)
  kfz_total INTEGER         — both directions combined
  pkw_r1 INTEGER            — passenger cars direction 1 (NULL/0 for 2026-01 and S02-06 stations)
  sv_r1 INTEGER             — heavy traffic direction 1: trucks + buses (NULL for 2026-01)

## Key formulas
  Heavy-vehicle share (%): ROUND(SUM(COALESCE(sv_r1,0))*100.0 / NULLIF(SUM(kfz_r1),0), 1)
  YoY change (%): ROUND((v2026 - v2025)*100.0 / NULLIF(v2025,0), 1)
  Always add LIMIT 20 unless user asks for more.

## Data sources
{catalog_description()}"""


# ── Tool specs ────────────────────────────────────────────────────────────────

_COCKPIT_TOOLS = [
    {
        "toolSpec": {
            "name": "execute_sql",
            "description": (
                "Run a DuckDB SELECT query against the BASt traffic dataset. "
                "Returns result_id (use with render_chart), column list, row count, "
                "overflow flag, and a preview of up to 50 rows. "
                "Use for any aggregate, time-series, YoY, or custom analysis."
            ),
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "DuckDB SQL SELECT (or WITH … SELECT). Must start with SELECT or WITH.",
                        }
                    },
                    "required": ["query"],
                }
            },
        }
    },
    {
        "toolSpec": {
            "name": "render_chart",
            "description": (
                "Render a Plotly chart from a previous execute_sql result. "
                "Call after execute_sql whenever the result has multiple rows worth visualising. "
                "The result_id comes from the execute_sql response."
            ),
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "result_id": {
                            "type": "string",
                            "description": "The result_id returned by execute_sql.",
                        },
                        "kind": {
                            "type": "string",
                            "enum": ["line", "scatter", "bar"],
                            "description": "Chart type.",
                        },
                        "title": {"type": "string"},
                        "x": {
                            "type": "string",
                            "description": "Column name for the x-axis.",
                        },
                        "y": {
                            "type": "string",
                            "description": "Column name for the y-axis.",
                        },
                        "series": {
                            "type": "string",
                            "description": (
                                "Optional column to group into multiple traces "
                                "(one trace per unique value). Use for comparing stations, states, etc. "
                                "Keep to ≤ 10 unique values."
                            ),
                        },
                        "x_label": {"type": "string", "description": "X-axis label with units."},
                        "y_label": {"type": "string", "description": "Y-axis label with units."},
                    },
                    "required": ["result_id", "kind", "title", "x", "y"],
                }
            },
        }
    },
    {
        "toolSpec": {
            "name": "get_object",
            "description": (
                "Retrieve a hydrated domain object — Station or Corridor — with computed properties "
                "(avg_daily_kfz_r1, sv_share_pct, traffic_volume_percentile, traffic_exposure_heuristic, peak_hour). "
                "Use before execute_sql when the user asks about a specific station or road/state pair. "
                "Returns an evidence_id required by create_action."
            ),
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "object_type": {
                            "type": "string",
                            "enum": ["station", "corridor"],
                        },
                        "object_id": {
                            "type": "string",
                            "description": "Station ID (e.g. 'NW5048') or corridor (e.g. 'A1/NW', 'B10/RP').",
                        },
                    },
                    "required": ["object_type", "object_id"],
                }
            },
        }
    },
    {
        "toolSpec": {
            "name": "create_action",
            "description": (
                "Propose an action for human review. Only call when the user explicitly requests it. "
                "Requires a valid evidence_id from get_object. "
                "Types: MAINTENANCE_RECOMMENDATION, ANOMALY_FLAG, REPORT_DRAFT."
            ),
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "idempotency_key": {
                            "type": "string",
                            "description": "UUID you generate. Re-submitting the same key with the same payload is a no-op.",
                        },
                        "type": {
                            "type": "string",
                            "enum": ["MAINTENANCE_RECOMMENDATION", "ANOMALY_FLAG", "REPORT_DRAFT"],
                        },
                        "title": {"type": "string", "description": "Short specific proposal title (≤ 80 chars)."},
                        "description": {
                            "type": "string",
                            "description": "Reasoning grounded in evidence, including metric values.",
                        },
                        "evidence_id": {
                            "type": "string",
                            "description": "The evidence_id returned by get_object.",
                        },
                    },
                    "required": ["idempotency_key", "type", "title", "description", "evidence_id"],
                }
            },
        }
    },
]


# ── Chart builder ─────────────────────────────────────────────────────────────

def _safe_val(v: Any) -> Any:
    """Convert pandas scalars to JSON-safe Python types."""
    if v is None:
        return None
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    if hasattr(v, "isoformat"):
        return v.isoformat()
    if hasattr(v, "item"):          # numpy scalar
        return v.item()
    return v


def _build_traces(
    df: pd.DataFrame,
    x: str,
    y: str,
    series: Optional[str],
) -> list[dict]:
    traces: list[dict] = []
    if series and series in df.columns:
        for group_name, group_df in df.groupby(series, sort=False):
            group_df = group_df.sort_values(x) if x in group_df.columns else group_df
            traces.append({
                "name": str(group_name),
                "x": [_safe_val(v) for v in group_df[x]],
                "y": [_safe_val(v) for v in group_df[y]],
            })
    else:
        sorted_df = df.sort_values(x) if x in df.columns else df
        traces.append({
            "name": y,
            "x": [_safe_val(v) for v in sorted_df[x]],
            "y": [_safe_val(v) for v in sorted_df[y]],
        })
    return traces


# ── SSE helpers ───────────────────────────────────────────────────────────────

def _sse(obj: dict) -> str:
    return f"data: {json.dumps(obj)}\n\n"


# ── Agentic loop ──────────────────────────────────────────────────────────────

MAX_ITERATIONS = 8


def _cockpit_stream(question: str):
    """Sync generator driving the cockpit Bedrock converse_stream agentic loop."""
    result_registry: dict[str, pd.DataFrame] = {}  # request-scoped

    try:
        bedrock = boto3.client("bedrock-runtime", region_name=_aws_region)
    except Exception as exc:
        yield _sse({"type": "error", "code": "bedrock_init", "message": str(exc), "recoverable": False})
        yield _sse({"type": "done", "status": "error"})
        return

    messages = [{"role": "user", "content": [{"text": question}]}]

    for iteration in range(MAX_ITERATIONS):
        try:
            response = bedrock.converse_stream(
                modelId=_bedrock_model,
                system=[{"text": _COCKPIT_SYSTEM}],
                messages=messages,
                toolConfig={"tools": _COCKPIT_TOOLS},
            )
        except Exception as exc:
            yield _sse({"type": "error", "code": "bedrock_stream", "message": str(exc), "recoverable": True})
            yield _sse({"type": "done", "status": "error"})
            return

        assistant_blocks: list[dict] = []
        current_text = ""
        pending_tool: Optional[dict] = None
        tool_input_raw = ""
        stop_reason: Optional[str] = None

        try:
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
                        yield _sse({"type": "tool_start", "name": pending_tool["name"]})

                elif "contentBlockDelta" in event:
                    delta = event["contentBlockDelta"]["delta"]
                    if "text" in delta:
                        current_text += delta["text"]
                        yield _sse({"type": "text", "delta": delta["text"]})
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

        except Exception as exc:
            yield _sse({"type": "error", "code": "stream_interrupted", "message": str(exc), "recoverable": True})
            yield _sse({"type": "done", "status": "error"})
            return

        messages.append({"role": "assistant", "content": assistant_blocks})

        if stop_reason != "tool_use":
            yield _sse({"type": "done", "status": "success"})
            return

        # ── Tool execution ────────────────────────────────────────────────────
        tool_results: list[dict] = []

        for block in assistant_blocks:
            if "toolUse" not in block:
                continue
            tu = block["toolUse"]
            name = tu["name"]
            inp = tu["input"]

            # ── execute_sql ───────────────────────────────────────────────────
            if name == "execute_sql":
                query = inp.get("query", "")
                yield _sse({"type": "tool_running", "query": query[:400]})
                try:
                    con = _get_con()
                    try:
                        result_id, df, overflow, columns = run_bounded_query(query, con)
                    finally:
                        con.close()
                    result_registry[result_id] = df
                    preview = json.loads(
                        df.head(PREVIEW_ROWS).to_json(orient="records", date_format="iso")
                    )
                    source_mode = "iceberg" if "iceberg_scan" in _parquet_expr else "parquet"
                    yield _sse({
                        "type": "query_result",
                        "result_id": result_id,
                        "columns": columns,
                        "row_count": len(df),
                        "overflow": overflow,
                        "preview": preview,
                        "source": "bast_traffic",
                        "source_mode": source_mode,
                    })
                    result_text = json.dumps({
                        "result_id": result_id,
                        "columns": columns,
                        "row_count": len(df),
                        "overflow": overflow,
                        "preview_rows": preview,
                        "note": (
                            "Call render_chart with this result_id to visualise the data."
                            if len(df) > 1 else ""
                        ),
                    })
                except QueryError as exc:
                    result_text = json.dumps({"error": "query_rejected", "message": str(exc)})
                except Exception as exc:
                    result_text = json.dumps({"error": "query_failed", "message": str(exc)})
                tool_results.append({
                    "toolUseId": tu["toolUseId"],
                    "content": [{"text": result_text}],
                })

            # ── render_chart ──────────────────────────────────────────────────
            elif name == "render_chart":
                result_id = inp.get("result_id", "")
                kind      = inp.get("kind", "")
                title     = inp.get("title", "")
                x_col     = inp.get("x", "")
                y_col     = inp.get("y", "")
                series    = inp.get("series")
                x_label   = inp.get("x_label") or x_col
                y_label   = inp.get("y_label") or y_col
                yield _sse({"type": "tool_running", "query": f"render_chart({kind}: {x_col} vs {y_col})"})

                df = result_registry.get(result_id)
                if df is None:
                    result_text = json.dumps({
                        "error": "invalid_result_id",
                        "message": f"result_id '{result_id}' not found in this session. Run execute_sql first.",
                    })
                elif kind not in ALLOWED_CHART_KINDS:
                    result_text = json.dumps({
                        "error": "invalid_kind",
                        "message": f"kind must be one of {sorted(ALLOWED_CHART_KINDS)}.",
                    })
                elif x_col not in df.columns:
                    result_text = json.dumps({
                        "error": "invalid_column",
                        "message": f"x column '{x_col}' not found. Available: {list(df.columns)}",
                    })
                elif y_col not in df.columns:
                    result_text = json.dumps({
                        "error": "invalid_column",
                        "message": f"y column '{y_col}' not found. Available: {list(df.columns)}",
                    })
                elif series and series not in df.columns:
                    result_text = json.dumps({
                        "error": "invalid_column",
                        "message": f"series column '{series}' not found. Available: {list(df.columns)}",
                    })
                else:
                    total_points = len(df)
                    if total_points > MAX_CHART_POINTS:
                        result_text = json.dumps({
                            "error": "too_many_points",
                            "message": (
                                f"Chart would have {total_points} data points (max {MAX_CHART_POINTS}). "
                                "Please aggregate further or reduce the date range, then run execute_sql again."
                            ),
                        })
                    else:
                        try:
                            traces = _build_traces(df, x_col, y_col, series)
                            yield _sse({
                                "type": "chart_spec",
                                "result_id": result_id,
                                "kind": kind,
                                "title": title,
                                "x_label": x_label,
                                "y_label": y_label,
                                "traces": traces,
                            })
                            result_text = json.dumps({
                                "rendered": True,
                                "kind": kind,
                                "traces": len(traces),
                                "points": total_points,
                            })
                        except Exception as exc:
                            result_text = json.dumps({"error": "render_failed", "message": str(exc)})
                tool_results.append({
                    "toolUseId": tu["toolUseId"],
                    "content": [{"text": result_text}],
                })

            # ── get_object ────────────────────────────────────────────────────
            elif name == "get_object":
                from api.ontology import get_object as ontology_get_object
                from api.actions import store_evidence
                obj_type = inp.get("object_type", "")
                obj_id   = inp.get("object_id", "")
                yield _sse({"type": "tool_running", "query": f"get_object({obj_type}, {obj_id})"})
                try:
                    con = _get_con()
                    try:
                        result = ontology_get_object(
                            con, _parquet_source(), obj_type, obj_id,
                            snapshot_id=_iceberg_manifest.get("snapshot_id"),
                        )
                    finally:
                        con.close()
                    if "error" not in result:
                        ev_id = store_evidence(_actions_db, obj_type, obj_id, result)
                        result["evidence_id"] = ev_id
                    result_text = json.dumps(result)
                except Exception as exc:
                    result_text = json.dumps({"error": str(exc)})
                tool_results.append({
                    "toolUseId": tu["toolUseId"],
                    "content": [{"text": result_text}],
                })

            # ── create_action ─────────────────────────────────────────────────
            elif name == "create_action":
                from api.actions import create_action as actions_create
                ikey  = inp.get("idempotency_key", "")
                atype = inp.get("type", "")
                title = inp.get("title", "")
                desc  = inp.get("description", "")
                ev_id = inp.get("evidence_id", "")
                yield _sse({"type": "tool_running", "query": f"create_action({atype})"})
                try:
                    action, outcome = actions_create(
                        _actions_db, ikey, atype, title, desc, ev_id,
                    )
                    if outcome == "conflict":
                        result_text = json.dumps({
                            "error": "conflict",
                            "message": (
                                "Same idempotency_key used with different content. "
                                "Generate a new UUID for a different proposal."
                            ),
                            "existing_action_id": action["id"] if action else None,
                        })
                    else:
                        result_text = json.dumps({
                            "action_id": action["id"],
                            "status": action["status"],
                            "outcome": outcome,
                        })
                except (ValueError, LookupError) as exc:
                    result_text = json.dumps({"error": str(exc)})
                except Exception as exc:
                    result_text = json.dumps({"error": f"Unexpected: {exc}"})
                tool_results.append({
                    "toolUseId": tu["toolUseId"],
                    "content": [{"text": result_text}],
                })

            else:
                tool_results.append({
                    "toolUseId": tu["toolUseId"],
                    "content": [{"text": json.dumps({"error": f"Unknown tool: {name}"})}],
                })

        messages.append({
            "role": "user",
            "content": [{"toolResult": tr} for tr in tool_results],
        })

    # Exceeded max iterations
    yield _sse({
        "type": "error",
        "code": "max_iterations",
        "message": f"Agent reached the {MAX_ITERATIONS}-iteration limit without finishing.",
        "recoverable": False,
    })
    yield _sse({"type": "done", "status": "error"})


# ── Endpoint ──────────────────────────────────────────────────────────────────

@router.get("/api/cockpit/ask")
def cockpit_ask(
    q: str = Query(..., description="Natural language question about BASt traffic data"),
):
    return StreamingResponse(
        _cockpit_stream(q),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
