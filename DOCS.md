# History Repair — User Guide

You've found the bad reading. Now here's how to fix it.

---

## Before you start

**Take a backup.** This app writes directly to your recorder database.
Every change it makes is logged and reversible from inside the app, but a
full Home Assistant backup is your safety net for anything a database-level
tool like this one can't reach. Create one from **Settings → System →
Backups** — the app's own onboarding will ask you to confirm you've done
this before it lets you correct anything.

**You need a supported recorder:** the default SQLite database, or an
external MariaDB or PostgreSQL that your `configuration.yaml`'s `recorder:`
section points at. If none of these apply, the app won't be able to
connect.

**SQLite installs need a wide filesystem grant.** SQLite is a single file
(`home-assistant_v2.db`), not a server this app can connect to over the
network — and Supervisor has no way to mount just that one file, so reaching
it means mounting the whole configuration directory it lives in: automations,
`secrets.yaml`, every other integration's config, all of it. This is wider
than most apps ask for, and there's no way around it while SQLite stays a
plain file. If you run MariaDB or PostgreSQL instead, this app never
touches anything outside its own database connection — the grant is simply
unused in that mode, though Supervisor still requires it be declared for
either mode to work.

---

## Configuration

| Option | Default | Meaning |
|---|---|---|
| `db_type` | `sqlite` | `sqlite`, `mariadb`, or `postgres` — pick whichever your recorder actually is. |
| `db_path` | `/homeassistant/home-assistant_v2.db` | Where the recorder file lives. Only used in `sqlite` mode; the default is correct for a standard install. |
| `db_host` | `core-mariadb` | Your database server's hostname. Only used in `mariadb`/`postgres` mode. The default matches the official MariaDB app. |
| `db_port` | `0` | Its port. `0` means "the standard port for whichever `db_type` you picked" (3306 for MariaDB, 5432 for PostgreSQL) — you only need to set this if your server runs on a non-standard port. |
| `db_name` | `homeassistant` | The database the recorder writes to. |
| `db_user` | `homeassistant` | The user to connect as. |
| `db_password` | *(empty)* | That user's password. Stored encrypted by the Supervisor, never shown back to you. |
| `log_level` | `info` | Raise to `debug` when reporting a problem. |
| `time_format` | `24` | `12` or `24`-hour clock for timestamps shown in the app. |

**Already running the official MariaDB app?** History Repair detects
it automatically at startup and uses its connection details in preference to
`db_host`/`db_port`/`db_user`/`db_password` above — you don't need to copy
anything in by hand. The manual fields above are still what's used for an
external database Home Assistant doesn't know about, or if automatic
detection finds nothing.

