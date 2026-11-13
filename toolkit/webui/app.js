/* Android AI 测试台前端（零依赖 vanilla JS + echarts） */
"use strict";

const $ = (sel, el = document) => el.querySelector(sel);
const $$ = (sel, el = document) => Array.from(el.querySelectorAll(sel));
const main = $("#main");
let kbCurrent = null;    // 当前编辑的知识卡名
let caseFilter = "all";  // 用例库当前模块过滤
let showDiag = false;    // 测试记录是否显示验证/调试会话
let recQuery = "";       // 测试记录搜索词（标题 / 结论 模糊匹配）
let recSearchTimer = null; // 输入防抖：停止输入 1s 后自动搜索
let recStatusFilter = []; // 测试记录结论筛选（多选）：空=不过滤
let viewSeq = 0;         // 导航令牌：迟到响应不覆盖当前视图

// ── 主题（浅色 / 深色）──
const THEME_KEY = "dsh-theme";
function systemPrefersDark() {
  try { return window.matchMedia("(prefers-color-scheme: dark)").matches; } catch { return false; }
}
function getTheme() {
  let saved = null;
  try { saved = localStorage.getItem(THEME_KEY); } catch {}
  return saved === "dark" || saved === "light" ? saved : (systemPrefersDark() ? "dark" : "light");
}
function applyTheme(mode) {
  const root = document.documentElement;
  if (mode === "dark") root.setAttribute("data-theme", "dark");
  else root.removeAttribute("data-theme");
  syncThemeButtons(mode);
}
function syncThemeButtons(mode) {
  const dark = (mode || getTheme()) === "dark";
  $$("[data-theme-toggle]").forEach(b => {
    b.textContent = dark ? "☀ 浅色" : "🌙 深色";
    b.title = dark ? "切换到浅色主题" : "切换到深色主题";
    b.setAttribute("aria-label", b.title);
    b.setAttribute("aria-pressed", String(dark));
  });
}
function setTheme(mode) {
  try { localStorage.setItem(THEME_KEY, mode); } catch {}
  applyTheme(mode);
}
function toggleTheme() { setTheme(getTheme() === "dark" ? "light" : "dark"); }
applyTheme(getTheme());   // 尽早应用，避免首屏闪白

