# History Repair

Your energy dashboard has a jagged spike where a WiFi dropout made your power
meter read 99,999 kWh for one sample. Your living room temperature graph is
flat for a month because one night it recorded −2000 °C and every other value
is now squeezed into a sliver at the top of the chart. You know the reading is
wrong. Home Assistant gives you no way to fix it.

**History Repair** does. It's a Home Assistant app that finds outlier
values in your sensor history, lets you correct them with a couple of clicks,
and keeps a full record of what changed — so a bad reading never costs you a
graph again.

## Nothing happens without a trace

Every correction this app makes is logged first, in an audit table
alongside your recorder data: the original value, what it was changed to, why,
who did it, and when. Made a mistake, or corrected the wrong point? Restore it
with one click — the original value comes straight back.

It never writes anything without recording what it's overwriting. That's not
a footnote — it's the reason this app is safe to point at your real data:
if in doubt, undo.

## What it does

- **Shows you the data before touching anything.** Browse every sensor with
  recorded history, open one, and look at its graph. Nothing changes until
  you say so.
- **Points out the likely culprits.** Readings that look wrong are flagged
  automatically, with a sensitivity slider for how aggressively — a flag is
  always a suggestion, never an automatic edit.
- **Fixes one reading, or a whole run of them.** Click a bad point and correct
  it, or drag across a stretch of an outage and fill it with a straight line
  between the good readings either side, or one replacement value.
- **Gets counters right.** An energy or water meter's running total is
  rebuilt through every reading after the one you corrected, so the energy
  dashboard doesn't just have one point fixed and every later total still
  wrong.
- **Fixes the history graph and the statistics behind it, together.** Home
  Assistant keeps a sensor's history in one table and its statistics summaries
  — the ones your energy dashboard and statistics cards actually read — in
  two more. Editing the database by hand only reaches the first. This app
  corrects all three, in one transaction, so nothing is left quietly
  disagreeing with anything else.
- **Undoes anything, any time.** From the graph or from a running list of
  every correction ever made.
- **Works with whichever database Home Assistant gave you.** SQLite (the
  default), MariaDB, or PostgreSQL — all three recorder backends Home
  Assistant itself supports.

## Installation

1. In Home Assistant, go to **Settings → Apps → App Store**.
2. From the ⋮ menu, choose **Repositories**, and add this repository's URL.
3. Install **History Repair**, then open it.
4. If your recorder is the default SQLite database, there's nothing to
   configure — start the app. Running MariaDB or PostgreSQL instead? Set
   `db_type` and your connection details on the **Configuration** tab first.
   Running the official MariaDB app already? History Repair finds it
   automatically — you won't need to type the connection details in twice.
5. Start it. It appears in your sidebar, ready to use.

Running Home Assistant in plain Docker rather than HAOS/Supervised? See
[Container installs](DOCS.md#container-installs-docker-compose) in DOCS.md —
`docker-compose.yml` at the root of this repository runs it standalone.

## First run

A short walkthrough: confirm you have a backup, and the app checks your
database connection is reachable, understood, and writable. It creates one new
table for its own audit trail — nothing else about your recorder database
changes.

## Security

Reachable only through Home Assistant's own login, admin-only, no port
published on your network. Your database password is stored encrypted by the
Supervisor and never shown back to you.

## Documentation

- [DOCS.md](DOCS.md) — the full user guide
- [ARCHITECTURE.md](ARCHITECTURE.md) — how it works, for developers
- [CONTRIBUTING.md](CONTRIBUTING.md) — development environment and conventions

## Licence

MIT. See [LICENSE.md](LICENSE.md).
