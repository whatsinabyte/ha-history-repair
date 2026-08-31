/* Entity browser: a searchable, paged list of everything the recorder has
   history for, with the sensor type the correction rules depend on. */

(() => {
  const PAGE_SIZE = 50;

  const rows = document.getElementById('entity-rows');
  const countLabel = document.getElementById('entity-count');
  const pageInfo = document.getElementById('page-info');
  const search = document.getElementById('search');
  const typeFilter = document.getElementById('type-filter');
  const prev = document.getElementById('prev');
  const next = document.getElementById('next');
  const sortableHeaders = document.querySelectorAll('th.sortable');

  // A sensible default direction per column: alphabetical for the name, but
  // "most recent"/"most corrected" first for the other two — nobody paging
  // through entities wants the ones untouched since forever, or with no
  // corrections at all, presented first.
  const DEFAULT_SORT_DIR = { entity_id: 'asc', last_updated: 'desc', corrections: 'desc' };

  // Restored from the URL on load, so following a link to an entity and then
  // clicking "Back to entities" returns to the page and filter left behind,
  // rather than always resetting to the first page. A bare "/" — the topbar
  // nav link, which carries no query of its own — falls back to the last
  // filter remembered in sessionStorage instead, so switching to Corrections
  // and back via the nav bar (not an entity link) does not lose it either.
  // An explicit URL always wins over the remembered one when both exist.
  let initialQuery = window.location.search.slice(1);
  if (!initialQuery) {
    try {
      initialQuery = sessionStorage.getItem('hr_entities_filters') || '';
    } catch (err) {
      initialQuery = '';
    }
  }
  const initialParams = new URLSearchParams(initialQuery);
  let offset = Number(initialParams.get('offset')) || 0;
  let sort = initialParams.get('sort') || 'entity_id';
  let sortDir = initialParams.get('dir') || DEFAULT_SORT_DIR[sort] || 'asc';
  let total = 0;
  let searchTimer = null;

  if (initialParams.get('search')) {
    search.value = initialParams.get('search');
  }
  if (initialParams.get('type')) {
    typeFilter.value = initialParams.get('type');
  }

  function updateSortIndicators() {
    sortableHeaders.forEach((th) => {
      th.classList.remove('sort-asc', 'sort-desc');
      if (th.dataset.sort === sort) {
        th.classList.add(sortDir === 'desc' ? 'sort-desc' : 'sort-asc');
      }
    });
  }

  function currentQuery() {
    const params = new URLSearchParams();
    if (offset) params.set('offset', offset);
    if (search.value.trim()) params.set('search', search.value.trim());
    if (typeFilter.value) params.set('type', typeFilter.value);
    if (sort !== 'entity_id') params.set('sort', sort);
    if (sortDir !== (DEFAULT_SORT_DIR[sort] || 'asc')) params.set('dir', sortDir);
    return params.toString();
  }

  function syncUrl() {
    const query = currentQuery();
    const url = query ? `?${query}` : window.location.pathname;
    // replaceState, not pushState: paging and typing a search should not
    // each add a browser-history entry of their own.
    window.history.replaceState(null, '', url);
    try {
      sessionStorage.setItem('hr_entities_filters', query);
    } catch (err) {
      /* Best-effort only — the URL itself is still this page load's source
         of truth either way, this only affects a future bare-"/" visit. */
    }
  }

  function typePill(type) {
    const label = { measurement: 'measurement', counter: 'counter' }[type] || 'unknown';
    return `<span class="pill ${HC.escape(type)}">${HC.escape(label)}</span>`;
  }

  function render(entities) {
    if (!entities.length) {
      rows.innerHTML = '<tr><td colspan="5" class="muted">No entities match.</td></tr>';
      return;
    }
    // Carried as an explicit URL parameter rather than left to the browser's
    // referrer or history: Home Assistant's Ingress panel renders this add-on
    // inside an iframe, which gets a "no-referrer" policy, so document.referrer
    // is empty and history.back() has nothing reliable to go back to.
    const query = currentQuery();
    const fromParam = query ? `?from=${encodeURIComponent(query)}` : '';
    rows.innerHTML = entities.map((entity) => {
      const name = entity.friendly_name
        ? `${HC.escape(entity.friendly_name)}<div class="muted mono">${HC.escape(entity.entity_id)}</div>`
        : `<span class="mono">${HC.escape(entity.entity_id)}</span>`;
      const value = entity.last_value === null || entity.last_value === undefined
        ? '—'
        : `${HC.escape(entity.last_value)}${entity.unit ? ' ' + HC.escape(entity.unit) : ''}`;
      const corrections = entity.correction_count
        ? `<span class="pill corrected">${entity.correction_count}</span>`
        : '<span class="muted">—</span>';
      const href = HC.url(`entity/${encodeURIComponent(entity.entity_id)}`) + fromParam;
      // data-label feeds a CSS ::before on narrow screens (see app.css), where
      // the table stops looking like a table and each row stacks into a card
      // instead — the label is what tells you which value is which once the
      // column headers are no longer visible above them.
      return `<tr>
        <td data-label="Entity"><a href="${href}">${name}</a></td>
        <td data-label="Type">${typePill(entity.sensor_type)}</td>
        <td class="mono" data-label="Last state value">${value}</td>
        <td class="muted col-updated" data-label="Last updated">${HC.escape(HC.formatTime(entity.last_updated_ts))}</td>
        <td data-label="Corrections">${corrections}</td>
      </tr>`;
    }).join('');
  }

  async function load() {
    syncUrl();
    updateSortIndicators();
    rows.innerHTML = '<tr><td colspan="5" class="muted">Loading…</td></tr>';
    const params = new URLSearchParams({ limit: PAGE_SIZE, offset, sort, dir: sortDir });
    if (search.value.trim()) params.set('search', search.value.trim());
    if (typeFilter.value) params.set('type', typeFilter.value);
    try {
      const data = await HC.get(`api/entities?${params}`);
      total = data.total;
      render(data.entities);
      countLabel.textContent = `${total} entit${total === 1 ? 'y' : 'ies'} with recorded history`;
      const shown = Math.min(offset + PAGE_SIZE, total);
      pageInfo.textContent = total ? `${offset + 1}–${shown} of ${total}` : '';
      prev.disabled = offset === 0;
      next.disabled = offset + PAGE_SIZE >= total;
    } catch (err) {
      rows.innerHTML = `<tr><td colspan="5"><div class="notice error">${HC.escape(err.message)}</div></td></tr>`;
      countLabel.textContent = '';
    }
  }

  search.addEventListener('input', () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => { offset = 0; load(); }, 250);
  });

  typeFilter.addEventListener('change', () => {
    offset = 0;
    load();
  });

  sortableHeaders.forEach((th) => {
    th.addEventListener('click', () => {
      const key = th.dataset.sort;
      if (sort === key) {
        sortDir = sortDir === 'asc' ? 'desc' : 'asc';
      } else {
        sort = key;
        sortDir = DEFAULT_SORT_DIR[key] || 'asc';
      }
      offset = 0;
      load();
    });
  });

  prev.addEventListener('click', () => {
    offset = Math.max(0, offset - PAGE_SIZE);
    load();
  });

  next.addEventListener('click', () => {
    offset += PAGE_SIZE;
    load();
  });

  load();
})();
