#!/usr/bin/env python3
"""
iceberg_ingest.py — Ingest completed monthly Parquet files into Apache Iceberg on S3.

Reads from data/parquet/year=YYYY/month=MM/traffic.parquet (produced by parse_bast.py)
and appends to a single Iceberg v2 table on S3 with month(date) partitioning.

Usage:
  python scripts/iceberg_ingest.py 2026_01          # ingest one month
  python scripts/iceberg_ingest.py all              # ingest all available months
  python scripts/iceberg_ingest.py 2026_01 --replace  # overwrite existing month

Each successful commit writes data/iceberg_manifest.json with the exact
metadata_location and snapshot_id for ECS to reference.

Dependencies (install separately — see requirements-iceberg.txt):
  pip install -r scripts/requirements-iceberg.txt
"""

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

BASE_DIR      = Path(__file__).parent.parent
PARQUET_ROOT  = BASE_DIR / "data" / "parquet"
CATALOG_DB    = BASE_DIR / "data" / "iceberg_s3_catalog.db"
MANIFEST_PATH = BASE_DIR / "data" / "iceberg_manifest.json"

S3_WAREHOUSE  = "s3://bast-traffic-demo-112220711619/iceberg"
S3_REGION     = "eu-central-1"
NAMESPACE     = "bast"
TABLE_NAME    = "bast.traffic"

# ── Expected schema ────────────────────────────────────────────────────────────
# date as DATE (not timestamp), all measurement columns present and nullable.
REQUIRED_COLUMNS = {
    "date", "hour", "station_id", "station_name", "state",
    "road_class", "road_number", "kfz_r1", "kfz_r2", "kfz_total",
    "sv_r1", "pkw_r1", "lat", "lon",
}


# ── Schema definition ──────────────────────────────────────────────────────────

def iceberg_schema():
    from pyiceberg.schema import Schema
    from pyiceberg.types import (
        DateType, DoubleType, IntegerType, NestedField, StringType,
    )
    return Schema(
        NestedField(1,  "date",         DateType(),    required=False),
        NestedField(2,  "hour",         IntegerType(), required=False),
        NestedField(3,  "station_id",   StringType(),  required=False),
        NestedField(4,  "station_name", StringType(),  required=False),
        NestedField(5,  "state",        StringType(),  required=False),
        NestedField(6,  "road_class",   StringType(),  required=False),
        NestedField(7,  "road_number",  StringType(),  required=False),
        NestedField(8,  "kfz_r1",       IntegerType(), required=False),
        NestedField(9,  "kfz_r2",       IntegerType(), required=False),
        NestedField(10, "kfz_total",    IntegerType(), required=False),
        NestedField(11, "sv_r1",        IntegerType(), required=False),
        NestedField(12, "pkw_r1",       IntegerType(), required=False),
        NestedField(13, "lat",          DoubleType(),  required=False),
        NestedField(14, "lon",          DoubleType(),  required=False),
    )


def iceberg_partition_spec():
    from pyiceberg.partitioning import PartitionField, PartitionSpec
    from pyiceberg.transforms import MonthTransform
    # Partition by month(date): date field has Iceberg field_id=1
    return PartitionSpec(
        PartitionField(source_id=1, field_id=1000, transform=MonthTransform(), name="date_month")
    )


# ── Catalog ────────────────────────────────────────────────────────────────────

def open_catalog():
    from pyiceberg.catalog.sql import SqlCatalog
    # PyIceberg picks up AWS credentials from the environment credential chain
    # (AWS_PROFILE, instance profile, ECS task role, etc.).
    # Do not pass Java-style credentials-provider settings here.
    return SqlCatalog(
        "bast_s3",
        **{
            "uri":        f"sqlite:///{CATALOG_DB}",
            "warehouse":  S3_WAREHOUSE,
            "s3.region":  S3_REGION,
        },
    )


