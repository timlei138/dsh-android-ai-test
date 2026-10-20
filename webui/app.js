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

/* 把总耗时拆成"模型 / 设备"两段。
 *
 * 为什么要拆：用户在报告里看到"耗时 79 秒"是没法优化的 ——
 * 模型一次 5~20 秒，点击一次才 200 毫秒，这两件事的优化手段完全不同。
 * 拆开后一眼就能看出这条用例到底是"模型太慢"还是"设备太慢"。
 *
 * model_s 是后加的列，老记录是 null —— 那就什么都不显示，
 * 不要用 0 去假装"模型没花时间"（那是假数据，比空着误导性强）。
 */
const fmtSplit = (r) => {
  const total = Number(r.elapsed_s), model = r.model_s == null ? null : Number(r.model_s);
  if (model == null || !isFinite(model) || !isFinite(total) || total <= 0) return '';
  const pct = Math.round(model / total * 100);
  const dev = Math.max(total - model, 0);
  return `（模型 ${fmtDur(model)} / ${pct}%，设备 ${fmtDur(dev)}）`;
};

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

/* ---------- 视图：总览 ----------
 *
 * 布局参照旧 skill 的仪表盘（三段顶栏 + 按用例列），但数据全换成我们自己的：
 *
 *   ① 用例总数 + 整体通过率   ← 注意这里的分母是**用例**，不是运行次数。
 *      旧 skill 混用过这两个口径，同一个"成功率"换个页面数就不一样了。
 *   ② 环形图：各用例最近一次的结论分布
 *   ③ 最近三天每天跑了多少、成多少败多少
 *   ④ 按用例列成功率（由低到高 —— 最该看的排最前）
 *
 * 不引 echarts：旧 skill 带了个 1MB 的 echarts.min.js，我们只用饼图和柱状图，
 * 用 SVG 手画就够，还能少一个要维护的第三方文件。
 */
