# AI Cockpit "Digital Brain" — revised architecture and delivery plan

Date: 20 September 2026
Status: architecture review only; implementation and deployment are separate tasks.

## Objective and scope

Add a conversational analysis workspace at `frontend/cockpit.html`, served from the existing S3 bucket. A user asks a traffic question; the API queries the existing BASt data, produces an explanation, and streams validated chart instructions. Later phases add maps, real-time incident overlays and the existing proposal workflow.

Phase 1 uses the existing FastAPI/ECS service, Bedrock model, DuckDB and Iceberg data. It needs no new AWS service or persistent database. "No data duplication" means no second persisted traffic dataset; bounded query results still need transient storage during a request.

Keep `/api/ask` and the current dashboard contract compatible. Add `/api/cockpit/ask` for the new protocol. Reuse proven components without copying the entire existing agent loop into a second implementation.

## Review findings and decisions

| Original proposal | Finding | Revised decision |
| --- | --- | --- |
| DuckDB queries S3 Parquet | The deployed-mode source in `api/main.py` now uses `iceberg_scan()` with bundled manifest metadata. Local mode still uses a Parquet glob. | Reuse `parquet_source()`; attach the actual source mode and snapshot to results. |
| Add an in-memory action list and a new `/api/actions` | Those routes already exist. SQLite stores proposals, evidence and atomic status history. A replacement list would break that contract. | Reuse `get_object`, `create_action`, `api/actions.py` and existing routes. |
| Have the model supply chart data | This could fabricate values or chart only a preview. The existing SQL tool materializes a full DataFrame, then gives the model only its first 50 rows. | The server retains bounded results; the model supplies column mappings and a result ID. |
| WFS/WMS returns GeoJSON | WMS returns rendered map images (OGC WMS spec). WFS delivers vector features; output format depends on the service capabilities document. | Separate vector features (WFS → GeoJSON) from raster background layers (WMS/WMTS → tile descriptors). Verify WFS `GetCapabilities` response before integration. |
| NRW motorway network as first overlay | The inspected Straßen.NRW download excludes Autobahns. GDI NRW is a geographic reference catalog, not a real-time traffic feed. | Start geo integration with NRW administrative district boundaries (DVG service). Use Autobahn GmbH API for real-time road construction and incident data. |
| S3 JSON append for persistence | The user has chosen S3 JSON as the cockpit action log store. Native S3 append is limited to directory buckets; the general-purpose bucket uses a read–modify–write cycle with ETag-based optimistic concurrency. | Cockpit action log persists to `s3://bast-traffic-demo-112220711619/cockpit/actions.json` using GET → update array → conditional PUT (`If-Match: <etag>`). Concurrent write conflicts retry once. SQLite remains the store for the existing proposal/evidence workflow; the cockpit log is a separate, simpler record. |
| Reliability as a later concern | Recent failures exposed stuck loading states and interrupted streams. | Stream lifecycle, bounded work, visible errors and recovery are Phase 1 acceptance criteria. |

Current proposal/evidence persistence is SQLite at `/tmp/bast_actions.db` on ECS and `data/bast_actions.db` locally. ECS replacement loses proposals. The cockpit action log at S3 survives redeployment. Reconciling the two stores is a Phase 3 decision.

## Data source catalog

The agent receives only sources backed by implemented adapters. The catalog is a small Python dict (in `api/source_catalog.py`) with: source ID, description, date coverage, query adapter, update frequency, attribution and known limitations.

| Source ID | Description | Type | Update cadence | Phase |
| --- | --- | --- | --- | --- |
| `bast_traffic` | BASt H1 2025 + H1 2026 station-hour observations | Historical Parquet via Iceberg | Static (snapshot) | 1 |
| `nrw_districts` | NRW administrative district boundaries (DVG) | WFS vector → GeoJSON | Quarterly | 2 |
| `autobahn_roadworks` | Current road construction sites on German Autobahns | REST API (GeoJSON) | Near real-time | 2 |
| `autobahn_incidents` | Current closures and warning messages on German Autobahns | REST API (GeoJSON) | Near real-time | 2 |

**Real-time traffic data rationale:** The Autobahn GmbH API (`autobahn.api.bund.dev`) is a free, public, no-authentication REST API operated by the federal motorway authority. It returns road works, closures and hazard warnings as structured JSON with coordinate data. This creates the key demo narrative: correlate historical BASt measurements (heavy traffic on A1 NW, June 2026) with current construction sites on the same corridor. GDI NRW is the right source for geographic reference data (boundaries, cadastral geometry); the Autobahn API is the right source for live road events.

