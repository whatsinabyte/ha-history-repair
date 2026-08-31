# Architecture

Developer reference for HA History Repair — what is actually built, and why
it is shaped the way it is.

---

## 1. The problem this tool exists to solve

A sensor records an impossible value — a temperature of −2000 °C during a WiFi
dropout, an energy meter that jumps by a year's consumption when a modem
reboots. The value is now permanent. It distorts every graph it appears in,
skews long-term aggregates, and there is no user-facing way to remove it.

The obvious fix — editing the `states` table in phpMyAdmin — is unsatisfying
for three reasons: it leaves no record of what was there before, it cannot be
undone, and it corrects only one of the three places the bad value now lives.

## 2. The three-table problem

Home Assistant keeps sensor data in three stores, and an outlier propagates
into all of them independently:

| Table | Holds | Read by |
|---|---|---|
| `states` | every state change at full resolution | history cards, ApexCharts, logbook, automations |
| `statistics_short_term` | 5-minute aggregates, purged after 10 days | statistics cards over short ranges |
| `statistics` | hourly aggregates, never purged | statistics cards over long ranges, energy dashboard |

Correcting `states` alone fixes history graphs and leaves the energy dashboard
wrong. This is the central technical challenge of the project, and every
correction handles all three tables in one transaction, never `states` alone.

The distinction that governs how is the sensor type:

- **Measurement sensors** (`state_class: measurement`) store `mean`, `min`,
  `max` per bucket. Each bucket is independent, so correcting one rebuilds
  just its own 5-minute and hourly buckets from the corrected `states` rows —
  nothing else is affected.
- **Counter sensors** (`state_class: total` / `total_increasing`) store
  `state` and a cumulative `sum`. An outlier offsets every later `sum` value,
  so a correction cascades forward through every statistics row after it —
  potentially years of them — recalculating each from the one before it
  (`hr_statistics.py`). The scope (how many rows) is computed and shown to the
  user before the correction is allowed to proceed.
- **Unknown sensors** (no statistics of any kind) have nothing beyond the
  state value to keep in sync, so a correction there only ever touches
  `states`.

## 3. Module reference

All production code is flat modules in `app/`, imported by bare name
(`pytest.ini` sets `pythonpath = app`).

| Module | Responsibility | Purity |
|---|---|---|
| `history_repair.py` | Entry point. Builds the app, logs the startup gates, starts the MQTT publisher, serves it under gunicorn in-process. Holds `ADDON_VERSION`. | I/O |
| `hr_config.py` | `AppConfig` / `DatabaseConfig` from the environment `run.sh` prepares. | Reads `os.environ` only |
| `hr_models.py` | `Entity`, `StatePoint`, `Correction`, `HealthReport`, `SensorType`, `Quality`. | **Pure** |
| `hr_db.py` | The `DatabaseAdapter` interface and the error taxonomy every layer above catches. | **Pure** |
| `hr_mariadb.py` | Implements the interface against recorder schema 28+, for MariaDB. | I/O |
| `hr_sqlite.py` | Implements the interface for SQLite — Home Assistant's default recorder — including its own lock-retry loop, since SQLite's single-writer model means a `database is locked` error is expected, not exceptional. | I/O |
| `hr_postgres.py` | Implements the interface for PostgreSQL, the third backend the recorder supports. Closest to `hr_mariadb.py` in shape; the dialect differences are listed in its module docstring, the sharpest being that an error aborts the whole transaction, so optional queries run inside a SAVEPOINT. | I/O |
| `hr_corrections.py` | Whether a correction is allowed and what it should contain. No SQL, no HTTP. | **Pure** given its adapter |
| `hr_statistics.py` | The 5-minute (time-weighted) and hourly statistics arithmetic, and the counter cascade. Verified against a real Home Assistant, not derived from the schema alone. | **Pure** |
| `hr_outliers.py` | Automatic candidate detection — a rolling-window Hampel filter (local median/MAD). | **Pure** |
| `hr_sql_utils.py` | Small dialect-agnostic SQL fragments shared by more than one adapter. | **Pure** |
| `hr_state.py` | The onboarding flag, persisted as JSON under `/config`. | Filesystem |
| `hr_discovery.py` | Finds a MariaDB or MQTT service Supervisor already knows about, via its `/services/*` API — used in preference to manually entered connection details when present. | I/O |
| `hr_ha_api.py` | Authenticates to Home Assistant Core's own WebSocket API and asks it to refresh its in-memory statistics cache after a correction, so the change is visible without a restart. Fails soft: this is a convenience, not a requirement for the correction itself. | I/O |
| `hr_mqtt.py` | Publishes a `needs_review` binary sensor over MQTT discovery, reflecting whether any correction no longer matches the database — usable on a dashboard or in an automation without opening this app. | I/O |
| `hr_web.py` | Flask routes, JSON API, Ingress middleware, error-to-status mapping. | I/O |