async function viewDashboard() {
  const [s, cases] = await Promise.all([
    api('/api/stats'),
    api('/api/cases').catch(() => ({ items: [] })),
  ]);
  if (!s.total) {
    main.innerHTML = `<h1>总览</h1>
      <div class="hint">还没有任何运行记录。先跑一个用例：
      <span class="mono">echo '{"case_name":"168"}' | python run_case_json.py</span></div>`;
    return;
  }

  const cs = s.cases || [];
  // ★ 口径：一个用例只算一次，取它最近一次的结论。
  //   用总运行次数当分母会让"反复重跑直到过"看起来像稳定性提升。
  const lastOk = cs.filter((c) => c.last_status === 'PASS').length;
  const lastFail = cs.filter((c) => c.last_status === 'FAIL').length;
  const lastBlocked = cs.filter((c) => c.last_status === 'BLOCKED').length;
  const lastOther = cs.length - lastOk - lastFail - lastBlocked;
  const passPct = cs.length ? Math.round((lastOk / cs.length) * 100) : 0;

  /* 最近三天：缺失的日子补 0，否则"昨天没跑"和"昨天全过"在图上没区别 */
  const dayMap = {};
  (s.days || []).forEach((d) => { dayMap[d.day] = d; });
  const labels = ['今天', '昨天', '前天'];
  const three = [];
  for (let i = 0; i < 3; i++) {
    const dt = new Date(Date.now() - i * 86400000);
    const key = `${dt.getFullYear()}-${String(dt.getMonth() + 1).padStart(2, '0')}-${String(dt.getDate()).padStart(2, '0')}`;
    const r = dayMap[key] || {};
    const tot = r.total || 0;
    three.push({
      label: labels[i], date: key, total: tot,
      pass: r.PASS || 0, fail: r.FAIL || 0, blocked: r.BLOCKED || 0,
      pct: tot ? Math.round((r.PASS || 0) / tot * 100) : null,
    });
  }

  /* 环形图：用 stroke-dasharray 画，不依赖任何图表库 */
  const ring = [
    { k: 'PASS', v: lastOk, c: 'var(--pass)' },
    { k: 'FAIL', v: lastFail, c: 'var(--fail)' },
    { k: 'BLOCKED', v: lastBlocked, c: 'var(--block)' },
    { k: '未跑', v: lastOther, c: 'var(--dim)' },
  ].filter((x) => x.v > 0);
  const R = 54, C = 2 * Math.PI * R;
  let off = 0;
  const arcs = ring.map((x) => {
    const len = cs.length ? (x.v / cs.length) * C : 0;
    const el = `<circle cx="70" cy="70" r="${R}" fill="none" stroke="${x.c}"
      stroke-width="20" stroke-dasharray="${len} ${C - len}"
      stroke-dashoffset="${-off}" transform="rotate(-90 70 70)"></circle>`;
    off += len;
    return el;
  }).join('');
  const ringHtml = ring.length ? `
    <svg viewBox="0 0 140 140" class="ring">
      <circle cx="70" cy="70" r="${R}" fill="none" stroke="var(--line)" stroke-width="20"></circle>
      ${arcs}
      <text x="70" y="66" text-anchor="middle" class="ring-n">${cs.length}</text>
      <text x="70" y="86" text-anchor="middle" class="ring-l">个用例</text>
    </svg>` : '<div class="empty">无用例</div>';

  /* 三天柱：高度按当天总数，里面再按成败分色 */
  const maxDay = Math.max(1, ...three.map((d) => d.total));
  const bars = three.slice().reverse().map((d) => {
    const h = (d.total / maxDay) * 100;
    const seg = (n) => d.total ? (n / d.total) * 100 : 0;
    return `<div class="bar-col">
      <div class="bar-stack" style="height:${d.total ? h : 3}%">
        ${d.total ? `<i style="height:${seg(d.pass)}%;background:var(--pass)"></i>
        <i style="height:${seg(d.fail)}%;background:var(--fail)"></i>
        <i style="height:${seg(d.blocked)}%;background:var(--block)"></i>`
        : '<i style="height:100%;background:var(--line)"></i>'}
      </div>
      <div class="bar-n">${d.total || ''}</div>
      <div class="bar-l">${esc(d.label)}</div>
      <div class="bar-pct">${d.pct == null ? '—' : d.pct + '%'}</div>
    </div>`;
  }).join('');

  /* 按用例：成功率由低到高，最该关注的排最前 */
  const rows = cs.map((c) => {
    // 用例名下能查到几次运行、各是什么结论
    return c;
  }).map((c) => {
    const okPct = c.last_status === 'PASS' ? 100
      : c.last_status ? 0 : null;
    return `<div class="mod-row" data-case="${esc(c.case_name)}">
      <div class="mod-head">
        <span class="mod-name">用例 ${esc(c.case_name)}</span>
        <span class="mod-meta">${c.n} 次运行 · 最近 ${esc(c.last_at || '—')}</span>
        <span class="mod-pct ${okPct === null ? 'none' : c.last_status === 'PASS' ? 'good' : 'bad'}">
          ${badge(c.last_status)}</span>
      </div>
    </div>`;
  }).join('');

  main.innerHTML = `<h1>总览</h1>
    <div class="panel">
      <div class="dash-top">
        <div class="dash-seg seg-count">
          <div class="seg-label">用例总数</div>
          <div class="big-num">${cs.length}</div>
          <div class="big-rate ${passPct >= 80 ? 'good' : passPct >= 50 ? 'mid' : 'bad'}">
            整体通过率 <b>${passPct}%</b></div>
          <div class="seg-note">按用例最近一次结论计，共 ${s.total} 次运行</div>
        </div>
        <div class="dash-seg seg-pie">
          <div class="seg-label">用例整体成功情况</div>
          ${ringHtml}
          <div class="chart-legend">
            ${ring.map((x) => `<span><i style="background:${x.c}"></i>${esc(x.k)} ${x.v}</span>`).join('')}
          </div>
        </div>
        <div class="dash-seg seg-bar">
          <div class="seg-label">最近三天成功 / 失败</div>
          <div class="bars">${bars}</div>
          <div class="chart-legend">
            <span><i style="background:var(--pass)"></i>成功</span>
            <span><i style="background:var(--fail)"></i>失败</span>
            <span><i style="background:var(--block)"></i>阻断</span>
          </div>
        </div>
      </div>
    </div>

    <div class="panel">
      <div class="panel-title">用例情况</div>
      <div class="mod-list">${rows || '<div class="muted">无用例</div>'}</div>
    </div>

    <div class="panel">
      <div class="panel-title">最近运行</div>
      <table><thead><tr><th>#</th><th>用例</th><th>结果</th><th>耗时</th>
        <th>自动化率</th><th>开始时间</th></tr></thead>
      <tbody>${(s.recent || []).slice(0, 8).map((r) => `<tr class="clickable" data-run="${r.run_id}">
        <td class="mono">#${r.run_id}</td>
        <td class="mono">${esc(r.case_name)}</td>
        <td>${badge(r.status)}</td>
        <td>${fmtDur(r.elapsed_s)}</td>
        <td>${r.auto_rate == null ? '—' : Math.round(r.auto_rate * 100) + '%'}</td>
        <td>${esc(r.started_at || '')}</td>
      </tr>`).join('') || '<tr><td colspan="6" class="empty">无</td></tr>'}</tbody></table>
    </div>`;
}

