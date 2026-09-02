#!/usr/bin/env python3
"""
parse_bast.py — Parse BASt Bestandsbandformat traffic counting data.

Usage:
  python parse_bast.py [YYYY_MM]          e.g. 2026_01  (default: 2026_01)

Reads all station files from data/raw/DZ_<YYYY_MM>_Rohdaten/, joins with
metadata, and writes partitioned Parquet to
data/parquet/year=<YYYY>/month=<MM>/traffic.parquet.

After parsing, upload to S3:
  aws s3 cp data/parquet/year=<YYYY>/month=<MM>/traffic.parquet \
    s3://bast-traffic-demo-112220711619/traffic/year=<YYYY>/month=<MM>/traffic.parquet \
    --profile claude-code
"""

import re
import sys
import time
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

# ── Paths ──────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent.parent

_MONTH_ARG = sys.argv[1] if len(sys.argv) > 1 else "2026_01"
_YEAR, _MON = _MONTH_ARG.split("_")
RAW_DIR    = BASE_DIR / "data" / "raw" / f"DZ_{_MONTH_ARG}_Rohdaten"
PARQUET_DIR = BASE_DIR / "data" / "parquet" / f"year={_YEAR}" / f"month={_MON}"
METADATA_CSV = RAW_DIR / f"_DZ_{_MONTH_ARG}_Metadaten.csv"

# ── Regex ──────────────────────────────────────────────────────────────────────
DATA_LINE_RE = re.compile(r"^(\d{6})\s+(\d{2}):(\d{2})\s+(.+)$")
VALUE_RE = re.compile(r"(\d+)-")

# ── UTM32 → WGS84 (via pyproj if available) ───────────────────────────────────
try:
    from pyproj import Transformer
    _transformer = Transformer.from_crs("EPSG:25832", "EPSG:4326", always_xy=True)

    def utm_to_wgs84(easting: float, northing: float):
        lon, lat = _transformer.transform(easting, northing)
        return round(lat, 6), round(lon, 6)

    HAS_PYPROJ = True
    print("pyproj available — converting UTM32 → WGS84")
except ImportError:
    HAS_PYPROJ = False
    print("pyproj not available — lat/lon will be None")

    def utm_to_wgs84(easting: float, northing: float):
        return None, None


# ── Load metadata ──────────────────────────────────────────────────────────────
def load_metadata() -> dict:
    """Return dict keyed by station number (int) → metadata dict."""
    meta = {}
    df = pd.read_csv(
        METADATA_CSV,
        sep=";",
        encoding="latin-1",
        dtype=str,
    )
    # Normalise column names (strip whitespace)
    df.columns = [c.strip() for c in df.columns]

    for _, row in df.iterrows():
        try:
            num = int(str(row["Dauerzaehlstellennummer"]).strip())
        except (ValueError, KeyError):
            continue

        # UTM coordinates use comma as decimal separator
        try:
            utm_e = float(str(row.get("Koordinaten_UTM32_E", "")).replace(",", "."))
            utm_n = float(str(row.get("Koordinaten_UTM32_N", "")).replace(",", "."))
            lat, lon = utm_to_wgs84(utm_e, utm_n)
        except (ValueError, TypeError):
            lat, lon = None, None

        meta[num] = {
            "station_name": str(row.get("Dauerzaehlstellenname", "")).strip(),
            "state": str(row.get("Landeskuerzel", "")).strip(),
            "road_class": str(row.get("Strassenklasse", row.get("Stra\xdfenklasse", ""))).strip(),
            "road_number": str(row.get("Strassennummer", row.get("Stra\xdfennummer", ""))).strip(),
            "lat": lat,
            "lon": lon,
        }
    print(f"Loaded metadata for {len(meta)} stations")
    return meta


