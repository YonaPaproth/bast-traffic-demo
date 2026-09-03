# BASt Traffic Demo — Backlog

Last updated: 2026-09-03

---

## ✅ Done (this session — 2026-09-03)

| Item | Notes |
|---|---|
| YoY H1 2025 vs H1 2026 comparison feature | New `/api/traffic/yoy` and `/api/traffic/yoy/stations` endpoints; YoY section in frontend with delta KPI cards + state bar chart + road-class grouped chart |
| S-line aware parser | `parse_bast.py` now reads S-line header per station to dynamically find PKW column offset; fixes kfz_r2=values[2]; adds sv_r1 + pkw_r1 columns; handles S02 06 format stations (no Pkw column) |
| 2025 H1 data downloaded | `DZ_2025_Rohdaten.zip` (883 MB) downloaded; months 01–06 unzipped into `data/raw/DZ_2025_0X_Rohdaten/` |
| 2025 H1 parsing in progress | Months 01–02 done; 03–06 + 2026 02–06 re-parse running in background (updates schema with sv_r1/pkw_r1) |
| COALESCE in YoY SQL | `SUM(COALESCE(pkw_r1,0))` / `SUM(COALESCE(sv_r1,0))` handles old 2026/01 Parquet without these columns |
| Bedrock system prompt updated | Now covers H1 2025 AND 2026; mentions pkw_r1, sv_r1, geopolitical context |

## ✅ Done (previous sessions)

| Item | Notes |
|---|---|
| FastAPI deployed to ECS Fargate | Task def `bast-api:2`, cluster `bast-cluster` |
| ALB + CloudFront HTTPS | Stable endpoint `d1905gj4v53w41.cloudfront.net` |
| GitHub Actions CI/CD | `docker-deploy.yml` (ECS) + `deploy.yml` (S3 frontend) |
| Frontend migrated from static JSON to live API | All 4 chart types call live endpoints |
| Bedrock Claude chat agent | `/api/ask?q=` SSE streaming, text-to-SQL agentic loop |
| ECS health check fix | Added `curl` to Dockerfile; `startPeriod: 120s` |
| CORS fix | Changed `/api/ask` from POST to GET; CloudFront CORS-CustomOrigin policy |
| Architecture page updated | Reflects ECS Fargate + ALB + CloudFront + Bedrock stack |
| Data extended to Jan–Jun 2026 | All 6 months parsed & uploaded to S3 (2026-09-02); 3.58B total KFZ, 1,943 stations, 181 days |
| Fix /api/stations HTTP 500 | Rewrote station query to scan Jan-only Parquet; removed OOM-prone SUM aggregate |
| Fix map station markers | Restored `total_kfz = SUM(kfz_r1)` to /api/stations |
| Average Daily Pattern Y-axis fix | Replaced Plotly div; explicit `range`/`dtick` from data |
| kfz_r2 removed from Hourly Profile | Parsing bug found; interim fix (hide Direction 2) |
| Default Hourly Profile station | Köln-Nord NW A1 (NW5048) |

---

## 🔴 Immediate (next steps)

### Upload 2025 + corrected 2026 Parquet to S3

After background parse completes (months 2025_03–06 and 2026_02–06), upload each file:

```bash
for year_month in 2025/month=01 2025/month=02 2025/month=03 2025/month=04 2025/month=05 2025/month=06 \
                  2026/month=02 2026/month=03 2026/month=04 2026/month=05 2026/month=06; do
  aws s3 cp "data/parquet/year=$year_month/traffic.parquet" \
    "s3://bast-traffic-demo-112220711619/traffic/year=$year_month/traffic.parquet" \
    --profile claude-code
done
```

Note: 2026/month=01 is kept as-is on S3 (raw data not available locally). COALESCE in API handles missing sv_r1/pkw_r1.

### Verify YoY charts on live demo
After S3 upload, open `https://bast-traffic-demo-112220711619.s3.eu-central-1.amazonaws.com/index.html`
and verify the YoY section shows real data (not the fallback "data unavailable" message).

### Test Bedrock chat with YoY questions
- "Which state had the biggest drop in freight traffic between 2025 and 2026?"
- "How did Autobahn vs Bundesstraße traffic compare in H1 2025 vs 2026?"

### Add `elasticloadbalancing:DescribeLoadBalancers` to `bast-git`
IAM console → `github-actions-ecs-ecr-policy` → add action `elasticloadbalancing:DescribeLoadBalancers`
Resource: `arn:aws:elasticloadbalancing:eu-central-1:112220711619:loadbalancer/app/bast-api-alb/*`

---

## 🟡 Phase 2 — Demo enhancements

### Re-parse 2026/01 with new schema (optional)
- Raw ZIP for January 2026 is not on S3 and not available locally
- Download from BASt website if Direction 2 or per-type breakdown is needed for that month
- Currently the COALESCE fallback gives sv_r1=0/pkw_r1=0 for 2026/01 — only affects Jan 2026 in YoY aggregate

### Re-enable Direction 2 in Hourly Profile
- kfz_r2 is now correctly parsed as values[2] in new Parquet files
- Add back Direction 2 series to the Hourly Profile chart
- Only shows for 2026/02–06 and 2025/01–06; 2026/01 on S3 still has old kfz_r2 (wrong)

### Dashboard actions from chat
- Parse `dashboard_action` JSON in SSE stream and update map filter / station selector

### Date range picker for overview chart
### Mobile layout

---

## 🟠 Phase 3 — Production ingestion

### Airflow / Step Functions orchestration
- Replace manual `parse_bast.py` + S3 upload with scheduled pipeline

### Great Expectations data quality

### AWS Glue Data Catalog (replace SQLite Iceberg catalog)

---

## 🔵 Phase 4 — Full open-source production stack

### Trino on ECS Fargate
### Project Nessie (git-like data versioning)
### Lake Formation RBAC
### MCP server for management agents

---

## 📋 Small improvements

- [ ] Custom domain (e.g. `bast-demo.accenture.com`) instead of raw CloudFront/S3 URLs
- [ ] CloudFront access logs → analyse demo usage
- [ ] Add `stale-while-revalidate` caching on overview/states endpoints
- [ ] Iceberg time travel demo in frontend
- [ ] Add Autobahn-only filter toggle to map
- [ ] `api/main.py` has leftover `BaseModel` import — clean up
- [ ] Delete `data/raw/` locally to free ~2.3 GB after re-parse is done
- [ ] Station name encoding bug — some names double-encoded UTF-8 as latin-1
- [ ] PKW share is ~12.5% aggregate (many S02 06 stations lack Pkw column) — consider flagging in UI that PKW stats only cover stations with per-type data
