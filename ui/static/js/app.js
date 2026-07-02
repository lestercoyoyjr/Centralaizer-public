function toast(msg, isError = false) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.style.background = isError ? '#7f1d1d' : '#1a1a18';
  t.style.opacity = '1';
  setTimeout(() => t.style.opacity = '0', 2800);
}

async function api(method, path, body = null) {
  const opts = { method, headers: { 'Content-Type': 'application/json' } };
  if (body) opts.body = JSON.stringify(body);
  const r = await fetch(path, opts);
  const data = await r.json();
  if (!r.ok) throw new Error(data.detail || 'Error');
  return data;
}

async function approveEntry(id, rowEl) {
  try {
    await api('POST', `/api/quarantine/${id}/approve`);
    rowEl.style.opacity = '0';
    setTimeout(() => rowEl.remove(), 300);
    toast('Memory approved and stored ✓');
    refreshStats();
  } catch(e) { toast(e.message, true); }
}

async function rejectEntry(id, rowEl) {
  try {
    await api('POST', `/api/quarantine/${id}/reject`);
    rowEl.style.opacity = '0';
    setTimeout(() => rowEl.remove(), 300);
    toast('Memory rejected');
    refreshStats();
  } catch(e) { toast(e.message, true); }
}

// Shared detail modal — clicking any row (memory / quarantine / skill) opens it
// with the FULL content and metadata. Clicks on a row's buttons/links are ignored.
function openDetail(title, badgeText, badgeClass, content, metaPairs) {
  document.getElementById('md-title').textContent = title;
  const typeEl = document.getElementById('md-type');
  typeEl.textContent = badgeText || '';
  typeEl.className = badgeText ? 'badge ' + (badgeClass || '') : '';
  document.getElementById('md-content').textContent = content || '';
  document.getElementById('md-meta').innerHTML = metaPairs
    .filter(([, v]) => v !== undefined && v !== '' && v !== 'None')
    .map(([k, v]) => `<dt>${k}</dt><dd>${escapeHtml(v)}</dd>`)
    .join('');
  document.getElementById('memory-modal').classList.add('open');
}
function rowClicked(event) { return event.target.closest('button') || event.target.closest('a'); }

function showMemoryDetail(event, tr) {
  if (rowClicked(event)) return;
  const d = tr.dataset;
  const meta = [['Agent', d.agent], ['Owner', d.owner], ['Trust', d.trust],
                ['Decay', d.decay], ['Accessed', d.accessed], ['Memory ID', d.id]];
  if (d.score) meta.push(['Search score', d.score]);
  if (d.matched) meta.push(['Matched via', d.matched]);
  openDetail('Memory', d.type, 'badge-' + (d.type || ''), d.content, meta);
}

function showQuarantineDetail(event, tr) {
  if (rowClicked(event)) return;
  const d = tr.dataset;
  openDetail('Quarantined write', '', '', d.content, [
    ['Agent', d.agent], ['Trust score', d.trust], ['Reason', d.reason],
    ['Submitted', d.date], ['Entry ID', d.id],
  ]);
}

function showSkillDetail(event, tr) {
  if (rowClicked(event)) return;
  const d = tr.dataset;
  openDetail('Skill: ' + d.name, d.level, 'badge-' + (d.level || ''), d.template || d.description, [
    ['Description', d.description], ['Level', d.level],
    ['Uses', d.uses], ['Successes', d.successes],
    ['Created', d.created], ['Updated', d.updated],
  ]);
}
function closeMemoryModal() { document.getElementById('memory-modal').classList.remove('open'); }
function closeListModal() { document.getElementById('list-modal').classList.remove('open'); }

async function showAgents() {
  try {
    const data = await api('GET', '/api/agents');
    document.getElementById('lm-title').textContent = 'Active agents';
    document.getElementById('lm-body').innerHTML = data.length
      ? '<table class="lm-table"><tr><th>Agent</th><th>Memories</th><th>Avg trust</th></tr>'
        + data.map(a => `<tr><td>${escapeHtml(a.agent_id)}</td><td>${a.count}</td><td>${a.avg_trust}</td></tr>`).join('')
        + '</table>'
      : '<p class="empty-sm">No agents have written memories yet.</p>';
    document.getElementById('list-modal').classList.add('open');
  } catch (e) { toast(e.message, true); }
}

async function showGraphEdges() {
  try {
    const data = await api('GET', '/api/graph');
    document.getElementById('lm-title').textContent = 'Graph edges';
    document.getElementById('lm-body').innerHTML = data.length
      ? '<table class="lm-table"><tr><th>From</th><th></th><th>To</th><th>Type</th><th>Weight</th></tr>'
        + data.map(e => `<tr><td>${escapeHtml(e.src)}…</td><td>→</td><td>${escapeHtml(e.dst)}…</td>`
            + `<td><span class="badge badge-${e.edge_type}">${e.edge_type}</span></td><td>${e.weight}</td></tr>`).join('')
        + '</table>'
      : '<p class="empty-sm">No graph edges yet.</p>';
    document.getElementById('list-modal').classList.add('open');
  } catch (e) { toast(e.message, true); }
}
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

