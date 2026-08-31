# Design proposal: backfilling the graph from long-term statistics

Status: **implemented and verified.** Backfill lives in
`hr_statistics.backfill_from_statistics`, wired into `hr_web.py`'s
`api_states` endpoint, with unit tests at the statistics layer, web-layer
tests via `FakeAdapter`, and a browser test confirming the line-colour
differentiator, the explanatory banner, and the blocked click on a
backfilled point. This should become a numbered addendum to
`ha-history-repair-design.md` and a new section of `ARCHITECTURE.md`;
until then it lives here on its own.

## The problem, precisely

Home Assistant's own recorder purges raw `states` rows after
`purge_keep_days` (10 days by default). Its own frontend history graph knows
this: past that point, it backfills the older part of the graph from the
`statistics` table (hourly aggregates, never purged), while the recent part
still comes from raw `states`. That's documented directly on
[home-assistant.io's history graph card page](https://www.home-assistant.io/dashboards/history-graph/)
and the [data science portal's statistics page](https://data.home-assistant.io/docs/statistics/).

This app's graph does not do that. `hr_web.py`'s `/api/entities/<id>/states`
endpoint always calls `db.fetch_states()` — raw `states` only — for every
range, including the 30-day option. On a default-retention installation, a
30-day view (and parts of the 7-day default view, on a shorter custom
retention) is asking for data that may simply no longer exist in `states`.
The result: the graph goes sparse or blank for the older part of the range,
rather than showing what Home Assistant's own UI would show for the same
period. That gap is what actually produced the "different timestamp" symptom
that started this — the visible discontinuity is real data missing, not a
formatting mismatch on the data that is there.

## What's already true and unaffected

- The **correction transaction itself is unaffected**. A correction always
  targets a real `states.state_id` (`hr_db.py`'s whole design rests on this —
  see `ha-history-repair-design.md` and `CLAUDE.md`'s "Known
  departures" #2). Once a `states` row is purged, there is nothing left to
  correct there regardless of what the graph shows. This proposal is about
  **making the graph honestly show what happened**, not about correcting
  further back than raw data survives.
- `fetch_statistics` and `get_statistics_metadata` already exist on every
  adapter (`hr_db.py`'s interface) — built for the correction cascade, not
  for display, but the read path needed here already exists and is already
  tested against all three backends.
- `statistics_short_term` purges on roughly the same schedule as `states` (10
  days by default — see `CLAUDE.md`'s statistics-arithmetic notes), so it is
  not a useful middle tier: by the time raw `states` are gone, the 5-minute
  buckets are usually gone too. This proposal only backfills from the hourly
  `statistics` table, which is never purged.

## Proposed behaviour

1. **Fetch raw `states` first, for the full requested range**, exactly as
   today.
2. **If the earliest returned state is later than the requested start**, the
   gap `[requested_start, earliest_state_ts)` is filled from hourly
   `statistics` rows for that entity, one point per hour bucket.
3. **The value plotted for a backfilled point**: `mean` for a measurement
   sensor, `state` (the meter reading at the bucket's end) for a counter —
   the same fields this app already treats as authoritative everywhere
   else (`CLAUDE.md`'s statistics-arithmetic section). No new arithmetic is
   introduced; this reads values already verified correct.
4. **Backfilled points are visually distinct and non-interactive, in two
   ways at once** — not relying on either alone:
   - **A different line colour** for the backfilled segment, not just a
     different point marker: the coarser, hourly-only granularity would
     likely give it away on its own, but a colour change is unambiguous even
     at a glance, before a viewer has consciously registered the point
     density changing.
   - A different point marker (smaller, hollow, no hover-to-correct).
   Clicking a backfilled point does nothing silently — it surfaces the same
   explanation described in point 6, so the answer to "why can't I correct
   this" is available exactly where the question arises, not only in a
   banner above the chart.
5. **Automatic outlier detection does not run on backfilled points.** A
   flagged point the user cannot act on is worse than an unflagged one — it
   invites a click that goes nowhere. Detection stays scoped to the raw
   `states` segment, where a flag is always actionable.
6. **The moment the graph actually contains backfilled data, it explains
   itself in text — why these points are shown, and why they are not
   correctable — not just where the boundary falls.** Two related but
   distinct facts, both stated: the data exists and is real (an hourly
   average from Home Assistant's own long-term statistics, never purged),
   and it cannot be corrected because there are two independent reasons, not
   one — an average is not a single reading with one true value to restore,
   and even if it were, the original `states` row it came from has already
   been purged, so there is nothing left to point a correction at. Drafted
   wording: *"Showing hourly averages before 19 Aug 2026 — the original
   readings have been purged from this range. These are Home Assistant's own
   long-term statistics, not raw readings, so there is no single original
   value to restore even if one looked wrong — they can be viewed here but
   not corrected."* Shown once as a banner above the chart when any point in
   the current view is backfilled, and repeated (short form) in the tooltip
   of each backfilled point itself.

## What this does *not* do

- **No correction of a purged reading.** The raw value is gone; reconstructing
  it from an hourly mean and writing a fabricated `states` row would
  contradict the entire audit model this app rests on (a correction always
  replaces a real, previously-recorded value).
- **No use of `statistics_short_term` as a third tier.** It purges on
  essentially the same schedule as `states`, so it very rarely has data that
  `statistics` doesn't already cover at hourly resolution, for meaningfully
  more implementation cost.
- **No change to the 24-hour range**, which never reaches the purge boundary
  in practice.

## Implementation sketch (for when this is approved)

- **`hr_models.py`**: `StatePoint` gains an optional field distinguishing an
  exact reading from a backfilled aggregate (e.g. `source: Literal["state",
  "statistics"]`, defaulting to `"state"` so nothing existing has to change).
  Pure module, no behaviour change to existing callers.
- **Each adapter's `fetch_states`** (or a new method layered on top of it —
  to be decided during implementation, since `fetch_states` is also called
  from the correction path where this backfill is never wanted) gains the
  backfill step described above, calling the adapter's own existing
  `fetch_statistics`.
- **`hr_web.py`'s `/api/entities/<id>/states`**: skips outlier detection for
  backfilled points; the response already carries per-point data, so this is
  a filter on which points get considered, not a new endpoint shape.
- **`entity.js`**: a second point style for `source: "statistics"`, the
  boundary notice, and disabling click-to-correct for that style.
- **Testing**: `hr_fakes.FakeAdapter` needs seeded statistics separate from
  its seeded states (it already supports both, per `hr_statistics.py`'s
  verified arithmetic), so a test can seed states only for a recent window
  and statistics further back, then assert the backfill kicks in exactly at
  the boundary — a case none of the existing fixtures currently express.
  Unit tests for the merge/boundary logic (pure), plus a browser test
  confirming the visual distinction and the disabled click behaviour.

## Resolved during review

- Backfilled points get a distinct **line colour**, not only a distinct point
  marker — deliberately not relying on the coarser granularity alone to
  signal the difference.
- The explanation is **two separate facts, both stated**: the data is real
  and shown because it exists (Home Assistant's own long-term statistics),
  and it cannot be corrected for two independent reasons (an average has no
  single true value to restore; the source `states` row is gone regardless).
  This appears as a banner the moment any point in view is backfilled, and
  again in that point's own tooltip.
- Scoped to the hourly `statistics` table only — `statistics_short_term` is
  not used, per the reasoning above.