// ── 工具 ──
async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) throw new Error(`${r.status}`);
  return r.json();
}
const esc = s => String(s ?? "").replace(/[&<>"]/g,
  c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const statusChip = s => s ? `<span class="chip ${esc(s)}">${esc(s)}</span>` : "";

/* 标准时间展示：ISO(含毫秒/无时区) → YYYY-MM-DD HH:MM:SS
   后端存的是本地时间 isoformat（如 2026-09-21T18:57:09.517），
   直接 slice 即可，避免 Date 解析无时区字符串产生偏移。 */
function fmtTime(v, withDate = true) {
  if (!v) return "—";
  const s = String(v).replace("T", " ");
  const date = s.slice(0, 10);
  const time = s.slice(11, 19);
  if (!time) return withDate ? date : "—";
  return withDate ? `${date} ${time}` : time;
}
const fmtDate = v => v ? String(v).replace("T", " ").slice(0, 10) : "—";

/* 耗时：秒 → 时/分/秒可读形式
   0s→"0秒"，45s→"45秒"，90s→"1分30秒"，3725s→"1时2分5秒"
   只保留有意义的高位单位，避免 "0时0分45秒" 这种噪音。 */
function fmtDuration(sec) {
  if (sec === null || sec === undefined || isNaN(sec)) return "—";
  const n = Math.round(Number(sec));
  if (n < 0) return "—";
  const h = Math.floor(n / 3600);
  const m = Math.floor((n % 3600) / 60);
  const s = n % 60;
  if (h) return m ? `${h}时${m}分${s}秒` : `${h}时${s}秒`;
  if (m) return `${m}分${s}秒`;
  return `${s}秒`;
}

// 详情接口只给 started_at/finished_at（没有 duration_seconds），现场算一个。
// 未收尾的会话（finished_at 为空）算到"此刻"。
function durationOf(s) {
  if (s.duration_seconds != null) return s.duration_seconds;
  const a = Date.parse(s.started_at);
  if (isNaN(a)) return null;
  const b = s.finished_at ? Date.parse(s.finished_at) : Date.now();
  if (isNaN(b)) return null;
  return (b - a) / 1000;
}

/* 状态中文标签（判定规则见 SKILL.md） */
const statusCN = {
  PASS: "通过", FAIL: "失败", BLOCKED: "阻断", ERROR: "错误",
  running: "执行中",
};
const statusText = s => s ? (statusCN[s] || s) : "—";

/* 会话 id 紧凑显示。
   id 是 `<uuid>:<local_id>` 复合串（多库聚合后才有的），
   列表里只显示 local_id，hover 时才给完整串。
   2026-10-28：列表「#」列原本直接打 s.id，改多库后变成一长串，
   用户反馈「之前是数字，现在不是了」。 */
const sidShort = id => {
  const s = String(id == null ? "" : id);
  const i = s.lastIndexOf(":");
  return i >= 0 ? s.slice(i + 1) : s;
};

/* 用例编号：id 形如 `com.zui.calendar_167`，列表里只显示 167。
   与后端 _case_number() 用同一套提取规则（末尾的 2-4 位数字）。 */
const caseNum = c => {
  if (c && c.number != null && String(c.number).trim() !== "") {
    return String(c.number);
  }
  const m = String((c && c.id) || "").match(/[_\-](\d{2,4})(?!\d)/);
  return m ? m[1] : String((c && c.id) || "—");
};

/* 模块名（包名）友好化 */
const pkgShort = p => {
  if (!p || p === "未识别") return "未识别";
  const parts = String(p).split(".");
  return parts.length > 2 ? parts.slice(-2).join(".") : p;
};

// 成功率 4 段配色：≥90 优秀(绿) / ≥70 良好(蓝绿) / ≥50 偏低(橙) / <50 差(红)
// total=0（还没有用例）时不评级，走默认色。
const rateLevel = (pct, total) => {
  if (!total) return "";
  if (pct >= 90) return "lv-a";
  if (pct >= 70) return "lv-b";
  if (pct >= 50) return "lv-c";
  return "lv-d";
};

// ── 通用弹窗 ──
function modal(title, bodyHTML, footerHTML = "") {
  const el = document.createElement("div");
  el.className = "modal-mask";
  el.innerHTML = `
    <div class="modal">
      <div class="modal-head"><b>${esc(title)}</b><span style="flex:1"></span>
        <button class="ghost modal-x" title="关闭">✕</button></div>
      <div class="modal-body">${bodyHTML}</div>
      ${footerHTML ? `<div class="modal-foot">${footerHTML}</div>` : ""}
    </div>`;
  document.body.appendChild(el);
  const close = () => el.remove();
  $(".modal-x", el).addEventListener("click", close);
  el.addEventListener("click", ev => { if (ev.target === el) close(); });
  return { el, close };
}

// ── 拖动模态框（用于轨迹回放：图较大，常要挪开遮挡）──
// 用 transform: translate() 位移，不改 left/top：面板仍由 flex 居中、不撑坏宽版回放布局。
function makeDraggable(panel, handle) {
  const h = handle || panel.querySelector(".modal-head");
  if (!h) return { reset() {} };
  let dx = 0, dy = 0, sx = 0, sy = 0, dragging = false;
  let base = null;
  const measure = () => {
    const t = panel.style.transform;
    panel.style.transform = "none";
    const r = panel.getBoundingClientRect();
    base = { l: r.left, t: r.top, w: r.width, h: r.height };
    panel.style.transform = t;
  };
  const clamp = () => {
    if (!base) measure();
    const keep = Math.min(120, base.w);
    const minX = -base.l - (base.w - keep);
    const maxX = window.innerWidth - keep - base.l;
    const minY = -base.t;
    const maxY = window.innerHeight - 48 - base.t;
    dx = Math.max(minX, Math.min(maxX, dx));
    dy = Math.max(minY, Math.min(maxY, dy));
  };
  const apply = () => { panel.style.transform = `translate(${dx}px, ${dy}px)`; };
  h.style.cursor = "move";
  h.style.touchAction = "none";
  h.style.userSelect = "none";
  h.title = "按住可拖动窗口（双击标题栏 / 点 ⌖ 复位）";
  h.addEventListener("pointerdown", ev => {
    if (ev.target.closest("button")) return;
    dragging = true; base = null;
    sx = ev.clientX - dx; sy = ev.clientY - dy;
    h.setPointerCapture(ev.pointerId);
    panel.classList.add("dragging");
  });
  h.addEventListener("pointermove", ev => {
    if (!dragging) return;
    dx = ev.clientX - sx; dy = ev.clientY - sy;
    clamp(); apply();
  });
  const end = ev => {
    if (!dragging) return;
    dragging = false;
    panel.classList.remove("dragging");
    try { h.releasePointerCapture(ev.pointerId); } catch {}
  };
  h.addEventListener("pointerup", end);
  h.addEventListener("pointercancel", end);
  const reset = () => { dx = 0; dy = 0; base = null; apply(); };
  h.addEventListener("dblclick", reset);
  window.addEventListener("resize", () => {
    if (dx || dy) { base = null; clamp(); apply(); }
  });
  return { reset };
}

// ── 视图路由（点哪个哪个高亮）──
const VIEWS = {
  dashboard: () => viewDashboard(),
  sessions: () => viewSessions(),
  cases: () => viewCaseLib(),
  knowledge: () => viewKnowledge(),
  vision: () => viewVision(),
};
function activateNav(view) {
  $$(".nav-item").forEach(x => x.classList.toggle("active", x.dataset.view === view));
}
$$(".nav-item").forEach(el =>
  el.addEventListener("click", () => {
    if (el.dataset.view !== "sessions" && recSearchTimer) {
      clearTimeout(recSearchTimer); recSearchTimer = null;
    }
    activateNav(el.dataset.view);
    (VIEWS[el.dataset.view] || viewDashboard)();
  }));

// ══════════════════ 仪表盘 ══════════════════
let dashCharts = [];
async function viewDashboard() {
  dashCharts.forEach(c => { try { c.dispose(); } catch {} });
  dashCharts = [];
  activateNav("dashboard");
  main.innerHTML = "<div class='sub'>加载中…</div>";
  const d = await api("/api/dashboard");
  const ov = d.overview || {};
  const total = ov.total || 0, pass = ov.pass || 0, fail = ov.fail || 0;
  const blocked = ov.blocked || 0, neverRun = ov.never_run || 0;
  const runPct = total ? Math.round(pass / total * 100) : 0;

  // 最近 3 天：横轴只显示日期（MM-DD），缺失的天补 0
  const labels = ["今天", "昨天", "前天"];
  const today = new Date();
  const threeDays = [];
  for (let i = 0; i < 3; i++) {
    const dt = new Date(today.getTime() - i * 86400000);
    const key = `${dt.getFullYear()}-${String(dt.getMonth() + 1).padStart(2, "0")}-${String(dt.getDate()).padStart(2, "0")}`;
    const r = (d.recent || []).find(x => x.day === key) || {};
    const tot = r.total || 0;
    threeDays.push({
      label: labels[i], date: key,
      total: tot, pass: r.pass || 0, fail: r.fail || 0, blocked: r.blocked || 0,
      passRate: tot ? Math.round((r.pass || 0) / tot * 100) : 0,
      failRate: tot ? Math.round((r.fail || 0) / tot * 100) : 0,
    });
  }
  // 柱状图按时间正序（前天→今天）
  const chartDays = [...threeDays].reverse();

  // 模块：按成功率从低到高排（最需要关注的排最前）
  const mods = (d.modules || []).map(m => {
    const ran = (m.pass || 0) + (m.fail || 0) + (m.blocked || 0);
    return {
      ...m,
      ran,
      passRate: ran ? Math.round((m.pass || 0) / ran * 100) : null,
    };
  }).sort((a, b) => {
    // 无执行记录的排最后；其余按成功率升序
    if (a.passRate === null && b.passRate === null) return b.total - a.total;
    if (a.passRate === null) return 1;
    if (b.passRate === null) return -1;
    return a.passRate - b.passRate;
  });

  main.innerHTML = `
    <h1>仪表盘</h1>

    <div class="panel">
      <div class="dash-top">
        <div class="dash-seg seg-count">
          <div class="seg-label">用例总数</div>
          <div class="big-num">${total}</div>
          <div class="big-rate ${rateLevel(runPct, total)}">整体成功率 <b>${runPct}%</b></div>
        </div>
        <div class="dash-seg seg-pie">
          <div class="seg-label">用例整体成功情况</div>
          <div id="chart-cases" class="chart-mid"></div>
          <div class="chart-legend-note" id="chart-note"></div>
        </div>
        <div class="dash-seg seg-bar">
          <div class="seg-label">最近三天成功 / 失败</div>
          <div id="chart-rate" class="chart-mid"></div>
        </div>
      </div>
    </div>

    <div class="panel">
      <div class="panel-title">按模块 · 按成功率由低到高</div>
      <div class="mod-list">
        ${mods.map(m => `
          <div class="mod-row">
            <div class="mod-head">
              <span class="mod-name" title="${esc(m.package)}">${esc(pkgShort(m.package))}</span>
              <span class="mod-meta">
                ${m.total} 个用例${m.ran ? ` · 已执行 ${m.ran}` : " · 未执行"}
              </span>
              <span class="mod-pct ${m.passRate === null ? "none"
                    : m.passRate >= 80 ? "good" : m.passRate >= 50 ? "mid" : "bad"}">
                ${m.passRate === null ? "—" : m.passRate + "%"}
              </span>
            </div>
            <div class="progress">
              <div class="progress-fill pass" style="width:${m.ran ? (m.pass || 0) / m.ran * 100 : 0}%"></div>
              <div class="progress-fill fail" style="width:${m.ran ? (m.fail || 0) / m.ran * 100 : 0}%"></div>
              <div class="progress-fill blocked" style="width:${m.ran ? (m.blocked || 0) / m.ran * 100 : 0}%"></div>
            </div>
            <div class="mod-legend">
              <span class="lg"><i style="background:${"#178a50"}"></i>成功 ${m.pass || 0}</span>
              <span class="lg"><i style="background:${"#d64545"}"></i>失败 ${m.fail || 0}</span>
              <span class="lg"><i style="background:${"#8250df"}"></i>阻断 ${m.blocked || 0}</span>
            </div>
          </div>`).join("") || "<div class='muted'>暂无用例</div>"}
      </div>
    </div>`;

  if (!window.echarts) {
    $("#chart-cases").innerHTML = "<div class='muted'>echarts.min.js 未加载</div>";
    return;
  }
  const C = { pass: "#178a50", fail: "#d64545", blocked: "#8250df", other: "#b9c0cf" };

  // ① 环形图：用例整体成功情况
  const pie = [
    { name: "成功", value: pass, itemStyle: { color: C.pass } },
    { name: "失败", value: fail, itemStyle: { color: C.fail } },
    { name: "阻断", value: blocked, itemStyle: { color: C.blocked } },
    { name: "未执行", value: neverRun, itemStyle: { color: C.other } },
  ].filter(x => x.value > 0);
  const c1 = echarts.init($("#chart-cases"));
  c1.setOption({
    tooltip: { trigger: "item", formatter: "{b}: {c} ({d}%)" },
    series: [{
      type: "pie", radius: ["52%", "76%"], center: ["50%", "50%"],
      label: { show: true, formatter: "{b}\n{c}", fontSize: 13, lineHeight: 17 },
      labelLine: { length: 8, length2: 8 },
      data: pie.length ? pie : [{ name: "无用例", value: 1, itemStyle: { color: C.other } }],
    }],
  });
  dashCharts.push(c1);
  $("#chart-note").innerHTML = `
    <span class="lg"><i style="background:${C.pass}"></i>成功 ${pass}</span>
    <span class="lg"><i style="background:${C.fail}"></i>失败 ${fail}</span>
    <span class="lg"><i style="background:${C.blocked}"></i>阻断 ${blocked}</span>
    <span class="lg"><i style="background:${C.other}"></i>未执行 ${neverRun}</span>`;

  // ② 柱状图：最近三天成功 / 失败次数（横轴=日期，悬停显示成功/失败/成功率）
  const c2 = echarts.init($("#chart-rate"));
  // 纵坐标只标 3 档：0 / 平均值 / 平均值×2。
  // 平均值 = 近三天「成功+失败」次数的均值。
  // ⚠️ 但取 max(平均值×2, 各天最大值)：否则某天柱子会顶出坐标轴外面（截断）。
  //    例：数据 6/0/0 → 平均值 2，严格按 0/2/4 会截断，故抬到 0/3/6。
  const dayTotals = chartDays.map(x => (x.pass || 0) + (x.fail || 0));
  const avg = dayTotals.length
    ? dayTotals.reduce((a, b) => a + b, 0) / dayTotals.length : 0;
  const avgTick = Math.max(1, Math.round(avg));
  const yMax = Math.max(avgTick * 2, ...dayTotals, 1);
  c2.setOption({
    tooltip: {
      trigger: "axis",
      axisPointer: { type: "shadow" },
      formatter: ps => {
        const x = chartDays[ps[0].dataIndex];
        const tot = x.total;
        const rate = tot ? Math.round(x.pass / tot * 100) : 0;
        const line = (label, val, color) =>
          `<div style="display:flex;align-items:center;gap:6px">
             <i style="width:9px;height:9px;border-radius:2px;background:${color};display:inline-block"></i>
             <span style="flex:1">${label}</span><b>${val}</b></div>`;
        return `<div style="min-width:150px">
            <div style="font-weight:600;margin-bottom:6px">${x.date}　${x.label}</div>
            ${line("成功", x.pass, C.pass)}
            ${line("失败", x.fail, C.fail)}
            ${x.blocked ? line("阻断", x.blocked, C.blocked) : ""}
            <div style="border-top:1px solid #e6e9ef;margin:6px 0 4px"></div>
            <div style="display:flex;gap:6px"><span style="flex:1">执行总数</span><b>${tot}</b></div>
            <div style="display:flex;gap:6px"><span style="flex:1">成功率</span><b>${rate}%</b></div>
          </div>`;
      },
    },
    grid: { left: 46, right: 18, top: 24, bottom: 30 },
    xAxis: {
      type: "category",
      data: chartDays.map(x => x.date),
      axisLabel: { fontSize: 12, color: "#737b90" },
      axisTick: { show: false },
    },
    yAxis: {
      type: "value",
      max: yMax,
      interval: yMax / 2,          // 只出 0 / 平均值 / 平均值×2 三档
      axisLabel: { fontSize: 12, color: "#737b90" },
      splitLine: { lineStyle: { color: "#eef0f4" } },
    },
    series: [
      { name: "成功", type: "bar", barMaxWidth: 46, barGap: "10%",
        itemStyle: { color: C.pass, borderRadius: [4, 4, 0, 0] },
        label: { show: true, position: "top", fontSize: 14, color: "#737b90" },
        data: chartDays.map(x => x.pass) },
      { name: "失败", type: "bar", barMaxWidth: 46,
        itemStyle: { color: C.fail, borderRadius: [4, 4, 0, 0] },
        label: { show: true, position: "top", fontSize: 14, color: "#737b90" },
        data: chartDays.map(x => x.fail) },
    ],
  });
  dashCharts.push(c2);

  if (!viewDashboard._resizeBound) {
    viewDashboard._resizeBound = true;
    window.addEventListener("resize", () =>
      dashCharts.forEach(c => { try { c.resize(); } catch {} }));
  }
}

// ══════════════════ 测试记录 ══════════════════
// 结论的本地化别名：输入中文也能命中对应状态（不区分大小写）
const STATUS_FILTERS = ["PASS", "FAIL", "BLOCKED", "ERROR"];
const STATUS_ALIASES = {
  PASS: ["pass", "通过", "成功", "合格", "已通过"],
  FAIL: ["fail", "失败", "不通过", "未通过", "错误"],
  BLOCKED: ["blocked", "阻断", "阻塞", "受阻", "无法判定"],
  ERROR: ["error", "错误"],
  RUNNING: ["running", "执行中", "运行中", "进行中"],
};
function matchSession(s, q) {
  const title = String(s.title || `会话 #${s.id}`).toLowerCase();
  if (title.includes(q)) return true;
  const st = String(s.status || "").toUpperCase();
  if (st.toLowerCase().includes(q)) return true;
  const alias = STATUS_ALIASES[st];
  return !!(alias && alias.some(a => a.includes(q)));
}

async function viewSessions() {
  const seq = ++viewSeq;
  activateNav("sessions");
  main.innerHTML = "<div class='sub'>加载中…</div>";
  let url = "/api/sessions" + (showDiag ? "?all=1" : "");
  if (recStatusFilter.length) {
    const p = recStatusFilter.map(x => "status=" + encodeURIComponent(x)).join("&");
    url += (url.includes("?") ? "&" : "?") + p;
  }
  const { sessions, kinds } = await api(url);
  if (seq !== viewSeq) return; // 用户已切走，放弃本次渲染
  const diagN = (kinds && kinds.diag) || 0;
  const testN = (kinds && kinds.test) || 0;

  const q = recQuery.trim().toLowerCase();
  let matched = q ? sessions.filter(s => matchSession(s, q)) : sessions;
  if (recStatusFilter.length) {
    const allow = recStatusFilter.map(x => x.toUpperCase());
    matched = matched.filter(s => allow.includes(String(s.status || "").toUpperCase()));
  }
  const fcount = recStatusFilter.length
    ? ` · 结论筛选 [${recStatusFilter.join("/")}] 命中 <b>${matched.length}</b> 条` : "";

  main.innerHTML = `
    <h1>测试记录</h1>
    <div class="lib-toolbar rec-toolbar">
      <input type="search" id="rec-search" class="rec-search" placeholder="按标题或结论搜索…"
        value="${esc(recQuery)}" title="按标题或结论模糊匹配（不区分大小写），停止输入 1 秒自动搜索">
      <div class="sub rec-stats">正式测试 <b>${testN}</b> 条${diagN ? ` · 验证/调试 <b>${diagN}</b> 条（默认不显示）` : ""}${
        q ? ` · 搜索"${esc(recQuery.trim())}"命中 <b>${matched.length}</b> 条` : ""}${fcount}</div>
      ${diagN ? `<label class="chk"><input type="checkbox" id="show-diag"${showDiag ? " checked" : ""}>显示验证记录</label>` : ""}
      <button class="ghost sm theme-toggle" data-theme-toggle title="切换浅色/深色主题">🌙 深色</button>
    </div>
    <div class="lib-toolbar rec-toolbar2">
      <div class="rec-filters">
        <span class="sub rec-flt-label">按结论筛选：</span>
        ${STATUS_FILTERS.map(st => `<button class="stab${recStatusFilter.includes(st) ? " on" : ""}"
          data-st="${st}" title="筛选结论为 ${st} 的记录（可多选）">${statusChip(st)}</button>`).join("")}
        ${recStatusFilter.length ? `<button class="ghost sm" id="rec-flt-clear" title="清除结论筛选">清除筛选</button>` : ""}
      </div>
      <button class="ghost sm danger rec-batch-del" id="rec-batch-del" disabled
        title="删除勾选的记录及其截图/日志证据，不可恢复">🗑 删除选中</button>
    </div>
    <div class="list-table recs">
      <div class="lt-head">
        <span class="c-chk"><input type="checkbox" id="rec-sel-all" title="全选 / 全不选（仅当前列表）"></span>
        <span class="c-id">#</span>
        <span class="c-title">标题</span>
        <span class="c-status">结论</span>
        <span class="c-pkg">模块</span>
        <span class="c-start">开始时间</span>
        <span class="c-dur">耗时</span>
        <span class="c-cnt">事件/断言</span>
        <span class="c-act">操作</span>
      </div>
      ${matched.map(s => `
        <div class="lt-row${s.kind === "diag" ? " diag" : ""}" data-id="${esc(s.id)}">
          <span class="c-chk"><input type="checkbox" class="rec-check" data-id="${esc(s.id)}" title="勾选后可批量删除"></span>
          <span class="c-id" title="${esc(s.id)}">${esc(sidShort(s.id))}</span>
          <span class="c-title" title="${esc(s.title || `会话 #${s.id}`)}">
            ${s.kind === "diag" ? '<span class="tag-diag">验证</span>' : ""}
            <a class="row-link" data-id="${esc(s.id)}">${esc(s.title || `会话 #${s.id}`)}</a>
          </span>
          <span class="c-status">${statusChip(s.status)}</span>
          <span class="c-pkg" title="${esc(s.package || "")}">${esc(pkgShort(s.package))}</span>
          <span class="c-start">${esc(fmtTime(s.started_at))}</span>
          <span class="c-dur">${fmtDuration(s.duration_seconds)}</span>
          <span class="c-cnt">${s.event_count} / ${s.finding_count}</span>
          <span class="c-act">
            <button class="ghost sm" data-replay="${esc(s.id)}"
              ${s.has_evidence ? "" : "disabled"}
              title="${s.has_evidence ? "回放本次执行的轨迹（截图串播）" : "该记录没有可用的截图，无法回放"}">▶ 回放</button>
            <button class="ghost sm danger" data-del="${esc(s.id)}" title="删除该记录及其截图/日志">删除</button>
          </span>
        </div>`).join("") || (q
          ? `<div class='lt-empty'>没有标题或结论匹配"${esc(recQuery.trim())}"的记录。</div>`
          : "<div class='lt-empty'>还没有记录。用 <code>session.py start</code> 开启会话后由 AI 执行用例。</div>")}
    </div>`;

  // 搜索框：停止输入 1 秒自动搜索；清空立即恢复
  const searchEl = $("#rec-search");
  if (searchEl) {
    searchEl.addEventListener("input", () => {
      recQuery = searchEl.value;
      if (recSearchTimer) { clearTimeout(recSearchTimer); recSearchTimer = null; }
      if (!recQuery.trim()) { viewSessions(); return; }
      recSearchTimer = setTimeout(() => { recSearchTimer = null; viewSessions(); }, 1000);
    });
    searchEl.addEventListener("keydown", ev => {
      if (ev.key === "Enter") {
        if (recSearchTimer) { clearTimeout(recSearchTimer); recSearchTimer = null; }
        recQuery = searchEl.value;
        viewSessions();
      }
    });
    if (recQuery) {
      searchEl.focus();
      try { searchEl.setSelectionRange(searchEl.value.length, searchEl.value.length); } catch {}
    }
  }
  const cb = $("#show-diag");
  if (cb) cb.addEventListener("change", () => { showDiag = cb.checked; viewSessions(); });
  $$(".stab", main).forEach(btn =>
    btn.addEventListener("click", () => {
      const st = btn.dataset.st;
      const i = recStatusFilter.indexOf(st);
      if (i >= 0) recStatusFilter.splice(i, 1);
      else recStatusFilter.push(st);
      viewSessions();
    }));
  const clr = $("#rec-flt-clear");
  if (clr) clr.addEventListener("click", () => { recStatusFilter = []; viewSessions(); });
  $$("[data-theme-toggle]").forEach(b =>
    b.addEventListener("click", () => { toggleTheme(); syncThemeButtons(); }));
  syncThemeButtons();
  $$(".row-link", main).forEach(el =>
    el.addEventListener("click", () => viewSession(el.dataset.id)));
  $$("[data-del]", main).forEach(el =>
    el.addEventListener("click", ev => {
      ev.stopPropagation();
      confirmDeleteSession(el.dataset.del);
    }));
  $$("[data-replay]", main).forEach(el =>
    el.addEventListener("click", ev => {
      ev.stopPropagation();
      replaySession(el.dataset.replay);
    }));

  // 批量删除
  const selAll = $("#rec-sel-all");
  const batchBtn = $("#rec-batch-del");
  const checkedIds = () => $$(".rec-check:checked", main).map(el => el.dataset.id);
  const syncBatch = () => {
    const ids = checkedIds();
    if (batchBtn) {
      batchBtn.disabled = !ids.length;
      batchBtn.textContent = ids.length ? `🗑 删除选中 (${ids.length})` : "🗑 删除选中";
    }
    if (selAll) selAll.checked = ids.length > 0 && ids.length === $$(".rec-check", main).length;
  };
  $$(".rec-check", main).forEach(el => el.addEventListener("change", syncBatch));
  if (selAll) selAll.addEventListener("change", () => {
    $$(".rec-check", main).forEach(el => { el.checked = selAll.checked; });
    syncBatch();
  });
  if (batchBtn) batchBtn.addEventListener("click", () => {
    const ids = checkedIds();
    if (ids.length) confirmDeleteSessions(ids);
  });
}

function confirmDeleteSessions(ids) {
  const m = modal("批量删除测试记录", `
    <div class="warn-text">确定删除 <b>${ids.length}</b> 条记录吗？</div>
    <div class="meta">将同时删除这些记录的事件流、断言，以及<b>磁盘上的截图与日志证据</b>，<b>不可恢复</b>。
    被测 App 本身不受影响。</div>
    <label class="chk" style="margin-top:10px">
      <input type="checkbox" id="del-ev-b" checked> 同时删除截图 / 日志文件
    </label>`,
    `<button class="ghost" data-cancel>取消</button>
     <button class="danger" data-ok>确认删除</button>`);
  $("[data-cancel]", m.el).addEventListener("click", m.close);
  $("[data-ok]", m.el).addEventListener("click", async () => {
    const withEv = $("#del-ev-b", m.el).checked;
    const okBtn = $("[data-ok]", m.el);
    okBtn.disabled = true; okBtn.textContent = "删除中…";
    let okN = 0, failN = 0;
    for (const id of ids) {
      try {
        const r = await api(`/api/sessions/${encodeURIComponent(id)}?evidence=${withEv ? 1 : 0}`,
                            { method: "DELETE" });
        if (r && r.ok) okN++; else failN++;
      } catch { failN++; }
    }
    m.close();
    await viewSessions();
    toast(`批量删除完成：成功 ${okN} 条${failN ? `，失败 ${failN} 条` : ""}`);
  });
}

function confirmDeleteSession(id) {
  const m = modal("删除测试记录", `
    <div class="warn-text">确定删除记录 <b>#${esc(id)}</b> 吗？</div>
    <div class="meta">将同时删除该记录的事件流、断言，以及<b>磁盘上的截图与日志证据</b>，<b>不可恢复</b>。
    被测 App 本身不受影响。</div>
    <label class="chk" style="margin-top:10px">
      <input type="checkbox" id="del-ev" checked> 同时删除截图 / 日志文件
    </label>`,
    `<button class="ghost" data-cancel>取消</button>
     <button class="danger" data-ok>确认删除</button>`);
  $("[data-cancel]", m.el).addEventListener("click", m.close);
  $("[data-ok]", m.el).addEventListener("click", async () => {
    const withEv = $("#del-ev", m.el).checked;
    const btn = $("[data-ok]", m.el);
    // ⚠️ 删除可能要几秒（几十个证据文件），必须有「删除中…」反馈，
    //    否则弹窗静止不动，用户以为卡死去刷新 → 连接断 → 状态不一致。
    btn.textContent = "删除中…";
    btn.disabled = true;

    let netOk = true;
    let r = null;
    try {
      r = await api(`/api/sessions/${id}?evidence=${withEv ? 1 : 0}`,
                    { method: "DELETE" });
    } catch (e) {
      netOk = false;         // 响应丢了 ≠ 删除失败，用核实兜底
    }

    // 删后核实：重新拉列表看这条还在不在（唯一可靠的事实）
    let stillThere = false;
    try {
      const list = await api("/api/sessions?all=1&kind=test");
      // ⚠️ 该接口返回 {sessions:[...], stats, kinds}（dict），不是数组
      const rows = (list && list.sessions) || [];
      const localNum = Number(String(id).split(":").pop());
      stillThere = rows.some(x =>
        x.id === id || Number(x.local_id) === localNum);
    } catch (e) { /* 核实失败时保守提示 */ }

    m.close();
    await viewSessions();
    if (stillThere) {
      toast(`删除失败：记录 #${id} 仍存在`);
    } else if (netOk && r && r.ok) {
      toast(`已删除记录 #${id}${withEv ? `，后台清理证据文件中` : ""}`);
    } else {
      // 网络中断但记录已不在 = 实际删成功了
      toast(`已删除记录 #${id}（响应中断，已核实）`);
    }
  });
}

/* 轻量提示条（操作反馈） */
function toast(msg) {
  const el = document.createElement("div");
  el.className = "toast";
  el.textContent = msg;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), 2600);
}

