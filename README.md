# dsl_monitor

Ein kleines DSL/Verbindungs-Monitoring.

- `run.py` startet alles: Fritz-Status-Bridge (`fritz_status_service.py`), Probe (`probe.py`), Web UI (`web.py`).
- Konfiguration über `.env` (niemals committen).
- Messdaten werden in einer SQLite-Datenbank gespeichert (`dsl_log.db`).

## UI / Plots

Die Web-UI zeigt zwei Plotly-Grafiken:

1) **Ping-Latenz (oben)** – `Plotly.newPlot('latency', …)`
   - Zeitreihe der Ping-Latenz in konfigurierbaren Buckets (`DSL_MONITOR_BUCKET_MINUTES`, Default 5 min).
   - Marker-Farben: **grün** = Ping OK, **rot** = Ping-Event/Outage (Bucket enthält `dsl_event_active`).
   - Hier sieht man die Ping-Ausfälle auf einen Blick.

2) **Status-Boxen (unten, zweite Zeile)** – `Plotly.newPlot('status', …)`
   - Quadrate pro Zeit-Bucket als kompakte Statusanzeige der **Systemreaktion**.
   - Drei separate Plotly-Traces (mit Legende):
     - 🔴 **rot** (`#f56565`) = **outage** – DSL-Event im Bucket (Ping-Event oder HTTP-Timeout).
     - 🔵 **blau** (`#63b3ed`) = **DSL** – Normalbetrieb über DSL-Verbindung. Wenn in einem Bucket kein Fritz-Status geloggt ist, wird DSL angenommen.
     - 🟡 **gelb** (`#ecc94b`) = **mobile** – FritzBox hat auf Mobilfunk-Fallback umgeschaltet.
   - Der User kann so im oberen Graph die Ping-Ausfälle sehen und in der Box-Zeile darunter die Systemreaktion/Status: ob Outage (rot), DSL (blau) oder Mobile-Fallback (gelb).

## Was ist ein „DSL-Event“?

Ein DSL-Event ist **der Zeitraum**, in dem wir aktiv Fritz-Synchron-/Verbindungstyp beobachten und in der UI „rot“ markieren.

**Trigger (Event-Start):**
- **Ping-Failures:** Ping läuft alle `DSL_MONITOR_PING_INTERVAL_SECONDS` (Default: 15s). Wenn `DSL_MONITOR_FAILURE_THRESHOLD` (Default: 3) Pings hintereinander fehlschlagen, startet ein Event.
- **Hohe Latenz:** Wenn ein Ping zwar ankommt, aber die Latenz über `DSL_MONITOR_PING_LATENCY_THRESHOLD_MS` (Default: 100ms) liegt, wird er als Failure gewertet und startet **sofort** ein DSL-Event (kein 3-Fail-Threshold nötig). Das erkennt den typischen Fall, wenn die FritzBox auf Mobilfunk-Fallback umschaltet – Pings kommen an, aber mit 150+ ms statt der normalen ~15ms über DSL.
- **HTTP-Timeout:** Zusätzlich lädt die Probe alle `DSL_MONITOR_HTTP_PROBE_INTERVAL_SECONDS` (Default: 300s) eine konfigurierbare URL (`DSL_MONITOR_HTTP_PROBE_URL`). Wenn diese Anfrage in ein Timeout läuft (`DSL_MONITOR_HTTP_PROBE_TIMEOUT_SECONDS`), startet ebenfalls ein DSL-Event.
- **Fritz meldet Mobile:** Auch im Normalbetrieb wird der Fritz-Status periodisch geprüft (alle `DSL_MONITOR_CONN_TYPE_NORMAL_POLL_INTERVAL_SECONDS`, Default: 1200s = 20min). Wenn dabei `connection_type == mobile` erkannt wird, startet **sofort** ein DSL-Event (`fritz_mobile`).

**Fritz-Polling (Zwei Raten):**
- **Normalbetrieb:** Fritz-Status alle 20min (`DSL_MONITOR_CONN_TYPE_NORMAL_POLL_INTERVAL_SECONDS`). Der Wert wird in der DB/UI als `connection_type` geloggt (dsl/mobile/unknown). Erkennt proaktiv einen dsl→mobile-Wechsel.
- **Während DSL-Event:** Fritz-Status alle 60s (`DSL_MONITOR_CONN_TYPE_POLL_INTERVAL_SECONDS`). Damit sieht man schnell, wann die FritzBox von mobile zurück auf DSL wechselt.

