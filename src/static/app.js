'use strict';

function text(id, value) {
  const node = document.getElementById(id);
  if (node) node.textContent = value == null ? '' : String(value);
}

function formatDate(seconds) {
  return seconds ? new Date(seconds * 1000).toLocaleString() : 'not yet';
}

function updateUsbStatus(s) {
  const view = s.target_view || {};
  const badge = document.getElementById('target-badge');
  if (badge) {
    badge.textContent = view.label || 'Unknown';
    badge.className = 'status-badge status-' + (view.code || 'unknown');
  }
  text('target-message', view.message || 'USB state unavailable.');
  text('usb-state', view.state || 'unknown');
  text('usb-speed', view.speed || 'UNKNOWN');
  text('usb-function', view.function || 'unknown');
  text('usb-image', view.backing_image || 'unknown');
  text('queued-state', s.queued ? 'Yes' : 'No');
  text('last-sync', formatDate(s.last_sync));
  text('status', (s.phase || 'starting') + (s.queued ? ' - changes queued' : '') +
       (s.usb_control_pending ? ' - USB control queued' : ''));
  text('error', s.error || (view.read_error ? 'Live USB status read failed: ' + view.read_error : ''));
}

async function refreshStatus() {
  try {
    const response = await fetch('/status', {credentials: 'same-origin'});
    if (!response.ok || response.redirected) return;
    updateUsbStatus(await response.json());
  } catch (_) {
    /* A temporary network disconnect should not replace the existing status. */
  }
}

function initDropZone() {
  const zone = document.getElementById('drop-zone');
  const input = document.getElementById('upload-files');
  const selection = document.getElementById('drop-selection');
  if (!zone || !input || !selection) return;

  function describe() {
    const files = Array.from(input.files || []);
    if (!files.length) {
      selection.textContent = 'No files selected';
    } else if (files.length === 1) {
      selection.textContent = files[0].name;
    } else {
      selection.textContent = files.length + ' files selected';
    }
  }

  zone.addEventListener('click', () => input.click());
  zone.addEventListener('keydown', event => {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      input.click();
    }
  });
  input.addEventListener('change', describe);

  ['dragenter', 'dragover'].forEach(name => zone.addEventListener(name, event => {
    event.preventDefault();
    event.stopPropagation();
    zone.classList.add('drag-active');
    if (event.dataTransfer) event.dataTransfer.dropEffect = 'copy';
  }));
  ['dragleave', 'drop'].forEach(name => zone.addEventListener(name, event => {
    event.preventDefault();
    event.stopPropagation();
    zone.classList.remove('drag-active');
  }));
  zone.addEventListener('drop', event => {
    if (!event.dataTransfer || !event.dataTransfer.files.length) return;
    const transfer = new DataTransfer();
    for (const file of event.dataTransfer.files) transfer.items.add(file);
    input.files = transfer.files;
    describe();
  });
}

initDropZone();
refreshStatus();
setInterval(refreshStatus, 3000);
