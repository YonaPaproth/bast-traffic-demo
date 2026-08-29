#!/usr/bin/env python3
"""
upload_s3.py — Upload partitioned Parquet files to S3.

Target: s3://bast-traffic-demo-112220711619/traffic/
Profile: demo
"""

import subprocess
import sys
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent
PARQUET_DIR = BASE_DIR / "data" / "parquet"
S3_BUCKET = "s3://bast-traffic-demo-112220711619"
S3_PREFIX = "traffic"
AWS_PROFILE = "demo"


def human_size(size_bytes: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} TB"


def upload_file(local_path: Path, s3_key: str) -> bool:
    s3_uri = f"{S3_BUCKET}/{s3_key}"
    size = local_path.stat().st_size
    print(f"  Uploading {local_path.relative_to(BASE_DIR)} ({human_size(size)}) → {s3_uri}")

    result = subprocess.run(
        [
            "aws", "s3", "cp",
            str(local_path),
            s3_uri,
            "--profile", AWS_PROFILE,
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"    ERROR: {result.stderr.strip()}", file=sys.stderr)
        return False
    return True


def main():
    if not PARQUET_DIR.exists():
        print(f"ERROR: Parquet directory not found: {PARQUET_DIR}")
        print("Run scripts/parse_bast.py first.")
        sys.exit(1)

    parquet_files = list(PARQUET_DIR.rglob("*.parquet"))
    if not parquet_files:
        print("No Parquet files found. Run scripts/parse_bast.py first.")
        sys.exit(1)

    print(f"BASt Traffic → S3 Upload")
    print(f"Bucket  : {S3_BUCKET}")
    print(f"Profile : {AWS_PROFILE}")
    print(f"Files   : {len(parquet_files)}")
    print()

    success = 0
    for local_path in sorted(parquet_files):
        # Build S3 key: traffic/year=2026/month=01/traffic.parquet
        relative = local_path.relative_to(PARQUET_DIR)
        s3_key = f"{S3_PREFIX}/{relative}"
        if upload_file(local_path, s3_key):
            success += 1

    print(f"\nUploaded {success}/{len(parquet_files)} file(s).")

    if success == len(parquet_files):
        print(f"\nAll files available at:")
        for local_path in sorted(parquet_files):
            relative = local_path.relative_to(PARQUET_DIR)
            print(f"  {S3_BUCKET}/{S3_PREFIX}/{relative}")
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