def ensure_table(catalog) -> object:
    """Return the Iceberg table, creating it if it does not exist.

    Fails hard if the table exists but has wrong schema or partition spec,
    rather than silently dropping it. Explicit --recreate is required.
    """
    if not catalog.table_exists(TABLE_NAME):
        # Create namespace separately; fail clearly if it already exists
        # under a different unexpected state rather than swallowing the error.
        existing_ns = [n[0] for n in catalog.list_namespaces()]
        if NAMESPACE not in existing_ns:
            catalog.create_namespace(NAMESPACE)

        tbl = catalog.create_table(
            TABLE_NAME,
            schema=iceberg_schema(),
            partition_spec=iceberg_partition_spec(),
            location=f"{S3_WAREHOUSE}/{NAMESPACE}/traffic",
        )
        print(f"Created Iceberg table {TABLE_NAME}")
        print(f"  Location  : {tbl.location()}")
        print(f"  Partition : month(date)")
        return tbl

    tbl = catalog.load_table(TABLE_NAME)

    # Validate: all required columns present
    existing_cols = {f.name for f in tbl.schema().fields}
    missing = REQUIRED_COLUMNS - existing_cols
    if missing:
        raise RuntimeError(
            f"Iceberg table {TABLE_NAME} is missing columns: {missing}.\n"
            f"The stale test artifact at {S3_WAREHOUSE} needs to be removed manually,\n"
            f"then re-run this script to create a fresh table.\n"
            f"  aws s3 rm {S3_WAREHOUSE}/bast/traffic/ --recursive --profile claude-code\n"
            f"  rm {CATALOG_DB}"
        )

    # Validate: partition spec is not empty
    if len(tbl.spec().fields) == 0:
        raise RuntimeError(
            f"Iceberg table {TABLE_NAME} has no partition spec (unpartitioned).\n"
            f"Remove the stale artifact and recreate:\n"
            f"  aws s3 rm {S3_WAREHOUSE}/bast/traffic/ --recursive --profile claude-code\n"
            f"  rm {CATALOG_DB}"
        )

    return tbl


# ── Source file helpers ────────────────────────────────────────────────────────

def month_parquet_path(year: str, mon: str) -> Path:
    return PARQUET_ROOT / f"year={year}" / f"month={mon}" / "traffic.parquet"


def all_available_months() -> list[tuple[str, str]]:
    months = []
    for p in sorted(PARQUET_ROOT.glob("year=*/month=*/traffic.parquet")):
        year = p.parent.parent.name.split("=")[1]
        mon  = p.parent.name.split("=")[1]
        months.append((year, mon))
    return months


def sha256_of_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ── Schema validation + normalisation ─────────────────────────────────────────

def validate_and_normalise(arrow_table: pa.Table, source_label: str) -> pa.Table:
    """Check required columns are present and cast date to date32."""
    # Drop Hive partition columns injected by PyArrow when reading from year=/month= paths
    extra_cols = set(arrow_table.schema.names) - REQUIRED_COLUMNS
    if extra_cols:
        arrow_table = arrow_table.drop_columns(sorted(extra_cols))

    cols = set(arrow_table.schema.names)
    missing = REQUIRED_COLUMNS - cols
    if missing:
        raise ValueError(f"{source_label}: missing columns {missing}")

    # date must be date32 for Iceberg DateType; Parquet may store as timestamp
    idx = arrow_table.schema.get_field_index("date")
    if arrow_table.schema.field("date").type != pa.date32():
        arrow_table = arrow_table.set_column(
            idx, "date", arrow_table.column("date").cast(pa.date32())
        )

    # pkw_r1 = 0 for stations without per-type breakdown.
    # Zero is the current domain convention inherited from the parser.
    # It means "no PKW measurement available", not "zero PKW traffic".
    # Callers that compute shares must filter pkw_r1 > 0.
    return arrow_table


# ── Idempotency ────────────────────────────────────────────────────────────────

def already_ingested(tbl, source_month: str, source_sha256: str) -> bool:
    """Return True if a snapshot with this month+checksum is already committed."""
    for snap in tbl.snapshots():
        props = snap.summary.additional_properties if snap.summary else {}
        if (props.get("source_month") == source_month
                and props.get("source_sha256") == source_sha256):
            print(f"  Already ingested (snapshot {snap.snapshot_id}). Skipping.")
            return True
    return False


def month_already_present(tbl, source_month: str) -> bool:
    """Return True if any snapshot has this month (any checksum)."""
    for snap in tbl.snapshots():
        props = snap.summary.additional_properties if snap.summary else {}
        if props.get("source_month") == source_month:
            return True
    return False


# ── Manifest ──────────────────────────────────────────────────────────────────

def update_manifest(tbl, ingested_months: list[str]) -> None:
    """Write data/iceberg_manifest.json with the exact committed metadata reference."""
    # Authoritative metadata location from the catalog, not filename sort.
    meta_location = tbl.metadata_location
    snap = tbl.current_snapshot()

    manifest = {
        "table":             TABLE_NAME,
        "warehouse":         S3_WAREHOUSE,
        "metadata_location": meta_location,
        "snapshot_id":       str(snap.snapshot_id) if snap else None,
        "total_records":     int(snap.summary.get("total-records", 0)) if snap else 0,
        "ingested_months":   sorted(ingested_months),
        "last_updated":      datetime.now(timezone.utc).isoformat(),
        "note": (
            "ECS should read iceberg_scan() with metadata_location, not a directory path. "
            "Snapshot files must be retained; do not expire snapshots referenced here."
        ),
    }

    with open(MANIFEST_PATH, "w") as f:
        json.dump(manifest, f, indent=2)
    print(f"\nManifest written -> {MANIFEST_PATH}")
    print(f"  metadata_location : {meta_location}")
    print(f"  snapshot_id       : {snap.snapshot_id if snap else 'none'}")
    print(f"  total_records     : {manifest['total_records']:,}")