### Why there is an adapter interface at all

SQLite is Home Assistant's default recorder database, so the majority of
potential users would have been excluded had only MariaDB been supported. The
`DatabaseAdapter` interface existed before `hr_sqlite.py` did, which meant
SQLite — and later PostgreSQL — arrived as an added implementation rather
than a rewrite of everything above it. The rule that kept this true: **no SQL
outside a concrete adapter**, and nothing above the adapter may assume a
dialect.

## 4. The correction transaction

```
BEGIN
  SELECT … FROM states JOIN states_meta WHERE state_id = ? FOR UPDATE
    ├─ row missing            → NotFound        → 404
    ├─ belongs to another id  → NotFound        → 404
    └─ value ≠ expected       → ConcurrentMod.  → 409
  INSERT INTO state_corrections (…, stats_corrected = 0)
  UPDATE states SET state = ? WHERE state_id = ?
COMMIT
```

Three properties matter here:

**The audit row is written first.** If the UPDATE fails, the audit row rolls
back with it. There is no window in which a change exists without its record,
or a record without its change.

**The target is the primary key.** Keying a correction on
`FROM_UNIXTIME(last_updated_ts) = :state_ts` was tried and dropped: that
column is a float epoch, so the comparison is unreliable after a DATETIME
round trip and cannot use the index. `state_id` is exact and indexed.

**The caller passes the value it expects to find.** The recorder writes to this
database continuously. If the row changed between the user loading the graph
and pressing save, the correction is refused rather than applied to a value the
user never saw.

Restore is the mirror image, with one addition: it refuses when the row no
longer holds the corrected value. That is the backup-restore case — putting the
"original" back would destroy whatever is there now.

### Lock contention

Each adapter bounds how long it will wait for a row the recorder is holding,
using whatever its backend calls that setting — `innodb_lock_wait_timeout` on
MariaDB, `lock_timeout` on PostgreSQL, and its own retry-with-backoff loop on
SQLite, whose single-writer model makes contention routine rather than
exceptional. All three surface the same `LockTimeout`, so the user is invited
to retry rather than the request hanging.

## 5. Startup gates

Run on connect, surfaced in onboarding and re-runnable from `/api/health`:

1. **Connectivity** — reachable and authenticated.
2. **Schema version** — `SELECT MAX(schema_version) FROM schema_changes`.
   Below 28 there is no `states_meta` table and every query here would fail,
   so the app refuses to operate and says why.
3. **Privilege** — that the user may actually `UPDATE`. MariaDB has to be
   asked indirectly, parsing `SHOW GRANTS FOR CURRENT_USER()` for `UPDATE` or
   `ALL PRIVILEGES` before ` ON `; PostgreSQL answers directly with
   `has_table_privilege`, which resolves inherited and role-based grants
   itself; SQLite has no users at all, so the file's own write permission is
   the equivalent. When the question cannot be answered, the check assumes
   success: better a correction that fails loudly than a user blocked by a
   question their server won't answer.

A failing gate never prevents the web server from starting — onboarding is
where the user reads what is wrong, and that screen has to be reachable.

## 6. Schema evolution

`statistics_meta` gained `mean_type` in schema 48, and `has_mean` is deprecated
from Home Assistant 2026.11. Rather than branching on a version number, the
adapter introspects `information_schema.columns` once and builds the
sensor-type SELECT fragment from whichever columns exist. Both eras work
without a code change, and a database with neither column degrades to
`unknown`, which is refused rather than mis-corrected.

