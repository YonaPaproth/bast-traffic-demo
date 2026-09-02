# BASt Traffic Demo — Backlog

Last updated: 2026-09-01

---

## ✅ Done (this session)

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
| Comparison table updated | Column D: cost, entry barrier, header subtitle all correct |
| Data extended to Jan–Jun 2026 | All 6 months parsed & uploaded to S3 (2026-09-02); 3.58B total KFZ, 1,943 stations, 181 days |

---

## 🔴 Immediate (blocking)

### Verify Bedrock chat works
- After commit `f6b0331` deploys (GET endpoint fix), test at the live demo
- Ask: "Which state has the most traffic?" — should stream an answer
- If still failing, check CloudFront logs or ECS task logs in CloudWatch (`/bast-api`)

### Add `elasticloadbalancing:DescribeLoadBalancers` to `bast-git`
- The "Print ALB endpoint" step in `docker-deploy.yml` fails silently
- IAM console → `github-actions-ecs-ecr-policy` → add action `elasticloadbalancing:DescribeLoadBalancers`
- Resource: `arn:aws:elasticloadbalancing:eu-central-1:112220711619:loadbalancer/app/bast-api-alb/*`

---

## 🟡 Phase 2 — Demo enhancements

### Re-parse all 6 months with corrected kfz_r2 + add LKW column

**Why:** Current `parse_bast.py` reads `values[1]` as `kfz_r2`, but in BASt Bestandsbandformat
`values[1]` is the quality indicator for Direction 1. The true Direction 2 count is at `values[2]`.
All `kfz_r2` values in the current Parquet files are wrong (quality codes, not counts).
Direction 2 has been hidden from the UI as an interim fix.

**Scope:**
- Fix `parse_bast.py`: `kfz_r2 = int(values[2])` (was `values[1]`)
- While re-parsing, also extract LKW (heavy vehicle) columns: `lkw_r1 = values[X]`, `lkw_r2 = values[X+1]`
  (verify column offsets against BASt format spec before coding)
- Re-run `parse_bast.py` for all 6 months (2026_01 through 2026_06)
- Upload corrected Parquet files to S3 (overwrite existing)
- Re-enable Direction 1 / Direction 2 lines in Hourly Profile chart once data is trustworthy
- Add LKW toggle / second dataset to Hourly Profile chart

**Reference:** BASt format header prefix `S02` means 2 values per measurement (count + quality).
Column layout: `KFZ_count_R1, KFZ_quality_R1, KFZ_count_R2, KFZ_quality_R2, ...`

---

### ~~Load Feb–Jun 2026 data~~ ✅ Done 2026-09-02
- All 6 months (Jan–Jun) live in S3 under `traffic/year=2026/month=XX/`
- Raw ZIPs remain at `s3://bast-traffic-demo-112220711619/raw/` for re-processing if needed

### Dashboard actions from chat
- Claude response can include a structured `dashboard_action` JSON block
- Frontend parses it and updates map filter / station selector
- Example: "Show me the A9" → Claude emits `{"dashboard_action":"filter_road","value":"A9"}` → frontend applies filter
- Add `dashboard_action` to `_BEDROCK_SYSTEM` prompt and parse it in the SSE stream handler

### Date range picker for overview chart
- Currently overview shows all data (Jan–Jun)
- Add a date range selector to let users zoom in on specific months

### Mobile layout
- Chat panel and charts need responsive tweaks for phones

---

## 🟠 Phase 3 — Production ingestion

### Airflow / Step Functions orchestration
- Replace manual `parse_bast.py` + S3 upload with scheduled pipeline
- Trigger monthly on BASt data release
- Idempotency: detect already-processed files, don't double-count

### Great Expectations data quality
- BASt raw data has gaps, wrong direction flags, implausible values
- Add quality checks before Parquet commit
- Surface data coverage/confidence in UI (e.g. "% stations reporting")

### AWS Glue Data Catalog (replace SQLite Iceberg catalog)
- SQLite catalog is local only; doesn't work on ECS
- Glue: ~$1/month, integrates with Athena + DuckDB REST
- Enables true Iceberg time travel queries from ECS

---

## 🔵 Phase 4 — Full open-source production stack (Option A)

### Trino on ECS Fargate
- Replace single-node DuckDB with distributed Trino
- Petabyte-scale; <10% slower than Databricks on analytics queries
- Needs Iceberg REST catalog (Glue or Nessie)

### Project Nessie (git-like data versioning)
- Branch, commit, rollback data like code
- Enables: dev → staging → prod data workflow
- Self-hostable on ECS or use Dremio Arctic (managed)

### Lake Formation RBAC
- Row/column-level security
- Multi-user, role-based data access
- Required for any real client data

### MCP server for management agents
- Expose API endpoints as MCP tools
- Claude Desktop / Claude Code can directly query live traffic data
- Good internal Accenture demo

---

## 📋 Small improvements

- [ ] Custom domain (e.g. `bast-demo.accenture.com`) instead of raw CloudFront/S3 URLs
- [ ] CloudFront access logs → analyse demo usage
- [ ] Add `stale-while-revalidate` caching on overview/states endpoints (slow S3 queries)
- [ ] Iceberg time travel demo in frontend (snapshot selector → compare traffic month-over-month)
- [ ] Add Autobahn-only filter toggle to map
- [ ] `api/main.py` has leftover `BaseModel` import after removing AskRequest — clean up