/* ---------- 视图：用例库 ----------
 *
 * 参照旧 skill 的用例库：列出 storage/cases 下的 YAML，点进去看原文。
 *
 * 和旧 skill 的差别（按我们的实际情况改的）：
 *   · 旧 skill 的用例是"数据库里的 case 记录 + 沉淀出来的 YAML"两份
 *   · 我们只有 YAML 一份 —— 用例就是文件，没有第二个真相
 *   · 所以这里直接把文件列出来，每个用例后面带"跑过几次 / 最近结论"，
 *     那部分从 runs 库里按 case_id 对上来
 */
async function viewCases() {
  const [{ items, dir }, s] = await Promise.all([
    api('/api/cases'),
    api('/api/stats').catch(() => ({ cases: [] })),
  ]);
  const byId = {};
  (s.cases || []).forEach((c) => { byId[c.case_name] = c; });

  const rows = items.map((c) => {
    const run = byId[c.case_id] || {};
    return `<tr class="clickable" data-casefile="${esc(c.name)}">
      <td class="mono">${esc(c.case_id || '—')}</td>
      <td>${esc(c.title)}</td>
      <td>${c.valid ? '' : '<span class="badge UNKNOWN">校验用</span>'}</td>
      <td>${run.n ? `<span class="badge ${esc(run.last_status || 'UNKNOWN')}">${esc(run.last_status || '—')}</span>` : '<span class="muted">未跑过</span>'}</td>
      <td>${run.n || 0} 次</td>
      <td class="mono" style="font-size:12px">${(c.size / 1024).toFixed(1)} KB</td>
      <td style="color:var(--dim);font-size:12.5px">${esc(c.mtime)}</td>
    </tr>`;
  }).join('');

  main.innerHTML = `<h1>用例库</h1>
    <div class="sub">共 ${items.length} 个用例文件 · <span class="mono">${esc(dir)}</span></div>
    <table><thead><tr><th>编号</th><th>用例</th><th></th><th>最近结论</th>
      <th>跑过</th><th>大小</th><th>修改时间</th></tr></thead>
    <tbody>${rows || '<tr><td colspan="7" class="empty">cases 目录里没有 YAML</td></tr>'}</tbody></table>`;
}

/* 单个用例文件：YAML 原文，只读
 *
 * ★ 为什么只读：用例 YAML 是"判据的唯一真相"，跑出来的结论都拿它对照。
 *   在网页上随手改一改，旧记录就会变成"新判据配旧结论"，等于自欺。 */