// ── 轨迹回放：把会话的截图串起来播，点击位置画圆点 ──────────────
// 不用录屏、不合成视频 —— 直接用已有截图当"简易录屏"。
// ⚠️ 坐标换算：x/y 是**原图坐标系**（size 是原图尺寸），
//    用百分比 `x/size_w*100%` 定位 → 图片被 CSS 缩成多大都不会偏。
async function replaySession(id) {
  let data;
  try {
    data = await api(`/api/sessions/${id}/replay`);
  } catch (e) {
    toast(`回放数据加载失败：${e.message}`);
    return;
  }
  if (!data || !data.total) {
    toast("这条记录没有可回放的截图");
    return;
  }

  const m = modal(`轨迹回放 · ${data.title}`, `
    <div class="rp">
      <div class="rp-stage">
        <img class="rp-img" alt="帧">
        <div class="rp-box"><div class="rp-dot hidden"></div></div>
        <div class="rp-gone hidden">🖼 该帧的截图已清理</div>
      </div>
      <div class="rp-sub"></div>
      <div class="rp-bar">
        <button class="ghost" data-rp="prev" title="上一帧">⏮</button>
        <button class="ghost rp-play" data-rp="play" title="播放/暂停">▶</button>
        <button class="ghost" data-rp="next" title="下一帧">⏭</button>
        <input class="rp-range" type="range" min="1" max="${data.total}" value="1">
        <span class="rp-count">1 / ${data.total}</span>
        <select class="rp-speed">
          <option value="2000">0.5×</option>
          <option value="1000" selected>1×</option>
          <option value="500">2×</option>
          <option value="250">4×</option>
        </select>
        <button class="ghost rp-recenter" data-rp="recenter" title="把窗口移回屏幕中央">⌖</button>
      </div>
      ${data.missing ? `<div class="rp-warn">⚠️ ${data.missing} 帧的截图已被清理，播放时显示占位</div>` : ""}
    </div>`);
  m.el.querySelector(".modal").classList.add("rp-modal");   // 宽版，图才看得清
  const drag = makeDraggable(m.el.querySelector(".modal"));
  $("[data-rp='recenter']", m.el).addEventListener("click", () => drag.reset());

  /* 舞台比例是 CSS 里的**常量 4/3**（电脑屏幕是宽的），任何会话、任何帧、任何朝向
     都不改动 —— 这才是「播放框一致」。图片自己 object-fit: contain：横屏帧
     （3840×2560）刚好铺满，竖屏帧（1904×3040）在固定区域里留左右边。将来真出现
     横竖切换，框也不会跳，只是那一帧的图留边而已。 */
  const img = $(".rp-img", m.el);
  const dot = $(".rp-dot", m.el);
  const box = $(".rp-box", m.el);
  const stage = $(".rp-stage", m.el);
  const sub = $(".rp-sub", m.el);
  const gone = $(".rp-gone", m.el);
  const range = $(".rp-range", m.el);
  const count = $(".rp-count", m.el);
  const playBtn = $(".rp-play", m.el);
  const speedSel = $(".rp-speed", m.el);

  let i = 0;
  let timer = null;

  // 图片解码完 / 窗口尺寸变了 → 重算内容区（圆点靠它对齐）
  img.addEventListener("load", layoutBox);
  const onResize = () => {
    if (!document.body.contains(m.el)) { window.removeEventListener("resize", onResize); return; }
    layoutBox();
  };
  window.addEventListener("resize", onResize);

  /* 把 .rp-box 摆到图片**真实内容区**上（object-fit: contain 之后的那块矩形）。
     舞台尺寸是固定的（CSS aspect-ratio 4/3），图片可能上下或左右留边 ——
     圆点必须在内容区里按百分比定位，否则会被留边带偏。 */
  function layoutBox() {
    const nw = img.naturalWidth, nh = img.naturalHeight;
    const bw = stage.clientWidth, bh = stage.clientHeight;   // ≈ 舞台内框
    if (!nw || !nh || !bw || !bh) { box.style.inset = "0"; return; }
    const s = Math.min(bw / nw, bh / nh);
    const cw = nw * s, ch = nh * s;
    box.style.left = `${(bw - cw) / 2}px`;
    box.style.top = `${(bh - ch) / 2}px`;
    box.style.width = `${cw}px`;
    box.style.height = `${ch}px`;
  }

  function show(n) {
    i = Math.max(0, Math.min(data.total - 1, n));
    const f = data.frames[i];
    range.value = i + 1;
    count.textContent = `${i + 1} / ${data.total}`;

    // ⚠️ 点击帧要展示**点击发生前**的画面，圆点才和画面对得上。
    //    act 的截图是动作执行后（settle 500ms）拍的"结果画面"，
    //    把圆点画在那上面必然错位。后端已算好 pre_evidence（上一帧的图）。
    const isClick = f.x != null && f.y != null;
    const usePre = isClick && f.pre_ok && f.pre_evidence;
    const shownEv = usePre ? f.pre_evidence : f.evidence;
    const shownOk = usePre ? true : f.ok;
    const [w, h] = (usePre ? f.pre_size : f.size) || [];

    // 图片缺失时不留破图；**舞台尺寸保持不变**（CSS 固定 aspect-ratio），
    // 否则整块会塌成一条，看着像页面坏了。
    // ⚠️ 用 `.hidden` class（项目约定，带 !important）而不是 `el.hidden` 属性 ——
    // 属性会被作者样式里的 display 盖掉（真实缺陷：图在却显示"已清理"占位）。
    gone.classList.toggle("hidden", !!shownOk);
    // ⚠️ 用 visibility 而非 display：display:none 会让 img 不占位、舞台塌掉
    img.style.visibility = shownOk ? "visible" : "hidden";
    if (shownOk) img.src = `/files/${shownEv}`;
    // ⚠️ 不按每帧原图改舞台 aspect-ratio —— 那正是「播放框忽大忽小」的根因。
    //    舞台比例打开时按会话主流朝向定一次，播放中恒定；图片自己 contain 缩放。

    // 点击位置圆点：在图片内容区里按**百分比**定位，缩放多少都准。
    // 坐标优先用后端算好的 hit_center（via 指向的那个节点的中心）——
    // 录制存的 x/y 和同帧 dump 偶尔不同步，照 x/y 画会"对不上步骤"。
    const hasDot = shownOk && w && h && isClick;
    dot.classList.toggle("hidden", !hasDot);
    const snapped = hasDot && !!(f.hit_center && f.hit_center.length === 2);
    dot.classList.toggle("rp-dot-snap", snapped);
    if (hasDot) {
      const cx = snapped ? f.hit_center[0] : f.x;
      const cy = snapped ? f.hit_center[1] : f.y;
      dot.style.left = `${(cx / w) * 100}%`;
      dot.style.top = `${(cy / h) * 100}%`;
    }
    // 图片是异步解码的：src 换了之后 naturalWidth 才有效 → 立刻算一次，
    // 同时挂 load 回调兜住「这次还没解码完」的情况。
    layoutBox();

    // 字幕条（在图下方，**不遮挡画面**）
    const parts = [];
    if (f.kind === "result") {
      // 末尾补的「最后一步之后的页面」：它不是一步操作，别写成"第 N 步"
      parts.push(`<span class="rp-sub-n">结果页面</span>`);
      parts.push(`<b>${esc(f.action || "结果页面")}</b>`);
      if (f.why) parts.push(esc(f.why));
    } else {
      parts.push(`<span class="rp-sub-n">第 ${f.seq} 步</span>`);
    }
    if (f.kind === "act") {
      parts.push(`<b>${esc(f.action || "操作")}</b>`);
      if (f.x != null) parts.push(`<code>${f.x}, ${f.y}</code>`);
      if (f.via) parts.push(`<code>${esc(f.via)}</code>`);
      if (f.why) parts.push(esc(f.why));
      // 圆点是不是吸附到了 via 指向的节点（没吸附成说明这一步的 dump 里找不到该节点）
      if (f.hit_center && f.hit_center.length === 2) {
        parts.push(`<span class="rp-snap-tag">圆点在 ${esc(f.via.split(/\s+/)[0] || "目标")} 上</span>`);
      }
    } else if (f.kind === "perm") {
      // 权限弹窗帧：圆点点在"允许/拒绝"按钮上（后端从 clicked.center 取的）
      parts.push(`<b>权限弹窗</b>`);
      if (f.x != null) parts.push(`<code>${f.x}, ${f.y}</code>`);
      if (f.via) parts.push(`<code>${esc(f.via)}</code>`);
      if (f.hit_center && f.hit_center.length === 2) {
        parts.push(`<span class="rp-snap-tag">圆点在按钮上</span>`);
      }
    } else if (f.kind !== "result") {
      parts.push(`<b>${esc(f.kind)}</b>`);
      if (f.detail) parts.push(esc(f.detail));
    }
    sub.innerHTML = parts.join('<span class="rp-sep">·</span>');
  }

  function stop() {
    if (timer) { clearInterval(timer); timer = null; }
    playBtn.textContent = "▶";
  }
  function play() {
    if (timer) { stop(); return; }
    playBtn.textContent = "⏸";
    const tick = () => {
      if (i >= data.total - 1) {
        // 播放到末帧：停播并把进度条/画面复位到起点，
        // 这样一次回放结束后再点播放会从头开始（关闭窗口后重开也是第 1 帧）。
        stop();
        show(0);
        return;
      }
      show(i + 1);
    };
    timer = setInterval(tick, Number(speedSel.value));
  }

  $("[data-rp='prev']", m.el).addEventListener("click", () => { stop(); show(i - 1); });
  $("[data-rp='next']", m.el).addEventListener("click", () => { stop(); show(i + 1); });
  playBtn.addEventListener("click", play);
  range.addEventListener("input", () => { stop(); show(Number(range.value) - 1); });
  speedSel.addEventListener("change", () => { if (timer) { stop(); play(); } });

  // 键盘：← → 翻帧，空格播放/暂停
  const onKey = ev => {
    if (!document.body.contains(m.el)) { document.removeEventListener("keydown", onKey); return; }
    if (ev.key === "ArrowLeft") { stop(); show(i - 1); }
    else if (ev.key === "ArrowRight") { stop(); show(i + 1); }
    else if (ev.key === " ") { ev.preventDefault(); play(); }
  };
  document.addEventListener("keydown", onKey);

  const origClose = $(".modal-x", m.el);
  origClose.addEventListener("click", () => {
    stop();
    window.removeEventListener("resize", onResize);
  });
  show(0);
}