def read_manifest() -> dict:
    if MANIFEST_PATH.exists():
        with open(MANIFEST_PATH) as f:
            return json.load(f)
    return {"ingested_months": []}


# ── Ingest one month ───────────────────────────────────────────────────────────

def ingest_month(catalog, year: str, mon: str, replace: bool = False) -> bool:
    source_label = f"{year}-{mon}"
    path = month_parquet_path(year, mon)

    if not path.exists():
        raise FileNotFoundError(f"Parquet not found: {path}")

    print(f"\n-- {source_label} ----------------------------------------------")
    print(f"  Source : {path}  ({path.stat().st_size / 1_048_576:.1f} MB)")

    checksum = sha256_of_file(path)
    print(f"  SHA256 : {checksum[:16]}…")

    tbl = ensure_table(catalog)

    if replace:
        if not month_already_present(tbl, source_label):
            print(f"  --replace given but {source_label} not previously ingested. Proceeding as first ingest.")
        else:
            # Positional overwrite: replace all rows for this month's partition.
            print(f"  Replacing existing data for {source_label} …")
            arrow_table = pq.read_table(str(path))
            arrow_table = validate_and_normalise(arrow_table, source_label)
            tbl.overwrite(
                arrow_table,
                overwrite_filter=f"date_month = '{year}-{mon}'",
                snapshot_properties={
                    "source_month":    source_label,
                    "source_sha256":   checksum,
                    "ingested_at":     datetime.now(timezone.utc).isoformat(),
                    "ingest_type":     "replace",
                },
            )
            snap = tbl.current_snapshot()
            print(f"  Replaced. New snapshot: {snap.snapshot_id}")
            return True

    if already_ingested(tbl, source_label, checksum):
        return False

    if month_already_present(tbl, source_label):
        raise RuntimeError(
            f"{source_label} is already present in the table with a different checksum.\n"
            f"If this is a corrected file, rerun with --replace to overwrite it explicitly."
        )

    arrow_table = pq.read_table(str(path))
    arrow_table = validate_and_normalise(arrow_table, source_label)

    print(f"  Rows   : {len(arrow_table):,}")
    print(f"  Appending …")

    tbl.append(
        arrow_table,
        snapshot_properties={
            "source_month":  source_label,
            "source_sha256": checksum,
            "ingested_at":   datetime.now(timezone.utc).isoformat(),
            "ingest_type":   "append",
        },
    )

    snap = tbl.current_snapshot()
    total = int(snap.summary.get("total-records", 0))
    print(f"  Committed snapshot {snap.snapshot_id}  (table total: {total:,} rows)")
    return True


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Ingest BASt Parquet into Iceberg on S3")
    parser.add_argument(
        "month",
        help="Month to ingest as YYYY_MM (e.g. 2026_01), or 'all' for all available months",
    )
    parser.add_argument(
        "--replace",
        action="store_true",
        help="Overwrite an already-ingested month (for corrected source files)",
    )
    args = parser.parse_args()

    catalog = open_catalog()

    if args.month == "all":
        months = all_available_months()
        if not months:
            print(f"No Parquet files found under {PARQUET_ROOT}", file=sys.stderr)
            sys.exit(1)
        print(f"Found {len(months)} months: {[f'{y}-{m}' for y,m in months]}")
    else:
        year, mon = args.month.split("_")
        months = [(year, mon)]

    any_ingested = False
    for year, mon in months:
        committed = ingest_month(catalog, year, mon, replace=args.replace)
        if committed:
            any_ingested = True

    if any_ingested or args.month == "all":
        # Reload table to get current state after all appends
        tbl = catalog.load_table(TABLE_NAME)
        manifest = read_manifest()
        existing_months = set(manifest.get("ingested_months", []))

        # Derive committed months from snapshot properties
        committed_months = set()
        for snap in tbl.snapshots():
            props = snap.summary.additional_properties if snap.summary else {}
            m = props.get("source_month")
            if m:
                committed_months.add(m)

        update_manifest(tbl, sorted(existing_months | committed_months))

    print("\nDone.")


if __name__ == "__main__":
    main()