Additional sources to evaluate in Phase 3 and beyond:
- **MDM Portal** (mdm-portal.de): DATEX II feeds for regional roads; requires registration.
- **Straßen.NRW WFS**: Road network topology for state roads (Landesstraßen); excludes Autobahns; verify coverage before use.
- **OpenStreetMap Overpass API**: POIs and road attributes; good for enrichment; data freshness varies.
- **Historical weather (DWD open data)**: Useful only if observation dates align with BASt coverage; present-day conditions do not explain H1 patterns.

## Architecture and ownership

```text
cockpit.html
    | question
    v
/api/cockpit/ask
    |
    +-- Bedrock tool loop
    |      |
    |      +-- execute_sql --> bounded query runner --> existing Iceberg snapshot
    |      |                        |
    |      |                        +--> request-scoped result registry
    |      |
    |      +-- render_chart(result_id, columns) --> validator --> chart_spec
    |      |
    |      +-- Phase 2: fetch_geolayer(source_id, bbox) --> DVG WFS (districts)
    |      |                                            --> Autobahn API (roadworks/incidents)
    |      |
    |      +-- Phase 3: get_object / create_action --> existing SQLite action store
    |      |            write_cockpit_action --> S3 JSON action log
    |
    +-- SSE lifecycle: progress, results, artifacts, error, done
```

The browser owns presentation. The backend owns query results, chart validation, provenance and proposal creation. The model chooses analysis steps and presentation mappings; it does not define trusted data or executable frontend code.

Suggested implementation boundaries:

- `api/cockpit.py`: endpoint, tool orchestration and stream lifecycle.
- `api/cockpit_models.py`: validated result, chart and event schemas.
- `api/query_runner.py`: bounded analytical execution and result references.
- `api/source_catalog.py`: configured sources and provenance.
- `api/cockpit_actions.py`: S3 JSON action log (GET/conditional-PUT cycle).
- `api/geodata.py`, added in Phase 2: WFS/vector adapters and Autobahn API adapter.
- `frontend/cockpit.html`: simple workspace and Plotly/Leaflet renderer.

Wire these into the existing app. If shared infrastructure moves out of `main.py`, use a neutral module or explicit dependency injection to avoid circular imports.

## Tool and result contracts

### execute_sql

Expose a logical traffic relation backed by the existing source adapter. Return a server-issued `result_id`, column names/types, row count, a small preview and provenance to the model.

Initial proposed budget: at most 5,000 result rows per query, with an explicit overflow flag. Fetch a bounded result rather than constructing an unlimited DataFrame and slicing it afterward. A chart request against an overflowed result should require a more selective or aggregated query.

Enforce one permitted analytical statement and approved relations/functions. Reject writes, extension loading and arbitrary filesystem/network readers. A "SELECT only" string check is insufficient. Keep the existing query connection's S3 capabilities inside the trusted adapter.

Add query deadline, cancellation, memory and concurrency controls. Confirm their values against the actual ECS task resources; do not assume parallel analytical scans are affordable. Limit agent tool iterations and close connections in `finally` paths. Cancellation must stop underlying work, not merely stop waiting for it.

### render_chart

Model input example:

```json
{
  "result_id": "r1",
  "kind": "line",
  "title": "Hourly traffic at A1 stations in NRW",
  "x": "hour",
  "y": "avg_vehicles",
  "series": "station_id",
  "x_label": "Hour of day",
  "y_label": "Average vehicles/hour"
}
```

Validate the result ID within the current request, allowed chart kind, existing columns, value types and overall point count. Derive traces from retained query results. Do not accept model-authored JavaScript, arbitrary Plotly configuration or replacement data arrays.

Support line, scatter and bar initially. Translate line into Plotly scatter traces with line mode. Keep units, aggregation, direction, date range and missing values visible. Preserve zero values and gaps distinctly. Transport large identifiers, including Iceberg snapshot IDs, as strings.

### fetch_geolayer — Phase 2

Accept a configured `source_id` and bounded area, not an arbitrary URL. The adapter determines endpoint, layer, supported format and coordinate transformation.

**Vector features (WFS):** Call the service's `GetCapabilities` endpoint first; use the advertised feature type name and output format (`application/json` or `GML`). Apply feature-count and bounding-box limits. If the service pages results, accumulate all pages before emitting. Emit GeoJSON in WGS84 for the frontend. Do not present a partial layer as complete.

**Autobahn API:** Call `https://autobahn.api.bund.dev/v0/details/roads/{road_id}/roadworks` (and `/incidents`). No authentication required. Parse the JSON array; convert coordinate pairs to GeoJSON. Apply response-size and timeout limits. The road ID is resolved from the model's natural-language request (e.g. "A1") against a small validated list; do not allow arbitrary road IDs from model output.

