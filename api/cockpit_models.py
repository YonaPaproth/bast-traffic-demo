"""
cockpit_models.py — validated schemas for cockpit SSE events and chart specs.
"""

from typing import Any, Literal, Optional
from pydantic import BaseModel

ALLOWED_CHART_KINDS: frozenset[str] = frozenset({"line", "scatter", "bar"})
MAX_CHART_POINTS = 2_000


class ColumnInfo(BaseModel):
    name: str
    type: str


class QueryResultEvent(BaseModel):
    type: Literal["query_result"] = "query_result"
    result_id: str
    columns: list[ColumnInfo]
    row_count: int
    overflow: bool
    preview: list[dict[str, Any]]
    source: str
    source_mode: str  # "iceberg" or "parquet"


class ChartTrace(BaseModel):
    name: str
    x: list[Any]
    y: list[Any]


class ChartSpecEvent(BaseModel):
    type: Literal["chart_spec"] = "chart_spec"
    result_id: str
    kind: str
    title: str
    x_label: str
    y_label: str
    traces: list[ChartTrace]


class ErrorEvent(BaseModel):
    type: Literal["error"] = "error"
    code: str
    message: str
    recoverable: bool = True


class DoneEvent(BaseModel):
    type: Literal["done"] = "done"
    status: Literal["success", "error"] = "success"
