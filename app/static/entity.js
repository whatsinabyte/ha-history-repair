/* History viewer and correction panel.

   The graph plots raw states rows. Already-corrected points are drawn larger
   and in the accent colour so a user can see at a glance what has been
   changed, and clicking any point opens the correction panel for it. */

(() => {
  const entity = window.HR_ENTITY;
  const correctable = window.HR_CORRECTABLE;

  const canvas = document.getElementById('chart');
  const rangeSelect = document.getElementById('range');
  const rangePrev = document.getElementById('range-prev');
  const rangeNext = document.getElementById('range-next');
  const rangeWindow = document.getElementById('range-window');
  const sensitivitySlider = document.getElementById('sensitivity');
  const sensitivityValue = document.getElementById('sensitivity-value');
  const pointCount = document.getElementById('point-count');
  const truncationNotice = document.getElementById('truncation-notice');
  const backfillNotice = document.getElementById('backfill-notice');
  const emptyNotice = document.getElementById('empty-notice');
  const chartLoading = document.getElementById('chart-loading');
  const isCounter = window.HR_IS_COUNTER;
  const panel = document.getElementById('panel');

  const chartWrap = document.getElementById('chart-wrap');
  const selectionOverlay = document.getElementById('selection-overlay');
  const bulkToggle = document.getElementById('bulk-toggle');
  const chartHint = document.getElementById('chart-hint');
  const bulkPanel = document.getElementById('bulk-panel');

  const bulkFields = {
    start: document.getElementById('bulk-start'),
    end: document.getElementById('bulk-end'),
    total: document.getElementById('bulk-total'),
    skipRow: document.getElementById('bulk-skip-row'),
    skip: document.getElementById('bulk-skip'),
    form: document.getElementById('bulk-form'),
    strategy: document.getElementById('bulk-strategy'),
    valueField: document.getElementById('bulk-value-field'),
    value: document.getElementById('bulk-value'),
    interpolateNotice: document.getElementById('bulk-interpolate-notice'),
    quality: document.getElementById('bulk-quality'),
    note: document.getElementById('bulk-note'),
    cascadeWarning: document.getElementById('bulk-cascade-warning'),
    cascadeAckRow: document.getElementById('bulk-cascade-ack-row'),
    cascadeAck: document.getElementById('bulk-cascade-ack'),
    save: document.getElementById('bulk-save'),
  };

  const fields = {
    time: document.getElementById('p-time'),
    value: document.getElementById('p-value'),
    orig: document.getElementById('p-orig'),
    origRow: document.getElementById('p-orig-row'),
    corrected: document.getElementById('p-corrected'),
    form: document.getElementById('p-form'),
    newValue: document.getElementById('p-new'),
    quality: document.getElementById('p-quality'),
    note: document.getElementById('p-note'),
    save: document.getElementById('p-save'),
    restore: document.getElementById('p-restore'),
    currentWarning: document.getElementById('p-current-warning'),
    cascadeWarning: document.getElementById('p-cascade-warning'),
    cascadeAckRow: document.getElementById('p-cascade-ack-row'),
    cascadeAck: document.getElementById('p-cascade-ack'),
  };

  let chart = null;
  let points = [];
  let selected = null;
  let bulkMode = false;
  let dragStartX = null;
  let bulkRange = null; /* { startTs, endTs } once a drag completes */

  const style = getComputedStyle(document.body);
  const accent = style.getPropertyValue('--accent').trim() || '#03a9f4';
  const textDim = style.getPropertyValue('--text-dim').trim() || '#727272';
  const border = style.getPropertyValue('--border').trim() || '#e0e0e0';
  const danger = style.getPropertyValue('--danger').trim() || '#db4437';
  const is12Hour = window.HR_TIME_FORMAT === '12';

  /* A point backfilled from Home Assistant's long-term statistics rather
     than read from states directly — see long-term-statistics-graph-design.md
     and hr_statistics.backfill_from_statistics. Deliberately signalled two
     ways at once rather than one: the coarser, hourly-only spacing would
     likely give it away on its own, but a colour change (textDim, applied to
     both the line segment and the point marker below) is unambiguous even
     before a viewer has consciously registered the point density changing. */
  function isBackfilled(raw) {
    return Boolean(raw && raw.source === 'statistics');
  }

  const BACKFILL_EXPLANATION =
    'This is an hourly average from Home Assistant long-term statistics, '
    + 'not a single reading — the original state row has already been purged, '
    + 'and an average has no one true value to restore even if it looked wrong. '
    + 'It can be viewed here but not corrected.';

  /* A corrected point is shown as a hollow accent-coloured dot regardless of
     whether it would also be flagged as a candidate — it has already been
     dealt with. An un-corrected candidate is drawn larger and in red, so a
     user scanning the graph finds it before reading any tooltip. A
     backfilled point is never corrected or a candidate (the backend never
     marks one as either), so those checks are safe to skip for it. */
  function pointStyle(raw) {
    if (isBackfilled(raw)) {
      return { radius: 2, background: '#ffffff', border: textDim, borderWidth: 1 };
    }
    if (raw && raw.corrected) {
      return { radius: 5, background: '#ffffff', border: accent, borderWidth: 2 };
    }
    if (raw && raw.candidate) {
      return { radius: 6, background: danger, border: danger, borderWidth: 1 };
    }
    return { radius: 2, background: accent, border: accent, borderWidth: 1 };
  }

  function buildChart() {
    chart = new Chart(canvas.getContext('2d'), {
      type: 'line',
      data: {
        datasets: [{
          label: entity.friendly_name || entity.entity_id,
          data: [],
          borderColor: accent,
          backgroundColor: accent,
          borderWidth: 1.5,
          tension: 0,
          spanGaps: true,
          // A line segment sits between two points; if either end is
          // backfilled, the segment connecting into it is drawn in the same
          // muted colour as the point itself, not the normal accent — the
          // colour change has to reach the line, not only the dots, to be
          // unambiguous at a glance.
          segment: {
            borderColor: (ctx) =>
              (isBackfilled(ctx.p0.raw) || isBackfilled(ctx.p1.raw)) ? textDim : accent,
          },
          pointRadius: (ctx) => pointStyle(ctx.raw).radius,
          pointHoverRadius: 8,
          pointBackgroundColor: (ctx) => pointStyle(ctx.raw).background,
          pointBorderColor: (ctx) => pointStyle(ctx.raw).border,
          pointBorderWidth: (ctx) => pointStyle(ctx.raw).borderWidth,
        }],
      },
      options: {
        maintainAspectRatio: false,
        parsing: false,
        normalized: true,
        // Only for Chart.js's own y-axis tick number formatting (thousand
        // separators etc.) — the date/time axis below deliberately does NOT
        // depend on this. See the comment there for why.
        locale: navigator.language,
        interaction: { mode: 'nearest', intersect: false },
        onClick: (event, elements) => {
          if (bulkMode || !elements.length) return;
          const point = points[elements[0].index];
          if (point.source === 'statistics') {
            HC.toast(BACKFILL_EXPLANATION, '', 8000);
            return;
          }
          select(point);
        },
        scales: {
          x: {
            type: 'time',
            time: {
              // Fixed literal tokens, not locale-aware macros: relying on the
              // ambient locale (whether via luxon's default or an explicit
              // `locale` option sourced from navigator.language) was tried
              // and rendered US-style 12-hour dates inside Home Assistant's
              // own Ingress panel for a viewer whose system was actually set
              // to 24-hour, day-month-year — the browser/WebView's own
              // default locale is not a reliable stand-in for the viewer's
              // real preference. The date part stays fixed; 12 vs 24-hour is
              // the one part a viewer might genuinely want either way, so
              // that alone comes from the time_format add-on option.
              tooltipFormat: is12Hour ? 'd MMM yyyy, h:mm:ss a' : 'd MMM yyyy, HH:mm:ss',
              // Every other date this add-on shows — the tooltip above,
              // HC.formatDate/formatTime, the correction panel — always
              // includes the year. The day/week axis ticks on a 7- or
              // 30-day graph did not ('d MMM' — "28 Aug" with no year), the
              // one place in the app a date appeared in a visibly different,
              // shorter shape than everywhere else.
              displayFormats: {
                hour: is12Hour ? 'h:mm a' : 'HH:mm',
                day: 'd MMM yyyy',
                week: 'd MMM yyyy',
                month: 'MMM yyyy',
              },
            },
            ticks: {
              color: textDim,
              maxRotation: 0,
              autoSkip: true,
            },
            grid: { color: border },
          },
          y: {
            title: entity.unit ? { display: true, text: entity.unit, color: textDim } : undefined,
            ticks: { color: textDim },
            grid: { color: border },
          },
        },
        plugins: {
          legend: { display: false },
          tooltip: {
            callbacks: {
              label: (ctx) => {
                const raw = ctx.raw;
                const unit = entity.unit ? ` ${entity.unit}` : '';
                if (isBackfilled(raw)) {
                  return [`Hourly average: ${raw.y}${unit}`, BACKFILL_EXPLANATION];
                }
                if (raw.corrected) {
                  return [
                    `Corrected: ${raw.y}${unit}`,
                    `Original: ${raw.original}${unit}`,
                  ];
                }
                if (raw.candidate) {
                  return [`${raw.y}${unit}`, 'Flagged as a possible outlier'];
                }
                return `${raw.y}${unit}`;
              },
            },
          },
        },
      },
    });
  }

  /* The right-hand edge of the visible window, in seconds. Paging with the
     ← / → buttons moves this back and forward by the current range's width;
     changing the range or letting new data arrive resets it to "now". */
  let windowEnd = Date.now() / 1000;

  function updateNavState() {
    const seconds = Number(rangeSelect.value);
    const start = windowEnd - seconds;
    const now = Date.now() / 1000;
    // A slack window rather than an exact equality check: "now" keeps moving
    // while the page sits open, so an exact match would drift stale within
    // seconds of loading and wrongly re-enable "later" on an unchanged view.
    // 30s, not 1s: windowEnd is captured once at click time, but this runs
    // again after an async fetch resolves — a slow request (or a loaded CI
    // runner) can easily put more than a second between the two, which
    // flipped this comparison and left "later" wrongly enabled under load
    // even though the view had not actually changed.
    rangeNext.disabled = windowEnd >= now - 30;
    rangeWindow.textContent = `${HC.formatDate(start)} – ${HC.formatDate(windowEnd)}`;
  }

  async function load() {
    const seconds = Number(rangeSelect.value);
    const end = windowEnd;
    const start = end - seconds;
    const threshold = Number(sensitivitySlider.value);
    updateNavState();
    pointCount.textContent = 'Loading…';

    /* The chart itself does not clear while new data loads, so switching
       period (prev/next, the range dropdown) with a fast connection could
       otherwise look like nothing happened until the new data suddenly
       appears — this overlay makes "a request is in flight" impossible to
       miss, and the nav controls are disabled with it so a second click
       during that window cannot fire an overlapping request. */
    chartLoading.hidden = false;
    rangePrev.disabled = true;
    rangeNext.disabled = true;
    rangeSelect.disabled = true;

    try {
      const data = await HC.get(
        `api/entities/${encodeURIComponent(entity.entity_id)}/states`
        + `?start=${start}&end=${end}&threshold=${threshold}`
      );
      /* Non-numeric states ('unknown', 'unavailable') cannot be plotted, but
         they are still real rows a user may want to correct — they are kept
         in `points` and drawn as gaps. */
      points = data.points;
      chart.data.datasets[0].data = points.map((p) => ({
        x: p.ts * 1000,
        y: p.numeric_value,
        corrected: Boolean(p.correction_id),
        original: p.original_value,
        candidate: Boolean(p.is_candidate),
        source: p.source,
      }));
      chart.update();

      const plottable = points.filter((p) => p.numeric_value !== null).length;
      const nonNumeric = points.length - plottable;
      const corrected = points.filter((p) => p.correction_id).length;
      const candidates = points.filter((p) => p.is_candidate).length;
      // A reading of "unknown" or "unavailable" (a dropout, an integration
      // restart) has no number to plot and is shown as a gap in the line
      // rather than a point — real, not a bug, but "19974 of 20000 points
      // plotted" alone does not say why the other 26 are missing.
      pointCount.textContent = `${plottable} of ${points.length} points plotted`
        + (nonNumeric ? ` (${nonNumeric} non-numeric, shown as gaps)` : '')
        + (candidates ? ` · ${candidates} flagged` : '')
        + (corrected ? ` · ${corrected} corrected` : '');

      /* An empty canvas is indistinguishable from a broken page. This happens
         for real whenever a sensor stopped reporting longer ago than the
         selected range, or once the recorder has purged that far back. */
      emptyNotice.hidden = points.length > 0;

      /* This range held more readings than one request returns, so the graph
         shows only its earliest part. Saying so matters more here than in a
         normal chart: a user hunting an outlier could otherwise conclude the
         later, unshown period was clean. */
      truncationNotice.hidden = !data.truncated;
      if (data.truncated) {
        truncationNotice.textContent =
          `Showing the earliest ${data.limit.toLocaleString()} readings of this range — `
          + 'there are more. Choose a shorter range to see the rest.';
      }

      /* Shown the moment any point in the current view was backfilled from
         long-term statistics (see long-term-statistics-graph-design.md) —
         not only on hover of one, so the "why" is available before a user
         has even found one, not only after clicking it and being told no. */
      const backfilled = points.filter((p) => p.source === 'statistics').length;
      backfillNotice.hidden = backfilled === 0;
      if (backfilled > 0) {
        const boundary = points.find((p) => p.source === 'state');
        const boundaryText = boundary ? ` before ${HC.formatDate(boundary.ts)}` : '';
        backfillNotice.textContent =
          `Showing hourly averages${boundaryText} — the original readings have `
          + 'been purged from this range. ' + BACKFILL_EXPLANATION;
      }
    } catch (err) {
      pointCount.textContent = '';
      HC.toast(err.message, 'error');
    } finally {
      chartLoading.hidden = true;
      rangeSelect.disabled = false;
      // updateNavState() already disables range-next at the latest window —
      // re-enabling it unconditionally here would undo that.
      updateNavState();
      rangePrev.disabled = false;
    }
  }

  /* The single source of truth for whether Save may be clicked, computed
     fresh from all three inputs every time rather than toggled ad hoc from
     whichever one just changed. */
  function syncCascadeGate() {
    fields.save.disabled = isCounter && !fields.form.hidden && !fields.cascadeAck.checked;
  }

  function select(point) {
    if (!point) return;
    selected = point;

    fields.time.textContent = HC.formatTime(point.ts);
    fields.value.textContent = point.value === null ? '—' : point.value;

    const isCorrected = Boolean(point.correction_id);
    fields.origRow.hidden = !isCorrected;
    fields.orig.textContent = point.original_value || '';
    fields.corrected.hidden = !isCorrected;

    /* A corrected point must be restored before it can be corrected again:
       re-correcting would otherwise record the previous correction as the
       "original" value and lose the real one. */
    fields.form.hidden = isCorrected || !correctable;

    fields.newValue.value = point.numeric_value === null ? '' : point.numeric_value;
    fields.note.value = '';
    fields.currentWarning.hidden = point !== points[points.length - 1];

    /* A counter correction rewrites every running total after this point, so
       the user is shown the scope and has to acknowledge it before saving —
       an energy reading from months ago can reach through years of totals. */
    fields.cascadeWarning.hidden = true;
    fields.cascadeAckRow.hidden = !isCounter || fields.form.hidden;
    fields.cascadeAck.checked = false;
    syncCascadeGate();
    if (isCounter && !fields.form.hidden) {
      HC.get(`api/entities/${encodeURIComponent(entity.entity_id)}/cascade-scope?state_ts=${point.ts}`)
        .then((data) => {
          const rows = data.scope.short_term + data.scope.hourly;
          fields.cascadeWarning.textContent =
            `This will rewrite ${rows.toLocaleString()} statistics rows — `
            + `${data.scope.hourly.toLocaleString()} hourly and `
            + `${data.scope.short_term.toLocaleString()} five-minute — because every `
            + 'running total after this reading depends on it.';
          fields.cascadeWarning.hidden = false;
        })
        .catch(() => { /* The correction is still allowed; only the preview failed. */ });
    }

    panel.hidden = false;
    if (!fields.form.hidden) fields.newValue.focus();
  }

  fields.cascadeAck.addEventListener('change', syncCascadeGate);

  fields.form.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (!selected) return;
    // Belt and braces alongside fields.save.disabled: a disabled submit
    // button happens to block a text field's Enter-key implicit submission
    // in the browsers this was checked against, but that is the browser's
    // own choice to make, not a guarantee this code can rely on across every
    // engine Home Assistant's Ingress iframe might run inside — and the
    // cascade scope being seen and acknowledged before a counter correction
    // proceeds is a hard requirement, not only a UX nicety.
    if (isCounter && !fields.form.hidden && !fields.cascadeAck.checked) return;
    fields.save.disabled = true;
    try {
      await HC.post('api/corrections', {
        entity_id: entity.entity_id,
        state_id: selected.state_id,
        expected_original: selected.value,
        new_value: fields.newValue.value,
        quality: fields.quality.value,
        note: fields.note.value,
      });
      HC.toast('Correction saved.', 'ok');
      panel.hidden = true;
      await load();
    } catch (err) {
      HC.toast(err.message, 'error');
    } finally {
      fields.save.disabled = false;
    }
  });

  fields.restore.addEventListener('click', async () => {
    if (!selected || !selected.correction_id) return;
    fields.restore.disabled = true;
    try {
      await HC.post(`api/corrections/${selected.correction_id}/restore`);
      HC.toast('Original state value restored.', 'ok');
      panel.hidden = true;
      await load();
    } catch (err) {
      HC.toast(err.message, 'error');
    } finally {
      fields.restore.disabled = false;
    }
  });

  document.getElementById('panel-close').addEventListener('click', () => {
    panel.hidden = true;
  });

  rangeSelect.addEventListener('change', () => {
    // A new range width starting from something other than "now" would show
    // a window nobody asked for — jump back to the latest data instead.
    windowEnd = Date.now() / 1000;
    load();
  });

  rangePrev.addEventListener('click', () => {
    windowEnd -= Number(rangeSelect.value);
    load();
  });

  rangeNext.addEventListener('click', () => {
    windowEnd = Math.min(windowEnd + Number(rangeSelect.value), Date.now() / 1000);
    load();
  });

  let sensitivityTimer = null;
  sensitivitySlider.addEventListener('input', () => {
    sensitivityValue.textContent = Number(sensitivitySlider.value).toFixed(1);
    /* Debounced rather than reloading on every drag tick — the slider fires
       continuously while dragged, and each tick is a network round trip. */
    clearTimeout(sensitivityTimer);
    sensitivityTimer = setTimeout(load, 200);
  });

  /* ── Bulk range correction ──────────────────────────────────────────
     Drag across the graph to select a range, then fill it either with a
     single value or a straight-line interpolation between the readings
     just outside the range. Every row still gets its own audit record;
     what changes is that they are chosen and reviewed together. */

  function setBulkMode(on) {
    bulkMode = on;
    chartWrap.classList.toggle('bulk-mode', on);
    bulkToggle.textContent = on ? 'Cancel range selection' : 'Correct a range';
    chartHint.textContent = on
      ? 'Drag across the graph to select a range.'
      : 'Click a point on the graph to correct it.';
    if (!on) {
      selectionOverlay.hidden = true;
      dragStartX = null;
    }
  }

  function pixelToTs(clientX) {
    const rect = canvas.getBoundingClientRect();
    const x = clientX - rect.left;
    return chart.scales.x.getValueForPixel(x) / 1000;
  }

  function updateOverlay(x1, x2) {
    const rect = canvas.getBoundingClientRect();
    const left = Math.min(x1, x2) - rect.left;
    const width = Math.abs(x2 - x1);
    selectionOverlay.style.left = `${left}px`;
    selectionOverlay.style.width = `${width}px`;
    selectionOverlay.hidden = false;
  }

  /* A click that barely moves must not be mistaken for a range — it is
     almost certainly someone trying to inspect a point while bulk mode
     happens to still be on. */
  const MIN_DRAG_PIXELS = 6;

  canvas.addEventListener('mousedown', (event) => {
    if (!bulkMode) return;
    dragStartX = event.clientX;
    updateOverlay(event.clientX, event.clientX);
  });

  window.addEventListener('mousemove', (event) => {
    if (!bulkMode || dragStartX === null) return;
    updateOverlay(dragStartX, event.clientX);
  });

  window.addEventListener('mouseup', (event) => {
    if (!bulkMode || dragStartX === null) return;
    const distance = Math.abs(event.clientX - dragStartX);
    const startClientX = dragStartX;
    dragStartX = null;
    if (distance < MIN_DRAG_PIXELS) {
      selectionOverlay.hidden = true;
      return;
    }
    const tsA = pixelToTs(startClientX);
    const tsB = pixelToTs(event.clientX);
    openBulkPanel(Math.min(tsA, tsB), Math.max(tsA, tsB));
  });

  async function openBulkPanel(startTs, endTs) {
    bulkRange = { startTs, endTs };
    bulkFields.start.textContent = HC.formatTime(startTs);
    bulkFields.end.textContent = HC.formatTime(endTs);
    bulkFields.total.textContent = '…';
    bulkFields.skipRow.hidden = true;
    bulkFields.note.value = '';
    bulkFields.value.value = '';
    bulkFields.strategy.value = 'interpolate';
    bulkFields.cascadeWarning.hidden = !isCounter;
    if (isCounter) {
      bulkFields.cascadeWarning.textContent =
        'This is a counter. Correcting this range rewrites the running total '
        + 'for every reading in it, and for every reading after it.';
    }
    bulkFields.cascadeAckRow.hidden = !isCounter;
    bulkFields.cascadeAck.checked = false;
    bulkPanel.hidden = false;

    try {
      const data = await HC.get(
        `api/entities/${encodeURIComponent(entity.entity_id)}/bulk-preview`
        + `?start=${startTs}&end=${endTs}`
      );
      bulkFields.total.textContent = data.preview.total;
      if (data.preview.already_corrected > 0) {
        bulkFields.skipRow.hidden = false;
        bulkFields.skip.textContent = data.preview.already_corrected;
      }
      bulkCanInterpolate = data.preview.can_interpolate;
      updateBulkSaveState();
    } catch (err) {
      bulkFields.total.textContent = '—';
      HC.toast(err.message, 'error');
    }
  }

  let bulkCanInterpolate = true;

  /* Two independent reasons can block saving — interpolation being
     impossible for this range, and a counter's cascade needing explicit
     acknowledgement — so the disabled state has to be recomputed from both
     every time, never overwritten by whichever one last had an event fire. */
  function updateBulkSaveState() {
    const wantsInterpolate = bulkFields.strategy.value === 'interpolate';
    bulkFields.valueField.hidden = wantsInterpolate;
    bulkFields.interpolateNotice.hidden = !wantsInterpolate || bulkCanInterpolate;

    const interpolationBlocked = wantsInterpolate && !bulkCanInterpolate;
    const cascadeUnacknowledged = isCounter && !bulkFields.cascadeAck.checked;
    bulkFields.save.disabled = interpolationBlocked || cascadeUnacknowledged;
  }

  bulkFields.strategy.addEventListener('change', updateBulkSaveState);
  bulkFields.cascadeAck.addEventListener('change', updateBulkSaveState);

  bulkFields.form.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (!bulkRange) return;
    if (isCounter && !bulkFields.cascadeAck.checked) return;

    bulkFields.save.disabled = true;
    try {
      const payload = {
        start: bulkRange.startTs,
        end: bulkRange.endTs,
        strategy: bulkFields.strategy.value,
        quality: bulkFields.quality.value,
        note: bulkFields.note.value,
      };
      if (bulkFields.strategy.value === 'constant') {
        payload.value = bulkFields.value.value;
      }
      const data = await HC.post(
        `api/entities/${encodeURIComponent(entity.entity_id)}/bulk-correction`,
        payload
      );
      HC.toast(`Corrected ${data.result.applied} readings.`, 'ok');
      bulkPanel.hidden = true;
      setBulkMode(false);
      await load();
    } catch (err) {
      HC.toast(err.message, 'error');
    } finally {
      bulkFields.save.disabled = false;
    }
  });

  document.getElementById('bulk-panel-close').addEventListener('click', () => {
    bulkPanel.hidden = true;
  });

  if (bulkToggle) {
    bulkToggle.addEventListener('click', () => setBulkMode(!bulkMode));
  }

  // The entities page passes its current page/search filter as an explicit
  // ?from= parameter on the link to this page (see entities.js) — not
  // document.referrer or browser history, because Home Assistant's Ingress
  // panel renders this add-on inside an iframe with a "no-referrer" policy,
  // which leaves referrer empty and history.back() with nothing usable to
  // return to. The plain href stays as the fallback for a bookmarked or
  // directly-opened entity page, which has no prior page to return to at all.
  const backLink = document.getElementById('back-to-entities');
  const fromQuery = new URLSearchParams(window.location.search).get('from');
  if (backLink && fromQuery) {
    backLink.href = `${HC.url('')}?${fromQuery}`;
  }

  buildChart();
  load();
})();