async function viewCaseFile(name) {
  const c = await api('/api/case/' + encodeURIComponent(name));
  const lines = c.content.split('\n');
  const body = lines.map((ln, i) => {
    const n = String(i + 1).padStart(4, ' ');
    // 极简高亮：注释、键、列表符。不做完整 YAML 语法解析 —— 那要引入高亮库，
    // 而我们看的是"哪一行写了什么"，够用了。
    let html = esc(ln);
    if (/^\s*#/.test(ln)) html = `<span class="c-cm">${html}</span>`;
    else html = html
      .replace(/^(\s*)(- )/, '$1<span class="c-ds">$2</span>')
      .replace(/^(\s*)([\w\u4e00-\u9fa5_-]+)(:)/, '$1<span class="c-k">$2</span>$3');
    return `<span class="ln"><i>${n}</i>${html}</span>`;
  }).join('\n');

  main.innerHTML = `<h1>${esc(c.name)}</h1>
    <div class="sub"><span class="mono">${esc(c.path)}</span> ·
      ${(c.size / 1024).toFixed(1)} KB · 只读（改判据请直接编辑文件后重跑）</div>
    <div class="codebox"><pre>${body}</pre></div>`;
  main.querySelectorAll('[data-casefile]').forEach((el) => {
    el.addEventListener('click', () => go('casefile', { name: el.dataset.casefile }));
  });
}

/* ---------- 视图：视觉模型 ----------
 *
 * 参照旧 skill 的视觉模型页。配置**写在同一个文件**里
 * （assets/storage/vision.json），也就是 decice/decide 运行时读的那份 ——
 * 网页不回显明文 key，留空保存表示"不改密钥"。
 */
async function viewVision() {
  const c = await api('/api/vision');
  main.innerHTML = `<h1>视觉模型</h1>
    <div class="sub">OpenAI 兼容通道。这份配置就是运行时读的
      <span class="mono">${esc(c.file)}</span></div>
    <div class="panel" style="max-width:660px">
      <div class="kb-bar">
        <b>视觉 / LLM 配置</b>
        <span style="flex:1"></span>
        <span class="muted" id="vs-state">${c.configured ? '已配置' : '未配置'}</span>
        <button class="ghost" id="vs-test">测试连接</button>
        <button id="vs-save">保存</button>
      </div>
      <div class="form">
        <label>Base URL
          <input id="vs-url" placeholder="https://api.deepseek.com" value="${esc(c.base_url || '')}"></label>
        <label>Model
          <input id="vs-model" placeholder="deepseek-flash / gpt-4o / qwen-vl-max" value="${esc(c.model || '')}"></label>
        <label>API Key
          <input id="vs-key" type="password" placeholder="${esc(c.api_key_masked || '未配置')}" value="" autocomplete="off"></label>
        <div class="meta">Key 只显示掩码；<b>留空保存 = 保留原值</b>（避免只想改 model 却把密钥清掉）。
          配置文件不进版本库。</div>
      </div>
      <div id="vs-result"></div>
    </div>`;

  const formVal = () => ({
    base_url: $('#vs-url').value,
    model: $('#vs-model').value,
    api_key: $('#vs-key').value,
  });

  const post = (path, body) => fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {}),
  }).then(async (r) => {
    const j = await r.json().catch(() => ({ error: '返回的不是 JSON' }));
    if (!r.ok) throw new Error(j.error || `HTTP ${r.status}`);
    return j;
  });

  $('#vs-save').addEventListener('click', async () => {
    const btn = $('#vs-save');
    btn.disabled = true;
    $('#vs-state').textContent = '保存中…';
    try {
      await post('/api/vision', formVal());
      $('#vs-key').value = '';
      $('#vs-state').textContent = '已保存 ' + new Date().toLocaleTimeString();
    } catch (e) {
      $('#vs-state').textContent = '保存失败：' + e.message;
    }
    btn.disabled = false;
  });

  // 测试连接：先存表单，再用后端发一次真请求。
  // 顺序很重要 —— 不先存就是拿旧配置在测，测通了也说明不了什么。
  $('#vs-test').addEventListener('click', async () => {
    const btn = $('#vs-test');
    const box = $('#vs-result');
    btn.disabled = true;
    box.innerHTML = '<div class="test-box pending">⏳ 正在用当前配置发一次真实请求（最多 40s）…</div>';
    try {
      await post('/api/vision', formVal());
      $('#vs-key').value = '';
      const r = await post('/api/vision/test');
      box.innerHTML = r.ok
        ? `<div class="test-box ok">✅ <b>连接成功</b> · 耗时 ${r.latency_ms}ms ·
            模型 <code>${esc(r.model)}</code>
           <div class="meta">模型回复：${esc(r.reply || '(空)')}</div>
           ${r.usage ? `<div class="meta">tokens: ${esc(JSON.stringify(r.usage))}</div>` : ''}</div>`
        : `<div class="test-box err">❌ <b>连接失败</b>：${esc(r.error || '未知错误')}
           ${r.detail ? `<div class="meta">响应：${esc(r.detail)}</div>` : ''}
           ${r.hint ? `<div class="meta">💡 ${esc(r.hint)}</div>` : ''}</div>`;
    } catch (e) {
      box.innerHTML = `<div class="test-box err">❌ 请求异常：${esc(e.message)}</div>`;
    }
    btn.disabled = false;
  });
}

