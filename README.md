# BASt Traffic Demo — Agent-First Analytics

A reference implementation for a simpler data and agent stack: open tables, embedded SQL, explicit domain objects, and evidence-linked action proposals. German traffic data from BASt provides the example; the architecture is intended to transfer to other analytical and operational use cases.

The app combines Apache Iceberg on S3, DuckDB inside FastAPI, Claude through Amazon Bedrock, and an HTML/JavaScript frontend with Plotly. It demonstrates analytical tools and a proposal → review → audit-history workflow. It does not execute operational changes.

## Explore

- [Live dashboard and agent](https://bast-traffic-demo-112220711619.s3.eu-central-1.amazonaws.com/index.html)
- [Management briefing](frontend/management.html) — potential, adoption, and value measurement
- [Architecture](frontend/architecture.html) — production responsibilities and the progression from this implementation
- [Deployed API documentation](https://d1905gj4v53w41.cloudfront.net/docs)
- [Published data provenance](https://d1905gj4v53w41.cloudfront.net/api/iceberg/info)

## Current capabilities

| Area | Implemented in source |
| --- | --- |
| Analytics | Traffic overview, station profiles, hourly patterns, state aggregates, and year-over-year comparisons |
| Agent | Bedrock Converse tool-use loop with SSE progress; SQL, object, and proposal tools |
| Objects | Station and Corridor retrieval with metrics, coverage notes, and provenance |
| Evidence | Server-retained object results referenced by an evidence ID |
| Actions | Maintenance recommendations, anomaly flags, and report drafts; idempotent creation and atomic approval/rejection history |
| Interface | Plotly dashboard, Traffic Operations Agent chat, and Actions panel with Pending and Log tabs |
| Deployment | Separate GitHub Actions workflows for the S3 frontend and Docker/ECR/ECS API |

These are implementation capabilities, not a production certification. Verify deployed endpoints, tool selection, and approval behavior against the revision being demonstrated.

## Dataset and meaning

The bundled [Iceberg manifest](api/iceberg_manifest.json), published on 19 September 2026, records:

- **15,176,273 station-hour observations**.
- **12 monthly batches:** January–June 2025 and January–June 2026.
- Table: `bast.traffic`.
- Snapshot ID: `7174431960541360254` (stored as a string to preserve JavaScript precision).

Rows are observations, not individual vehicles. Summing counts across stations can count the same vehicle multiple times. Station availability varies by period.

Raw files use BASt's Bestandsbandformat. The parser reads station metadata, direction-specific counts, and coordinates, converting UTM32 to WGS84. BASt hour 1 represents 00:00–01:00 and hour 24 represents 23:00–00:00.

Object metrics currently use **H1 2026 and direction 1**:

- `traffic_volume_percentile` ranks observed volume; it does not measure congestion, speed, or delay.
- `traffic_exposure_heuristic` is an experimental traffic-based score, not validated maintenance urgency.
- Missing observations and unavailable vehicle classifications affect comparisons. A zero value does not always establish complete coverage.

Read each object's metric notes and coverage information. No external explanatory evidence sources are integrated; possible causes of traffic changes remain hypotheses unless supported by available evidence.

## Architecture

```text
BASt monthly files + metadata
          |
     parse_bast.py
          |
   local monthly Parquet
          |
    iceberg_ingest.py
          |
   Iceberg table on S3 ---- published manifest ---- Docker image
                                                   |
Browser -- CloudFront / ALB -- FastAPI on ECS Fargate
                                 |       |
                              DuckDB   Claude / Bedrock
                                 |       |
                           iceberg_scan  | tools
                                         +-- execute_sql
                                         +-- get_object --> retained evidence
                                         +-- create_action(evidence_id)
                                                 |
                                            SQLite actions
                                                 |
                                       review through API / UI
```

The deployed API reads the exact metadata location in `api/iceberg_manifest.json`, rather than globbing raw data files. Publishing a new table version requires updating that bundled manifest and deploying a new image. Retain referenced metadata and data files for snapshot replay.

The ingestion catalog is local SQLite at `data/iceberg_s3_catalog.db`. It coordinates ingestion, is not shipped to ECS, and is separate from the action database. ECS uses the manifest reference rather than a shared catalog service.

## Run locally

### Prerequisites

- Python 3.11 and an isolated Python environment.
- Extracted BASt files, including their metadata CSV, or prepared local Parquet files.
- AWS credentials and permission to invoke the configured Bedrock model only if using chat. Local Parquet analytics do not require AWS access.

The model/inference-profile identifier is defined by `_BEDROCK_MODEL` in [api/main.py](api/main.py). AWS region defaults to `eu-central-1`.

### 1. Install dependencies

From the repository root, using Git Bash on Windows or a Unix-compatible terminal:

```bash
python -m venv .venv
# macOS/Linux:
source .venv/bin/activate
# Windows Git Bash: use source .venv/Scripts/activate instead
python -m pip install -r requirements.txt
```

Use an approved, already-configured AWS credential source for Bedrock access. Do not put credentials in source files or frontend code.

### 2. Prepare local data

For January 2026, extract the station files and `_DZ_2026_01_Metadaten.csv` into `data/raw/DZ_2026_01_Rohdaten/`.

```bash
python scripts/parse_bast.py 2026_01
```

Output: `data/parquet/year=2026/month=01/traffic.parquet`. Repeat with other `YYYY_MM` values. Full dashboard comparisons need the corresponding months from both years; a single month supports only a limited local check.

### 3. Start the API

Leave `PARQUET_S3_PATH` unset for local Parquet mode. Ensure `data/` exists, including when starting with pre-generated data.

```bash
python -c "from pathlib import Path; Path('data').mkdir(exist_ok=True)"
python -m uvicorn api.main:app --reload --port 8000
```

Open [local API docs](http://localhost:8000/docs). Local evidence and actions persist in `data/bast_actions.db`.

**Local provenance limitation:** local mode reads a Parquet glob, not an Iceberg snapshot. The application also loads the bundled manifest when present, so a snapshot ID appearing on a local object is not proof that the local files match that snapshot. Use the Iceberg read path for snapshot-based evidence demonstrations.

### 4. Connect the frontend

In [frontend/index.html](frontend/index.html), change the existing `API_BASE` declaration for local development:

```javascript
const API_BASE = 'http://localhost:8000';
```

It defaults to the deployed CloudFront API. Serving the HTML locally alone does not switch the backend. Restore the deployed URL before publishing the frontend.

In a second terminal:

```bash
python -m http.server 3000 --directory frontend
```

Open [the local dashboard](http://localhost:3000/index.html). Keep the frontend's bundled `data/` assets available as well.

## Ingest and publish Iceberg data

This separate maintainer workflow writes to S3; it is not required for local Parquet development.

1. Configure an authorised AWS credential source and review the warehouse/region constants in [scripts/iceberg_ingest.py](scripts/iceberg_ingest.py). The defaults target this demo's account; use your own warehouse for an independent deployment.
2. Keep the existing ingestion catalog when extending the same table. A fresh local catalog does not automatically discover the existing S3 table.
3. Install ingestion dependencies, including SQLAlchemy for the SQLite catalog:

```bash
python -m pip install -r scripts/requirements-iceberg.txt sqlalchemy
python scripts/iceberg_ingest.py 2026_01
# Or ingest all locally available monthly Parquet files:
python scripts/iceberg_ingest.py all
```

The importer normalises the schema, partitions by month, and records source-month/checksum properties. A previously ingested month with the same checksum is skipped; a changed checksum is rejected unless explicitly replacing that month's data. The CLI offers `--replace` for corrections; validate its behavior on a separate test table before changing the shared dataset.

After ingestion:

- Inspect `data/iceberg_manifest.json`: verify the committed metadata location, snapshot ID, and record count.
- Copy it into `api/iceberg_manifest.json` as the explicit publication step. The importer does not update the API copy automatically.
- Confirm the ECS task role can list/read the required Iceberg paths, then rebuild and deploy the image.
- Verify `/api/iceberg/info`, an analytical query, and an object response against the intended version. `/health` alone does not validate table access.

`scripts/upload_s3.py` uploads raw Parquet to the legacy `traffic/` prefix; it does not commit an Iceberg snapshot. `create_iceberg.py` and `query_iceberg.py` are earlier experimental helpers. The latter still selects metadata by filename order and should not establish reproducible evidence.

## API and agent tools

Base URL: `https://d1905gj4v53w41.cloudfront.net` for the demo; `http://localhost:8000` for local development. Swagger at `/docs` describes parameters and defaults.

| Method | Route | Purpose |
| --- | --- | --- |
| GET | `/health` | Process health; does not validate table access |
| GET | `/api/stations` | Station metadata |
| GET | `/api/traffic/daily` | Daily counts; requires `station_id`, accepts `start` and `end` |
| GET | `/api/traffic/hourly` | Station hourly counts; requires `station_id`, accepts `date` |
| GET | `/api/traffic/overview` | Traffic overview and KPIs |
| GET | `/api/traffic/states` | State aggregates |
| GET | `/api/traffic/hourly-pattern` | Hourly pattern for `month=YYYY-MM` |
| GET | `/api/traffic/yoy` | Year-over-year comparison |
| GET | `/api/traffic/yoy/stations` | Station comparison; accepts `limit` |
| GET | `/api/objects/station/NW5048` | Station object and server-issued evidence reference |
| GET | `/api/objects/corridor/A1/NW` | Corridor object and server-issued evidence reference |
| GET | `/api/ask?q=...` | Agent response as an SSE stream |
| GET | `/api/actions` | Proposals; optional `status` and `type` filters |
| GET | `/api/actions/{id}` | Proposal details and status history |
| PATCH | `/api/actions/{id}` | Approve or reject a pending proposal |
| GET | `/api/iceberg/info` | Published manifest in S3 mode; local catalog information otherwise |
| GET | `/api/iceberg/snapshots` | Legacy local-catalog history; 404 when the catalog is absent, including normal ECS deployment |

Proposal creation uses the agent's `create_action` tool; there is no general REST POST endpoint for creation.

- `execute_sql(query)`: additional DuckDB analyses composed by the agent.
- `get_object(object_type, object_id)`: structured object with retained evidence and provenance.
- `create_action(idempotency_key, type, title, description, evidence_id)`: binds a proposal to stored evidence rather than accepting replacement evidence from the agent.

Example questions: “Tell me about station NW5048”; “What's the situation on the A3 in NW?”; “Draft a report proposal for station NW5048 using its available evidence.” Tool preference is prompted; verify actual selection rather than assuming it is guaranteed.

## Proposal and review contract

Types: `MAINTENANCE_RECOMMENDATION`, `ANOMALY_FLAG`, and `REPORT_DRAFT`.

- Same idempotency key and payload returns the existing proposal; a conflicting payload is rejected.
- Evidence carries the object result and its snapshot/metric provenance from retrieval time.
- Allowed transitions: `pending → approved` and `pending → rejected`. Both are terminal.
- Transitions and append-only history are written atomically. A conflicting second resolution receives HTTP 409.
- PATCH accepts JSON such as `{"status":"approved","resolved_by":"Operations Team"}`. The reviewer name is caller attribution, not authenticated identity.
- Approval records a decision; it does not schedule maintenance, send notifications, or execute an external action.

The Actions panel shows pending proposals and resolved history. It refreshes after a chat `done` event. The API and panel do not establish production authentication, a complete evidence-review experience, or continuous monitoring.

## Deployment and configuration

| Setting | Behavior |
| --- | --- |
| `PARQUET_S3_PATH` | A non-empty value selects S3 mode. Despite the legacy name, the table reference comes from the bundled manifest. Unset selects local Parquet. |
| `AWS_REGION` | Defaults to `eu-central-1` |
| `api/iceberg_manifest.json` | Required with a metadata location in S3 mode; missing configuration fails startup |
| `_BEDROCK_MODEL` in `api/main.py` | Bedrock inference-profile identifier; currently Claude Haiku 4.5 |
| `API_BASE` in `frontend/index.html` | Frontend API destination; defaults to the demo CloudFront URL |

The [Dockerfile](Dockerfile) pre-installs DuckDB's `httpfs` and `iceberg` extensions and bundles `api/`. ECS uses its task role for S3 and Bedrock. Ingestion dependencies and the local catalog are not part of the API image.

On pushes to `master`:

- [Frontend workflow](.github/workflows/deploy.yml): `frontend/**` changes upload HTML and data assets to S3.
- [API workflow](.github/workflows/docker-deploy.yml): `api/**`, `Dockerfile`, or `requirements.txt` changes build/push to ECR, update ECS, and wait for stability.
- Both support manual dispatch. README-only changes trigger neither deployment.

These workflows target existing AWS resources and configured CI credentials; they are not infrastructure provisioning templates. For a fork, adapt the bucket, registry, cluster, service, roles, and frontend URL.

For endpoint checks, prefer the browser over CloudFront HTTPS. Follow [CLAUDE.md](CLAUDE.md): do not use PowerShell's `Invoke-RestMethod` or `Invoke-WebRequest` for outbound network tests, and do not probe raw ALB IPs. Agents must also follow the workspace's per-command shell approval policy.

## Demo boundaries and production path

- **Ephemeral state:** ECS uses `/tmp/bast_actions.db`. Task replacement loses evidence/actions; multiple tasks do not share one queue. Local mode uses `data/bast_actions.db`.
- **Identity and isolation:** reviewer attribution is unverified, and the demo lacks production end-user authorization. Generated SQL needs enforced isolation and resource limits.
- **Evidence:** the action workflow retains object evidence, not a durable record of every SQL query or a resumable investigation. Evidence access needs session/tenant scoping and retention rules.
- **Operation:** investigations are user-triggered. No scheduled or ingestion-triggered monitoring, operational write-back, or proven production availability target is included.
- **Metrics:** traffic exposure is experimental. Coverage and source-format differences must remain visible in conclusions.

The production reference adds shared catalog access where needed, governed query execution, durable investigations, and RDS PostgreSQL for actions/evidence. External execution also needs authenticated approval, durable dispatch, downstream deduplication, and reconciliation. Choose distributed query engines or streaming only when measured requirements justify them. See [architecture.html](frontend/architecture.html).

## Repository map

```text
api/
  main.py                  Routes, Bedrock tools, runtime configuration
  ontology.py              Station/ Corridor definitions and metrics
  actions.py               SQLite evidence, proposals, status history
  iceberg_manifest.json    Published metadata reference for the image
scripts/
  parse_bast.py             Monthly BASt raw files → Parquet
  iceberg_ingest.py         Monthly Parquet → Iceberg + data manifest
  requirements-iceberg.txt  Additional ingestion dependencies
  upload_s3.py              Legacy raw-Parquet upload helper
  create_iceberg.py         Earlier Iceberg experiment
  query_iceberg.py          Earlier query helper; not manifest-pinned
frontend/
  index.html               Dashboard, agent chat, action queue
  management.html          General approach and business case
  architecture.html        Production reference and progression
  data/                    Bundled frontend assets
.github/workflows/         Frontend and API deployments
data/                      Local raw files, Parquet, catalogs, action state
Dockerfile                 API container with DuckDB extensions
requirements.txt           Application dependencies
```

## Data source

Traffic data: [Bundesanstalt für Straßenwesen (BASt), automatic traffic counting](https://www.bast.de/DE/Verkehrstechnik/Fachthemen/v2-verkehrszaehlung). Preserve source attribution and consult the terms accompanying downloaded datasets. This repository's computed metrics and AI-generated interpretations are not BASt assessments.
