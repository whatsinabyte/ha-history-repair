/* Shared helpers. Ingress serves the add-on under a token-prefixed path, so
   every request is built relative to HR_BASE rather than the domain root. */

const HC = {
  url(path) {
    const base = window.HR_BASE || '/';
    return base.replace(/\/$/, '') + '/' + String(path).replace(/^\//, '');
  },

  async request(path, options = {}) {
    const response = await fetch(HC.url(path), {
      headers: { 'Content-Type': 'application/json' },
      ...options,
    });
    let payload = null;
    try {
      payload = await response.json();
    } catch (err) {
      payload = null;
    }
    if (!response.ok) {
      const message = (payload && payload.error) || `Request failed (${response.status})`;
      const error = new Error(message);
      error.kind = payload && payload.kind;
      error.status = response.status;
      error.payload = payload;
      throw error;
    }
    return payload;
  },

  get(path) {
    return HC.request(path);
  },

  post(path, body) {
    return HC.request(path, { method: 'POST', body: JSON.stringify(body || {}) });
  },

  /* `duration` defaults from `kind` (an error gets longer to read), but can
     be given explicitly for a neutral message that is simply longer than the
     default 4s comfortably allows — without borrowing "error" styling's red
     border for something that is not actually an error. */
  toast(message, kind = '', duration = null) {
    const el = document.getElementById('toast');
    if (!el) return;
    el.textContent = message;
    el.className = `toast ${kind}`.trim();
    el.hidden = false;
    clearTimeout(HC._toastTimer);
    const ms = duration !== null ? duration : (kind === 'error' ? 8000 : 4000);
    HC._toastTimer = setTimeout(() => { el.hidden = true; }, ms);
  },

  /* Timestamps come from the recorder as float epoch seconds.

     Deliberately not `.toLocaleString()` with no arguments: that resolves
     against whatever the browser or WebView's own default locale happens to
     be, which is not reliably the viewer's actual OS/regional preference —
     confirmed in practice inside Home Assistant's own Ingress panel, which
     rendered US-style 12-hour "8/26/2026, 9:19:36 PM" for someone whose
     system was set to 24-hour "28 Aug 2026, 21:38:36". Explicit options and a
     fixed locale (en-GB, which already defaults to day-month-year) gives one
     deterministic, correct-everywhere date format instead; 12 vs 24-hour is
     the one part of this a viewer might genuinely want either way, so that
     part alone comes from the time_format add-on option (window.HR_TIME_FORMAT,
     "12" or "24") rather than being fixed too. */
  formatTime(ts) {
    if (ts === null || ts === undefined) return '—';
    return new Intl.DateTimeFormat('en-GB', {
      day: 'numeric',
      month: 'short',
      year: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
      hour12: window.HR_TIME_FORMAT === '12',
    }).format(new Date(ts * 1000));
  },

  /* Same deterministic reasoning as formatTime, without the time-of-day
     part — for labels that only need to orient the viewer to a date. */
  formatDate(ts) {
    if (ts === null || ts === undefined) return '—';
    return new Intl.DateTimeFormat('en-GB', {
      day: 'numeric',
      month: 'short',
      year: 'numeric',
    }).format(new Date(ts * 1000));
  },

  /* `created_at`/`restored_at`/`dismissed_at` come from the recorder as
     `.isoformat()` strings, not epoch seconds like state_ts — and the three
     database adapters do not even agree on shape: SQLite's own
     `datetime.now(timezone.utc).isoformat()` carries a "+00:00" offset, but
     MariaDB/PostgreSQL hand back a naive driver-level datetime with no
     offset marker at all ("2026-08-29T13:06:09.453177"). `new Date()` on a
     string with no offset is parsed as the *browser's* local time, not UTC —
     silently shifting a genuinely UTC timestamp by the viewer's own offset
     and printing it unconverted alongside it, which is exactly the
     "definitely not the local one" symptom a viewer would notice on the
     Corrections page. Appending 'Z' only when no offset is already present
     forces the naive case to be read as UTC too, and formatTime's own
     Intl.DateTimeFormat is left to convert it correctly from there. */
  formatIsoTime(iso) {
    if (!iso) return '—';
    const hasOffset = /[Zz]|[+-]\d\d:\d\d$/.test(iso);
    const ms = Date.parse(hasOffset ? iso : `${iso}Z`);
    return Number.isNaN(ms) ? '—' : HC.formatTime(ms / 1000);
  },

  escape(value) {
    return String(value === null || value === undefined ? '' : value)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  },
};
