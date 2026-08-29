# BASt Traffic Demo — Backlog

## 🚀 Production-Grade Iceberg Setup

### Was fehlt (Demo → Prod)

| Komponente | Demo (aktuell) | Produktion |
|---|---|---|
| Iceberg Katalog | SQLite lokal | AWS Glue Data Catalog oder Project Nessie |
| Auth | Static AWS Key | IAM Roles + Lake Formation |
| Ingest | Manuelles Script | Airflow / Step Functions / EventBridge |
| Datenqualität | Keine Checks | Great Expectations / dbt |
| Schema Evolution | Manuell | Iceberg-native (automatisch) |
| Compute | DuckDB lokal | Athena Serverless / EMR |
| Monitoring | Keins | CloudWatch + Alerting |
| Multi-User / RBAC | Nein | S3 ACLs + Lake Formation |

### Konkrete Schritte
1. **Glue Catalog** statt SQLite — 1-2 Tage, ~1$/Monat
2. **Athena** als serverlose Query Engine direkt auf S3/Iceberg
3. **Airflow oder Step Functions** für täglichen SAP→S3→Iceberg Ingest
4. **Lake Formation** für Row-Level Security und Data Catalog
5. **Great Expectations** für Datenqualitätschecks vor Iceberg-Commit

---

## 🎙️ Claude Voice Interface — „Ask the Data"

### Idee
Natürlichsprachliches Query-Interface als Alternative zu Power BI Self-Service.
Besser für: Ad-hoc-Fragen, Mobile, nicht-technische User, Handlungsempfehlungen.

### Beispiel-Interaction
```
User: "Welche Autobahnabschnitte hatten letzten Monat 
       überdurchschnittlichen Verkehr an Sonntagabenden?"

Claude: [ruft Athena/DuckDB an] → [gibt strukturierte Antwort]
       "Die A3 bei Köln, A8 München-Ost und A2 Hannover 
        hatten 40% mehr Verkehr als der Monatsdurchschnitt.
        Soll ich eine Heatmap dafür erstellen?"
```

### Stack
```
Voice Input (Whisper API)
       ↓
Claude (Tool Use: text-to-SQL)
       ↓
DuckDB / Athena → Iceberg auf S3
       ↓
Claude formuliert Antwort + Plotly Chart-Spec
       ↓
Voice Output (ElevenLabs / Polly) + Visual im Browser
```

### PoC-Plan (~3 Tage)
1. FastAPI Endpoint `POST /ask` — nimmt Freitext / Audio
2. Claude mit Tool `run_sql(query: str)` → DuckDB (Demo) / Athena (Prod)
3. Claude formuliert Antwort + optionale Chart-Spec (JSON)
4. Frontend: Chat-UI + Mikrofon-Button + Plotly-Render

### Warum besser als Power BI Self-Service
- Kein Drill-Down nötig — Antwort direkt in Sprache
- Mobile-first — natürlicher als Touch auf Pivot-Tabellen
- Kontext zwischen Fragen (Conversation Memory)
- Handlungsempfehlung statt nur Rohdaten
- Kein Lizenzmodell (vs. PBI Premium)

### Nächster konkreter Schritt
Demo-Page `frontend/chat.html` bauen:
- Text-Input gegen BASt-Daten per DuckDB
- Claude text-to-SQL mit Tool Use
- Gleiche Logik wie Athena-Prod, aber lokal
- Als 4. Tab in der Nav neben Dashboard / Architektur

---

## 🗺️ Weitere offene Punkte

- [ ] Mehr Monate laden (Feb–Jun 2026 BASt-Daten)
- [ ] Custom Domain statt S3-URL
- [ ] Git Repo anlegen und pushen (github.com)
- [ ] Iceberg Time Travel Demo (Snapshot-Vergleich in Frontend)
- [ ] Glue Catalog statt SQLite Katalog
