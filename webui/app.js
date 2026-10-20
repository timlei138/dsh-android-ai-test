/* 用例执行台前端 —— 原生 JS，无框架无构建。
 *
 * 三个视图：
 *   dashboard  总览：各状态数量、每个用例最近一次结果、最近运行
 *   runs       运行记录：历次运行列表（这是"历史不再覆盖"的直接体现）
 *   run/<id>   单次详情：断言 / 账本时间轴 / 截图证据 / 自动化率 / 原始日志
 *
 * 数据全部来自 webui.py 的 /api/*，字段名和 reporting/store.py 的列一一对应。 */

const $ = (s, r = document) => r.querySelector(s);
const main = $('#main');
let STATE = { view: 'dashboard', case: '' };

/* ---------- 小工具 ---------- */
const esc = (s) => String(s == null ? '' : s)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  .replace(/"/g, '&quot;');

const badge = (st) => `<span class="badge ${esc(st || 'UNKNOWN')}">${esc(st || '—')}</span>`;

const fmtDur = (s) => {
  if (s == null) return '—';
  s = Number(s);
  if (!isFinite(s)) return '—';
  return s >= 60 ? `${(s / 60).toFixed(1)} 分` : `${s.toFixed(1)} 秒`;
};

const fmtMs = (ms) => ms == null ? '' : `${Math.round(Number(ms))}ms`;

/* 截图 URL：按 run_id + 动作序号取，不拼文件路径。
 *
 * 早先是把库里存的绝对路径尾巴拼成 /evidence/xxx —— 结果立刻踩坑：
 * 截图会落在 tmp/shots 和 assets/storage/evidence 两处，
 * 而两边尾巴都可能长成 step05/shot.png，服务端分不清该读哪个，只能 403。
 * 现在服务端直接按 run_id+seq 去库里查真实路径，唯一来源是数据库。 */
function shotUrl(runId, seq, which) {
  return `/shot/${runId}/${seq}/${which}`;
}

async function api(path) {
  const r = await fetch(path);
  const j = await r.json().catch(() => ({ error: '返回的不是 JSON' }));
  if (!r.ok) throw new Error(j.error || `HTTP ${r.status}`);
  return j;
}

/* ---------- 侧栏 ---------- */
async function renderSidebar() {
  try {
    const s = await api('/api/stats');
    const box = $('#case-nav');
    if (!s.cases || !s.cases.length) {
      box.innerHTML = '';
      return;
    }
    box.innerHTML = s.cases.map((c) => {
      const cls = c.last_status || 'UNKNOWN';
      return `<a class="nav-item" data-case="${esc(c.case_name)}">
        用例 ${esc(c.case_name)}
        <span class="cnt">${c.n}次 ${badge(cls).replace('badge', 'badge')}</span>
      </a>`;
    }).join('');
  } catch (e) {
    console.warn('侧栏加载失败', e);
  }
}

/* ---------- 视图：总览 ---------- */
async function viewDashboard() {
  const s = await api('/api/stats');
  if (!s.total) {
    main.innerHTML = `<h1>总览</h1>
      <div class="hint">还没有任何运行记录。先跑一个用例：
      <span class="mono">echo '{"case_name":"168"}' | python run_case_json.py</span></div>`;
    return;
  }
  const bs = s.by_status || {};
  const cards = `
    <div class="cards">
      <div class="card"><div class="n">${s.total}</div><div class="l">总运行次数</div></div>
      <div class="card pass"><div class="n">${bs.PASS || 0}</div><div class="l">PASS</div></div>
      <div class="card fail"><div class="n">${bs.FAIL || 0}</div><div class="l">FAIL</div></div>
      <div class="card block"><div class="n">${bs.BLOCKED || 0}</div><div class="l">BLOCKED</div></div>
      <div class="card"><div class="n">${fmtDur(s.avg_elapsed_s)}</div><div class="l">平均耗时</div></div>
    </div>`;

  const caseRows = (s.cases || []).map((c) => `<tr class="clickable" data-case="${esc(c.case_name)}">
      <td class="mono">${esc(c.case_name)}</td>
      <td>${badge(c.last_status)}</td>
      <td>${c.n}</td>
      <td>${esc(c.last_at || '—')}</td>
    </tr>`).join('');

  const recentRows = (s.recent || []).map((r) => `<tr class="clickable" data-run="${r.run_id}">
      <td class="mono">#${r.run_id}</td>
      <td class="mono">${esc(r.case_name)}</td>
      <td>${badge(r.status)}</td>
      <td>${fmtDur(r.elapsed_s)}</td>
      <td>${r.auto_rate == null ? '—' : Math.round(r.auto_rate * 100) + '%'}</td>
      <td>${esc(r.started_at || '')}</td>
    </tr>`).join('');

  main.innerHTML = `<h1>总览</h1>
    <div class="sub">每次运行都会留一条历史，不再互相覆盖。</div>
    ${cards}
    <h2>各用例最近一次</h2>
    <table><thead><tr><th>用例</th><th>最近结果</th><th>运行次数</th><th>最近运行时间</th></tr></thead>
    <tbody>${caseRows || '<tr><td colspan="4" class="empty">无</td></tr>'}</tbody></table>
    <h2>最近运行动态</h2>
    <table><thead><tr><th>#</th><th>用例</th><th>结果</th><th>耗时</th><th>自动化率</th><th>开始时间</th></tr></thead>
    <tbody>${recentRows || '<tr><td colspan="6" class="empty">无</td></tr>'}</tbody></table>`;
}

/* ---------- 视图：运行记录列表 ---------- */
async function viewRuns(caseName) {
  const runs = await api('/api/runs' + (caseName ? `?case=${encodeURIComponent(caseName)}` : ''));
  const title = caseName ? `用例 ${esc(caseName)} 的运行记录` : '全部运行记录';
  const rows = runs.map((r) => `<tr class="clickable" data-run="${r.run_id}">
      <td class="mono">#${r.run_id}</td>
      <td class="mono">${esc(r.case_name)}</td>
      <td>${badge(r.status)}</td>
      <td>${r.steps_done == null ? '—' : r.steps_done}/${r.steps_total == null ? '?' : r.steps_total}</td>
      <td>${r.auto_rate == null ? '—' : Math.round(r.auto_rate * 100) + '%'}</td>
      <td>${fmtDur(r.elapsed_s)}</td>
      <td>${esc(r.started_at || '')}</td>
      <td style="color:var(--dim);font-size:12.5px">${esc((r.summary || '').slice(0, 60))}</td>
    </tr>`).join('');
  main.innerHTML = `<h1>${title}</h1>
    <div class="sub">${runs.length} 次运行</div>
    <table><thead><tr><th>#</th><th>用例</th><th>结果</th><th>步数</th>
      <th>自动化率</th><th>耗时</th><th>开始时间</th><th>摘要</th></tr></thead>
    <tbody>${rows || '<tr><td colspan="8" class="empty">还没有运行</td></tr>'}</tbody></table>`;
}

/* ---------- 视图：单次详情 ---------- */
async function viewRun(runId) {
  const run = await api('/api/run/' + runId);
  const findings = run.findings || [];
  const acts = run.actions || [];

  const pass = findings.filter((f) => f.status === 'PASS').length;
  const fail = findings.filter((f) => f.status === 'FAIL').length;

  /* 断言 */
  const findingsHtml = findings.map((f) => {
    const cls = (f.status || '').toLowerCase() === 'pass' ? 'pass'
      : (f.status || '').toLowerCase() === 'fail' ? 'fail' : 'unknown';
    return `<div class="finding ${cls}">
      <div class="hd">
        <span class="fid">${esc(f.fid || '')}</span>
        <span class="dsc">${esc(f.descr || f.kind || '')}</span>
        ${badge(f.status)}
      </div>
      ${f.actual ? `<div class="res">${esc(f.actual)}</div>` : ''}
    </div>`;
  }).join('');

  /* 自动化率 —— 这是"代码判定 vs 模型判定"的比例，用户最关心的指标之一 */
  const model = run.by_model || 0;
  const code = run.by_code || 0;
  const tot = model + code;
  const rep = JSON.parse(run.report_json || '{}');
  // 模型对话只有开了 LLM_TRACE 才有文件。没开就别给一个点开会 404 的链接 ——
  // "链接点了报错"比"明确说这次没记录"更让人困惑。
  let llmExists = false;
  if (run.llm_path) {
    try {
      llmExists = (await fetch(`/api/file/${run.run_id}/llm`,
        { method: 'HEAD' })).ok;
    } catch (_) { llmExists = false; }
  }
  const reasons = ((rep.strong_stats || {}).fallback_reasons) || [];
  const rateHtml = tot ? `
    <div class="ratebar">
      <div class="code" style="width:${(code / tot) * 100}%"></div>
      <div class="model" style="width:${(model / tot) * 100}%"></div>
    </div>
    <div class="rateleg">
      <span><i style="background:var(--accent)"></i>代码判定 ${code} 步</span>
      <span><i style="background:#f0a43a"></i>模型判定 ${model} 步</span>
      <span>自动化率 ${Math.round((code / tot) * 100)}%</span>
    </div>
    ${reasons.length ? `<ul class="fallbacks">${reasons.map((r) =>
      `<li>${esc(r)}</li>`).join('')}</ul>` : ''}`
    : '<div class="sub">这次没有强证据统计</div>';

  /* 时间轴。账本一行一个动作，含点前点后页面、耗时、是否 changed、截图 */
  const tl = acts.map((a) => {
    const verb = a.action || 'click';
    const verbCls = verb.startsWith('input') ? 'input'
      : verb.startsWith('key') ? 'key' : '';
    const tgt = a.text || a.rid || (a.x != null ? `(${a.x},${a.y})` : '');
    const before = (a.before_act || '').split('.').pop();
    const after = (a.after_act || '').split('.').pop();
    const chg = a.changed === 1 ? '<span class="chg y">有变化</span>'
      : a.changed === 0 ? '<span class="chg n">无变化</span>' : '';
    const sb = a.shot_before ? shotUrl(run.run_id, a.seq, 'before') : '';
    const sa = a.shot_after ? shotUrl(run.run_id, a.seq, 'after') : '';
    const shots = (sb || sa) ? `<div class="shots">
        ${sb ? `<figure style="margin:0"><img src="${sb}" data-cap="动作 #${a.seq} 之前：${esc(before)}" loading="lazy"></figure>` : ''}
        ${sa ? `<figure style="margin:0"><img src="${sa}" data-cap="动作 #${a.seq} 之后：${esc(after)}" loading="lazy"></figure>` : ''}
      </div>` : '';
    return `<div class="act ${a.changed === 0 ? 'nochange' : ''}">
      <span class="seq">${a.seq == null ? '' : '#' + a.seq}</span>
      <div class="body">
        <div class="line1">
          ${a.step ? `<span class="mono" style="color:#9aa4b2">${esc(a.step)}</span>` : ''}
          <span class="verb ${verbCls}">${esc(verb)}</span>
          <span class="tgt">${esc(tgt)}</span>
          ${chg}
          ${before || after ? `<span class="nav">${esc(before || '?')} → ${esc(after || '?')}</span>` : ''}
        </div>
        ${a.why ? `<div class="why">${esc(a.why)}</div>` : ''}
        <div class="meta">
          ${fmtMs(a.ms)}${a.ms_click != null ? ` · 点击 ${fmtMs(a.ms_click)}` : ''}${a.ms_observe != null ? ` · 观察 ${fmtMs(a.ms_observe)}` : ''}
          ${a.perm ? ` · 权限 ${esc(String(a.perm))}` : ''}
          ${a.error ? ` · <span style="color:var(--fail)">${esc(a.error)}</span>` : ''}
        </div>
        ${shots}
      </div>
    </div>`;
  }).join('');

  main.innerHTML = `<h1>运行 #${run.run_id} · 用例 <span class="mono">${esc(run.case_name)}</span></h1>
    <div class="sub">${esc(run.started_at || '')} · 耗时 ${fmtDur(run.elapsed_s)} ·
      引擎 ${esc(run.engine || '—')}</div>
    <div class="cards">
      <div class="card ${(run.status || '').toLowerCase()}"><div class="n" style="font-size:20px">${badge(run.status)}</div><div class="l">运行结论</div></div>
      <div class="card"><div class="n">${run.steps_done == null ? '—' : run.steps_done}${run.steps_total ? '/' + run.steps_total : ''}</div><div class="l">完成步数</div></div>
      <div class="card"><div class="n">${pass}</div><div class="l">断言通过</div></div>
      <div class="card ${fail ? 'fail' : ''}"><div class="n">${fail}</div><div class="l">断言失败</div></div>
      <div class="card"><div class="n">${acts.length}</div><div class="l">动作次数</div></div>
    </div>
    <div class="hint">${esc(run.summary || '')}</div>

    <h2>断言结果</h2>
    <div class="findings">${findingsHtml || '<div class="empty">这次没有断言</div>'}</div>

    <h2>自动化率 / 降级原因</h2>
    <div class="rateleg" style="margin-bottom:6px">
      <a href="/api/file/${run.run_id}/log" target="_blank">完整运行日志</a>
      <a href="/api/file/${run.run_id}/ledger" target="_blank">原始账本 jsonl</a>
      ${run.llm_path && llmExists
        ? `<a href="/api/file/${run.run_id}/llm" target="_blank">模型对话</a>`
        : `<span style="color:var(--dim)">模型对话：本次未记录（LLM_TRACE=0）</span>`}
    </div>
    ${rateHtml}

    <h2>动作时间轴</h2>
    <div class="timeline">${tl || '<div class="empty">没有动作记录</div>'}</div>`;

  // 图片点击放大
  main.querySelectorAll('.shots img').forEach((img) => {
    img.addEventListener('click', () => {
      $('#lightbox-img').src = img.src;
      $('#lightbox-cap').textContent = img.dataset.cap || '';
      $('#lightbox').classList.remove('hidden');
    });
  });
}

/* ---------- 路由 ---------- */
async function route() {
  main.innerHTML = '<div class="empty">加载中…</div>';
  // 侧栏高亮
  document.querySelectorAll('#sidebar .nav-item').forEach((a) => {
    a.classList.toggle('active', a.dataset.view === STATE.view
      && !STATE.case);
  });
  try {
    if (STATE.view === 'run') await viewRun(STATE.runId);
    else if (STATE.view === 'runs') await viewRuns(STATE.case);
    else await viewDashboard();
  } catch (e) {
    main.innerHTML = `<div class="empty">加载失败：${esc(e.message)}</div>`;
  }
}

function go(view, extra = {}) {
  STATE = { view, case: extra.case || '', runId: extra.runId };
  history.replaceState(STATE, '', `#${view}${extra.runId ? '/' + extra.runId : ''}${extra.case ? '?case=' + encodeURIComponent(extra.case) : ''}`);
  route();
}

/* 事件委托：侧栏、表格行 */
document.addEventListener('click', (e) => {
  const nav = e.target.closest('.nav-item');
  if (nav && nav.dataset.view) {
    const v = nav.dataset.view;
    if (v === 'runs') return go('runs');
    return go('dashboard');
  }
  const cnav = e.target.closest('[data-case]');
  if (cnav && !cnav.dataset.run) {
    return go('runs', { case: cnav.dataset.case });
  }
  const row = e.target.closest('tr[data-run]');
  if (row) return go('run', { runId: row.dataset.run });
  if (e.target.id === 'lightbox' || e.target.id === 'lightbox-img') {
    $('#lightbox').classList.add('hidden');
  }
});

document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') $('#lightbox').classList.add('hidden');
});

/* 启动 */
(async () => {
  await renderSidebar();
  go('dashboard');
})();