# ── Parse a single station file ────────────────────────────────────────────────
def parse_station_file(path: Path, meta: dict) -> list[dict]:
    """Parse one BASt station file and return a list of record dicts."""
    # Derive station_id from filename (e.g., "BB3592" from "BB3592.261")
    station_id = path.stem  # e.g. "BB3592"
    # Extract numeric part for metadata lookup
    numeric_match = re.search(r"(\d+)$", station_id)
    station_num = int(numeric_match.group(1)) if numeric_match else None
    station_meta = meta.get(station_num, {})

    records = []
    try:
        with open(path, "r", encoding="latin-1", errors="replace") as f:
            for line in f:
                line = line.rstrip("\r\n")
                # Skip header lines
                if not line or line[0] in ("H", "R", "S"):
                    continue

                m = DATA_LINE_RE.match(line)
                if not m:
                    continue

                date_str, hh, mm, values_str = m.groups()
                # Parse date: YYMMDD → datetime
                try:
                    date = pd.to_datetime(date_str, format="%y%m%d")
                except ValueError:
                    continue

                hour = int(hh)

                # Extract numeric values (strip trailing `-`)
                values = VALUE_RE.findall(values_str)
                if len(values) < 2:
                    continue

                kfz_r1 = int(values[0])
                kfz_r2 = int(values[1])
                kfz_total = kfz_r1 + kfz_r2

                records.append({
                    "date": date,
                    "hour": hour,
                    "station_id": station_id,
                    "station_name": station_meta.get("station_name", ""),
                    "state": station_meta.get("state", ""),
                    "road_class": station_meta.get("road_class", ""),
                    "road_number": station_meta.get("road_number", ""),
                    "kfz_r1": kfz_r1,
                    "kfz_r2": kfz_r2,
                    "kfz_total": kfz_total,
                    "lat": station_meta.get("lat"),
                    "lon": station_meta.get("lon"),
                })
    except Exception as e:
        print(f"  WARNING: Failed to parse {path.name}: {e}", file=sys.stderr)

    return records


# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    t0 = time.time()
    print(f"BASt Traffic Parser")
    print(f"Raw data dir : {RAW_DIR}")
    print(f"Output dir   : {PARQUET_DIR}")
    print()

    meta = load_metadata()

    # Collect all station files (exclude CSV metadata file)
    station_files = sorted(
        p for p in RAW_DIR.iterdir()
        if p.is_file() and not p.name.startswith("_") and not p.suffix == ".csv"
    )
    print(f"Found {len(station_files)} station files\n")

    all_records = []
    for i, path in enumerate(station_files, 1):
        records = parse_station_file(path, meta)
        all_records.extend(records)
        if i % 200 == 0 or i == len(station_files):
            print(f"  [{i}/{len(station_files)}] Parsed {len(all_records):,} records so far …")

    print(f"\nBuilding DataFrame …")
    df = pd.DataFrame(all_records)

    if df.empty:
        print("ERROR: No records parsed. Check data directory and format.")
        sys.exit(1)

    # Type coercion
    df["date"] = pd.to_datetime(df["date"])
    df["hour"] = df["hour"].astype("int16")
    df["kfz_r1"] = df["kfz_r1"].astype("int32")
    df["kfz_r2"] = df["kfz_r2"].astype("int32")
    df["kfz_total"] = df["kfz_total"].astype("int32")
    df["lat"] = df["lat"].astype("float64")
    df["lon"] = df["lon"].astype("float64")

    # ── Write Parquet ──────────────────────────────────────────────────────────
    PARQUET_DIR.mkdir(parents=True, exist_ok=True)
    out_path = PARQUET_DIR / "traffic.parquet"
    table = pa.Table.from_pandas(df, preserve_index=False)
    pq.write_table(table, out_path, compression="snappy")

    elapsed = time.time() - t0
    size_mb = out_path.stat().st_size / 1_048_576

    # ── Stats ──────────────────────────────────────────────────────────────────
    print(f"\n{'='*55}")
    print(f"  Total rows       : {len(df):>12,}")
    print(f"  Unique stations  : {df['station_id'].nunique():>12,}")
    print(f"  Date range       : {df['date'].min().date()} -> {df['date'].max().date()}")
    print(f"  Total KFZ        : {df['kfz_total'].sum():>12,}")
    print(f"  Output file      : {out_path}")
    print(f"  File size        : {size_mb:>11.2f} MB")
    print(f"  Elapsed          : {elapsed:>11.1f} s")
    print(f"{'='*55}\n")


if __name__ == "__main__":
    main()