/**
 * 断言下面的「证据」行。
 *
 * 两种引用要分开对待：
 *   1. 截图还在 → 可点链接。**文字用会话内序号 seq**（时间线上标的就是 #seq），
 *      锚点仍用 events.id（唯一）。之前文字写的是主键 id（10353 这种），
 *      跟时间线上的 #2 对不上，看着像「跳错了」。
 *   2. 截图已被清理（历史数据）→ 不给链接，只写「已清理」。
 *      点过去什么都没有，反而让人以为证据丢了。
 */
function evidenceLinks(s, f) {
  const refs = (f.event_refs || []).filter(r => r.seq != null);
  if (!refs.length) return "";
  const html = refs.map(r => {
    const label = `#${esc(r.seq)}`;
    return r.has_file
      ? `<a href="#event-${s.id}-${r.id}">${label}</a>`
      : `<span class="ev-gone-link" title="该事件的截图已被清理，事件条目本身还在">${label}<span class="ev-gone-tag">已清理</span></span>`;
  }).join(" · ");
  return `<div class="small">证据：${html}</div>`;
}

async function viewSession(id) {
  main.innerHTML = "<div class='sub'>加载中…</div>";
  const s = await api(`/api/sessions/${id}`);  const kindLabel = { observe: "👁 观察", act: "✋ 操作", read: "🔍 读取",
                      note: "📝 备注",
                      state: "⚙️ 状态", logcat: "📎 取证", crash: "💥 崩溃",
                      perm: "🔐 权限", vision: "👀 看图" };

  // 相邻事件时间差 = AI 决策耗时；事件自身 duration_ms = 工具执行耗时
  const ts = e => Date.parse(e.created_at);
  let prev = null;
  const gaps = {};
  for (const e of s.events) {
    if (prev !== null && !isNaN(ts(e)) && !isNaN(ts(prev)))
      gaps[e.seq] = Math.max(0, (ts(e) - ts(prev)) / 1000);
    prev = e;
  }
  const getAct = e => { try { return (JSON.parse(e.data_json || "{}").action) || {}; } catch { return {}; } };
  const getCrash = e => {
    try {
      const c = JSON.parse(e.data_json || "{}").crash;
      return c && c.scope ? c : null;
    } catch { return null; }
  };
  const scopeLabel = { target: "被测包", related: "关联包", other: "其他包" };
  const fmtGap = sec => sec >= 60 ? `${Math.floor(sec / 60)}m${Math.round(sec % 60)}s` : `${sec.toFixed(1)}s`;
  // 有无可回放的截图（图可能被 cleanup 轮转掉）→ 决定回放按钮是否可点
  const hasEv = (s.events || []).some(e => e.evidence);

  main.innerHTML = `
    <button class="ghost back">← 返回列表</button>
    <button class="ghost" data-replay="${s.id}" ${hasEv ? "" : "disabled"}
      title="${hasEv ? "回放本次执行的轨迹（截图串播）" : "该记录没有可用的截图，无法回放"}">▶ 回放轨迹</button>
    <div class="detail-head">
      <h1>${esc(s.title || `会话 #${s.id}`)} ${statusChip(s.status)}</h1>
      <div class="meta">模块 ${esc(s.package || "—")} · 设备 ${esc(s.device || "—")} ·
        开始 ${esc(fmtTime(s.started_at))} → 结束 ${esc(fmtTime(s.finished_at))} · 耗时 ${fmtDuration(durationOf(s))}</div>
      ${s.user_input ? `<div class="quote">用例口述：${esc(s.user_input)}</div>` : ""}
      <div class="findings">
        ${s.findings.map(f => `
          <div class="finding">${statusChip(f.status)}
            ${f.expect ? `<b>期望</b>${esc(f.expect)}` : ""}
            ${f.actual ? `<span class="small">　实际：${esc(f.actual)}</span>` : ""}
            ${f.note ? `<div class="small">${esc(f.note)}</div>` : ""}
            ${evidenceLinks(s, f)}
          </div>`).join("") || "<span class='muted'>无断言记录</span>"}
      </div>
    </div>
    <div class="final-result">
      <span class="final-label">最终结果</span>${statusChip(s.status)}
      <span class="final-summary">${esc(s.summary || (s.status === "running" ? "执行中…" : "未写结论"))}</span>
    </div>
    <h1 style="font-size:16px;margin:18px 0 12px">执行时间线</h1>
    <div class="timeline">
      ${s.events.map(e => {
        const act = getAct(e);
        const why = act.why || null;
        return `
        <div class="event ${esc(e.kind)}" id="event-${s.id}-${e.id}">
          <div class="event-head">
            <span class="event-tool">${kindLabel[e.kind] || esc(e.kind)} · ${esc(e.tool || "")}</span>
            ${e.duration_ms != null ? `<span class="metric">执行 ${fmtDuration(e.duration_ms / 1000)}</span>` : ""}
            ${gaps[e.seq] != null ? `<span class="metric">决策间隔 ${fmtGap(gaps[e.seq])}</span>` : ""}
            <span class="event-ts">#${e.seq} ${esc(fmtTime(e.started_at || e.created_at))}</span>
          </div>
          ${e.detail ? `<div class="event-detail">${esc(e.detail)}</div>` : ""}
          ${(() => {
            const c = getCrash(e);
            if (!c) return "";
            return `<div class="crash-box ${esc(c.scope)}">
              ⚠️ ${scopeLabel[c.scope] || c.scope} ${esc(c.type || "CRASH")} · ${esc(c.process || "?")}
              ${c.time ? `· ${esc(c.time)}` : ""}
              ${c.log_path ? ` · <a href="/files/${esc(c.log_path)}" target="_blank">崩溃前20s日志</a>` : "（无日志）"}
            </div>`;
          })()}
          ${why ? `<div class="why">理由：${esc(why)}</div>` : ""}
          ${(() => {
            const ev = e.evidence;
            if (!ev) return "";
            if (ev.endsWith(".png") || ev.endsWith(".jpg") || ev.endsWith(".jpeg"))
              // ⚠️ 图可能已被清理（cleanup.py 轮转 / 手工删除）——那时别留个破图，
              // 给个"证据已清理"的占位，记录与文本结果照常保留。
              return `<img class="shot" loading="lazy" src="/files/${esc(ev)}"
                onerror="this.outerHTML='<div class=&quot;ev-gone&quot;>🖼 证据已清理（${esc(ev.split("/").pop())}）</div>'">`;
            const name = ev.split("/").pop();
            const dl = e.kind === "logcat" || ev.endsWith(".log");
            return `<div class="ev-file">📎 <a href="/files/${esc(ev)}"
              target="_blank"${dl ? " download" : ""}>${esc(name)}</a></div>`;
          })()}
        </div>`;
      }).join("") || "<span class='muted'>无事件</span>"}
    </div>`;
  $(".back", main).addEventListener("click", viewSessions);
  const rpBtn = $("[data-replay]", main);
  if (rpBtn) rpBtn.addEventListener("click", () => replaySession(s.id));
  $$("img.shot", main).forEach(img =>
    img.addEventListener("click", () => {
      $("#lightbox-img").src = img.src;
      $("#lightbox").classList.remove("hidden");
    }));

  // 「证据：#N」跳转。
  //
  // 不能只靠浏览器的原生 hash 跳转：事件图是 loading="lazy"，点下去的瞬间
  // 上方那些图还没加载、高度是 0，浏览器按当时的布局算好落点就完事了；
  // 之后图陆续撑开，目标条目被顶到屏幕外（实测 1000182 点 #55 后
  // 目标落在视口下方 1920px，重设 hash 也没用 —— 布局早已不同）。
  // 所以：拦下点击、手动滚，并在图片陆续加载期间反复校正落点。
  //
  // 高亮用 class（.ev-hit）而不是 :target —— replaceState 改 hash 不会让
  // 浏览器重新计算 :target，样式会不亮。:target 规则留着兜底（直接粘贴带
  // # 的 URL 进来时浏览器自己会跳、会亮）。
  main.addEventListener("click", ev => {
    const a = ev.target.closest?.('a[href^="#event-"]');
    if (!a) return;
    const el = document.getElementById(a.getAttribute("href").slice(1));
    if (!el) return;
    ev.preventDefault();
    $$(".event.ev-hit", main).forEach(x => x.classList.remove("ev-hit"));
    el.classList.add("ev-hit");
    const settle = () => el.scrollIntoView({ block: "start" });
    settle();
    let n = 0;
    const again = () => {
      if (!document.body.contains(el) || n++ > 12) return;
      settle();
      setTimeout(again, 120);
    };
    setTimeout(again, 60);
    $$("img.shot", main).forEach(img => {
      if (!img.complete) img.addEventListener("load", settle, { once: true });
    });
    history.replaceState(null, "", "#" + el.id);
  });
}

$("#lightbox").addEventListener("click", () => $("#lightbox").classList.add("hidden"));

// ══════════════════ 用例库 ══════════════════
async function viewCaseLib() {
  activateNav("cases");
  main.innerHTML = "<div class='sub'>加载中…</div>";
  const [{ items }, { modules }] = await Promise.all([
    api("/api/cases"), api("/api/modules"),
  ]);

  // 按模块（包名）分类；用例自身的 package 优先，缺失归「未识别」
  const grpOf = c => c.package || "未识别";
  const groups = new Map();
  for (const c of items) {
    const g = grpOf(c);
    if (!groups.has(g)) groups.set(g, []);
    groups.get(g).push(c);
  }
  const groupNames = [...groups.keys()].sort();
  if (caseFilter !== "all" && !groups.has(caseFilter)) caseFilter = "all";
  const shown = caseFilter === "all" ? items : (groups.get(caseFilter) || []);

  const tabs = `
    <div class="tabs">
      <span class="tab ${caseFilter === "all" ? "on" : ""}" data-g="all">全部 ${items.length}</span>
      ${groupNames.map(g => `
        <span class="tab ${caseFilter === g ? "on" : ""}" data-g="${esc(g)}">
          ${esc(pkgShort(g))} ${groups.get(g).length}</span>`).join("")}
    </div>`;

  main.innerHTML = `
    <h1>用例库</h1>
    <div class="lib-toolbar">
      ${tabs}
      <span style="flex:1"></span>
      <button id="case-add">+ 添加用例</button>
    </div>
    <div class="list-table">
      <div class="lt-head">
        <span class="c-id">#</span>
        <span class="c-title">用例标题</span>
        <span class="c-pkg">模块</span>
        <span class="c-status">最近结论</span>
        <span class="c-run">跑过</span>
        <span class="c-start">最近运行</span>
        <span class="c-act">操作</span>
      </div>
      ${shown.map(c => `
        <div class="lt-row" data-id="${c.id}">
          <span class="c-id" title="${esc(c.id)}">${esc(caseNum(c))}</span>
          <span class="c-title" title="${esc(c.title || "(无标题)")}"><a class="row-link" data-id="${c.id}">${esc(c.title || "(无标题)")}</a></span>
          <span class="c-pkg" title="${esc(c.package || "")}">${esc(pkgShort(grpOf(c)))}</span>
          <span class="c-status">${statusChip(c.last_status) || "<span class='muted'>未执行</span>"}</span>
          <span class="c-run">${c.run_count ?? 0} 次</span>
          <span class="c-start">${esc(fmtTime(c.last_run_at))}</span>
          <span class="c-act">
            <button class="ghost sm" data-del="${c.id}">删除</button>
          </span>
        </div>`).join("") || "<div class='lt-empty'>该分类下暂无用例。跑完一次会话后用 <code>session.py case save --session &lt;id&gt;</code> 提升。</div>"}
    </div>`;

  $$(".tab", main).forEach(el =>
    el.addEventListener("click", () => { caseFilter = el.dataset.g; viewCaseLib(); }));
  $$(".row-link", main).forEach(el =>
    el.addEventListener("click", () => openCaseView(el.dataset.id)));
  $$("[data-del]", main).forEach(el =>
    el.addEventListener("click", () => confirmDeleteCase(el.dataset.del)));
  $("#case-add").addEventListener("click", openCaseAdd);
}

function openCaseAdd() {
  const m = modal("添加用例", `
    <label class="fld">标题
      <input id="ca-title" placeholder="如 联想日历_168_图库导入入口"></label>
    <label class="fld">模块（包名）
      <input id="ca-pkg" placeholder="如 com.zui.calendar"></label>
    <label class="fld">用例原文（口述的步骤 + 预期）
      <textarea id="ca-input" spellcheck="false"
        placeholder="前提：...&#10;操作步骤：&#10;1. ...&#10;预期结果：&#10;1. ..."></textarea></label>
    <div class="meta" id="ca-state"></div>`,
    `<button class="ghost" data-cancel>取消</button>
     <button data-ok>添加</button>`);
  $("[data-cancel]", m.el).addEventListener("click", m.close);
  $("[data-ok]", m.el).addEventListener("click", async () => {
    const st = $("#ca-state", m.el);
    st.textContent = "保存中…";
    try {
      await api("/api/cases", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          title: $("#ca-title", m.el).value,
          package: $("#ca-pkg", m.el).value,
          input: $("#ca-input", m.el).value,
        }),
      });
      m.close();
      viewCaseLib();
    } catch (e) {
      st.textContent = "保存失败：" + e.message;
    }
  });
}

async function openCaseView(id) {
  const c = await api(`/api/cases/${id}`);
  // 该用例是否已有程序化描述（YAML）。有则开第二个页签，**只读**。
  let spec = null;
  try {
    const { items } = await api("/api/case-specs");
    spec = (items || []).find(x => String(x.case_id) === String(c.id)) || null;
  } catch { /* 用例描述接口不可用时，静默降级为只有原文 */ }

  const tabs = spec
    ? `<div class="casetabs">
         <button class="casetab active" data-tab="src">用例原文</button>
         <button class="casetab" data-tab="yaml">程序化描述 (YAML)</button>
       </div>`
    : "";

  // 只有真有 .md 原文的用例才能编辑（只有 _steps.yaml 的孤立产物没有原文）
  const canEdit = !!c.has_md;

  const m = modal(`用例 #${c.id} · ${c.title || ""}`, `
    <div class="meta" style="margin-bottom:8px">
      模块 ${esc(c.package || "未识别")} · 跑过 ${c.run_count ?? 0} 次 ·
      最近 ${esc(fmtTime(c.last_run_at))} ${statusChip(c.last_status)} ·
      关联记录 ${c.last_session_id ? "#" + c.last_session_id : "—"}
    </div>
    ${tabs}
    <div data-pane="src">
      <div class="panel-title">用例原文</div>
      <pre class="case-pre">${esc(c.input || "(空)")}</pre>
    </div>
    ${spec ? `<div data-pane="yaml" hidden>
      <div class="panel-title">程序化描述
        <span class="muted" style="font-weight:400;font-size:12px">
          · ${esc(spec.name)} · <b>只读</b>（${spec.readonly_reason || "由会话沉淀生成"}）</span>
      </div>
      <pre class="case-pre" id="cyaml">加载中…</pre>
    </div>` : ""}`,
    `<span class="muted" id="c-edit-state" style="margin-right:auto"></span>
     <button class="ghost" data-cancel>关闭</button>
     ${canEdit ? `<button data-edit>编辑原文</button>` : ""}`);
  $("[data-cancel]", m.el).addEventListener("click", m.close);

  if (canEdit) {
    // 编辑态：把「用例原文」那块 pre 换成 textarea，底部按钮换成 保存 / 取消
    const pane = $('[data-pane="src"]', m.el);
    const srcBtn = $("[data-edit]", m.el);
    let draft = c.input || "";

    const enterEdit = () => {
      srcBtn.hidden = true;
      $("[data-cancel]", m.el).hidden = true;
      $(".casetab", m.el) && $$(".casetab", m.el).forEach(b => { b.disabled = true; });
      pane.innerHTML = `
        <div class="panel-title">用例原文 · 编辑中
          <span class="muted" style="font-weight:400;font-size:12px">
            · 保存后直接写回 <code>${esc(c.package)}/${esc(c.id)}.md</code></span></div>
        <textarea class="case-edit" spellcheck="false">${esc(draft)}</textarea>`;
      const ta = $(".case-edit", pane);
      ta.focus();
      const foot = $(".modal-foot", m.el);
      const saveBtn = document.createElement("button");
      saveBtn.textContent = "保存";
      const cancelBtn = document.createElement("button");
      cancelBtn.className = "ghost";
      cancelBtn.textContent = "取消";
      foot.appendChild(cancelBtn);
      foot.appendChild(saveBtn);

      cancelBtn.addEventListener("click", () => { m.close(); openCaseView(c.id); });
      saveBtn.addEventListener("click", async () => {
        const content = ta.value;
        if (!content.trim()) { $("#c-edit-state", m.el).textContent = "原文不能为空"; return; }
        saveBtn.disabled = true;
        $("#c-edit-state", m.el).textContent = "保存中…";
        try {
          const r = await api(`/api/cases/${encodeURIComponent(c.id)}/content`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ content }),
          });
          const kb = (r.bytes / 1024).toFixed(1);
          toast(`已保存 · ${r.path} · ${kb} KB` +
                (r.db_synced ? " · 库记录已同步" : ""));
          m.close();
          openCaseView(c.id);   // 重新拉一次，显示写回后的真实内容
        } catch (e) {
          saveBtn.disabled = false;
          $("#c-edit-state", m.el).textContent = "保存失败：" + e.message;
        }
      });
    };
    srcBtn.addEventListener("click", enterEdit);
  }

  if (spec) {
    // 惰性加载：切到 YAML 页签时才取内容
    let loaded = false;
    const showTab = async which => {
      $$(".casetab", m.el).forEach(b => b.classList.toggle("active", b.dataset.tab === which));
      $$("[data-pane]", m.el).forEach(p => { p.hidden = p.dataset.pane !== which; });
      if (which === "yaml" && !loaded) {
        loaded = true;
        try {
          const { content } = await api(`/api/case-specs/${encodeURIComponent(spec.title)}`);
          $("#cyaml", m.el).textContent = content;
        } catch (e) {
          $("#cyaml", m.el).textContent = "读取失败：" + e.message;
        }
      }
    };
    $$(".casetab", m.el).forEach(b =>
      b.addEventListener("click", () => { if (!b.disabled) showTab(b.dataset.tab); }));
  }
}

// 用例**原文**可改（写回 assets/cases/<应用>/<stem>.md，格式统一 CRLF）。
// 程序化描述（_steps.yaml）仍只读：它由"已验证成功的会话"沉淀，
// 手工改动会破坏它与证据/知识卡的对应关系 —— 改原文后重跑再沉淀即可。


function confirmDeleteCase(id) {
  const m = modal("删除用例", `
    <div class="warn-text">确定删除用例 <b>#${esc(id)}</b> 吗？</div>
    <div class="meta">将<b>一并删除</b>：<br>
      · 该用例的<b>全部历史测试记录</b>（含截图、dump、日志）<br>
      · 该用例的程序化描述文件（.yaml / .md）<br>
      <b class="danger-text">不可恢复。</b></div>`,
    `<button class="ghost" data-cancel>取消</button>
     <button class="danger" data-ok>确认删除</button>`);
  $("[data-cancel]", m.el).addEventListener("click", m.close);
  $("[data-ok]", m.el).addEventListener("click", async () => {
    const btn = $("[data-ok]", m.el);
    btn.disabled = true;
    btn.textContent = "删除中…";
    try {
      const r = await api(`/api/cases/${id}`, { method: "DELETE" });
      m.close();
      viewCaseLib();
      toast(`已删除用例 #${id}` +
            (r && r.sessions ? `，连带 ${r.sessions} 条测试记录` : "") +
            (r && r.spec_files ? `、${r.spec_files} 个描述文件` : ""));
    } catch (e) {
      btn.disabled = false;
      btn.textContent = "确认删除";
      toast("删除失败：" + e.message);
    }
  });
}

