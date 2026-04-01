/* AI-Caller – Frontend JS */

// ── Socket.IO connection ──────────────────────────────────────────────────────
const socket = io({ transports: ['websocket', 'polling'] });

socket.on('connect', () => {
  console.log('Socket connected');
});

socket.on('status', (data) => {
  updatePhoneStatus(data.active, data.username, data.host);
});

socket.on('new_call', (data) => {
  showToast(`📞 Eingehender Anruf von ${data.caller}`);
});

socket.on('call_ended', (data) => {
  showToast(`Anruf #${data.call_id} beendet (${data.duration}s)`, 'success');
  // Reload page after short delay to show new message
  setTimeout(() => window.location.reload(), 1500);
});

socket.on('new_inbox', (data) => {
  showToast(`Neue Nachricht: ${data.subject}`, 'primary');
  // Update unread badge
  const badge = document.querySelector('.nav-link .badge');
  if (badge) {
    const current = parseInt(badge.textContent) || 0;
    badge.textContent = current + 1;
  }
});

// ── Phone status indicator ────────────────────────────────────────────────────
function updatePhoneStatus(active, username, host) {
  const dot = document.getElementById('phone-status-dot');
  const text = document.getElementById('phone-status-text');
  if (!dot || !text) return;

  dot.className = 'status-dot ' + (active ? 'status-online' : 'status-offline');
  text.textContent = active
    ? `Verbunden (${username}@${host})`
    : 'Nicht verbunden';
}

// ── Toast notifications ───────────────────────────────────────────────────────
function showToast(message, type = 'primary') {
  const toastEl = document.getElementById('live-toast');
  const toastBody = document.getElementById('live-toast-body');
  if (!toastEl || !toastBody) return;

  toastBody.textContent = message;
  toastEl.className = `toast align-items-center text-bg-${type} border-0`;
  const toast = new bootstrap.Toast(toastEl, { delay: 4000 });
  toast.show();
}

// ── Status polling (fallback) ─────────────────────────────────────────────────
setInterval(() => {
  fetch('/api/status')
    .then(r => r.json())
    .then(data => {
      updatePhoneStatus(
        data.phone.active,
        data.phone.username,
        data.phone.host
      );
    })
    .catch(() => {});
}, 15000);

// ── Auto-dismiss alerts ───────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('.alert.alert-success').forEach(el => {
    setTimeout(() => {
      const bsAlert = bootstrap.Alert.getOrCreateInstance(el);
      bsAlert.close();
    }, 4000);
  });
});
