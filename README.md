# dsl_monitor

Ein kleines DSL/Verbindungs-Monitoring.

- `run.py` startet alles: Fritz-Status-Bridge (`fritz_status_service.py`), Probe (`probe.py`), Web UI (`web.py`).
- Konfiguration über `.env` (niemals committen).
- Logs werden als CSV geschrieben (`dsl_log.csv`).

## UI / Plots

Die Web-UI zeigt zwei Plotly-Grafiken:

1) **Ping-Latenz (oben)**
   - Zeitreihe der Ping-Latenz (Buckets)
   - Marker-Farben: **grün** = Ping OK, **rot** = Ping-Event/Outage (Bucket mit `dsl_event_active`)

2) **Status-Boxen (unten, zweite Zeile)**
   - Quadrate pro Zeit-Bucket als kompakte Statusanzeige
   - Farben:
     - **rot** = outage (Ping-Event im Bucket)
     - **blau** = DSL
     - **gelb** = mobile
   - Wenn in einem Bucket kein Fritz-Status geloggt ist, wird **DSL angenommen** (damit „OK-Phasen“ sichtbar als DSL erscheinen).

## Was ist ein „DSL-Event“?

Ein DSL-Event ist **der Zeitraum**, in dem wir aktiv Fritz-Synchron-/Verbindungstyp beobachten und in der UI „rot“ markieren.

**Trigger (Event-Start):**
- **Ping-Failures:** Ping läuft alle `DSL_MONITOR_PING_INTERVAL_SECONDS` (Default: 15s). Wenn `DSL_MONITOR_FAILURE_THRESHOLD` (Default: 3) Pings hintereinander fehlschlagen, startet ein Event.
- **HTTP-Timeout:** Zusätzlich lädt die Probe alle `DSL_MONITOR_HTTP_PROBE_INTERVAL_SECONDS` (Default: 300s) eine konfigurierbare URL (`DSL_MONITOR_HTTP_PROBE_URL`). Wenn diese Anfrage in ein Timeout läuft (`DSL_MONITOR_HTTP_PROBE_TIMEOUT_SECONDS`), startet ebenfalls ein DSL-Event.

**Während DSL-Event:**
- Fritz-Verbindungstyp wird **nur während eines Events** abgefragt und **max. 1× pro Minute** (rate-limited über `DSL_MONITOR_CONN_TYPE_POLL_INTERVAL_SECONDS`).
- Wenn Fritz `connection_type == mobile`, wird die „mobile duration“ hochgezählt.
- Die UI färbt Buckets **rot**, wenn `dsl_event_active` gesetzt ist (Ping-Event: 3× Ping hintereinander ohne Antwort; ggf. auch HTTP-Timeout).

**Event-Ende:**
- Sobald Fritz wieder `connection_type == dsl` meldet → Ende (`dsl_event_end_reason=recovered_to_dsl`).
- Oder nach `DSL_MONITOR_DSL_EVENT_MAX_SECONDS` (Default 45min) → Ende (`dsl_event_end_reason=max_duration`).

## Retention (Anzeige vs. CSV)

- `DSL_MONITOR_RETENTION_DAYS` wirkt **nur auf die Anzeige** in der Web-UI (Filter beim Lesen/Aggregieren).
- Die CSV (`dsl_log.csv`) bleibt **standardmäßig vollständig** (alle Messwerte, kein automatisches Löschen).
- Optional kannst du explizites Pruning einschalten:
  - `DSL_MONITOR_CSV_RETENTION_DAYS=<tage>`
  - Default ist `0` (= unendlich / kein Pruning).

## CSV Schema (saubere Version)

`dsl_log.csv` enthält exakt diese Spalten (keine Backward-Compat):

- `timestamp`: UTC ISO Zeitstempel
- `ping_target`: Host/IP für den Primär-Ping
- `ping_ok`: `1`/`0`
- `latency_ms`: Ping-Latenz (nur bei ok)
- `consecutive_failures`: Zähler der Ping-Fails
- `dsl_event_active`: `1`/`0`
- `dsl_event_trigger`: `ping_failures` oder `http_timeout`
- `dsl_event_duration_seconds`: Laufzeit des aktuellen Events
- `dsl_event_end_reason`: `recovered_to_dsl` oder `max_duration` (leer während aktiv)
- `connection_type`: `dsl`/`mobile`/`unknown` (nur relevant während Event)
- `mobile_duration_seconds`: Dauer in `mobile` (während Event)
- `http_probe_ok`: `1`/`0`/leer (noch nie geprüft)
- `http_probe_error`: z.B. `timeout` oder `HTTP 500`

## Quickstart

1) `.env` anlegen/anpassen (Beispiel steht im Repo: `.env.example`).

2) Install + systemd Setup:

```bash
./install.sh
```

Danach:
- UI: `http://<DSL_MONITOR_WEB_HOST>:<DSL_MONITOR_WEB_PORT>` (typisch `http://127.0.0.1:9076`)

## Debug

```bash
sudo journalctl -u dsl-monitor.service -f

# Fritz TR-064 Bridge direkt (sollte am zuverlässigsten sein)
curl -sS http://127.0.0.1:9077/status | jq

# Web-Backend Proxy (das ist das, was die UI im Browser nutzt)
curl -sS http://127.0.0.1:9076/api/fritz_status | jq

# UI-Daten (Buckets + aktuelles dsl_event_active)
curl -sS http://127.0.0.1:9076/api/data | jq

# Manuelles Ad-hoc Check (Fritz + HTTP Probe)
curl -sS -X POST http://127.0.0.1:9076/api/check_dsl_now | jq

# HTTP Probe Status (live, cached)
curl -sS http://127.0.0.1:9076/api/http_probe_status | jq
```

## Wichtige .env Keys

Siehe `.env.example` (dort sind alle Keys inkl. Bedeutung dokumentiert). Wichtig sind i.d.R.:

- Fritz: `FRITZ_HOST`, `FRITZ_USER`, `FRITZ_PASSWORD`, `FRITZ_STATUS_PORT`, `DSL_CONN_STATUS_URL`
- Probe/Event: `DSL_MONITOR_PING_INTERVAL_SECONDS`, `DSL_MONITOR_FAILURE_THRESHOLD`, `DSL_MONITOR_DSL_EVENT_MAX_SECONDS`
- HTTP-Probe: `DSL_MONITOR_HTTP_PROBE_URL`, `DSL_MONITOR_HTTP_PROBE_INTERVAL_SECONDS`, `DSL_MONITOR_HTTP_PROBE_TIMEOUT_SECONDS`
- Web: `DSL_MONITOR_WEB_HOST`, `DSL_MONITOR_WEB_PORT`
- Orchestrator: `DSL_MONITOR_START_FRITZ_BRIDGE`
- Optional CSV-Retention: `DSL_MONITOR_CSV_RETENTION_DAYS` (Default 0 = keep forever)