**Raster/tile backgrounds (WMS/WMTS):** Emit a typed raster-layer descriptor, not GeoJSON. The frontend Leaflet adapter interprets the descriptor and fetches tiles directly. Proxying feature retrieval removes browser CORS dependency for WFS; it does not automatically apply to raster tiles.

### Actions — Phase 3

**Cockpit action log (S3 JSON):** `write_cockpit_action` fetches `cockpit/actions.json`, appends `{ts, text, source, result_id?}`, and does a conditional PUT with `If-Match` on the current ETag. One retry on conflict. The agent can record analytical conclusions; it cannot approve proposals or alter proposal status.

**Proposal workflow (SQLite):** Expose the existing `get_object` and `create_action` tools. Preserve evidence references, idempotency, proposal types and human approval/rejection. The agent cannot approve its own proposal.

Do not relabel every insight as an action. An ordinary analytical explanation belongs in the conversation. If insight journaling becomes a requirement, design it explicitly rather than changing the proposal schema to `{ts, text}`.

## SSE and request lifecycle

Retain existing event names where compatible. Add a version and request identifier to cockpit events. Proposed additions:

- `query_result`: bounded data plus columns, result ID, source mode and provenance.
- `chart_spec`: validated chart metadata referencing an already streamed result.
- `geo_layer`: validated vector features or a typed raster descriptor, in Phase 2.
- `action_created`: existing proposal ID and status, in Phase 3.
- `error`: stable code, understandable message and recoverability.
- `done`: terminal status: success, error or cancelled.

Use a streaming transport adapter that can emit heartbeat comments while Bedrock or SQL work is running. Test through the real CloudFront/ALB path; heartbeat intervals and maximum request duration must respect that path's actual settings. Catch errors both when creating and while consuming the Bedrock stream.

The client must handle split UTF-8 characters, partial frames, line endings and connection closure without a terminal event. Preserve text and completed artifacts when a stream breaks. Provide Stop and explicit retry controls; do not automatically replay an agent request that might create proposals.

GET with a query parameter can preserve the initial transport pattern, but questions appear in URLs. Multi-turn state should use a request body rather than a growing URL; changing to POST requires testing CORS and CloudFront behavior.

## Delivery phases and acceptance gates

### Phase 1 — dependable questions, results and charts

Deliver the separate endpoint, bounded SQL tool, result registry, chart validation and simple cockpit. Add dashboard navigation while preserving existing behavior. Start with a small source description and suggested questions. Do not require the full catalog UI, maps or action writes.

Example milestone: "Show average hourly traffic for A1 stations in NRW for June 2026, direction 1."

Acceptance:

1. The agent filters and aggregates the real dataset; every plotted value matches the referenced query result.
2. A line chart and supporting table show the selected period, direction and units.
3. Line, scatter and bar specs render; invalid columns/specs produce useful errors.
4. Overflow, empty results and missing vehicle classifications are explicit (2026/01 lacks sv_r1/pkw_r1).
5. SQL failure, HTTP failure, interrupted stream and cancellation exit loading states without hanging.
6. Existing dashboard plots and `/api/ask` pass regression checks after the new endpoint is deployed.
7. A cold API run and a repeated run are tested through CloudFront; cached responses are not accepted as proof of database health.
8. The frontend deployment script validates inline JavaScript syntax before uploading `cockpit.html`.

### Phase 2 — districts, real-time incidents and station maps

**Milestone A — geographic reference:** "Show NRW stations by heavy-vehicle share for June 2026, overlaid on district boundaries."

Use NRW administrative district boundaries from the DVG WFS service as the first external vector source. Verify exact feature type name, output format, coordinate system and attribution from the `GetCapabilities` response during the integration spike. Use polygon containment for district assignment; do not guess from station names. Resolve geographic names before analysis. Show coverage explicitly (stations with no district match, stations missing sv_r1).

**Milestone B — real-time overlay:** "Show current road construction sites on the A1 and compare with historical heavy-vehicle traffic at nearby BASt stations."

Integrate the Autobahn GmbH API for roadworks and incidents on a specified road (e.g. A1, A3). Render a Leaflet map with three layers: BASt station bubbles (from query result), district boundaries (WFS), construction site markers (Autobahn API). The agent selects the road from the user's question against the validated road list.

Acceptance:

