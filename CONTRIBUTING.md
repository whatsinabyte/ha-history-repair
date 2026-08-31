# Contributing to HA History Repair

This document covers the development environment, the test suite, and the
conventions this project follows.

Before writing code, read [ARCHITECTURE.md](ARCHITECTURE.md) — in particular
the module purity rules.

---

## Prerequisites

- Python 3.12 or later
- For running tests: nothing else. The unit suite has no database dependency.
- For the MariaDB/PostgreSQL/MQTT integration suites: Docker or Colima, and
  (for MariaDB) Homebrew — see "The development database" below.
- For live testing: a Home Assistant installation, on any of the three
  supported recorder backends.

## Repository layout

```
app/                  ← all production Python, flat modules, hr_ prefixed
  static/vendor/      ← Chart.js and Luxon, vendored (no CDN at runtime)
  templates/          ← Jinja templates
  requirements.txt    ← runtime dependencies only
tests/                ← test suite + hr_fakes.py (in-memory adapter)
translations/         ← en.yaml, the app options documentation
config.yaml           ← app manifest
Dockerfile / run.sh / apparmor.txt
repository.json       ← HA app store manifest
ruff.toml / mypy.ini / pytest.ini
```

`pytest.ini` sets `pythonpath = app tests`, so both production modules and
`hr_fakes` are imported by bare name, without a package prefix.

## Development environment

```bash
python3.12 -m venv .venv-check
.venv-check/bin/python -m pip install -r requirements-dev.txt
```

`requirements-dev.txt` is a superset of `app/requirements.txt` and
`requirements-test.txt`, and adds the static analysis tools.

Verify:

```bash
.venv-check/bin/python -m pytest -q
```

## The development database

The integration tests and the local UI run against a disposable MariaDB with a
genuine Home Assistant recorder schema. **Never point either at a live
recorder database.**

The schema is not hand-written. `dev/build_schema.py` imports Home Assistant's
own SQLAlchemy models and asks them to emit their `CREATE TABLE` statements, so
the result is exactly what a real installation has, at whatever schema version
the installed `homeassistant` package declares.

One-time setup:

```bash
brew install mariadb
uv venv --python 3.13 .venv-ha-schema
VIRTUAL_ENV=.venv-ha-schema uv pip install homeassistant pymysql
```

Then, each time you want a fresh database:

```bash
./dev/mariadb.sh start                              # isolated server on port 3399
.venv-ha-schema/bin/python dev/build_schema.py      # authentic HA schema
.venv-check/bin/python dev/seed_recorder.py         # 7 days of sensor history
```

The seed data deliberately covers every shape the app must handle: a
measurement sensor with three injected outliers (including a −2000 °C
implausible reading), a clean measurement sensor, a counter with a spike, a
sensor peppered with `unknown`/`unavailable`, and an entity with no statistics
at all.

`./dev/mariadb.sh` also takes `stop`, `status`, `shell`, and `reset` — `reset`
deletes the whole `.devdb/` directory, so nothing needs preserving.

To click through the UI against it:

```bash
./dev/run_local.sh            # http://127.0.0.1:8099/
./dev/run_local.sh --fresh    # …with onboarding reset
```

### PostgreSQL

