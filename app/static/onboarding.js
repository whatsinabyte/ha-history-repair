/* Three-step onboarding: backup acknowledgement, connection test, and where
   to find the add-on afterwards. The finish button unlocks only once the
   first two have passed. */

(() => {
  const ack = document.getElementById('backup-ack');
  const testButton = document.getElementById('test-connection');
  const status = document.getElementById('connection-status');
  const detail = document.getElementById('health-detail');
  const finish = document.getElementById('finish');
  const hint = document.getElementById('finish-hint');

  let healthy = false;

  function refreshFinish() {
    const ready = ack.checked && healthy;
    finish.disabled = !ready;
    hint.textContent = ready
      ? 'Ready.'
      : 'Confirm your backup and test the connection first.';
  }

  function renderHealth(health) {
    const lines = [];
    if (health.server_version) {
      lines.push(`<div class="readout"><span>Database</span><span class="mono">${HC.escape(health.server_version)}</span></div>`);
    }
    if (health.schema_version) {
      lines.push(`<div class="readout"><span>Recorder schema</span><span class="mono">${HC.escape(health.schema_version)}</span></div>`);
    }
    lines.push(`<div class="readout"><span>May write</span><span>${health.can_update ? 'yes' : 'no'}</span></div>`);
    for (const err of health.errors || []) {
      lines.push(`<div class="notice error">${HC.escape(err)}</div>`);
    }
    for (const warning of health.warnings || []) {
      lines.push(`<div class="notice warn">${HC.escape(warning)}</div>`);
    }
    detail.innerHTML = lines.join('');
  }

  testButton.addEventListener('click', async () => {
    testButton.disabled = true;
    status.textContent = 'Testing…';
    try {
      const health = await HC.get('api/health');
      healthy = Boolean(health.ok);
      status.textContent = healthy ? 'Connected.' : 'Checks failed — see below.';
      status.className = healthy ? 'ok' : 'muted';
      renderHealth(health);
    } catch (err) {
      healthy = false;
      status.textContent = err.message;
      detail.innerHTML = `<div class="notice error">${HC.escape(err.message)}</div>`;
    } finally {
      testButton.disabled = false;
      refreshFinish();
    }
  });

  ack.addEventListener('change', refreshFinish);

  document.getElementById('copy-yaml').addEventListener('click', async () => {
    const text = document.getElementById('card-yaml').textContent;
    try {
      await navigator.clipboard.writeText(text);
      HC.toast('YAML copied.', 'ok');
    } catch (err) {
      HC.toast('Copy failed — select the text and copy it manually.', 'error');
    }
  });

  finish.addEventListener('click', async () => {
    finish.disabled = true;
    try {
      await HC.post('api/onboarding', { backup_acknowledged: true });
      window.location.href = HC.url('');
    } catch (err) {
      HC.toast(err.message, 'error');
      finish.disabled = false;
    }
  });

  refreshFinish();
})();