/* 详情页顶部的"原始用例信息"
 *
 * 需求原话：「记录详情里面要包含原始 case 信息。这样就知道是哪个 case 了。」
 *
 * ## 为什么必须存快照，而不是只存路径
 *
 * YAML 是会改的 —— 168 的 a3 判据就改过两次（先钉死 activity，
 * 再换 album_reached，最后交模型）。如果旧记录只存路径，
 * 点开看到的就是"新判据 + 旧结论"，反过来还以为当初就是照这么判的。
 * 所以跑完那一刻把内容快照进库（runs.case_yaml），这里优先显示快照。
 *
 * 老记录（加这个字段之前跑的）没有快照，退回显示当前文件，
 * 并**明确标出来"这是当前版本，不是你跑的那份"** —— 不要假装一样。
 */
function caseInfoHtml(run) {
  const snap = run.case_yaml || '';
  const path = run.case_yaml_path || '';
  const fname = path.split(/[\\/]/).pop();
  const rep = (() => { try { return JSON.parse(run.report_json || '{}'); } catch (_) { return {}; } })();
  const pkg = (rep && rep.package) || '';

  if (!snap && !path) {
    return `<h2>原始用例</h2>
      <div class="hint">这次运行没有记录用例来源（早于该功能加入）。</div>`;
  }

  /* ⚠️ 这里**只用快照**，绝不拿当前文件顶替。
   *
   * 之前是这样写的：没有快照时去 /api/case/<文件名> 拉当前 YAML 显示。
   * 看着"信息更全"，实际是在骗人 —— 用户要的是"这次跑的是哪个 case"，
   * 拿现在的文件去回答，等于把两个版本混成一份。
   * 老记录就老实说"没存快照"，要看当前版本去用例库点。
   */
  const lines = snap.split('\n');
  const body = lines.map((ln, i) => {
    const n = String(i + 1).padStart(4, ' ');
    let html = esc(ln);
    if (/^\s*#/.test(ln)) html = `<span class="c-cm">${html}</span>`;
    else html = html
      .replace(/^(\s*)(- )/, '$1<span class="c-ds">$2</span>')
      .replace(/^(\s*)([\w\u4e00-\u9fa5_-]+)(:)/, '$1<span class="c-k">$2</span>$3');
    return `<span class="ln"><i>${n}</i>${html}</span>`;
  }).join('\n');

  const head = `<div class="casebar">
      ${fname ? `<span class="mono">${esc(fname)}</span>` : ''}
      ${pkg ? `<span class="mono dim">${esc(pkg)}</span>` : ''}
      ${snap ? '<span class="tag ok">跑的时候那份</span>' : ''}
      ${fname ? `<a class="ghost-link" data-casefile="${esc(fname)}">在用例库看当前版本</a>` : ''}
      <span style="flex:1"></span>
      ${snap ? `<span class="muted">${lines.length} 行</span>` : ''}
    </div>`;

  if (!snap) {
    return `<h2>原始用例</h2>
      ${head}
      <div class="hint warn">⚠️ 这次运行早于"记录用例快照"功能，库里有文件名但没有内容。<br>
        为了不把<b>现在</b>的判据当<b>当时</b>的判据，这里不显示内容；
        点上面的链接可以去用例库看当前版本。</div>`;
  }

  return `<h2>原始用例</h2>
    ${head}
    <div class="codebox collapsed"><pre>${body}</pre></div>`;

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
    <div class="sub">${esc(run.started_at || '')} · 耗时 ${fmtDur(run.elapsed_s)}${fmtSplit(run)} ·
      引擎 ${esc(run.engine || '—')}</div>
    <div class="cards">
      <div class="card ${(run.status || '').toLowerCase()}"><div class="n" style="font-size:20px">${badge(run.status)}</div><div class="l">运行结论</div></div>
      <div class="card"><div class="n">${run.steps_done == null ? '—' : run.steps_done}${run.steps_total ? '/' + run.steps_total : ''}</div><div class="l">完成步数</div></div>
      <div class="card"><div class="n">${pass}</div><div class="l">断言通过</div></div>
      <div class="card ${fail ? 'fail' : ''}"><div class="n">${fail}</div><div class="l">断言失败</div></div>
      <div class="card"><div class="n">${acts.length}</div><div class="l">动作次数</div></div>
    </div>
    <div class="hint">${esc(run.summary || '')}</div>

    ${caseInfoHtml(run)}

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
    else if (STATE.view === 'cases') await viewCases();
    else if (STATE.view === 'casefile') await viewCaseFile(STATE.name);
    else if (STATE.view === 'vision') await viewVision();
    else await viewDashboard();
  } catch (e) {
    main.innerHTML = `<div class="empty">加载失败：${esc(e.message)}</div>`;
  }
}