`hr_postgres.py` needs the same treatment, via Docker rather than a native
binary (PostgreSQL is not otherwise part of this project's toolchain):

```bash
./dev/postgres.sh start
.venv-ha-schema/bin/python dev/build_schema.py \
  --dsn postgresql+psycopg://hatest:hatest@127.0.0.1:5442/ha_test
.venv-check/bin/python dev/seed_recorder.py \
  --postgres-dsn postgresql://hatest:hatest@127.0.0.1:5442/ha_test
HR_PG_TEST_DSN=postgresql://hatest:hatest@127.0.0.1:5442/ha_test \
  .venv-check/bin/python -m pytest tests/test_postgres_integration.py
```

`./dev/postgres.sh` takes the same `stop`/`status`/`shell`/`reset` verbs as
`mariadb.sh`.

### MQTT

`hr_mqtt.py`'s publisher needs a real broker to verify its actual wire
behaviour (retained messages, the discovery payload, the will) — a fake
client in the unit tests proves it *calls* the right methods, not that those
calls produce what Home Assistant expects to receive:

```bash
./dev/mosquitto.sh start
HR_MQTT_TEST_HOST=127.0.0.1 HR_MQTT_TEST_PORT=1893 \
  .venv-check/bin/python -m pytest tests/test_mqtt_integration.py
```

## Checking the packaging

The app is installed by Supervisor building the Dockerfile on the user's own
device, so a build failure is a failure to install. `dev/docker_check.sh`
reproduces that locally, without hardware:

```bash
brew install colima docker docker-buildx
colima start --cpu 4 --memory 4

./dev/docker_check.sh          # build, run, and check
./dev/docker_check.sh --arm    # also build the aarch64 image an ARM board runs
./dev/docker_check.sh --keep   # leave it running to click through
./dev/docker_check.sh --clean  # tear down
```

It mirrors the shape of a real installation: a MariaDB container standing in
for the official MariaDB app, and the app image reaching it *by container
name* over a Docker network, exactly as it would reach `core-mariadb`. The
image's own entry point runs, so `run.sh` really parses a `/data/options.json`
in the Supervisor's format. It then checks that the page serves, that the
health endpoint reports a supported schema, and that Ingress path rewriting
prefixes the asset URLs.

Two things worth knowing if you change it:

- **Bind-mount only paths colima shares into its VM.** macOS `mktemp -d`
  returns `/var/folders/...`, which it does not share — the mount silently
  appears empty inside the container. The script keeps its temporary
  directories inside the repository for this reason.
- **Wait for the database the way the next step connects to it.** The official
  MariaDB image runs a temporary bootstrap server on a socket while
  initialising, so an in-container `mariadb-admin ping` reports ready long
  before the server accepts TCP connections.

## Running the test suite

Parallel across all cores, which `pytest.ini` makes the default:

```bash
.venv-check/bin/python -m pytest -q
```

Integration tests are skipped unless their environment variable is set —
`HR_TEST_DSN` for MariaDB, `HR_PG_TEST_DSN` for PostgreSQL,
`HR_SQLITE_TEST_DB` for SQLite, `HR_MQTT_TEST_HOST`/`HR_MQTT_TEST_PORT` for
MQTT:

```bash
HR_TEST_DSN=mysql://hatest:hatest@127.0.0.1:3399/ha_test \
  .venv-check/bin/python -m pytest -q
```

The database-backed suites refuse to run against any database whose name does
not begin with `ha_test`, so a mistyped DSN cannot damage real history.

With coverage, as CI runs it:

```bash
HR_TEST_DSN=... .venv-check/bin/python -m pytest --cov=app --cov-report=term-missing
```

To debug a single failure, turn parallelism off — worker output interleaves:

```bash
.venv-check/bin/python -m pytest -n0 tests/test_web.py -v
```

### Parallel safety

`--dist=loadscope` is required, not optional. It keeps every test in a class on
one worker; xdist's default distribution can split a class across workers,
which then race on whatever that class shares.

Two further rules follow from the same concern, and both have already caught
real intermittent failures here:

- **Never hardcode a writable path.** Use the `tmp_path` fixture, which is
  unique per test. A fixed path is safe only for as long as no two sessions run
  at once — and mutation testing, CI matrices, and a second terminal all break
  that assumption.
- **Give every globally-named resource a per-process suffix**, not merely a
  per-worker one. The integration suite clones its database as
  `ha_test_<worker>_<pid>` and names its test trigger with the pid for exactly
  this reason: a bare `gw0` suffix collides when two runs start together.

Verify parallel safety empirically. Run the whole suite a dozen times in a row
and watch for intermittent failures rather than reasoning that it should be
fine — that is how both of the flakes fixed so far were found.

Thorough property-test run, before a release (500 examples per property):

```bash
HYPOTHESIS_PROFILE=thorough .venv-check/bin/python -m pytest
```

Tests run in randomised order (`pytest-randomly`). When one fails, replay it
with the seed printed in the output:

```bash
.venv-check/bin/python -m pytest --randomly-seed=<seed>
```

### Test file ownership

| Source | Test file |
|---|---|
| `hr_corrections.py` | `test_corrections.py` |
| `hr_web.py` | `test_web.py` |
| `hr_config.py` | `test_config.py` |
| `hr_mariadb.py` | `test_mariadb.py`, `test_mariadb_integration.py` (real server) |
| `hr_sqlite.py` | `test_sqlite_integration.py` (real database) |
| `hr_postgres.py` | `test_postgres_integration.py` (real server) |
| `hr_statistics.py` | `test_statistics.py`, `test_statistics_fidelity.py` (real Home Assistant) |
| `hr_outliers.py` | `test_outliers.py` |
| `hr_discovery.py` | `test_discovery.py` |
| `hr_mqtt.py` | `test_mqtt.py`, `test_mqtt_integration.py` (real broker) |
| `hr_ha_api.py` | `test_ha_api.py`, `test_ha_api_live.py` (real Home Assistant) |
| `hr_state.py` | `test_state.py` |
| `config.yaml`, `run.sh`, `translations/`, vendored assets | `test_packaging.py` |
| The frontend JavaScript and templates | `test_browser.py` (Playwright) |

## Static analysis

All five must pass clean before a pull request.

```bash
.venv-check/bin/python -m ruff check app/ tests/
.venv-check/bin/python -m ruff format --check app/ tests/
.venv-check/bin/python -m mypy app/
.venv-check/bin/python -m vulture app/ --min-confidence 80
.venv-check/bin/python -m bandit -r app/
```

Bandit's B608 (hardcoded SQL) findings in `hr_mariadb.py` are suppressed
individually with `# nosec B608` and a comment explaining why the
interpolation is safe — always because it is a module constant or an
introspected column name, with every user value passed as a bound parameter.
Never silence B608 globally, and never add a new suppression without
confirming that is still true.

## Coding conventions

**Module boundaries.** `hr_models.py` and `hr_db.py` are pure — no I/O, no
state. `hr_corrections.py` contains no SQL and no HTTP. If a change seems to
require crossing one of these lines, reconsider the design first.

**All SQL lives in `hr_mariadb.py`.** Nothing above the adapter layer may
assume a dialect; a SQLite adapter is a planned addition, and every leak makes
it more expensive.

**The audit row is written before the states UPDATE**, in the same
transaction. This is the guarantee the whole tool rests on.

**Style.** Python 3.12, line length 100, Ruff-formatted. British spelling in
prose and identifiers (`serialise`, `normalise`).

**No backwards compatibility burden.** There is no installed user base
requiring API stability yet. Remove dead code, rename freely when the new name
is clearer.

## Testing philosophy

- Prefer Hypothesis property tests where a rule should hold across a range of
  inputs, and pin real observed cases with `@example`.
- Assert on HTTP status codes in web tests, not only bodies — the frontend
  branches on 400 vs 409 vs 503 and shows a different message for each.
- **Any rule added to `MariaDBAdapter` must be added to `FakeAdapter` in
  `tests/hr_fakes.py`.** The fake reimplements the adapter's behaviour, not
  its SQL, and silent drift between the two makes every web-layer test
  meaningless.

## Submitting changes

1. Branch from `main`.
2. Run the full test suite and all static analysis; both must be clean.
3. Add or update tests for any changed behaviour.
4. Update `CHANGELOG.md` under `[Unreleased]`.
5. Open a pull request describing what changed and why.

For anything that changes the correction transaction, the audit schema, or the
set of sensor types that can be corrected, open a discussion first.
