# Plan: Ontology + Action Layer
## BASt Traffic Demo — Agentic Reinvention Extension

**Date:** 2026-09-19  
**Author:** Yona Paproth, Accenture  
**Status:** Draft — for internal review

---

## Background

The BASt Traffic Demo currently runs a live Claude AI agent doing text-to-SQL against 3.58B vehicle records on S3. The agent answers questions. This plan extends it to **propose and log actions** — turning a read-only query tool into a read-write operational agent with a formal action layer.

The motivation: the Palantir Foundry + Databricks partnership (announced 2025) positions Foundry's Ontology System as the application layer above any data platform, including Databricks. The proposition here is that **a conversational AI agent running on an open-source stack can replicate that value without the GUI, the ontology platform license, or the Palantir contract** — and in some ways surpasses it because the interface is natural language rather than low-code Workshop apps.

---

## What Palantir's Ontology Gives You (and the Gap Today)

| Foundry capability | Current demo | After this plan |
|---|---|---|
| Named business objects | SQL rows | Python dataclasses: Station, Corridor, MaintenanceWindow |
| Computed properties on objects | Recomputed per query | Defined once in `ontology.py`, reused by agent |
| Action types with write-back | None — read only | 3 action types: Maintenance, Anomaly Flag, Report Draft |
| Human approval workflow | None | Pending → Approved/Rejected in frontend |
| Audit trail | None | SQLite log, timestamped, attributed |
| Proactive monitoring | None | On-demand anomaly scan → auto-creates actions |

**What we are not building:** a GUI application builder (Foundry Workshop), a full knowledge graph (Stardog/RDF), or a managed ontology server. The agent is the application layer. Conversation replaces the GUI.

---

## Architecture Change

```
TODAY:
  User asks → Claude writes SQL → DuckDB → answer (read-only)

AFTER:
  User asks → Claude understands OBJECTS → queries + reasons
            → proposes ACTIONS → human approves → AUDIT LOG
```

**New files (no new AWS services):**

```
api/
  ontology.py     ← object definitions (Station, Corridor, MaintenanceWindow)
  actions.py      ← action types, SQLite store, CRUD logic
  monitor.py      ← proactive anomaly scan (Phase 4)
```

Actions are stored in SQLite inside the ECS container — ephemeral per deploy, intentional for demo (always starts fresh). No DynamoDB, no RDS, no additional AWS cost.

---

## Phase 1 — Ontology as Code + Enriched Agent
**Effort: 3–4 days**

### What we build

**`api/ontology.py`** — three Python dataclasses representing the domain model:

- **`Station`** — `id, name, state, road_class, road_number, lat, lon`  
  + computed: `avg_daily_kfz`, `congestion_percentile`, `maintenance_priority_score`

- **`Corridor`** — a road segment (all stations sharing `road_number + state`, e.g. A9 Bavaria)  
  + computed: `total_kfz`, `peak_hour`, `anomaly_count_last_30d`

- **`MaintenanceWindow`** — agent-proposed, human-approved  
  + `station_id, proposed_date_range, reason, estimated_diversion_impact`

**New agent tool: `get_object(type, id)`**

Added alongside the existing `execute_sql` tool. Returns a hydrated ontology object — properties pulled from DuckDB, formatted as a structured dict. The agent chooses this tool when the user asks about a specific entity rather than an aggregate query.

**Enriched system prompt**

The agent's system prompt is extended with the object schema: "You understand these business objects: Station, Corridor, MaintenanceWindow. Prefer `get_object` before writing SQL when the user asks about a specific entity by name or ID."

### Demo moment

> "Tell me about station NW5048"

Agent returns the Station as a structured object: name, state, road class, current congestion percentile vs. all stations, maintenance priority score. Not a raw SQL result — a named business object the agent can reason about in subsequent turns.

---

## Phase 2 — Action Layer (Backend)
**Effort: 3–4 days**

### What we build

**`api/actions.py`** — three Action Types:

| Action Type | When triggered | What it records |
|---|---|---|
| `MAINTENANCE_RECOMMENDATION` | Agent, on user request | Proposed maintenance window with station, date range, justification |
| `ANOMALY_FLAG` | Agent or monitor scan | Unusual traffic pattern with station, description, severity |
| `REPORT_DRAFT` | Agent, on user request | Draft report text for a regulator or internal submission |

**SQLite schema:**
```sql
actions(
  id TEXT PRIMARY KEY,
  type TEXT,
  status TEXT,          -- pending / approved / rejected
  title TEXT,
  description TEXT,
  object_id TEXT,
  created_at TIMESTAMP,
  resolved_at TIMESTAMP,
  resolved_by TEXT,
  data_json TEXT
)
```

