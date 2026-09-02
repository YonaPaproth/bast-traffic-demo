# CLAUDE.md — BASt Traffic Demo

Context file for Claude Code. Read this before touching anything.

---

## What this is

A live AWS demo for Accenture client pitches. Shows an open-source data stack (S3 + Apache Iceberg + DuckDB + FastAPI) competing with Databricks/Fabric, plus a live Claude Bedrock chat agent doing text-to-SQL against ~3.58B vehicle records. Built entirely with Claude Code.

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
| **Bedrock model** | `eu.anthropic.claude-haiku-4-5-20251001-v1:0` | EU cross-region inference profile; marketplace-activated; invoked from ECS task |

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
| `GET` | `/api/stations` | All stations; flat array; fields: `station_id, station_name, state, road_class, road_number, lat, lon, total_kfz` |
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
- **Volume:** ~3.58B vehicle records, 1,943 counting stations, 181 days
- **Format:** Parquet on S3, glob: `s3://bast-traffic-demo-112220711619/traffic/**/*.parquet`
- **Columns:** `station_id, station_name, state, road_class, road_number, lat, lon, date DATE, hour INTEGER (0-23), kfz_r1, kfz_r2, kfz_total INTEGER`
- **Iceberg catalog:** SQLite (local only; not used on ECS — ECS queries Parquet directly via DuckDB httpfs)
- **Raw ZIPs on S3:** `s3://bast-traffic-demo-112220711619/raw/` — Feb–Jun ZIPs only; Jan ZIP not uploaded

**kfz_r2 parsing bug (known, not yet fixed):** `parse_bast.py` reads `values[1]` as `kfz_r2`, but `values[1]` is the BASt quality indicator for Direction 1 — the real Direction 2 count is at `values[2]`. All `kfz_r2` values in current Parquet files are wrong. Direction 2 is hidden from the UI. Re-parse task is in BACKLOG.md Phase 2.

---

## Accenture SOC / network policy — HARD RULES

**Never use PowerShell to make outbound HTTP/HTTPS connections to external IPs or AWS endpoints.**

On 2026-09-01, the Accenture Security Operations Center (ASOC) isolated this workstation because Claude Code ran a PowerShell `Invoke-RestMethod` call to the live ALB IP (`18.198.51.123:8000`). PowerShell making outbound connections to non-corporate IPs is a high-fidelity attack-pattern signature for ASOC and triggers automatic isolation.

**Rules that apply in this project:**

1. **No `Invoke-RestMethod` or `Invoke-WebRequest` in PowerShell** — ever, against any external host (AWS, ALB, CloudFront, internet).
2. **No PowerShell HTTP calls to raw IP addresses** — even internal-looking ranges.
3. **To smoke-test a live endpoint, use the Bash tool with `curl`** (Git Bash, not PowerShell). `curl` via Bash does not trigger the SOC rule. Example: `curl -s https://d1905gj4v53w41.cloudfront.net/health`.
4. **Prefer browser verification or trusting CI/CD output** over any local HTTP test after a deploy. The GitHub Actions workflow already waits for ECS stability.
5. **If PowerShell must run a network-adjacent command** (e.g. `aws` CLI, `docker`), that is fine — those are signed corporate-managed binaries. The rule is specifically about PowerShell's own HTTP cmdlets against external hosts.

---

## Known issues / watch out

1. **`/api/iceberg/info` returns 404 on ECS** — SQLite catalog doesn't exist inside the container. This is expected and fine for the demo.

2. **`bast-git` missing `elasticloadbalancing:DescribeLoadBalancers`** — the "Print ALB endpoint" step in `docker-deploy.yml` will fail. Doesn't block the deploy but creates a noisy failure. Fix: add this permission to `github-actions-ecs-ecr-policy` in IAM console.

3. **CORS on CloudFront** — CloudFront behavior has `CORS-CustomOrigin` origin request policy + all HTTP methods allowed. `/api/ask` uses GET (not POST) to avoid OPTIONS preflight. If you add any new POST endpoints called from the frontend, you'll need to verify OPTIONS preflights work.

4. **ECS health check** — requires `curl` in the image (already installed via apt-get in Dockerfile). Health check: `curl -f http://localhost:8000/health`. `startPeriod: 120s` to allow time for httpfs extension load.

5. **Task def version** — the workflow explicitly pins `bast-api:2`. If you register a new task def version, update both the `update-service` and `create-service` lines in `docker-deploy.yml`.

6. **`/api/stations` scans January only** — to avoid OOM on the full 3.58B-row glob, the station metadata query reads only `year=2026/month=01/traffic.parquet`. Station attributes (name, state, road class, lat/lon) are stable across months. `total_kfz` in the response is `SUM(kfz_r1)` for January only — relative values are correct for map bubble sizing.

7. **kfz_r2 in Parquet is wrong** — see Data section above. Do not add any new UI features that rely on `kfz_r2` or `kfz_total` until the re-parse is done.

8. **Hourly Profile default station** — frontend defaults to `NW5048` (Köln-Nord, A1, NW). Falls back to `d[0]` if not present.

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

1. **Verify Bedrock chat works** — test `/api/ask?q=Which+state+has+most+traffic` at the live demo; check CloudWatch `/bast-api` logs if it fails
2. **Add `elasticloadbalancing:DescribeLoadBalancers`** to `bast-git` IAM policy (noisy deploy step)
3. **Re-parse all 6 months** — fix `kfz_r2 = int(values[2])` in `parse_bast.py`, add LKW column, re-upload to S3, re-enable Direction 2 in UI (see BACKLOG.md Phase 2 for full scope)
4. **Delete local `data/raw/`** — free ~2.3 GB; Feb–Jun ZIPs are on S3; re-download Jan from BASt when needed for re-parse
5. Phase 2 stretch: Dashboard actions from chat (parse `dashboard_action` JSON in SSE stream)
