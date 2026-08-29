"""
query_iceberg.py — Query the BASt Iceberg table via DuckDB.

Tries S3 first (with metadata file path), falls back to local, then PyIceberg.
"""

import os
import sys
import json
import subprocess
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent
LOCAL_WAREHOUSE = BASE_DIR / "data" / "iceberg_local"
S3_BUCKET = "s3://bast-traffic-demo-112220711619"
S3_TABLE_PATH = f"{S3_BUCKET}/iceberg/bast/traffic"
LOCAL_TABLE = str(LOCAL_WAREHOUSE / "bast" / "traffic")


def get_aws_credentials():
    try:
        result = subprocess.check_output(
            ["aws", "configure", "export-credentials", "--profile", "demo", "--format", "process"],
            stderr=subprocess.PIPE,
        )
        return json.loads(result)
    except Exception:
        return None


def get_latest_metadata(table_path: str, is_s3: bool = False, creds=None) -> str:
    """Return path to latest metadata.json file."""
    if is_s3:
        try:
            cmd = ["aws", "s3", "ls", f"{table_path}/metadata/", "--profile", "demo"]
            result = subprocess.check_output(cmd, stderr=subprocess.PIPE).decode()
            files = [l.split()[-1] for l in result.strip().split("\n") if ".metadata.json" in l]
            files.sort()
            return f"{table_path}/metadata/{files[-1]}"
        except Exception as e:
            print(f"  Could not list S3 metadata: {e}")
            return None
    else:
        meta_dir = Path(table_path) / "metadata"
        files = sorted(meta_dir.glob("*.metadata.json"))
        if files:
            return str(files[-1])
        return None


def run_query(con, table_ref: str, is_metadata_path: bool = False) -> object:
    """Run state aggregation query."""
    if is_metadata_path:
        scan_expr = f"iceberg_scan('{table_ref}')"
    else:
        scan_expr = f"iceberg_scan('{table_ref}', allow_moved_paths=true)"

    return con.execute(f"""
        SELECT
            state,
            SUM(kfz_total) AS total_vehicles,
            COUNT(DISTINCT station_id) AS stations
        FROM {scan_expr}
        GROUP BY state
        ORDER BY total_vehicles DESC
        LIMIT 10
    """).df()


def query_s3(creds):
    import duckdb
    print("\n── Querying S3 Iceberg via DuckDB ──────────────────────────────")
    try:
        meta_path = get_latest_metadata(S3_TABLE_PATH, is_s3=True, creds=creds)
        if not meta_path:
            print("  Could not find S3 metadata path")
            return False

        print(f"  Using metadata: {meta_path}")
        con = duckdb.connect()
        con.execute("INSTALL iceberg; LOAD iceberg;")
        con.execute(f"SET s3_region='eu-central-1';")
        con.execute(f"SET s3_access_key_id='{creds['AccessKeyId']}';")
        con.execute(f"SET s3_secret_access_key='{creds['SecretAccessKey']}';")
        if "SessionToken" in creds:
            con.execute(f"SET s3_session_token='{creds['SessionToken']}';")

        result = run_query(con, meta_path, is_metadata_path=True)
        print("✓ S3 Iceberg query succeeded!")
        print(result.to_string(index=False))
        con.close()
        return True
    except Exception as e:
        print(f"⚠ S3 query failed: {e}")
        return False


def query_local():
    import duckdb
    print("\n── Querying LOCAL Iceberg via DuckDB ───────────────────────────")
    try:
        meta_path = get_latest_metadata(LOCAL_TABLE, is_s3=False)
        if not meta_path:
            print("  Could not find local metadata path")
            return False

        print(f"  Using metadata: {meta_path}")
        con = duckdb.connect()
        con.execute("INSTALL iceberg; LOAD iceberg;")

        result = run_query(con, meta_path, is_metadata_path=True)
        print("✓ Local Iceberg query succeeded!")
        print(result.to_string(index=False))
        con.close()
        return True
    except Exception as e:
        print(f"⚠ Local DuckDB query failed: {e}")
        return False


def query_via_pyiceberg():
    """Fallback: use PyIceberg to read + pandas for display."""
    from pyiceberg.catalog.sql import SqlCatalog
    print("\n── Querying via PyIceberg (fallback) ───────────────────────────")
    try:
        catalog = SqlCatalog(
            "bast_local",
            **{
                "uri": f"sqlite:///{BASE_DIR / 'data' / 'iceberg_catalog.db'}",
                "warehouse": f"file://{BASE_DIR / 'data' / 'iceberg_local'}",
            },
        )
        table = catalog.load_table("bast.traffic")
        df = table.scan(limit=2_000_000).to_pandas()
        result = (
            df.groupby("state")
            .agg(total_vehicles=("kfz_total", "sum"), stations=("station_id", "nunique"))
            .sort_values("total_vehicles", ascending=False)
            .head(10)
            .reset_index()
        )
        print("✓ PyIceberg query succeeded!")
        print(result.to_string(index=False))
        return True
    except Exception as e:
        print(f"⚠ PyIceberg fallback failed: {e}")
        return False


def main():
    print("=== BASt Iceberg Query Demo ===")

    creds = get_aws_credentials()
    s3_ok = False
    local_ok = False

    if creds:
        s3_ok = query_s3(creds)

    local_ok = query_local()

    if not local_ok:
        print("Trying PyIceberg fallback...")
        query_via_pyiceberg()

    print("\n=== Query Summary ===")
    print(f"  S3 Iceberg    : {'✓ OK' if s3_ok else '✗ Failed'}")
    print(f"  Local Iceberg : {'✓ OK' if local_ok else '✗ Failed'}")


if __name__ == "__main__":
    main()