1. Coordinate alignment: station points fall inside correct district polygons.
2. Multi-geometry: district layer renders correctly for polygons and multipolygons.
3. Missing coordinates: stations without lat/lon are excluded with a count shown.
4. Provider timeout: WFS or Autobahn API timeout produces a visible partial-result warning, not a crash.
5. Layer limits: feature-count cap is enforced; partial layers are labeled.
6. Station metrics in the map popup agree with values in the chart and table for the same query result.
7. Autobahn API returns empty array for a road with no current incidents; map renders without error.
8. Advanced mode exposes tool activity, rendered artifacts and a data table drawn from the same result IDs. On narrow screens, use tabs or stacked panels.

A motorway geometry overlay from Straßen.NRW requires a separately verified source; the inspected package excludes Autobahns and must not be represented as providing motorway geometry.

### Phase 3 — shared proposals and S3 action log

Connect the cockpit to the existing SQLite proposal workflow (`/api/actions`). Both pages show the same records and status history. Poll only while the relevant panel is visible; refresh after an action event.

Deploy the S3 JSON cockpit action log (`cockpit/actions.json`) and `write_cockpit_action` tool. The log is separate from SQLite proposals: it records analytical conclusions and is readable across ECS task replacements.

Acceptance:

1. One explicit proposal request creates one evidence-linked pending action; both pages show the same proposal with the same status history.
2. Repeat/idempotency behavior is correct.
3. Human approval/rejection in `index.html` is reflected in the cockpit panel after the next poll.
4. `write_cockpit_action` appends to S3 and survives an ECS task replacement (verified by restarting the task and confirming previous entries are still present).
5. Conditional-PUT conflict on concurrent write retries once and either succeeds or emits a clear error.
6. Second external source chosen around a specific question with verified date coverage matching BASt observations.

### Phase 4 — sessions and reusable workspaces

Add multi-turn sessions with explicit isolation, expiry and storage semantics. Saved workspaces should carry chart configuration and reproducible query/source references; a URL hash alone cannot restore expired request-scoped result IDs.

Add chart export and later source registration. New source endpoints require validation and a supported adapter before the agent can use them. Do not let chat text directly extend backend network access.

## Resolved choices and remaining decisions

Resolved:

- First geo layer: NRW administrative districts (DVG WFS).
- Real-time traffic data: Autobahn GmbH API (roadworks + incidents), no authentication required.
- Action persistence: S3 JSON for cockpit log; SQLite retained for existing proposal/evidence workflow.
- Phase 1 action writing: out of scope.
- Phase 1 interface: simple mode with an optional result table.
- Existing model: retain initially and evaluate chart/tool correctness before changing.
- OGC separation: WFS delivers vector features; WMS/WMTS delivers raster tiles. These use separate adapters and SSE event types.

Remaining decisions:

- Durable reconciliation of cockpit S3 log and SQLite proposals (Phase 3).
- Second external source with verified date coverage.
- Verified motorway geometry provider (Autobahn geometries are not in the Straßen.NRW DVG package).
- ECS concurrency and query resource budgets (requires inspection of deployed task metrics).
- CORS and CloudFront behavior for any future POST endpoints.

This document does not modify application code or publish anything. Existing uncommitted dashboard recovery work remains separate.

## Sources and verification

Repository review: `api/main.py`, `api/actions.py`, `README.md`; existing action contracts and query behavior inspected on 20 September 2026.

- [NRW administrative boundaries and DVG service](https://www.bezreg-koeln.nrw.de/geobasis-nrw/produkte-und-dienste/verwaltungskarten-und-grenzen/digitale-verwaltungsgrenzen): official WFS source for district boundaries.
- [Autobahn GmbH open API](https://autobahn.api.bund.dev): free public REST API for road works, closures and warning messages on German Autobahns; no authentication required.
- [GDI NRW geo catalog](https://www.gdi.nrw/komponenten/geokatalognrw-eine-komponente-der-gdi-nrw): NRW geographic reference data catalog; primarily static reference layers, not real-time traffic feeds.
- [Straßen.NRW dataset documentation](https://www.opengeodata.nrw.de/produkte/transport_verkehr/strassennetz/datenbeschreibung_strassennetz.pdf): the inspected package excludes Autobahns.
- [OGC WMS specification](https://www.ogc.org/standards/wms/): WMS supplies georeferenced map images (raster); WFS supplies vector features. These are separate protocols requiring separate adapters.
- [AWS S3 append documentation](https://docs.aws.amazon.com/AmazonS3/latest/userguide/directory-buckets-objects-append.html): native append is limited to S3 Express One Zone objects in directory buckets. The cockpit action log uses conditional PUT with ETag-based optimistic concurrency on a standard bucket object.
- [MDM Portal](https://www.mdm-portal.de): DATEX II feeds for regional and Autobahn roads; requires registration; evaluate for Phase 3+ if Autobahn API coverage is insufficient.