**Während DSL-Event:**
- Wenn Fritz `connection_type == mobile`, wird die „mobile duration" hochgezählt.
- Die UI färbt Buckets **rot**, wenn `dsl_event_active` gesetzt ist.

**Event-Ende:**
- Sobald Pings wieder erfolgreich sind (Latenz normal) UND Fritz `connection_type == dsl` meldet → Ende (`dsl_event_end_reason=recovered_to_dsl`). **Wichtig:** Solange Pings noch fehlschlagen, bleibt das Event aktiv – auch wenn Fritz `dsl` meldet (DSL-Sync kann stehen, obwohl Internet-Routing gestört ist).
- Oder nach `DSL_MONITOR_DSL_EVENT_MAX_SECONDS` (Default 45min) → Ende (`dsl_event_end_reason=max_duration`).
- Nach Event-Ende: Fritz-Polling geht zurück auf 20min-Intervall.

## Retention (Anzeige vs. Datenbank)

- `DSL_MONITOR_RETENTION_DAYS` wirkt **nur auf die Anzeige** in der Web-UI (SQL-Filter beim Lesen).
- Die SQLite-DB (`dsl_log.db`) behält **standardmäßig alle Messwerte** (kein automatisches Löschen).
- Optional kannst du explizites Pruning einschalten:
  - `DSL_MONITOR_DB_RETENTION_DAYS=<tage>`
  - Default ist `0` (= unendlich / kein Pruning). Pruning läuft max. 1×/Stunde.

## SQLite Schema

`dsl_log.db` enthält die Tabelle `measurements` mit diesen Spalten:

| Spalte | Typ | Beschreibung |
|---|---|---|
| `timestamp` | TEXT | UTC ISO Zeitstempel |
| `ping_target` | TEXT | Host/IP für den Primär-Ping |
| `ping_ok` | INTEGER | `1`/`0` |
| `latency_ms` | REAL | Ping-Latenz in ms (NULL bei Failure) |
| `consecutive_failures` | INTEGER | Zähler der Ping-Fails |
| `dsl_event_active` | INTEGER | `1`/`0` |
| `dsl_event_trigger` | TEXT | `ping_failures`, `http_timeout`, `high_latency` oder `fritz_mobile` – nur während Event aktiv bzw. in der Abschlusszeile, danach leer. |
| `dsl_event_duration_seconds` | REAL | Laufzeit des aktuellen Events (NULL wenn kein Event) |
| `dsl_event_end_reason` | TEXT | `recovered_to_dsl` oder `max_duration` – nur in der Zeile, in der das Event endet. |
| `connection_type` | TEXT | `dsl`/`mobile`/`unknown` (wird immer geloggt) |
| `mobile_duration_seconds` | REAL | Dauer in `mobile` (NULL wenn nicht mobile) |
| `http_probe_ok` | INTEGER | `1`/`0`/NULL (noch nie geprüft) |
| `http_probe_error` | TEXT | z.B. `timeout` oder `HTTP 500` |

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
- Fritz-Polling: `DSL_MONITOR_CONN_TYPE_NORMAL_POLL_INTERVAL_SECONDS` (Default 1200 = 20min), `DSL_MONITOR_CONN_TYPE_POLL_INTERVAL_SECONDS` (Default 60s, während Event)
- Probe/Event: `DSL_MONITOR_PING_INTERVAL_SECONDS`, `DSL_MONITOR_FAILURE_THRESHOLD`, `DSL_MONITOR_PING_LATENCY_THRESHOLD_MS`, `DSL_MONITOR_DSL_EVENT_MAX_SECONDS`
- HTTP-Probe: `DSL_MONITOR_HTTP_PROBE_URL`, `DSL_MONITOR_HTTP_PROBE_INTERVAL_SECONDS`, `DSL_MONITOR_HTTP_PROBE_TIMEOUT_SECONDS`
- Web: `DSL_MONITOR_WEB_HOST`, `DSL_MONITOR_WEB_PORT`
- Orchestrator: `DSL_MONITOR_START_FRITZ_BRIDGE`
- Optional DB-Retention: `DSL_MONITOR_DB_RETENTION_DAYS` (Default 0 = keep forever)
