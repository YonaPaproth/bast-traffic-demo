# CLAUDE.md — BASt Traffic Demo

Context file for Claude Code. Read this before touching anything.

---

## What this is

A live AWS demo for Accenture client pitches. Shows an open-source data stack (S3 + Apache Iceberg + DuckDB + FastAPI) competing with Databricks/Fabric, plus a live Claude Bedrock chat agent doing text-to-SQL against ~569M vehicle records. Built entirely with Claude Code.

---

## Live URLs

| Resource | URL |
|---|---|
| **Frontend (S3 static)** | `https://bast-traffic-demo-112220711619.s3.eu-central-1.amazonaws.com/index.html` |
| **Architecture page** | same bucket, `architecture.html` |
| **API (HTTPS via CloudFront)** | `https://d1905gj4v53w41.cloudfront.net` |
| **API health check** | `https://d1905gj4v53w41.cloudfront.net/health` |
| **API docs (Swagger)** | `https://d1905gj4v53w41.cloudfront.net/docs` |
| **ALB (HTTP only)** | `http://bast-api-alb-1659310948.eu-central-1.elb.amazonaws.com` |

---

## AWS Resources

| Service | Name / ID | Notes |
|---|---|---|
| **S3 bucket** | `bast-traffic-demo-112220711619` | `eu-central-1`; Parquet under `traffic/**/*.parquet`; frontend under root |
| **ECR repo** | `bast-api` | `112220711619.dkr.ecr.eu-central-1.amazonaws.com/bast-api` |
| **ECS cluster** | `bast-cluster` | Fargate |
| **ECS service** | `bast-api-service` | 1 task, task def `bast-api:2` |
| **Task def** | `bast-api:2` | 0.5 vCPU / 1 GB; uses `bast-api:latest` image |
| **ALB** | `bast-api-alb` | Internet-facing; listener on :80; forwards to ECS |
| **CloudFront** | `E1WGCWTNHFN4VX` | `d1905gj4v53w41.cloudfront.net`; CachingDisabled; CORS-CustomOrigin origin request policy |
| **Bedrock model** | `anthropic.claude-sonnet-4-6` | `eu-central-1`; invoked from ECS task |

**IAM roles:**
- `bast-api-task-role` — S3 read (`traffic/**`) + `bedrock:InvokeModelWithResponseStream`
- `bast-task-execution-role` — ECR pull + CloudWatch Logs write
- `bast-git` — GitHub Actions deploy user; has ECR + ECS + ELB describe permissions

**Security constraint (hard):** AWS credentials for `claude-bast`/`claude-code` IAM user stay on the local machine only. Never commit to git, never put in GitHub Secrets, never log or echo them.

---

## CI/CD (GitHub Actions)

| Workflow | Trigger | What it does |
|---|---|---|
| `.github/workflows/docker-deploy.yml` | push to `api/**`, `Dockerfile`, `requirements.txt` | Build Docker image → push to ECR (SHA + `latest` tags) → `update-service --task-definition bast-api:2 --force-new-deployment` → wait stable |
| `.github/workflows/deploy.yml` | push to `frontend/**` | `aws s3 sync frontend/ s3://...` |

The workflow uses `bast-api:2` explicitly (not `LATEST`) because the task definition pins the image to `bast-api:latest` and we want the task def version to be stable. When you register a new task definition, update the `--task-definition` arg in both the `create` and `update` paths of `docker-deploy.yml`.

---

## API endpoints

All served via CloudFront HTTPS (`https://d1905gj4v53w41.cloudfront.net`).

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Health check → `{"status":"ok"}` |
| `GET` | `/api/stations` | All stations; flat array; fields include `total_kfz` |
| `GET` | `/api/traffic/overview` | KPIs + daily totals + top10_stations (field: `total_kfz`) |
| `GET` | `/api/traffic/states` | Per-state totals (field: `total_kfz`) |
| `GET` | `/api/traffic/hourly?station_id=&date=` | Hourly profile for one station/day |
| `GET` | `/api/traffic/daily?station_id=&start=&end=` | Daily totals for one station |
| `GET` | `/api/ask?q=` | Bedrock chat agent; streams SSE (`text/event-stream`) |
| `GET` | `/api/iceberg/info` | Iceberg table metadata (local only; 404 on ECS) |

**`/api/ask` SSE event types:**

```
data: {"type":"text","delta":"..."}
data: {"type":"tool_start","name":"execute_sql"}
data: {"type":"tool_running","query":"SELECT ..."}
data: {"type":"done"}
```

---

## Data

- **Source:** BASt open data, Jan–Jun 2026 (6 months)
- **Volume:** ~569M vehicle records, 1,832 counting stations
- **Format:** Parquet on S3, glob: `s3://bast-traffic-demo-112220711619/traffic/**/*.parquet`
- **Columns:** `station_id, station_name, state, road_class, road_number, lat, lon, date DATE, hour INTEGER (0-23), kfz_r1, kfz_r2, kfz_total INTEGER`
- **Iceberg catalog:** SQLite (local only; not used on ECS — ECS queries Parquet directly via DuckDB httpfs)

---

## Known issues / watch out

1. **`/api/iceberg/info` returns 404 on ECS** — SQLite catalog doesn't exist inside the container. This is expected and fine for the demo.

2. **`bast-git` missing `elasticloadbalancing:DescribeLoadBalancers`** — the "Print ALB endpoint" step in `docker-deploy.yml` will fail. Doesn't block the deploy but creates a noisy failure. Fix: add this permission to `github-actions-ecs-ecr-policy` in IAM console.

3. **CORS on CloudFront** — CloudFront behavior has `CORS-CustomOrigin` origin request policy + all HTTP methods allowed. `/api/ask` uses GET (not POST) to avoid OPTIONS preflight. If you add any new POST endpoints called from the frontend, you'll need to verify OPTIONS preflights work.

4. **ECS health check** — requires `curl` in the image (already installed via apt-get in Dockerfile). Health check: `curl -f http://localhost:8000/health`. `startPeriod: 120s` to allow time for httpfs extension load.

5. **Task def version** — the workflow explicitly pins `bast-api:2`. If you register a new task def version, update both the `update-service` and `create-service` lines in `docker-deploy.yml`.

---

## Architecture diagram (text)

```
BASt CSV/ZIP → parse_bast.py → Parquet on S3 (Iceberg v2 format)
                                        ↓
                               ECS Fargate (FastAPI)
                               DuckDB httpfs → S3 Parquet
                               boto3 → Bedrock Claude
                                        ↓
                               ALB (bast-api-alb) :80
                                        ↓
                               CloudFront HTTPS (E1WGCWTNHFN4VX)
                                        ↓
                               S3 Static Frontend (index.html)
                               Plotly charts + SSE chat panel
```

---

## Next steps (see BACKLOG.md for full list)

1. Verify Bedrock chat works after the GET endpoint deploy (commit `f6b0331`)
2. Add `elasticloadbalancing:DescribeLoadBalancers` to `bast-git` IAM policy
3. Phase 2: Parse and load Feb–Jun 2026 data (currently only Jan is loaded)
4. Phase 2 stretch: Dashboard actions from chat (parse `dashboard_action` JSON in SSE stream)
