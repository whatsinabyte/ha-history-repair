/* Corrections audit manager: the full trail, with restore. */

(() => {
  const rows = document.getElementById('rows');
  const hideRestored = document.getElementById('hide-restored');
  const orphanedBanner = document.getElementById('orphaned-banner');
  const exportCsv = document.getElementById('export-csv');

  // Same reasoning as the entities page's filter/search: remembered per tab,
  // so navigating away via the topbar (Entities) and back (Corrections)
  // does not silently reset it to the default every time.
  try {
    hideRestored.checked = sessionStorage.getItem('hr_hide_restored') === '1';
  } catch (err) {
    /* Defaults to unchecked, same as before this existed. */
  }

  function updateExportLink() {
    const query = hideRestored.checked ? '?include_restored=0' : '';
    exportCsv.href = HC.url(`api/corrections/export.csv${query}`);
  }

  const QUALITY_LABELS = {
    bad_comm: 'Bad communication',
    out_of_range: 'Out of range',
    spike: 'Spike',
    frozen: 'Frozen',
    uncertain: 'Uncertain',
  };

  function render(corrections) {
    if (!corrections.length) {
      rows.innerHTML = '<tr><td colspan="7" class="muted">No corrections yet.</td></tr>';
      return;
    }
    rows.innerHTML = corrections.map((c) => {
      const restored = Boolean(c.restored_at);
      const href = HC.url(`entity/${encodeURIComponent(c.entity_id)}`);
      const action = restored
        ? `<span class="muted">Restored ${HC.escape(HC.formatIsoTime(c.restored_at))}</span>`
        : `<button type="button" class="danger" data-restore="${c.id}">Restore</button>`;
      const note = c.note ? `<div class="muted">${HC.escape(c.note)}</div>` : '';
      return `<tr${restored ? ' class="muted"' : ''}>
        <td data-label="Entity"><a href="${href}" class="mono">${HC.escape(c.entity_id)}</a></td>
        <td data-label="Recorded at">${HC.escape(HC.formatTime(c.state_ts))}</td>
        <td class="mono" data-label="Original">${HC.escape(c.original_value)}</td>
        <td class="mono" data-label="Corrected to">${HC.escape(c.corrected_value)}</td>
        <td data-label="Reason">${HC.escape(QUALITY_LABELS[c.quality] || c.quality)}${note}</td>
        <td data-label="By">${HC.escape(c.created_by)}</td>
        <td>${action}</td>
      </tr>`;
    }).join('');
  }

  function renderOrphaned(corrections) {
    if (!corrections.length) {
      orphanedBanner.innerHTML = '';
      return;
    }
    const items = corrections.map((c) => {
      const href = HC.url(`entity/${encodeURIComponent(c.entity_id)}`);
      return `<li>
        <a href="${href}" class="mono">${HC.escape(c.entity_id)}</a> —
        this app wrote <span class="mono">${HC.escape(c.corrected_value)}</span>,
        but the recorder now holds something else, likely from a restored backup.
        <button type="button" class="danger" data-dismiss="${c.id}">Dismiss</button>
        or open the entity above to re-apply the correction yourself.
      </li>`;
    }).join('');
    orphanedBanner.innerHTML = `<div class="notice warn">
      <strong>${corrections.length} correction(s) no longer match the database.</strong>
      A Home Assistant backup was likely restored after these were made. Nothing
      is changed automatically — dismiss each one to accept the current state value,
      or re-apply the correction from the entity page.
      <ul style="margin: 8px 0 0; padding-left: 20px;">${items}</ul>
    </div>`;
  }

  async function loadOrphaned() {
    try {
      const data = await HC.get('api/corrections/orphaned');
      renderOrphaned(data.corrections);
    } catch {
      // Non-critical: the main corrections table below still loads and is
      // more informative than a banner failure would be.
    }
  }

  async function load() {
    rows.innerHTML = '<tr><td colspan="7" class="muted">Loading…</td></tr>';
    const query = hideRestored.checked ? '?include_restored=0' : '';
    updateExportLink();
    try {
      const data = await HC.get(`api/corrections${query}`);
      render(data.corrections);
    } catch (err) {
      rows.innerHTML = `<tr><td colspan="7"><div class="notice error">${HC.escape(err.message)}</div></td></tr>`;
    }
    loadOrphaned();
  }

  rows.addEventListener('click', async (event) => {
    const button = event.target.closest('[data-restore]');
    if (!button) return;
    button.disabled = true;
    try {
      await HC.post(`api/corrections/${button.dataset.restore}/restore`);
      HC.toast('Original state value restored.', 'ok');
      await load();
    } catch (err) {
      HC.toast(err.message, 'error');
      button.disabled = false;
    }
  });

  orphanedBanner.addEventListener('click', async (event) => {
    const button = event.target.closest('[data-dismiss]');
    if (!button) return;
    button.disabled = true;
    try {
      await HC.post(`api/corrections/${button.dataset.dismiss}/dismiss`);
      HC.toast('Dismissed.', 'ok');
      await loadOrphaned();
    } catch (err) {
      HC.toast(err.message, 'error');
      button.disabled = false;
    }
  });

  hideRestored.addEventListener('change', () => {
    try {
      sessionStorage.setItem('hr_hide_restored', hideRestored.checked ? '1' : '0');
    } catch (err) {
      /* Best-effort only — this tab's own toggle still works either way. */
    }
    load();
  });

  load();
})();