**New API endpoints:**

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/actions` | List all actions (filterable: `?status=pending&type=MAINTENANCE_RECOMMENDATION`) |
| `POST` | `/api/actions` | Create action (called by agent tool, not directly by user) |
| `PATCH` | `/api/actions/{id}` | Approve or reject (`{"status":"approved","resolved_by":"Operations Team"}`) |
| `GET` | `/api/actions/{id}` | Single action detail |

**New agent tool: `create_action(type, object_id, title, description, data)`**

The agent calls this when it has gathered enough evidence to make a recommendation. The tool call itself is the proposal — it will not create an action without SQL-backed justification in the same reasoning chain.

### Demo moment

> "Should we schedule maintenance on the A3 near Frankfurt this month?"

Agent queries hourly profiles for the A3 Frankfurt corridor, identifies the lowest-traffic window, proposes `MAINTENANCE_RECOMMENDATION` via `create_action`. Action appears immediately in the pending queue in the frontend.

---

## Phase 3 — Frontend: Actions Panel
**Effort: 2–3 days**

### What we build

A new collapsible panel in `index.html` alongside the existing chat interface.

**Pending tab:**
- One card per pending action: type badge, title, agent's reasoning, object reference
- `Approve` and `Reject` buttons → call `PATCH /api/actions/{id}`
- On action: card moves to the log, timestamp recorded

**Log tab:**
- All resolved actions (approved + rejected), reverse chronological
- Columns: type, title, object, proposed by agent at (time), resolved by (name) at (time), outcome

### Demo moment

The full loop is visible in a single screen: agent proposes on the left (chat), action appears in the panel on the right, human approves with one click, audit log builds up.

**This is the visual equivalent of Foundry's Action Types panel — without the Workshop license.**

---

## Phase 4 — Proactive Monitoring (Optional for first demo)
**Effort: 2 days**

### What we build

**`api/monitor.py`** — on-demand anomaly scan:

- Computes Z-score for each station's last 7 days vs. its 6-month hourly baseline
- Flags stations where Z > 2.5 (statistically unusual volume)
- Returns top 5 anomalies sorted by severity
- Auto-creates `ANOMALY_FLAG` actions in the pending queue

**New endpoint:** `GET /api/monitor` — triggered by a button in the frontend, not a scheduler (simpler, more controllable in a demo context).

### Demo moment

Click "Run monitoring scan" — three `ANOMALY_FLAG` actions appear in the pending queue without anyone asking the agent a question.

> "The agent noticed this. You didn't have to ask."

This is Phase 2 of the Reinvention Arc: **agent monitors**, not just answers.

---

## Full Demo Script

1. **Objects** — click a station on the map → side panel shows it as a Station object with congestion percentile and maintenance priority score. *"This is our ontology. Not a row in a table — a business object with computed properties."*

2. **Agent understands objects** — type "Which corridors on the A9 have the highest congestion this month?" → agent uses `get_object` + `execute_sql`, answers in terms of Corridor objects, not raw numbers. *"The agent understands the domain model."*

3. **Action proposed** — type "Recommend a maintenance window for the A3 near Frankfurt" → agent queries, reasons, proposes `MAINTENANCE_RECOMMENDATION` → action appears in pending queue. *"That's the action layer. Same concept as Palantir Action Types — no GUI, no platform license."*

4. **Human approves** — click Approve → moves to log with timestamp. *"Full audit trail. Humans decide; agents execute."*

5. **Proactive** (Phase 4) — click "Run monitoring scan" → anomaly flags surface unprompted. *"This is Phase 2: the agent monitors, not just answers."*

6. **Punchline** — *"Palantir built the best version of this for the pre-LLM era. We just showed you the post-LLM version — at 15× lower infrastructure cost, with conversation as the application layer."*

---

## Build Sequence

| Week | Phase | Deliverable |
|---|---|---|
| Week 1 | Phase 1 | `ontology.py` + `get_object` tool + enriched system prompt |
| Week 1–2 | Phase 2 | `actions.py` + 4 new API endpoints + `create_action` tool |
| Week 2 | Phase 3 | Actions panel in `index.html` (pending + log tabs) |
| Week 3 | Phase 4 | `monitor.py` + anomaly scan endpoint + frontend button |

**Prerequisite:** `parse_bast.py` complete and 2025 + re-parsed 2026 Parquet uploaded to S3. The ontology computed properties (congestion percentile, maintenance priority score) require the full H1 2025 + H1 2026 dataset to be meaningful.

---

## What This Is Not

- **Not a knowledge graph.** No RDF, no SPARQL, no Stardog. The domain is time-series traffic analytics — homogeneous columnar data, not a heterogeneous enterprise entity graph. A triple store would add operational complexity without adding value here.

- **Not trying to replicate Foundry Workshop.** We are not building a no-code GUI application builder. The thesis is that **conversation replaces the GUI** — ops teams interact through chat, not Workshop apps.

- **Not production-grade yet.** SQLite is not multi-user. The action store resets on container redeploy. These are the right tradeoffs for a PoC that demonstrates the pattern. Phase 4 of the Reinvention Arc (scale) addresses these.

---

## Connection to the Holy Trinity Pitch

This build demonstrates all three pillars in a single demo:

- **Pillar 1 (Open Source Foundation):** The same S3 + Iceberg + DuckDB stack, now with an ontology layer on top.
- **Pillar 2 (AI Agent Layer):** Agent moves from answering to proposing and acting — with audit trail.
- **Pillar 3 (Agentic Process Reinvention):** The demo is the proof point. Forward-deployed engineers translate domain knowledge into object definitions and action types. This is what scales to production.

> The moat is not the platform. The moat is knowing what actions to define, what objects matter to this client, and how to embed the agent into their operations.

---

*BASt Traffic Demo · Accenture · September 2026*