## 7. Security posture

The app is served exclusively through Supervisor **Ingress**. It publishes
no LAN port, because anything that can reach this application can rewrite the
recorder database. Ingress also supplies `X-Remote-User-Display-Name`, which
becomes the `created_by` on every audit row.

Database credentials are declared as `password` in the options schema, so the
Supervisor stores them encrypted and masks them in the UI. They are never
rendered by this application.

Three further grants in `config.yaml`, each scoped to exactly what it is for:

- `homeassistant_api: true` — lets `hr_ha_api.py` authenticate to Home
  Assistant Core's own WebSocket API, to refresh its in-memory statistics
  cache after a correction. Without it, Supervisor still hands out a
  `SUPERVISOR_TOKEN`, but one only valid for Supervisor's own management API.
- `services: [mysql:want, mqtt:want]` — read access to whichever add-on is
  currently providing a MariaDB or MQTT service, for `hr_discovery.py`. Both
  are optional; the app works identically with neither present.
- `watchdog` pointed at this app's own `/api/health` — a native Supervisor
  mechanism that restarts the app if it stops responding, needing no
  user-maintained automation.

## 8. Frontend

Vanilla JavaScript with Chart.js, Luxon, and the Chart.js Luxon adapter
vendored into `app/static/vendor/`. Nothing is fetched from a CDN: apps
must work on an isolated network.

Every request is built relative to `window.HR_BASE`, which Flask fills in from
the Ingress-aware `SCRIPT_NAME`. Hardcoding `/api/...` would break the moment
the page is served under `/api/hassio_ingress/<token>/`.

## 9. Testing

Around 780 tests, run in randomised order across all six cores
(`--dist=loadscope`). Roughly 520 run with no external dependency at all
(`pytest -q`, a couple of minutes); the remainder are integration tests
against a real MariaDB, PostgreSQL, or MQTT broker, gated behind an
environment variable each and skipped otherwise.

| File | Covers |
|---|---|
| `test_corrections.py` | Validation rules, property tests over the full float range, the cascade decision |
| `test_web.py` | Routing, API contracts, error-to-status mapping, Ingress middleware |
| `test_browser.py` | The actual UI in a real browser (Playwright): the correction flow, bulk range correction, the audit page, responsive layout |
| `test_mariadb.py` / `test_mariadb_integration.py` | Value parsing, sensor typing, schema introspection, query-shape guards, and (integration) every one of the above against a real MariaDB |
| `test_sqlite_integration.py` | The same parity, against a real SQLite database |
| `test_postgres_integration.py` | The same parity, against a real PostgreSQL, plus the dialect differences noted in `hr_postgres.py`'s own docstring |
| `test_statistics.py` / `test_statistics_fidelity.py` | The 5-minute/hourly arithmetic, the latter against a real running Home Assistant |
| `test_outliers.py` | The Hampel filter's detection behaviour and its known, accepted limitations |
| `test_discovery.py` | The Supervisor `/services/*` query and the discovery-vs-manual merge |
| `test_mqtt.py` / `test_mqtt_integration.py` | The `needs_review` publisher, against a fake transport and a real broker |
| `test_ha_api.py` / `test_ha_api_live.py` | The statistics-cache refresh call, against a fake WebSocket and a real Home Assistant |
| `test_state.py` | Onboarding persistence and its failure modes |
| `test_config.py` | Environment parsing, including discovery precedence |
| `test_packaging.py` | Drift between `config.yaml`, `run.sh`, `translations/en.yaml`, vendored assets, and the code |

`tests/hr_fakes.py` holds `FakeAdapter`, an in-memory recorder that
reimplements the adapter's *rules* rather than its SQL. This is what lets the
web and service layers be tested exhaustively without a database — and it is
also the thing most likely to rot, so any rule added to a real adapter
belongs there too.

The query-shape tests in `test_mariadb.py` are unusual: they read the
adapter's own source and assert on it, to pin choices (keying on `state_id`,
not a DATETIME comparison; `DOUBLE`, not `FLOAT`) that read like they could be
"corrected" back into a subtler bug by someone unfamiliar with why they are
that way.