// ══════════════════ 视觉模型 ══════════════════
async function viewVision() {
  activateNav("vision");
  const c = await api("/api/vision");
  main.innerHTML = `
    <h1>视觉模型</h1>
    <div class="card" style="max-width:620px;cursor:default">
      <div class="kb-bar">
        <b>OpenAI 兼容配置</b>
        <span style="flex:1"></span>
        <span class="muted" id="vs-state"></span>
        <button class="ghost" id="vs-test">测试连接</button>
        <button id="vs-save">保存</button>
      </div>
      <div class="form">
        <label>Base URL<input id="vs-url" placeholder="https://api.example.com/v1" value="${esc(c.base_url || "")}"></label>
        <label>Model<input id="vs-model" placeholder="gpt-4o / qwen-vl-max / glm-4v ..." value="${esc(c.model || "")}"></label>
        <label>API Key<input id="vs-key" type="password" placeholder="${esc(c.api_key_masked || "未配置")}" value=""></label>
        <div class="meta">Key 只显示掩码；留空保存则保留原值。配置文件：storage/vision.json（不入库）。</div>
      </div>
      <div id="vs-result"></div>
    </div>`;

  $("#vs-save").addEventListener("click", async () => {
    const btn = $("#vs-save");
    btn.disabled = true;
    $("#vs-state").textContent = "保存中…";
    await api("/api/vision", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        base_url: $("#vs-url").value,
        model: $("#vs-model").value,
        api_key: $("#vs-key").value,
      }),
    });
    $("#vs-key").value = "";
    $("#vs-state").textContent = "已保存 " + fmtTime(new Date().toISOString());
    btn.disabled = false;
  });

  // 测试连接：先保存当前表单（否则测的是旧配置），再用后端的真实请求验证
  $("#vs-test").addEventListener("click", async () => {
    const btn = $("#vs-test");
    btn.disabled = true;
    const box = $("#vs-result");
    box.innerHTML = `<div class="test-box pending">⏳ 正在用当前配置发起一次真实请求（最多 30s）…</div>`;
    try {
      await api("/api/vision", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          base_url: $("#vs-url").value,
          model: $("#vs-model").value,
          api_key: $("#vs-key").value,
        }),
      });
      const r = await api("/api/vision/test", { method: "POST" });
      if (r.ok) {
        box.innerHTML = `<div class="test-box ok">
          ✅ <b>连接成功</b> · 耗时 ${r.latency_ms}ms · 模型 <code>${esc(r.model)}</code>
          <div class="meta">模型回复：${esc(r.reply || "(空)")}</div>
          ${r.usage ? `<div class="meta">tokens: ${esc(JSON.stringify(r.usage))}</div>` : ""}
        </div>`;
      } else {
        box.innerHTML = `<div class="test-box err">
          ❌ <b>连接失败</b>：${esc(r.error || "未知错误")}
          ${r.detail ? `<div class="meta">响应：${esc(r.detail)}</div>` : ""}
          ${r.hint ? `<div class="meta">💡 ${esc(r.hint)}</div>` : ""}
        </div>`;
      }
    } catch (e) {
      box.innerHTML = `<div class="test-box err">❌ 请求异常：${esc(e.message)}</div>`;
    }
    btn.disabled = false;
  });
}