**Running an MQTT broker?** (The official Mosquitto app, or any other.)
History Repair finds it the same way, and — with no configuration
needed — publishes a `needs_review` sensor: on whenever a correction this
app made no longer matches what's in your database (see [Keeping your
audit trail honest](#keeping-your-audit-trail-honest) below), off otherwise.
Put it on a dashboard, or wire an automation to it, without opening this
app at all.

In `mariadb`/`postgres` mode, the database user needs `SELECT` and `UPDATE`
on the recorder database, plus `CREATE` for the one table this app adds —
the official MariaDB app grants all of these by default. In `sqlite` mode
there's no separate database user; file permissions on
`home-assistant_v2.db` are what govern access, and the filesystem mount
above is what makes the file reachable at all.

---

## First run

**1. Backup confirmation.** A checkbox — not a formality. Read the warning
above first.

**2. Connection test.** Checks three things and shows you each result: that
the database is reachable, that its schema is one this app understands
(schema version 28 or later — Home Assistant 2022.4 or newer), and that it
can be written to.

**3. Where to find it.** The app appears in your sidebar. Prefer it on a
dashboard instead? The embed YAML is shown with a copy button.

Finishing onboarding creates one new table, `state_corrections`, for the
audit trail. Nothing else about your recorder database changes.

---

## Correcting a value

**Find the sensor.** The entity list shows everything with recorded history.
Filter by entity ID, or by type (Measurement/Counter/Unknown — see [Counter
and energy sensors](#counter-and-energy-sensors) below for what that means),
and sort by entity, last updated, or how many corrections it already has.

**Look at the data.** Open the entity and pick a range — 24 hours, 7 days, or
30 days. The graph plots the recorded values. A point that's already been
corrected is drawn larger and in a different colour; hovering it shows both
the corrected and the original value.

**Or let the app point one out.** Likely outliers are flagged
automatically — a spike, a sensor stuck at one value, a meter jumping and
dropping straight back. The **sensitivity** slider controls how aggressively:
lower catches more candidates, including borderline ones; higher flags only
the most extreme. A flag is only ever a suggestion — nothing changes until
you click the point yourself and save.

**Correct the point.** Click it — flagged or not. The panel shows when it
was recorded and what it currently reads. Enter the right value, pick why the
original was wrong, and add a note if you like:

| Reason | Use when |
|---|---|
| Spike | An implausible jump with no physical cause |
| Bad communication | A network dropout or integration failure |
| Out of range | A physically impossible reading |
| Frozen | The sensor stuck at one value |
| Uncertain | You're not sure why |

These are for your own records — they don't change how the correction is
applied.

For a counter or energy sensor, the panel also shows how many statistics
rows the correction will rewrite before you can save — see [Counter and
energy sensors](#counter-and-energy-sensors) for why that number can be
large.

**Save.** The original value is recorded first, then the new one is written
— both in a single database transaction, so nothing is left half-applied if
something goes wrong partway through.

### If saving fails

**"This value changed since you loaded it."** The recorder wrote to that row
between you opening the graph and pressing save. Reload and try again —
you're seeing this instead of your correction silently landing on a value you
never actually looked at.

**"The recorder is currently holding this row."** Home Assistant was writing
to the database at that exact moment. Wait a second and retry.

---

## Correcting a whole range at once

Several bad readings in a row — a WiFi outage that produced a dozen of them,
not just one — don't need correcting individually. Click **Correct a
range**, then drag across the affected stretch of the graph.

A panel shows how many readings are in the range (and how many are already
corrected and will be left alone), and lets you choose how to fill it:

- **Interpolate** draws a straight line between the last good reading before
  the range and the first good one after it, and places each corrected
  reading on that line. Use it for anything that varies continuously —
  temperature, humidity, pressure.
- **One value** replaces every reading in the range with the same number.
  Use it when the sensor was stuck, or repeatedly reporting one specific
  implausible value.

Every reading still gets its own entry in the corrections list and can be
restored individually — a bulk correction is a fast way to make many
corrections, not one correction covering many rows.

Interpolation is refused if the range doesn't have a numeric reading on both
sides to draw a line between; pick a narrower range, or use one value
instead.

---

## Undoing a correction

Every correction can be undone, from the corrections list or from the point
itself on the graph. Restore puts the original value back and marks the
correction as restored — it stays in the list as a record rather than
disappearing.

Restore refuses if the row no longer holds what this app wrote there.
That usually means a Home Assistant backup was restored from before the
correction was made — refusing is deliberate, since putting the "original"
back would overwrite whatever's actually in that row now, which isn't what
you asked for.

### Keeping your audit trail honest

Restoring an old backup can leave a correction in your audit list pointing
at a database row that no longer agrees with it, or no longer exists — an
**orphaned correction**. The app checks for this and shows a banner when
it finds one, with a dismiss button once you've looked into it. If you have
an MQTT broker, the same check drives the `needs_review` sensor mentioned
above, so you'd know even without opening the app.

### Exporting the audit trail

The corrections list has an **Export CSV** button, for a record outside the
app — your own change log, or to hand to someone else. It exports whatever
the list is currently showing: tick **Hide restored** before exporting, and
the CSV reflects that same filter.

---

## What gets corrected, and why it matters

Home Assistant stores a sensor's data in three places, and an outlier lands
in all three:

| Where | Corrected? | What reads it |
|---|---|---|
| `states` | **Yes** | History graph cards, ApexCharts, the logbook |
| `statistics_short_term` | **Yes** | Statistics graph cards, short ranges |
| `statistics` | **Yes** | Statistics graph cards, long ranges, the **energy dashboard** |

The statistics tables don't store your raw readings — they store 5-minute and
hourly summaries of them. When a reading changes, its summaries have to
change too, and this app recalculates them exactly the way Home Assistant
itself does: the 5-minute average weights each reading by how long it was in
effect, and the hourly figures summarise the 5-minute ones. Editing the
database by hand — the usual workaround before a tool like this existed —
only ever reaches the first table, leaving the other two quietly wrong.

**You may need to restart Home Assistant afterwards.** It also keeps
statistics in memory. The database is corrected immediately, but a
statistics card or the energy dashboard may keep showing the old value until
Home Assistant reloads them — the app tries to refresh this automatically
where it can, but a restart is the reliable fallback if a stale value
lingers. History graph cards are unaffected and update immediately.

**Corrections older than ten days** touch the hourly statistics only — Home
Assistant deletes the 5-minute figures after ten days but keeps the hourly
ones forever. There's no 5-minute row left to update in that case; nothing
is lost, and the app handles it automatically.

### Graphing further back than your raw history goes

Ask for a longer range than your recorder keeps raw readings for (10 days by
default), and the graph doesn't just stop — it continues using Home
Assistant's hourly long-term statistics, which are never purged, drawn in a
different colour with a note explaining what you're looking at. Those points
can't be corrected: an hourly average has no one true value to put back even
if it looks wrong, and the original reading behind it is already gone. This
matches exactly what Home Assistant's own history graph does once your raw
data runs out.

## Counter and energy sensors

Sensors that accumulate a total — energy, gas, and water meters — are
corrected too, but differently: their statistics carry a running total, so
fixing one reading means recalculating every total that came after it, all
the way to the present.

This app calls a sensor a "Counter" specifically when Home Assistant has
recorded a **Sum** statistic for it — the same "Sum" column shown on
Developer Tools → Statistics. A sensor can have Mean/Min/Max statistics
without ever having a Sum (a temperature, or a calculated value like a
coefficient of performance) — those are shown as "Measurement" instead, and
correcting one only touches that single reading, with no running total to
rewrite and no acknowledgement needed.

**Before you save, the panel shows the scope** — how many statistics rows
the correction will rewrite — and asks you to tick a box confirming you
understand before it lets you proceed. For a reading from months ago on a
long-running meter, that can be thousands of rows; it's still one atomic
transaction, and still fully reversible.

**Undoing a counter correction** puts the original reading back and
recalculates the running total again from there, rather than replaying
stored numbers — there could be far too many to store. The result is
identical to what was there before.

Counters can be corrected in a range too, the same way as any other sensor —
the recalculation happens for each reading in the range, in the order it
occurred, so the totals come out the same as correcting them one at a time.

## Sensors without Home Assistant statistics

Not every sensor gets a statistics summary from Home Assistant — some report
values without ever being assigned a `state_class`. These are still fully
correctable: without a statistics table entry, there's simply nothing beyond
the reading itself to keep in sync, so a correction here changes only the
state value. The app tells you this at the point of correcting one.

## What this app does not correct

### Very recent values

Correcting a sensor's *current* reading won't update its entity card until
the device next reports — Home Assistant holds the current state in memory,
separately from the database. History graphs are unaffected and update
immediately.

### Attributes

A few integrations — notably the SQL sensor — store a full-precision number
in the state's attributes alongside a rounded one in the state itself. This
app corrects the state, not the attributes. If a custom card reads that
attribute directly, it keeps showing the old number. Standard MQTT, Modbus,
Z-Wave, and Nibe sensors are unaffected.

---

## Security

Reachable only through Home Assistant's own login, admin-only, no port
published on your network — anything that could reach this app could
rewrite your recorder database. Your database password is stored encrypted
by the Supervisor and never shown back to you. Every correction records
which Home Assistant user made it.

SQLite mode requires read-write access to your whole configuration
directory, for the reason explained under [Before you start](#before-you-start).
If you use `mariadb` or `postgres` mode and want this app to hold no
filesystem access beyond its own directory, that's only achievable by
building it yourself with the grant removed from `config.yaml` — there's no
in-UI way to relinquish it.

---

## Container installs (docker-compose)

Everything above assumes HAOS or Supervised, where this runs as an app
behind Supervisor Ingress. Running Home Assistant as a plain Docker container
instead? Use `docker-compose.yml` at the root of this repository rather than
installing it as an app — there's no Supervisor in that setup to build or
run apps.

Read the comments at the top of that file before running it: without Ingress
there's no built-in authentication, so the port binds to `127.0.0.1` only by
default, and corrections are attributed to "unknown" rather than a real
username. Point `HR_DB_HOST` at your existing database container (or switch
to `HR_DB_TYPE: sqlite` and mount your Home Assistant config directory), then
`docker compose up -d` and browse to `http://127.0.0.1:8099/`.

---

## Troubleshooting

**"Recorder schema N is too old."** Your Home Assistant predates version
2022.4, when the database layout this app relies on was introduced.
Update Home Assistant.

**"The database user lacks the UPDATE privilege."** (`mariadb`/`postgres`
mode.) Grant it in your database app's configuration — the official
MariaDB app grants all privileges by default, so this usually means an
explicit privilege list was set somewhere.

**"The app cannot write to '...'"** (`sqlite` mode.) `db_path` doesn't
point at a writable file. Check it matches where your installation actually
keeps `home-assistant_v2.db` — the default is almost always correct — and
that nothing else has the file open exclusively.

**"Could not read the recorder schema version."** The app connected, but
the database doesn't look like a Home Assistant recorder database. Check
`db_path` in `sqlite` mode, or `db_name` in `mariadb`/`postgres` mode.

**Cannot connect at all.** In `mariadb`/`postgres` mode, check `db_host` and
`db_port`, and that your database app is actually running; if your
database is outside Home Assistant, make sure it accepts connections from
this app's container. In `sqlite` mode, check that `db_path` exists.
