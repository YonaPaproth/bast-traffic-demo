# BASt Traffic Demo Pipeline

Demo-Pipeline für Verkehrsdaten der [Bundesanstalt für Straßenwesen (BASt)](https://www.bast.de) — Dauerzählstellen Januar 2026.

## Architektur

```
┌─────────────────────────────────────────────────────────────────┐
│                        DATA PIPELINE                            │
│                                                                 │
│  BASt Open Data          Parser             Parquet             │
│  (2.361 Stationen)  ──► parse_bast.py ──► data/parquet/        │
│  data/raw/*.261           (Python)          year=2026/          │
│                                              month=01/          │
│                                               traffic.parquet   │
└──────────────────────────────┬──────────────────────────────────┘
                               │
                    ┌──────────▼──────────┐
                    │   upload_s3.py       │
                    │   (AWS CLI --profile │
                    │    demo)             │
                    └──────────┬──────────┘
                               │
                    s3://bast-traffic-demo-112220711619/traffic/
                               │
         ┌─────────────────────┼──────────────────────┐
         │                     │                       │
┌────────▼─────────┐  ┌────────▼────────┐   ┌─────────▼────────┐
│  FastAPI Backend │  │   DuckDB        │   │ React Dashboard  │
│  api/main.py     │◄─┤ (in-process,    │   │ frontend/        │
│  :8000           │  │  local Parquet) │   │ index.html       │
└──────────────────┘  └─────────────────┘   └──────────────────┘
```

## Voraussetzungen

- Python 3.11+
- AWS CLI (`aws`) mit konfiguriertem Profil `demo`
- Rohdaten in `data/raw/DZ_2026_01_Rohdaten/`

## Setup & Ausführung

```bash
# 1. Virtuelle Umgebung erstellen
python3.11 -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate

# 2. Abhängigkeiten installieren
pip install -r requirements.txt

# 3. Daten parsen (ca. 2–5 Minuten)
python scripts/parse_bast.py

# 4. Parquet-Dateien zu S3 hochladen
python scripts/upload_s3.py

# 5. API starten
uvicorn api.main:app --reload --port 8000

# 6. Frontend öffnen
# Option A: Direkt im Browser öffnen
open frontend/index.html

# Option B: Kleiner HTTP-Server (empfohlen für CORS)
python -m http.server 3000 --directory frontend
# → http://localhost:3000
```

## API-Endpunkte

| Methode | Pfad | Beschreibung |
|---------|------|--------------|
| `GET` | `/health` | Health-Check |
| `GET` | `/api/stations` | Alle Zählstellen mit Metadaten |
| `GET` | `/api/traffic/daily?station_id=XX&start=2026-01-01&end=2026-01-31` | Täglicher Verkehr pro Station |
| `GET` | `/api/traffic/hourly?station_id=XX&date=2026-01-15` | Stündlicher Verkehr (Station + Tag) |
| `GET` | `/api/traffic/overview` | Tagesübersicht + Top 10 + KPIs |
| `GET` | `/api/traffic/states` | Verkehr nach Bundesland |

### Beispiele

```bash
# Alle Stationen
curl http://localhost:8000/api/stations | python -m json.tool | head -50

# Gesamtübersicht
curl http://localhost:8000/api/traffic/overview | python -m json.tool

# Täglicher Verkehr für eine Station
curl "http://localhost:8000/api/traffic/daily?station_id=BB3592&start=2026-01-01&end=2026-01-31"

# Stundenprofil
curl "http://localhost:8000/api/traffic/hourly?station_id=BB3592&date=2026-01-15"

# Bundesland-Aufschlüsselung
curl http://localhost:8000/api/traffic/states
```

## Dashboard-Features

Das React-Dashboard (`frontend/index.html`) bietet:

- **KPI-Karten**: Gesamtfahrzeuge, Ø Tagesverkehr, Anzahl Stationen
- **Zeitreihe**: Täglicher Gesamtverkehr aller Stationen (Plotly)
- **Top-10-Balkendiagramm**: Verkehrsreichste Zählstellen
- **Kreisdiagramm**: Verkehr nach Bundesland
- **Stationsselektor**: Stundenprofil für beliebige Zählstelle

## Projektstruktur

```
bast-traffic-demo/
├── data/
│   ├── raw/
│   │   └── DZ_2026_01_Rohdaten/     # BASt-Rohdaten (2.361 Stationen)
│   └── parquet/
│       └── year=2026/month=01/
│           └── traffic.parquet      # Geparstes Ausgabeformat
├── scripts/
│   ├── parse_bast.py                # Datenparser
│   └── upload_s3.py                 # S3-Upload
├── api/
│   └── main.py                      # FastAPI-Backend
├── frontend/
│   └── index.html                   # React-Dashboard (CDN-only)
├── requirements.txt
└── README.md
```

## Datenformat

Die BASt-Rohdaten liegen im **Bestandsbandformat** vor:

```
H36413592 12 A 2     Netzen       V2.0;   ← Stationskopf
R03 03 Berlin (A10)  O Magdeburg  W;      ← Strecken-/Richtungsinfo
S02 09 KFZ SV Mot Pkw ...;               ← Fahrzeugkategorien
260101 01:00  55-  18-  30- ...           ← Messdaten (YYMMDD HH:MM Werte-)
```

- **Extraktion**: Datum, Stunde, KFZ_R1, KFZ_R2 (erste zwei Zählwerte)
- **Encoding**: latin-1 / cp1252
- **Koordinaten**: UTM32 → WGS84 (via pyproj)

## Datenquelle

© Bundesanstalt für Straßenwesen (BASt)  
[https://www.bast.de/DE/Verkehrstechnik/Fachthemen/v2-verkehrszaehlung](https://www.bast.de/DE/Verkehrstechnik/Fachthemen/v2-verkehrszaehlung)  
Open Data — freie Nutzung mit Quellenangabe.