async function deleteMemory(id, rowEl) {
  if (!confirm('Delete this memory? This cannot be undone.')) return;
  try {
    await api('DELETE', `/api/memories/${id}`);
    rowEl.style.opacity = '0';
    setTimeout(() => rowEl.remove(), 300);
    toast('Memory deleted');
    refreshStats();
  } catch(e) { toast(e.message, true); }
}

async function runDecay() {
  try {
    const d = await api('POST', '/api/manager/decay');
    document.getElementById('lm-title').textContent = 'Decay run';
    let html = `<p class="empty-sm">Scanned <b>${d.scanned}</b> memories · archived <b>${d.archived}</b>`
      + ` (score decayed below 0.05). Half-life is <b>${d.half_life_days} days</b>; a memory must sit`
      + ` untouched for months before it's archived.</p>`;
    if (d.lowest && d.lowest.length) {
      html += '<table class="lm-table"><tr><th>Closest to archival</th><th>Decay</th><th>Last access</th></tr>'
        + d.lowest.map(m => `<tr><td>${escapeHtml(m.content)}…</td><td>${m.decayed_score.toFixed(3)}</td>`
            + `<td>${(m.accessed_at || '').slice(0,10)}</td></tr>`).join('') + '</table>';
    }
    document.getElementById('lm-body').innerHTML = html;
    document.getElementById('list-modal').classList.add('open');
    refreshStats();
  } catch(e) { toast(e.message, true); }
}

async function runPromote() {
  try {
    const d = await api('POST', '/api/manager/promote');
    document.getElementById('lm-title').textContent = 'Promote skills';
    let html = `<p class="empty-sm">Promoted <b>${d.promoted}</b> skill(s). A skill goes draft→active at`
      + ` <b>${d.threshold}</b> successes, active→crystallized at <b>${d.threshold * 3}</b>.</p>`;
    if (d.skills && d.skills.length) {
      html += '<table class="lm-table"><tr><th>Skill</th><th>Level</th><th>Successes</th></tr>'
        + d.skills.map(s => `<tr><td>${escapeHtml(s.name)}</td>`
            + `<td><span class="badge badge-${s.level}">${s.level}</span></td><td>${s.success_count}</td></tr>`).join('')
        + '</table>';
    } else {
      html += '<p class="empty-sm">No skills yet — add one or let agents promote procedural memories.</p>';
    }
    document.getElementById('lm-body').innerHTML = html;
    document.getElementById('list-modal').classList.add('open');
  } catch(e) { toast(e.message, true); }
}

async function refreshStats() {
  try {
    const s = await api('GET', '/api/stats');
    for (const [k, v] of Object.entries(s)) {
      const el = document.querySelector(`[data-stat="${k}"]`);
      if (el) el.textContent = v;
    }
  } catch(_) {}
}

// ── theme toggle (light/dark) ─────────────────────────────────────────
function applyThemeLabel() {
  const dark = document.documentElement.getAttribute('data-theme') === 'dark';
  const btn = document.getElementById('theme-toggle');
  if (btn) {
    btn.textContent = dark ? '☀️' : '🌙';
    btn.title = dark ? 'Switch to light theme' : 'Switch to dark theme';
  }
}
function toggleTheme() {
  const next = document.documentElement.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
  document.documentElement.setAttribute('data-theme', next);
  try { localStorage.setItem('czr-theme', next); } catch (e) {}
  applyThemeLabel();
}
document.addEventListener('DOMContentLoaded', applyThemeLabel);

function openWriteModal() { document.getElementById('write-modal').classList.add('open'); }
function closeWriteModal() { document.getElementById('write-modal').classList.remove('open'); }

async function submitWrite(e) {
  e.preventDefault();
  const form = e.target;
  const body = {
    agent_id: form.agent_id.value || 'ui-user',
    content: form.content.value,
    memory_type: form.memory_type.value,
    owner: form.owner.value || 'shared',
  };
  try {
    const r = await api('POST', '/api/memories', body);
    closeWriteModal();
    toast(`Memory ${r.status} (trust: ${r.trust?.toFixed(2)})`);
    form.reset();
    setTimeout(() => location.reload(), 800);
  } catch(e) { toast(e.message, true); }
}

function openSkillModal() { document.getElementById('skill-modal')?.classList.add('open'); }
function closeSkillModal() { document.getElementById('skill-modal')?.classList.remove('open'); }

async function submitSkill(e) {
  e.preventDefault();
  const form = e.target;
  try {
    await api('POST', '/api/skills', {
      name: form.name.value,
      description: form.description.value,
      template: form.template.value,
    });
    closeSkillModal();
    toast('Skill created ✓');
    setTimeout(() => location.reload(), 800);
  } catch(e) { toast(e.message, true); }
}

document.addEventListener('click', e => {
  if (e.target.classList.contains('modal-bg')) e.target.classList.remove('open');
});
document.addEventListener('keydown', e => {
  if (e.key === 'Escape')
    document.querySelectorAll('.modal-bg.open').forEach(m => m.classList.remove('open'));
});
