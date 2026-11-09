/* Android AI 测试台前端（零依赖 vanilla JS + echarts） */
"use strict";

const $ = (sel, el = document) => el.querySelector(sel);
const $$ = (sel, el = document) => Array.from(el.querySelectorAll(sel));
const main = $("#main");
let kbCurrent = null;    // 当前编辑的知识卡名
let caseFilter = "all";  // 用例库当前模块过滤
let showDiag = false;    // 测试记录是否显示验证/调试会话

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
async function viewSessions() {
  activateNav("sessions");
  main.innerHTML = "<div class='sub'>加载中…</div>";
  const { sessions, kinds } = await api("/api/sessions" + (showDiag ? "?all=1" : ""));
  const diagN = (kinds && kinds.diag) || 0;
  const testN = (kinds && kinds.test) || 0;

  main.innerHTML = `
    <h1>测试记录</h1>
    <div class="lib-toolbar">
      <div class="sub" style="margin:0">正式测试 <b>${testN}</b> 条${diagN ? ` · 验证/调试 <b>${diagN}</b> 条（默认不显示）` : ""}</div>
      <span style="flex:1"></span>
      ${diagN ? `<label class="chk"><input type="checkbox" id="show-diag"${showDiag ? " checked" : ""}>显示验证记录</label>` : ""}
    </div>
    <div class="list-table recs">
      <div class="lt-head">
        <span class="c-id">#</span>
        <span class="c-title">标题</span>
        <span class="c-status">结论</span>
        <span class="c-pkg">模块</span>
        <span class="c-start">开始时间</span>
        <span class="c-dur">耗时</span>
        <span class="c-cnt">事件/断言</span>
        <span class="c-act">操作</span>
      </div>
      ${sessions.map(s => `
        <div class="lt-row${s.kind === "diag" ? " diag" : ""}" data-id="${s.id}">
          <span class="c-id" title="${esc(s.id)}">${esc(sidShort(s.id))}</span>
          <span class="c-title" title="${esc(s.title || `会话 #${s.id}`)}">
            ${s.kind === "diag" ? '<span class="tag-diag">验证</span>' : ""}
            <a class="row-link" data-id="${s.id}">${esc(s.title || `会话 #${s.id}`)}</a>
          </span>
          <span class="c-status">${statusChip(s.status)}</span>
          <span class="c-pkg" title="${esc(s.package || "")}">${esc(pkgShort(s.package))}</span>
          <span class="c-start">${esc(fmtTime(s.started_at))}</span>
          <span class="c-dur">${fmtDuration(s.duration_seconds)}</span>
          <span class="c-cnt">${s.event_count} / ${s.finding_count}</span>
          <span class="c-act">
            <button class="ghost sm" data-replay="${s.id}"
              ${s.has_evidence ? "" : "disabled"}
              title="${s.has_evidence ? "回放本次执行的轨迹（截图串播）" : "该记录没有可用的截图，无法回放"}">▶ 回放</button>
            <button class="ghost sm danger" data-del="${s.id}" title="删除该记录及其截图/日志">删除</button>
          </span>
        </div>`).join("") || "<div class='lt-empty'>还没有记录。用 <code>session.py start</code> 开启会话后由 AI 执行用例。</div>"}
    </div>`;

  const cb = $("#show-diag");
  if (cb) cb.addEventListener("change", () => { showDiag = cb.checked; viewSessions(); });
  $$(".row-link", main).forEach(el =>
    el.addEventListener("click", () => viewSession(el.dataset.id)));
  $$("[data-del]", main).forEach(el =>
    el.addEventListener("click", ev => {
      ev.stopPropagation();          // 不能触发"点击查看"
      confirmDeleteSession(el.dataset.del);
    }));
  $$("[data-replay]", main).forEach(el =>
    el.addEventListener("click", ev => {
      ev.stopPropagation();
      replaySession(el.dataset.replay);
    }));
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
        <div class="rp-dot hidden"></div>
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
      </div>
      ${data.missing ? `<div class="rp-warn">⚠️ ${data.missing} 帧的截图已被清理，播放时显示占位</div>` : ""}
    </div>`);
  m.el.querySelector(".modal").classList.add("rp-modal");   // 宽版，图才看得清

  const img = $(".rp-img", m.el);
  const dot = $(".rp-dot", m.el);
  const sub = $(".rp-sub", m.el);
  const gone = $(".rp-gone", m.el);
  const range = $(".rp-range", m.el);
  const count = $(".rp-count", m.el);
  const playBtn = $(".rp-play", m.el);
  const speedSel = $(".rp-speed", m.el);

  let i = 0;
  let timer = null;

  function show(n) {
    i = Math.max(0, Math.min(data.total - 1, n));
    const f = data.frames[i];
    range.value = i + 1;
    count.textContent = `${i + 1} / ${data.total}`;

    // 图片缺失时不留破图；**舞台尺寸保持不变**（靠 CSS 的 aspect-ratio），
    // 否则整块会塌成一条，看着像页面坏了。
    // ⚠️ 用 `.hidden` class（项目约定，带 !important）而不是 `el.hidden` 属性 ——
    // 属性会被作者样式里的 display 盖掉（真实缺陷：图在却显示"已清理"占位）。
    gone.classList.toggle("hidden", !!f.ok);
    // ⚠️ 用 visibility 而非 display：display:none 会让 img 不占位、舞台塌掉
    img.style.visibility = f.ok ? "visible" : "hidden";
    if (f.ok) img.src = `/files/${f.evidence}`;
    if (f.size) $(".rp-stage", m.el).style.aspectRatio = `${f.size[0]} / ${f.size[1]}`;

    // 点击位置圆点：用**百分比**换算，图片缩放多少都准
    const [w, h] = f.size || [];
    const hasDot = f.ok && w && h && f.x != null && f.y != null;
    dot.classList.toggle("hidden", !hasDot);
    if (hasDot) {
      dot.style.left = `${(f.x / w) * 100}%`;
      dot.style.top = `${(f.y / h) * 100}%`;
    }

    // 字幕条（在图下方，**不遮挡画面**）
    const parts = [`<span class="rp-sub-n">第 ${f.seq} 步</span>`];
    if (f.kind === "act") {
      parts.push(`<b>${esc(f.action || "操作")}</b>`);
      if (f.x != null) parts.push(`<code>${f.x}, ${f.y}</code>`);
      if (f.via) parts.push(`<code>${esc(f.via)}</code>`);
      if (f.why) parts.push(esc(f.why));
    } else {
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
      if (i >= data.total - 1) { stop(); return; }
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
  origClose.addEventListener("click", stop);
  show(0);
}

async function viewSession(id) {
  main.innerHTML = "<div class='sub'>加载中…</div>";
  const s = await api(`/api/sessions/${id}`);  const kindLabel = { observe: "👁 观察", act: "✋ 操作", read: "🔍 读取",
                      note: "📝 备注",
                      state: "⚙️ 状态", logcat: "📎 取证", crash: "💥 崩溃",
                      perm: "🔐 权限" };

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
            ${(f.event_ids || []).length ? `<div class="small">证据：
              ${f.event_ids.map(id => `<a href="#event-${s.id}-${id}">事件 ${esc(id)}</a>`).join(" · ")}
            </div>` : ""}
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
    `<button class="ghost" data-cancel>关闭</button>`);
  $("[data-cancel]", m.el).addEventListener("click", m.close);

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
      b.addEventListener("click", () => showTab(b.dataset.tab)));
  }
}

// ⚠️ 用例**不提供 WebUI 修改入口**（有意为之，勿加回）：
//    case.input（用例原文）与同名 YAML（程序化描述）是同一份用例的两种形态，
//    一旦只改其中一个，两者就会静默不一致 —— 属于"合法但内容对不上"，
//    和"YAML 能解析但中文已被编码毁掉"是同一类问题。
//    正确姿势：改原文 → 重跑用例 → 重新沉淀 YAML。该流程在 WebUI 之外完成。
//    服务端 POST /api/cases/<id> 也已一并拒绝（见 webui.py）。


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
