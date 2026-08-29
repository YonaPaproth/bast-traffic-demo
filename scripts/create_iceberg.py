"""
create_iceberg.py — Convert BASt Parquet data to Apache Iceberg format.

Creates:
  - Local Iceberg catalog (SQLite) at data/iceberg_catalog.db
  - Local warehouse at data/iceberg_local/ (for API use)
  - S3 warehouse at s3://bast-traffic-demo-112220711619/iceberg/ (for demo)
"""

import os
import sys
import json
import subprocess
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

BASE_DIR = Path(__file__).parent.parent
PARQUET_PATH = BASE_DIR / "data" / "parquet" / "year=2026" / "month=01" / "traffic.parquet"
LOCAL_CATALOG_DB = BASE_DIR / "data" / "iceberg_catalog.db"
LOCAL_WAREHOUSE = str(BASE_DIR / "data" / "iceberg_local")
S3_WAREHOUSE = "s3://bast-traffic-demo-112220711619/iceberg"

os.chdir(BASE_DIR)  # Ensure relative paths work


def get_aws_credentials():
    """Export AWS credentials from the 'demo' profile."""
    try:
        result = subprocess.check_output(
            ["aws", "configure", "export-credentials", "--profile", "demo", "--format", "process"],
            stderr=subprocess.PIPE,
        )
        creds = json.loads(result)
        os.environ["AWS_ACCESS_KEY_ID"] = creds["AccessKeyId"]
        os.environ["AWS_SECRET_ACCESS_KEY"] = creds["SecretAccessKey"]
        if "SessionToken" in creds:
            os.environ["AWS_SESSION_TOKEN"] = creds["SessionToken"]
        os.environ["AWS_DEFAULT_REGION"] = "eu-central-1"
        print("✓ AWS credentials loaded from 'demo' profile")
        return True
    except Exception as e:
        print(f"⚠ Could not load AWS credentials: {e}")
        return False


def load_parquet():
    print(f"Loading parquet: {PARQUET_PATH}")
    table = pq.read_table(str(PARQUET_PATH))
    print(f"✓ Loaded {len(table):,} rows, schema: {table.schema}")
    return table


def create_local_iceberg(table: pa.Table):
    """Create local Iceberg catalog + table for API use."""
    from pyiceberg.catalog.sql import SqlCatalog

    print("\n── Creating LOCAL Iceberg catalog ──────────────────────────────")
    Path(LOCAL_WAREHOUSE).mkdir(parents=True, exist_ok=True)

    catalog = SqlCatalog(
        "bast_local",
        **{
            "uri": f"sqlite:///{LOCAL_CATALOG_DB}",
            "warehouse": f"file://{LOCAL_WAREHOUSE}",
        },
    )

    # Create namespace
    try:
        catalog.create_namespace("bast")
        print("✓ Namespace 'bast' created")
    except Exception:
        print("  Namespace 'bast' already exists")

    # Drop existing table if present
    try:
        catalog.drop_table("bast.traffic")
        print("  Dropped existing 'bast.traffic' table")
    except Exception:
        pass

    # Create table + append data
    iceberg_table = catalog.create_table("bast.traffic", schema=table.schema)
    iceberg_table.append(table)

    snap = iceberg_table.current_snapshot()
    print(f"✓ Local Iceberg table created")
    print(f"  Location    : {iceberg_table.location()}")
    print(f"  Snapshot ID : {snap.snapshot_id}")
    print(f"  Num snapshots: {len(list(iceberg_table.snapshots()))}")

    return iceberg_table, snap.snapshot_id


def create_s3_iceberg(table: pa.Table, aws_ok: bool):
    """Create S3-backed Iceberg catalog + table."""
    if not aws_ok:
        print("\n⚠ Skipping S3 Iceberg (no AWS credentials)")
        return None, None

    from pyiceberg.catalog.sql import SqlCatalog

    S3_CATALOG_DB = BASE_DIR / "data" / "iceberg_s3_catalog.db"

    print("\n── Creating S3 Iceberg catalog ─────────────────────────────────")

    catalog = SqlCatalog(
        "bast_s3",
        **{
            "uri": f"sqlite:///{S3_CATALOG_DB}",
            "warehouse": S3_WAREHOUSE,
            "s3.region": "eu-central-1",
            "s3.endpoint": "https://s3.eu-central-1.amazonaws.com",
        },
    )

    # Create namespace
    try:
        catalog.create_namespace("bast")
        print("✓ Namespace 'bast' created")
    except Exception:
        print("  Namespace 'bast' already exists")

    # Drop existing table if present
    try:
        catalog.drop_table("bast.traffic")
        print("  Dropped existing 'bast.traffic' table")
    except Exception:
        pass

    try:
        iceberg_table = catalog.create_table("bast.traffic", schema=table.schema)
        iceberg_table.append(table)

        snap = iceberg_table.current_snapshot()
        print(f"✓ S3 Iceberg table created")
        print(f"  Location    : {iceberg_table.location()}")
        print(f"  Snapshot ID : {snap.snapshot_id}")
        return iceberg_table, snap.snapshot_id
    except Exception as e:
        print(f"⚠ S3 Iceberg creation failed: {e}")
        return None, None


def main():
    print("=== BASt Traffic → Apache Iceberg ===\n")
    aws_ok = get_aws_credentials()

    table = load_parquet()

    local_table, local_snap_id = create_local_iceberg(table)

    s3_table, s3_snap_id = create_s3_iceberg(table, aws_ok)

    print("\n=== Summary ===")
    print(f"  Local catalog : {LOCAL_CATALOG_DB}")
    print(f"  Local warehouse: {LOCAL_WAREHOUSE}")
    print(f"  Local snapshot ID: {local_snap_id}")
    if s3_snap_id:
        print(f"  S3 warehouse  : {S3_WAREHOUSE}")
        print(f"  S3 snapshot ID: {s3_snap_id}")
    print("\n✅ Done! Iceberg tables ready.")


if __name__ == "__main__":
    main()