// ══════════════════ 知识库 ══════════════════
async function viewKnowledge(name) {
  activateNav("knowledge");
  main.innerHTML = "<div class='sub'>加载中…</div>";
  const { items } = await api("/api/knowledge");
  if (!kbCurrent && items.length) kbCurrent = name || items[0].name;
  let editor = "";
  if (kbCurrent) {
    const { content } = await api(`/api/knowledge/${encodeURIComponent(kbCurrent)}`);
    editor = `
      <div class="kb-editor">
        <div class="kb-bar">
          <b>${esc(kbCurrent)}</b>
          <span style="flex:1"></span>
          <span class="muted" id="kb-state"></span>
          <button id="kb-save">保存</button>
        </div>
        <textarea id="kb-text" spellcheck="false">${esc(content)}</textarea>
      </div>`;
  }
  main.innerHTML = `
    <h1>知识库</h1>
    <div class="kb">
      <div class="kb-list">
        ${items.map(i => `
          <div class="card" data-name="${esc(i.name)}" style="${i.name === kbCurrent ? "border-color:var(--accent)" : ""}">
            <div class="title" style="font-size:13.5px">${esc(i.name)}</div>
            <div class="meta">${i.size} 字节 · ${esc(fmtTime(i.mtime))}</div>
          </div>`).join("")}
      </div>
      ${editor}
    </div>`;
  $$(".kb-list .card", main).forEach(el =>
    el.addEventListener("click", () => { kbCurrent = el.dataset.name; viewKnowledge(); }));
  const ta = $("#kb-text");
  if (ta) {
    $("#kb-save").addEventListener("click", async () => {
      const btn = $("#kb-save");
      btn.disabled = true;
      $("#kb-state").textContent = "保存中…";
      await api(`/api/knowledge/${encodeURIComponent(kbCurrent)}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ content: ta.value }),
      });
      $("#kb-state").textContent = "已保存 " + fmtTime(new Date().toISOString());
      btn.disabled = false;
    });
  }
}

activateNav("dashboard");
viewDashboard();


/* ── 常驻主题按钮（sidebar 底部，所有视图都能切换）──
   viewSessions() 里的按钮是每次重渲染生成的，那边已各自绑定；
   这里只负责 sidebar 里那个不随视图重建的按钮。 */
(function initGlobalTheme() {
  // 主题切换按钮现在只在「测试记录」页工具行（viewSessions 里每次重渲染都会绑定），
  // sidebar 里那个重复的已移除；这里只负责首屏同步按钮文字。
  syncThemeButtons();
})();