function go(view, extra = {}) {
  STATE = {
    view, case: extra.case || '', runId: extra.runId, name: extra.name,
  };
  const q = extra.case ? '?case=' + encodeURIComponent(extra.case)
    : extra.name ? '?name=' + encodeURIComponent(extra.name) : '';
  history.replaceState(STATE, '', `#${view}${extra.runId ? '/' + extra.runId : ''}${q}`);
  route();
}

/* 事件委托：侧栏、表格行、用例文件链接 */
document.addEventListener('click', (e) => {
  const nav = e.target.closest('.nav-item');
  if (nav && nav.dataset.view) {
    const v = nav.dataset.view;
    if (v === 'runs') return go('runs');
    if (v === 'cases') return go('cases');
    if (v === 'vision') return go('vision');
    return go('dashboard');
  }
  // 用例文件（用例库的表格行、详情页的"在用例库打开"）
  const cf = e.target.closest('[data-casefile]');
  if (cf) return go('casefile', { name: cf.dataset.casefile });
  const cnav = e.target.closest('[data-case]');
  if (cnav && !cnav.dataset.run) {
    return go('runs', { case: cnav.dataset.case });
  }
  const row = e.target.closest('tr[data-run]');
  if (row) return go('run', { runId: row.dataset.run });
  // 用例详情里的代码框：点一下展开/收起（YAML 动辄 200 行）
  const code = e.target.closest('.codebox.collapsed');
  if (code) code.classList.remove('collapsed');
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
