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
| Data extended to Jan–Jun 2026 | Was Jan only |

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

### Load Feb–Jun 2026 data
- Currently only Jan 2026 Parquet is in S3 (569M records)
- BASt publishes monthly. Download + run `parse_bast.py` for each month
- Upload to `s3://bast-traffic-demo-112220711619/traffic/`
- No API changes needed — glob picks up new files automatically

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
