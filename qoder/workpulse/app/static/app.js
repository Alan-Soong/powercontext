/* WorkPulse front-end — dependency free. */
'use strict';

const state = {
  project: localStorage.getItem('wp.project') || '',
  view: 'overview',
  projects: [],
  docTypes: { minutes: '会议纪要', chat: '聊天记录', doc: '文档', task: '任务清单', note: '随手记' },
  lastReport: null,
  search: { mode: 'hybrid', ft: 0.45, q: '' },
  badges: { tasks: '', memory: '' },
};

const STATUS = { todo: '待开始', doing: '进行中', done: '已完成', blocked: '阻塞' };
const VIEW_META = {
  overview: ['概览', '项目状态、最近资料与待办一屏掌握'],
  search: ['混合检索', '一条 SQL 同时用全文、向量和结构化条件，答案永远带出处'],
  docs: ['资料库', '已入库的纪要、聊天与文档，可追溯到每个分块'],
  tasks: ['任务看板', '从资料里抽出来的待办，负责人、截止日、状态一目了然'],
  report: ['周报生成', '按周聚合进展与风险，PowerContext 记忆自动复用'],
  memory: ['长期记忆', 'PowerContext 保存的口径、决策与结论，跨会话复用'],
  evidence: ['技术证据', '建表 DDL、检索 SQL、PowerContext 调用记录，均可核对'],
};

/* ---------- utils ---------- */
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

