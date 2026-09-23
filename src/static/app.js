'use strict';
async function refreshStatus() {
  try {
    const response = await fetch('/status', {credentials: 'same-origin'});
    if (!response.ok || response.redirected) return;
    const s = await response.json();
    document.getElementById('status').textContent = (s.phase || 'starting') + (s.queued ? ' - changes queued' : '') + (s.last_sync ? ' - last sync: ' + new Date(s.last_sync * 1000).toLocaleString() : '');
    document.getElementById('error').textContent = s.error || '';
  } catch (_) { /* A temporary disconnect should not replace the file list. */ }
}
setInterval(refreshStatus, 5000);