function esc(value) {
  return String(value == null ? '' : value)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function toast(message, kind = '') {
  const node = document.createElement('div');
  node.className = 'toast ' + kind;
  node.textContent = message;
  $('#toasts').appendChild(node);
  setTimeout(() => { node.style.opacity = '0'; node.style.transition = 'opacity .3s'; }, 3200);
  setTimeout(() => node.remove(), 3600);
}

async function api(path, options = {}) {
  const res = await fetch(path, options);
  const type = res.headers.get('content-type') || '';
  const body = type.includes('application/json') ? await res.json() : await res.text();
  if (!res.ok) {
    const detail = body && body.detail ? body.detail : (typeof body === 'string' ? body : JSON.stringify(body));
    throw new Error(detail || ('HTTP ' + res.status));
  }
  return body;
}

function queryTerms(query) {
  const raw = String(query || '').split(/[\s,，。;；、:：!?！？]+/).filter(Boolean);
  const out = new Set();
  for (const term of raw) {
    out.add(term);
    if (/[\u4e00-\u9fff]/.test(term) && term.length > 3) {
      for (let i = 0; i < term.length - 1; i += 1) out.add(term.slice(i, i + 2));
    }
  }
  return Array.from(out).filter((t) => t.length >= 2).sort((a, b) => b.length - a.length).slice(0, 10);
}

function highlight(text, query) {
  let out = esc(text);
  for (const term of queryTerms(query)) {
    const safe = esc(term);
    out = out.split(safe).join('<mark>' + safe + '</mark>');
  }
  return out;
}

function snippet(text, query, limit = 260) {
  const clean = String(text || '').replace(/\s+/g, ' ').trim();
  if (clean.length <= limit) return clean;
  const parts = clean.split(/(?<=[。！？；;!?])/).filter((s) => s.trim());
  const terms = queryTerms(query);
  let best = 0;
  let bestScore = -1;
  parts.forEach((part, index) => {
    const score = terms.reduce((acc, term) => acc + (part.includes(term) ? 1 : 0), 0);
    if (score > bestScore) { bestScore = score; best = index; }
  });
  let out = parts[best] || clean.slice(0, limit);
  let left = best - 1;
  let right = best + 1;
  while (out.length < limit && (left >= 0 || right < parts.length)) {
    if (right < parts.length && out.length + parts[right].length <= limit) out += parts[right++];
    else if (left >= 0 && out.length + parts[left].length <= limit) out = parts[left--] + out;
    else break;
  }
  return (left >= 0 ? '…' : '') + out + (right < parts.length ? '…' : '');
}

function fmtDate(value) {
  if (!value) return '—';
  return String(value).slice(0, 10);
}

/* ---------- minimal markdown ---------- */
function inline(text) {
  let out = esc(text);
  out = out.replace(/`([^`]+)`/g, '<code>$1</code>');
  out = out.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  out = out.replace(/(^|[^*])\*([^*]+)\*/g, '$1<em>$2</em>');
  out = out.replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" rel="noreferrer">$1</a>');
  return out;
}

function renderMarkdown(md) {
  const lines = String(md || '').replace(/\r\n/g, '\n').split('\n');
  const out = [];
  let listType = null;
  let inTable = false;

  const closeList = () => { if (listType) { out.push('</' + listType + '>'); listType = null; } };
  const closeTable = () => { if (inTable) { out.push('</tbody></table>'); inTable = false; } };

  for (let i = 0; i < lines.length; i += 1) {
    const line = lines[i];
    const trimmed = line.trim();

    if (/^\|/.test(trimmed)) {
      const cells = trimmed.replace(/^\||\|$/g, '').split('|').map((c) => c.trim());
      const isSep = cells.every((c) => /^:?-{2,}:?$/.test(c));
      if (!inTable) {
        closeList();
        out.push('<table><thead><tr>' + cells.map((c) => '<th>' + inline(c) + '</th>').join('') + '</tr></thead><tbody>');
        inTable = true;
        if (isSep) { out[out.length - 1] = '<table><thead><tr>' + cells.map((c) => '<th></th>').join('') + '</tr></thead><tbody>'; }
        continue;
      }
      if (isSep) continue;
      out.push('<tr>' + cells.map((c) => '<td>' + inline(c) + '</td>').join('') + '</tr>');
      continue;
    }
    closeTable();

    if (!trimmed) { closeList(); continue; }
    if (/^-{3,}$/.test(trimmed)) { closeList(); out.push('<hr/>'); continue; }

    const heading = trimmed.match(/^(#{1,4})\s+(.*)$/);
    if (heading) { closeList(); out.push('<h' + heading[1].length + '>' + inline(heading[2]) + '</h' + heading[1].length + '>'); continue; }

    if (/^>\s?/.test(trimmed)) { closeList(); out.push('<blockquote>' + inline(trimmed.replace(/^>\s?/, '')) + '</blockquote>'); continue; }

    const ordered = trimmed.match(/^(\d+)\.\s+(.*)$/);
    const bullet = trimmed.match(/^[-*]\s+(.*)$/);
    if (ordered || bullet) {
      const type = ordered ? 'ol' : 'ul';
      if (listType !== type) { closeList(); out.push('<' + type + '>'); listType = type; }
      out.push('<li>' + inline(ordered ? ordered[2] : bullet[1]) + '</li>');
      continue;
    }
    closeList();
    out.push('<p>' + inline(trimmed) + '</p>');
  }
  closeList(); closeTable();
  return out.join('\n');
}

/* ---------- health / chrome ---------- */
async function refreshHealth() {
  try {
    const health = await api('/api/health');
    const raw = health.seekdb.version || '';
    const match = raw.match(/seekdb[- ]?v?([\d.]+)/i);
    setDot('seekdb', health.seekdb.ok, health.seekdb.ok ? 'v' + (match ? match[1] : 'ok') : 'down');
    setDot('pc', health.powercontext.reachable, health.powercontext.reachable ? 'ready' : 'offline');
    $('#txt-embed').textContent = health.embedding;
  } catch (err) {
    setDot('seekdb', false, 'error');
    setDot('pc', false, 'error');
  }
}

function applyBadges(tasks, memory) {
  if (tasks != null) state.badges.tasks = tasks || '';
  if (memory != null) state.badges.memory = memory || '';
  $('#badge-open-tasks').textContent = state.badges.tasks;
  $('#badge-memory').textContent = state.badges.memory;
}

function setDot(name, ok, label) {
  const dot = $('#dot-' + name);
  const text = $('#txt-' + name);
  if (dot) dot.className = 'dot ' + (ok ? 'ok' : 'bad');
  if (text) text.textContent = label;
}

function setView(view, arg) {
  state.view = view;
  $$('.view').forEach((node) => node.classList.remove('active'));
  const container = $('#view-' + view);
  if (container) container.classList.add('active');
  $$('.nav-item').forEach((node) => node.classList.toggle('active', node.dataset.view === view));
  const meta = VIEW_META[view] || ['', ''];
  $('#view-title').textContent = meta[0];
  $('#view-sub').textContent = meta[1];
  localStorage.setItem('wp.view', view);
  const hash = '#' + view + (arg ? '=' + encodeURIComponent(arg) : '');
  if (location.hash !== hash) history.replaceState(null, '', hash);
  renderView(view, arg);
}

function parseHash() {
  const raw = (location.hash || '').replace(/^#/, '');
  if (!raw) return { view: '', arg: '' };
  const idx = raw.indexOf('=');
  if (idx < 0) return { view: raw, arg: '' };
  return { view: raw.slice(0, idx), arg: decodeURIComponent(raw.slice(idx + 1)) };
}

async function renderView(view, arg) {
  try {
    if (view === 'overview') await renderOverview();
    if (view === 'search') renderSearch(arg);
    if (view === 'docs') await renderDocs();
    if (view === 'tasks') await renderTasks();
    if (view === 'report') renderReportForm(arg);
    if (view === 'memory') await renderMemory();
    if (view === 'evidence') await renderEvidence();
  } catch (err) {
    toast('加载失败：' + err.message, 'err');
  }
}

async function loadProjects() {
  const data = await api('/api/projects');
  state.projects = data.projects || [];
  if (!state.project || !state.projects.includes(state.project)) {
    state.project = state.projects[0] || '';
  }
  const select = $('#project-select');
  select.innerHTML = state.projects.map((p) => '<option value="' + esc(p) + '">' + esc(p) + '</option>').join('')
    + '<option value="__new__">＋ 新建项目…</option>';
  if (state.project) select.value = state.project;
  localStorage.setItem('wp.project', state.project);
}

/* ---------- overview ---------- */
async function renderOverview() {
  const data = await api('/api/overview?project=' + encodeURIComponent(state.project || ''));
  const stats = data.stats || {};
  $('#badge-open-tasks').textContent = (data.tasks || []).filter((t) => t.status === 'todo' || t.status === 'doing').length || '';
  $('#badge-memory').textContent = (data.memory && data.memory.entries ? data.memory.entries.length : 0) || '';
  applyBadges(
    (data.tasks || []).filter((t) => t.status === 'todo' || t.status === 'doing').length,
    (data.memory && data.memory.entries ? data.memory.entries.length : 0)
  );

  const cards = [
    ['资料', stats.documents || 0, '份', '已入库文档'],
    ['分块', stats.indexed_chunks || stats.chunks || 0, '个', 'seekdb 检索单元'],
    ['待办', (data.tasks || []).length, '条', '非已完成',
    ],
    ['检索', stats.searches || 0, '次', '累计混合检索'],
  ];
  const cardsHtml = cards.map((c) => `
    <div class="stat">
      <div class="label">${esc(c[0])}</div>
      <div class="value">${esc(c[1])}<small>${esc(c[2])}</small></div>
      <div class="foot">${esc(c[3])}</div>
    </div>`).join('');

  const docRows = (data.documents || []).map((doc) => `
    <tr>
      <td>${esc(doc.title)}</td>
      <td><span class="pill kind">${esc(state.docTypes[doc.doc_type] || doc.doc_type)}</span></td>
      <td class="mono nowrap">${esc(fmtDate(doc.doc_date))}</td>
      <td class="mono">${esc(doc.chunk_count)}</td>
    </tr>`).join('') || '<tr><td colspan="4" class="empty">还没有资料，点击右上角「导入资料」</td></tr>';

  const openTasks = (data.tasks || []).filter((t) => t.status !== 'done').slice(0, 6);
  const taskHtml = openTasks.map((t) => `
    <div class="task-row">
      <div class="task-main">
        <div class="task-title">${esc(t.title)}</div>
        <div class="task-meta">
          <span class="pill ${esc(t.status)}">${esc(STATUS[t.status] || t.status)}</span>
          <span>${esc(t.owner || '负责人待定')}</span>
          <span class="mono">${esc(t.due_date || '未定截止')}</span>
        </div>
      </div>
    </div>`).join('') || '<div class="empty">暂无待办，导入一份会议纪要试试</div>';

  const memory = data.memory || {};
  const memHtml = (memory.entries || []).map((entry) => `
    <div class="mem-item">
      <div class="head"><span class="pill kind">${esc(entry.kind)}</span><span class="muted mono">v${esc(entry.version || 1)}</span></div>
      <div class="text">${esc(entry.text)}</div>
    </div>`).join('') || `<div class="empty">${memory.available ? '该项目还没有长期记忆' : 'PowerContext 未连接（记忆功能降级）'}</div>`;

  const searches = (data.searches || []).slice(0, 6).map((row) => `
    <tr>
      <td>${esc(row.query)}</td>
      <td class="mono">${esc(row.mode)}</td>
      <td class="mono">${esc(row.hits)}</td>
      <td class="mono">${esc(row.latency_ms)}ms</td>
    </tr>`).join('') || '<tr><td colspan="4" class="empty">还没有检索记录</td></tr>';

  $('#view-overview').innerHTML = `
    <div class="grid stats">${cardsHtml}</div>
    <div class="grid two" style="margin-top:14px">
      <div class="card">
        <div class="card-head"><h2>最近入库</h2><span class="hint">${esc(state.project || '全部项目')}</span>
          <span class="spacer"></span><button class="btn small ghost" data-goto="docs">全部资料</button></div>
        <table><thead><tr><th>标题</th><th>类型</th><th>日期</th><th>分块</th></tr></thead><tbody>${docRows}</tbody></table>
      </div>
      <div class="card">
        <div class="card-head"><h2>进行中的任务</h2><span class="spacer"></span>
          <button class="btn small ghost" data-goto="tasks">任务看板</button></div>
        ${taskHtml}
      </div>
    </div>
    <div class="grid two" style="margin-top:14px">
      <div class="card">
        <div class="card-head"><h2>PowerContext 记忆</h2><span class="hint">跨会话复用</span>
          <span class="spacer"></span><button class="btn small ghost" data-goto="memory">管理</button></div>
        <div class="mem-list">${memHtml}</div>
      </div>
      <div class="card">
        <div class="card-head"><h2>最近检索</h2><span class="hint">seekdb</span>
          <span class="spacer"></span><button class="btn small ghost" data-goto="evidence">证据</button></div>
        <table><thead><tr><th>查询</th><th>模式</th><th>命中</th><th>耗时</th></tr></thead><tbody>${searches}</tbody></table>
      </div>
    </div>`;

  $$('[data-goto]').forEach((btn) => btn.addEventListener('click', () => setView(btn.dataset.goto)));
}

/* ---------- search ---------- */
function renderSearch(arg) {
  if (arg) {
    state.search.q = arg;
  }
  const prior = state.search;
  $('#view-search').innerHTML = `
    <div class="card">
      <form id="search-form">
        <div class="searchbar">
          <input type="text" id="q" placeholder="例：退款超时现在什么进度？谁在负责？" value="${esc(prior.q)}" autocomplete="off" />
          <button class="btn primary" type="submit">检索</button>
        </div>
        <div class="controls">
          <div class="seg" id="mode-seg">
            <button type="button" data-mode="hybrid" class="${prior.mode === 'hybrid' ? 'active' : ''}">混合</button>
            <button type="button" data-mode="fulltext" class="${prior.mode === 'fulltext' ? 'active' : ''}">仅全文</button>
            <button type="button" data-mode="vector" class="${prior.mode === 'vector' ? 'active' : ''}">仅向量</button>
          </div>
          <label class="slider">关键词权重
            <input type="range" id="ftw" min="0" max="1" step="0.05" value="${prior.ft}" />
            <b id="ftw-val">${prior.ft.toFixed(2)}</b>
            <span class="muted">（1 − 该值 = 语义权重）</span>
          </label>
          <label>类型
            <select id="doc-type">
              <option value="">全部</option>
              ${Object.entries(state.docTypes).map(([k, v]) => `<option value="${k}">${v}</option>`).join('')}
            </select>
          </label>
          <label>起始 <input type="date" id="date-from" /></label>
          <label>结束 <input type="date" id="date-to" /></label>
        </div>
      </form>
    </div>
    <div id="search-out"></div>`;

  $('#mode-seg').addEventListener('click', (event) => {
    const btn = event.target.closest('button[data-mode]');
    if (!btn) return;
    state.search.mode = btn.dataset.mode;
    $$('#mode-seg button').forEach((node) => node.classList.toggle('active', node === btn));
  });
  $('#ftw').addEventListener('input', (event) => {
    state.search.ft = Number(event.target.value);
    $('#ftw-val').textContent = state.search.ft.toFixed(2);
  });
  $('#search-form').addEventListener('submit', (event) => {
    event.preventDefault();
    runSearch();
  });
  if (prior.q) runSearch();
}

async function runSearch() {
  const q = $('#q').value.trim();
  if (!q) { toast('请输入查询内容', 'err'); return; }
  state.search.q = q;
  const out = $('#search-out');
  out.innerHTML = '<div class="card"><div class="empty">检索中…</div></div>';
  const params = new URLSearchParams({
    q,
    project: state.project,
    mode: state.search.mode,
    ft_weight: state.search.ft,
    limit: 8,
  });
  const type = $('#doc-type').value;
  if (type) params.set('doc_type', type);
  const from = $('#date-from').value;
  const to = $('#date-to').value;
  if (from) params.set('date_from', from);
  if (to) params.set('date_to', to);

  try {
    const data = await api('/api/search?' + params.toString());
    const answer = data.answer || { text: '', citations: [] };
    const answerHtml = `<div class="answer"><h3>基于原文的回答</h3>${
      esc(answer.text).split('\n').map((line) => '<div class="line">' + highlight(line, '') + '</div>').join('')
    }</div>`;

    const hits = (data.hits || []).map((hit) => {
      const ftPct = Math.min(100, Math.round((hit.ft_score || 0) * 220));
      const vecPct = hit.vec_dist == null ? 0 : Math.min(100, Math.round((1 - hit.vec_dist) * 100));
      const scorePct = Math.min(100, Math.round((hit.score || 0) * 100));
      return `
      <div class="hit">
        <div class="hit-top">
          <span class="title">${esc(hit.title)}</span>
          <span class="pill kind">${esc(state.docTypes[hit.doc_type] || hit.doc_type)}</span>
          <span class="mono">${esc(hit.doc_date)}</span>
          <span class="mono muted">chunk#${esc(hit.chunk_id)}</span>
          <span class="muted">${esc(hit.source_name || '')}</span>
        </div>
        <div class="hit-text">${highlight(snippet(hit.text, data.query), data.query)}</div>
        <div class="scores">
          <span>融合 <b>${scorePct}%</b><span class="bar" style="margin-left:6px"><i style="width:${scorePct}%"></i></span></span>
          <span>全文 <b>${esc((hit.ft_score || 0).toFixed(3))}</b><span class="bar ft" style="margin-left:6px"><i style="width:${ftPct}%"></i></span></span>
          <span>向量 <b>${hit.vec_dist == null ? '—' : esc(hit.vec_dist.toFixed(3))}</b><span class="bar vec" style="margin-left:6px"><i style="width:${vecPct}%"></i></span></span>
        </div>
      </div>`;
    }).join('') || '<div class="card"><div class="empty">没有命中。换个关键词，或先在「资料库」导入更多资料。</div></div>';

    out.innerHTML = `
      <div class="card" style="margin-top:14px">
        <div class="card-head"><h2>检出 ${data.hits.length} 段</h2>
          <span class="hint">${esc(data.embedding_provider)} · ${esc(data.latency_ms)}ms · 权重 全文 ${esc(data.ft_weight)} / 向量 ${esc(data.vec_weight)}</span></div>
        ${answerHtml}
        <div class="hits">${hits}</div>
        <details class="sql"><summary>查看本次检索使用的那一条 SQL</summary><pre class="code wrap">${esc(data.sql)}</pre></details>
      </div>`;
  } catch (err) {
    out.innerHTML = '<div class="card"><div class="empty">检索失败：' + esc(err.message) + '</div></div>';
  }
}

/* ---------- docs ---------- */
async function renderDocs() {
  const data = await api('/api/documents?project=' + encodeURIComponent(state.project || ''));
  const rows = (data.documents || []).map((doc) => `
    <tr>
      <td>${esc(doc.title)}</td>
      <td><span class="pill kind">${esc(state.docTypes[doc.doc_type] || doc.doc_type)}</span></td>
      <td class="mono nowrap">${esc(fmtDate(doc.doc_date))}</td>
      <td>${esc(doc.author || doc.source_name || '—')}</td>
      <td class="mono">${esc(doc.chunk_count)}</td>
      <td><button class="btn small ghost" data-del="${esc(doc.doc_id)}">删除</button></td>
    </tr>`).join('') || '<tr><td colspan="6" class="empty">还没有资料</td></tr>';

  $('#view-docs').innerHTML = `
    <div class="card">
      <div class="card-head"><h2>${esc(state.project || '全部项目')}</h2>
        <span class="hint">共 ${(data.documents || []).length} 份，按日期倒序</span>
        <span class="spacer"></span><button class="btn small" id="btn-ingest-2">导入资料</button></div>
      <table><thead><tr><th>标题</th><th>类型</th><th>日期</th><th>来源/作者</th><th>分块</th><th></th></tr></thead>
      <tbody>${rows}</tbody></table>
    </div>`;
  $('#btn-ingest-2').addEventListener('click', openIngest);
  $$('[data-del]').forEach((btn) => btn.addEventListener('click', async () => {
    if (!confirm('删除这份资料及其全部分块？')) return;
    await api('/api/documents/' + btn.dataset.del, { method: 'DELETE' });
    toast('已删除', 'ok');
    renderDocs();
  }));
}

/* ---------- tasks ---------- */
async function renderTasks() {
  const data = await api('/api/tasks?project=' + encodeURIComponent(state.project || ''));
  const tasks = data.tasks || [];
  const counts = data.counts || {};
  $('#badge-open-tasks').textContent = (counts.todo || 0) + (counts.doing || 0) || '';
  applyBadges((counts.todo || 0) + (counts.doing || 0));

  const groups = ['doing', 'blocked', 'done', 'todo'];
  const sections = groups.map((status) => {
    const items = tasks.filter((t) => t.status === status);
    if (!items.length) return '';
    const rows = items.map((task) => `
      <div class="task-row">
        <div class="task-main">
          <div class="task-title">${esc(task.title)}</div>
          <div class="task-meta">
            <span>${esc(task.owner || '负责人待定')}</span>
            <span class="mono">截止 ${esc(task.due_date || '未定')}</span>
            <span class="mono">${esc(task.priority)}</span>
            ${task.source_doc_id ? `<span class="mono muted">chunk#${esc(task.source_chunk || '-')}</span>` : ''}
          </div>
        </div>
        <div class="task-actions">
          <select data-task="${esc(task.task_id)}">
            ${Object.entries(STATUS).map(([k, v]) => `<option value="${k}" ${k === task.status ? 'selected' : ''}>${v}</option>`).join('')}
          </select>
        </div>
      </div>`).join('');
    return `<div class="card" style="margin-top:14px">
        <div class="card-head"><h2>${STATUS[status]}</h2><span class="hint">${items.length} 条</span></div>
        ${rows}
      </div>`;
  }).join('');

  $('#view-tasks').innerHTML = sections || '<div class="card"><div class="empty">还没有待办。导入会议纪要后会自动抽取。</div></div>';

  $$('select[data-task]').forEach((select) => select.addEventListener('change', async () => {
    try {
      const res = await api('/api/tasks/' + select.dataset.task + '/status', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ status: select.value, memo: '' }),
      });
      toast(res.memory_ok ? '状态已更新，并写入 PowerContext 记忆' : '状态已更新', 'ok');
      renderTasks();
    } catch (err) {
      toast('更新失败：' + err.message, 'err');
    }
  }));
}

/* ---------- report ---------- */
function renderReportForm(arg) {
  const today = new Date().toISOString().slice(0, 10);
  $('#view-report').innerHTML = `
    <div class="card">
      <div class="card-head"><h2>生成周报</h2><span class="hint">按自然周聚合原文、任务与长期记忆</span></div>
      <div class="controls" style="margin-top:0">
        <label>截至 <input type="date" id="week-end" value="${today}" /></label>
        <label>回溯天数 <input type="number" id="week-days" value="7" min="1" max="31" style="width:80px" /></label>
        <button class="btn primary" id="btn-gen">生成周报草稿</button>
        <button class="btn" id="btn-save" disabled>保存并写入记忆</button>
        <span class="muted" id="report-meta"></span>
      </div>
    </div>
    <div id="report-out"></div>`;

  $('#btn-gen').addEventListener('click', async () => {
    $('#btn-gen').disabled = true;
    $('#report-out').innerHTML = '<div class="card" style="margin-top:14px"><div class="empty">正在汇总原文、任务与记忆…</div></div>';
    try {
      const data = await api('/api/report', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          project: state.project,
          week_end: $('#week-end').value,
          days: Number($('#week-days').value) || 7,
          save: false,
        }),
      });
      state.lastReport = data;
      renderReport(data);
      $('#btn-save').disabled = false;
    } catch (err) {
      $('#report-out').innerHTML = '<div class="card"><div class="empty">生成失败：' + esc(err.message) + '</div></div>';
    } finally {
      $('#btn-gen').disabled = false;
    }
  });

  $('#btn-save').addEventListener('click', async () => {
    if (!state.lastReport) return;
    $('#btn-save').disabled = true;
    try {
      const data = await api('/api/report', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          project: state.project,
          week_end: state.lastReport.week_end,
          days: Number($('#week-days').value) || 7,
          save: true,
          remember: true,
        }),
      });
      state.lastReport = data;
      renderReport(data);
      toast('周报已保存，结论已写入 PowerContext', 'ok');
    } catch (err) {
      toast('保存失败：' + err.message, 'err');
      $('#btn-save').disabled = false;
    }
  });

  if (arg === 'auto') {
    setTimeout(() => { const btn = $('#btn-gen'); if (btn) btn.click(); }, 80);
  }
}

function renderReport(data) {
  const memory = data.memory || {};
  const entries = memory.entries || [];
  const memHtml = entries.length ? entries.map((entry) => `
      <div class="mem-item">
        <div class="head"><span class="pill kind">${esc(entry.kind)}</span></div>
        <div class="text">${esc(entry.text)}</div>
      </div>`).join('')
    : `<div class="empty">${memory.available ? '本次没有召回记忆' : 'PowerContext 不可用，报告仅基于原文'}</div>`;

  const citations = (data.citations || []).slice(0, 12).map((c) => `
      <div class="kv"><b class="mono">chunk#${esc(c.chunk_id)}</b>
        <span>${esc(c.doc_title)} · ${esc(c.doc_date)} · ${esc(c.section === 'risk' ? '风险' : '进展')}</span></div>`).join('')
    || '<div class="empty">没有引用</div>';

  $('#report-meta').textContent = `${data.week_start} ~ ${data.week_end} · 资料 ${data.stats.documents} 份 · 引用 ${data.citations.length} 段`;
  $('#report-out').innerHTML = `
    <div class="report-wrap" style="margin-top:14px">
      <div class="card">
        <div class="card-head"><h2>周报草稿</h2>
          <span class="hint">${data.stats.documents} 份资料 · ${data.stats.progress_items} 条进展 · ${data.stats.risks} 条风险</span>
          <span class="spacer"></span>
          <button class="btn small ghost" id="btn-copy">复制 Markdown</button>
        </div>
        <div class="md">${renderMarkdown(data.markdown)}</div>
        ${data.report_id ? `<div class="muted mono" style="margin-top:10px">report_id: ${esc(data.report_id)}</div>` : ''}
      </div>
      <div>
        <div class="card">
          <div class="card-head"><h2>记忆复用</h2><span class="hint">PowerContext</span></div>
          <div class="muted" style="font-size:12px;margin-bottom:8px">
            ${memory.available ? `scope ${esc((memory.scope_id || '').slice(0, 18))}… · 召回状态 ${esc(memory.status)}` : '未连接'}
          </div>
          <div class="mem-list">${memHtml}</div>
        </div>
        <div class="card">
          <div class="card-head"><h2>引用索引</h2><span class="hint">可回查原文</span></div>
          ${citations}
        </div>
      </div>
    </div>`;

  const copy = $('#btn-copy');
  if (copy) copy.addEventListener('click', async () => {
    await navigator.clipboard.writeText(data.markdown);
    toast('已复制 Markdown', 'ok');
  });
}

/* ---------- memory ---------- */
async function renderMemory() {
  const data = await api('/api/memory?project=' + encodeURIComponent(state.project || ''));
  const entries = data.entries || [];
  applyBadges(null, entries.length);
  const list = entries.map((entry) => `
      <div class="mem-item">
        <div class="head"><span class="pill kind">${esc(entry.kind)}</span>
          <span class="muted mono">${esc(entry.state || '')} · v${esc(entry.version || 1)}</span></div>
        <div class="text">${esc(entry.text)}</div>
      </div>`).join('') || `<div class="empty">${data.available ? '该项目还没有记忆，写一条试试' : 'PowerContext 未连接：请先启动 server 并配置 WP_PC_TOKEN'}</div>`;

  $('#view-memory').innerHTML = `
    <div class="grid two">
      <div class="card">
        <div class="card-head"><h2>记忆条目</h2><span class="hint">${esc(state.project)}</span></div>
        <div class="mem-list">${list}</div>
      </div>
      <div>
        <div class="card">
          <div class="card-head"><h2>写入一条记忆</h2></div>
          <form id="mem-form" class="form">
            <label>类型
              <select name="kind">
                <option value="preference">preference · 周报口径/偏好</option>
                <option value="decision">decision · 已确认的决策</option>
                <option value="progress">progress · 项目进度</option>
                <option value="summary">summary · 阶段结论</option>
              </select>
            </label>
            <label>内容<textarea name="text" rows="3" placeholder="例：周报只写三段，每条进展必须带出处"></textarea></label>
            <label>写入原因（可选）<input name="reason" placeholder="例：周会确认的汇报口径" /></label>
            <div class="form-actions"><button class="btn primary" type="submit">写入 PowerContext</button></div>
          </form>
        </div>
        <div class="card">
          <div class="card-head"><h2>召回测试</h2><span class="hint">/v1/context/prepare</span></div>
          <div class="controls" style="margin-top:0">
            <input type="text" id="recall-q" class="sm" style="flex:1" placeholder="例：周报 三段式" />
            <button class="btn" id="btn-recall">召回</button>
          </div>
          <pre class="code wrap" id="recall-out" style="max-height:260px;overflow:auto">${esc(data.recall && data.recall.content ? data.recall.content : '（尚无召回结果）')}</pre>
        </div>
      </div>
    </div>`;

  $('#mem-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const form = event.target;
    try {
      await api('/api/memory', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          project: state.project,
          kind: form.kind.value,
          text: form.text.value,
          reason: form.reason.value,
        }),
      });
      toast('已写入 PowerContext', 'ok');
      renderMemory();
    } catch (err) {
      toast('写入失败：' + err.message, 'err');
    }
  });

  $('#btn-recall').addEventListener('click', async () => {
    const q = $('#recall-q').value.trim();
    if (!q) return;
    const out = $('#recall-out');
    out.textContent = '召回中…';
    try {
      const res = await api('/api/memory/search?project=' + encodeURIComponent(state.project) + '&q=' + encodeURIComponent(q));
      out.textContent = (res.hits || []).map((h) => '[' + (h.kind || 'note') + '] ' + h.text).join('\n') || '（没有命中）';
    } catch (err) {
      out.textContent = '召回失败：' + err.message;
    }
  });
}

/* ---------- evidence ---------- */
async function renderEvidence() {
  const data = await api('/api/evidence');
  const ddl = data.ddl || {};
  const calls = (data.powercontext_calls || []).slice(-12).reverse().map((call) => `
      <tr>
        <td class="mono nowrap">${esc((call.ts || '').slice(11))}</td>
        <td class="mono">${esc(call.op)}</td>
        <td class="mono">${esc(call.path)}</td>
        <td class="mono">${esc(call.status)}</td>
        <td class="mono">${esc(call.latency_ms)}ms</td>
      </tr>`).join('') || '<tr><td colspan="5" class="empty">还没有 PowerContext 调用</td></tr>';

  const searches = (data.search_log || []).map((row) => `
      <tr>
        <td class="mono nowrap">${esc((row.ts || '').slice(5))}</td>
        <td>${esc(row.query)}</td>
        <td class="mono">${esc(row.mode)}</td>
        <td class="mono">${esc(row.hits)}</td>
        <td class="mono">${esc(row.latency_ms)}ms</td>
      </tr>`).join('') || '<tr><td colspan="5" class="empty">还没有检索</td></tr>';

  $('#view-evidence').innerHTML = `
    <div class="card">
      <div class="card-head"><h2>seekdb 建表语句</h2><span class="hint">FULLTEXT(ik) + VECTOR(cosine/HNSW) 同表共存</span></div>
      <pre class="code wrap">${esc(ddl.chunks || '')}</pre>
    </div>
    <div class="card">
      <div class="card-head"><h2>最近一条混合检索 SQL</h2><span class="hint">一条语句内完成 全文 + 向量 + 结构化过滤</span></div>
      <pre class="code wrap">${esc(data.last_search_sql || '（先做一次检索）')}</pre>
    </div>
    <div class="grid two" style="margin-top:14px">
      <div class="card">
        <div class="card-head"><h2>检索日志</h2><span class="hint">search_log 表</span></div>
        <table><thead><tr><th>时间</th><th>查询</th><th>模式</th><th>命中</th><th>耗时</th></tr></thead><tbody>${searches}</tbody></table>
      </div>
      <div class="card">
        <div class="card-head"><h2>PowerContext 调用</h2><span class="hint">logs/powercontext_calls.jsonl</span></div>
        <table><thead><tr><th>时间</th><th>操作</th><th>路径</th><th>状态</th><th>耗时</th></tr></thead><tbody>${calls}</tbody></table>
      </div>
    </div>`;
}

/* ---------- ingest modal ---------- */
function openIngest() {
  $('#ingest-modal').hidden = false;
  $('#ingest-result').innerHTML = '';
  const form = $('#ingest-form');
  form.title.value = '';
  form.text.value = '';
  form.doc_date.value = new Date().toISOString().slice(0, 10);
  if (!state.project) toast('先新建一个项目', 'err');
}

async function submitIngest(event) {
  event.preventDefault();
  const form = event.target;
  const button = form.querySelector('button[type=submit]');
  button.disabled = true;
  try {
    const payload = {
      project: state.project,
      doc_type: form.doc_type.value,
      title: form.title.value.trim(),
      doc_date: form.doc_date.value.trim(),
      source_name: form.source_name.value.trim(),
      text: form.text.value,
      remember: form.remember.checked,
    };
    const data = await api('/api/ingest', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    $('#ingest-result').innerHTML = `
      <div class="card" style="background:#fbfaf7">
        <div class="kv"><b>入库</b><span>${esc(data.chunks)} 个分块 · ${esc(data.latency_ms)}ms · doc_id ${esc(data.doc_id)}</span></div>
        <div class="kv"><b>抽取待办</b><span>${data.tasks.length} 条${data.tasks.length ? '：' + data.tasks.slice(0, 3).map((t) => esc(t.title)).join(' / ') : ''}</span></div>
        <div class="kv"><b>决策</b><span>${data.decisions.length} 条</span></div>
        <div class="kv"><b>记忆写入</b><span>${data.memory_writes.length ? data.memory_writes.map((w) => esc(w.kind) + (w.ok ? ' ✓' : ' ✗')).join('，') : '未写入（PowerContext 不可用或已关闭）'}</span></div>
      </div>`;
    toast('导入完成', 'ok');
    loadProjects().then(() => renderView('docs'));
  } catch (err) {
    toast('导入失败：' + err.message, 'err');
  } finally {
    button.disabled = false;
  }
}

async function submitUpload(event) {
  event.preventDefault();
  const form = event.target;
  const file = form.file.files[0];
  if (!file) return;
  const body = new FormData();
  body.append('project', state.project);
  body.append('doc_type', form.doc_type.value);
  body.append('title', file.name.replace(/\.[^.]+$/, ''));
  body.append('remember', 'true');
  body.append('file', file);
  try {
    const data = await api('/api/ingest/file', { method: 'POST', body });
    toast(`已导入 ${file.name}（${data.chunks} 个分块）`, 'ok');
    loadProjects().then(() => renderView('docs'));
  } catch (err) {
    toast('上传失败：' + err.message, 'err');
  }
}

/* ---------- boot ---------- */
async function boot() {
  refreshHealth();
  await loadProjects();
  const fromHash = parseHash();
  const initial = fromHash.view || localStorage.getItem('wp.view') || 'overview';
  setView(VIEW_META[initial] ? initial : 'overview', fromHash.arg);

  $('#nav').addEventListener('click', (event) => {
    const btn = event.target.closest('.nav-item');
    if (btn) setView(btn.dataset.view);
  });
  $('#project-select').addEventListener('change', (event) => {
    if (event.target.value === '__new__') {
      const name = prompt('新项目名称（例：支付中台改造）');
      if (name && name.trim()) {
        state.project = name.trim();
        localStorage.setItem('wp.project', state.project);
        const option = new Option(state.project, state.project, true, true);
        event.target.add(option, 0);
        event.target.value = state.project;
        toast('已切换到新项目，导入第一份资料即可创建', 'ok');
        renderView(state.view);
      } else {
        event.target.value = state.project || '';
      }
      return;
    }
    state.project = event.target.value;
    localStorage.setItem('wp.project', state.project);
    renderView(state.view);
  });
  $('#btn-refresh').addEventListener('click', () => { refreshHealth(); renderView(state.view); loadProjects(); });
  $('#btn-ingest').addEventListener('click', openIngest);
  $$('[data-close-modal]').forEach((btn) => btn.addEventListener('click', () => { $('#ingest-modal').hidden = true; }));
  $('#ingest-modal').addEventListener('click', (event) => {
    if (event.target.id === 'ingest-modal') $('#ingest-modal').hidden = true;
  });
  $('#ingest-form').addEventListener('submit', submitIngest);
  $('#upload-form').addEventListener('submit', submitUpload);
}

document.addEventListener('DOMContentLoaded', boot);
