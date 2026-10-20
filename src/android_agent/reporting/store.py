# -*- coding: utf-8 -*-
"""把每次运行落进 SQLite，供 WebUI 查。

## 为什么现在才需要数据库

在此之前，一次运行的产出是：
    tmp/clicks_176.jsonl   账本
    tmp/report_176.json    结论
    tmp/llm_176.md         模型对话

这三样**每次都覆盖**。于是"上次跑和这次跑有什么区别"根本无从回答 ——
而这恰恰是 WebUI 最该回答的问题（同一个用例今天过了、昨天没过，
是代码改了还是 App 改了？）。

所以这里不是"给 jq 加个壳"，而是**把覆盖改成追加**：
每次运行分配一个 run_id，历史全部留下。

## 表和真实数据怎么对应

我们的账本行已经是扁平的、字段齐全的（seq/action/target/before/after/
ms/changed/why/shot_*），所以不学 skill 那样拆成 events + data_json 两层 ——
直接一列一列存。好处是 SQL 里能直接按 changed/action 过滤、按 ms 排序，
不用把 JSON 捞出来在 Python 里筛。

    runs     一次运行 = 一行（用例、状态、耗时、自动化率、日志路径）
    actions  账本里的一行 = 一行（点击/输入/按键 + 证据路径）
    findings 报告里的一条断言/问题 = 一行
    (模型对话不落库，只存路径 —— 它有几百 KB，进库只会拖慢查询)

## 为什么不复用 skill 的 db.py

看过了：它 5 张表，核心是 `events` + `data_json` 泛型存储，
还带着 `cases`/`modules`/`knowledge` 的管理接口 —— 那些我们没有对应的东西
（用例是 YAML 在磁盘上，知识卡也在磁盘上）。
硬搬过来会有三张永远空着的表，而我们的真实字段（ms_click、
shot_before、changed）反而要挤进 data_json 里，查询就废了。

只借它的**思路**（run 一张表 + 明细一张表 + 结论一张表），字段按我们的来。
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

_DB_DEFAULT = "assets/storage/runs.db"

# 建表语句。只用 CREATE TABLE IF NOT EXISTS，重复启动不会破坏数据。
_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    -- 用例标识。用 YAML 文件名（不含扩展名）最稳：
    -- 数字 id（176）在报告里是 int，在文件名里是字符串，两边对不齐会查不到。
    case_name   TEXT NOT NULL,
    case_id     TEXT,
    case_title  TEXT,
    status      TEXT,              -- PASS / FAIL / BLOCKED
    summary     TEXT,
    steps_done  INTEGER,
    steps_total INTEGER,
    auto_rate   REAL,              -- 代码直点占比
    elapsed_s   REAL,
    by_model    INTEGER,           -- 交给模型判定的步数
    by_code     INTEGER,           -- 代码直点的步数
    engine      TEXT,
    started_at  TEXT,
    started_ts  REAL,
    finished_at TEXT,
    -- 产物路径：账本/日志/模型对话/报告。故意只存路径不存内容 ——
    -- 模型对话动辄几百 KB，进库会让列表查询变慢。
    ledger_path TEXT,
    log_path    TEXT,
    llm_path    TEXT,
    report_path TEXT,
    -- ★ 这次跑的是**哪个用例文件**。没有它，报告只能告诉你 "168 跑过了"，
    --   却指不回"当时那份 YAML 长什么样"—— 而 YAML 是会变的
    --   （168 就改过 a3 的判据）。光看结论没法复核判据对不对。
    --
    --   存两份：
    --     case_yaml_path  文件路径，点进去能看当前版本
    --     case_yaml       跑的那一刻的内容快照
    --
    --   为什么要快照：YAML 改过之后，旧记录如果只存路径，看到的
    --   就是**新判据配上旧结论**，等于在骗自己。
    case_yaml_path TEXT,
    case_yaml   TEXT,
    report_json TEXT              -- 完整报告，用于"详情页要什么给什么"
);

CREATE TABLE IF NOT EXISTS actions (
    action_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      INTEGER NOT NULL REFERENCES runs(run_id),
    seq         INTEGER,
    step        TEXT,
    action      TEXT,             -- click / input / key:back
    -- 目标拆成三列：页面上按 rid 搜、按文案搜都用得上
    rid         TEXT,
    text        TEXT,             -- 控件的文字，或输入进去的内容
    x           INTEGER,
    y           INTEGER,
    before_act  TEXT,
    after_act   TEXT,
    changed     INTEGER,          -- 1 有进展 / 0 没进展 / NULL 判不了
    ms          REAL,
    ms_click    REAL,
    ms_observe  REAL,
    why         TEXT,
    perm        TEXT,
    error       TEXT,
    -- 证据：截图和 XML 的路径。列表页用它渲染缩略图，点开看大图。
    shot_before TEXT,
    shot_after  TEXT,
    xml_before  TEXT,
    xml_after   TEXT
);

CREATE TABLE IF NOT EXISTS findings (
    finding_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      INTEGER NOT NULL REFERENCES runs(run_id),
    kind        TEXT,             -- assert / issue
    fid         TEXT,             -- a1 / a2 …（断言编号）
    status      TEXT,             -- PASS / FAIL / UNKNOWN
    descr       TEXT,
    expected    TEXT,
    actual      TEXT,
    note        TEXT
);

CREATE INDEX IF NOT EXISTS idx_runs_case   ON runs(case_name, run_id DESC);
CREATE INDEX IF NOT EXISTS idx_actions_run ON actions(run_id, seq);
CREATE INDEX IF NOT EXISTS idx_findings_run ON findings(run_id);
"""


def db_path(skill_root: str | Path | None = None) -> Path:
    """库文件位置。默认放 assets/storage/runs.db。"""
    if skill_root:
        return Path(skill_root) / "storage" / "runs.db"
    import os
    root = os.environ.get("ANDROID_SKILL_ROOT")
    if root:
        return Path(root) / "storage" / "runs.db"
    return Path(_DB_DEFAULT)


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    """打开（必要时建）库。返回的连接 row_factory 已设成 dict。"""
    p = Path(path) if path else db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(p), timeout=10.0)
    con.row_factory = sqlite3.Row
    con.executescript(_SCHEMA)
    _migrate(con)
    return con


#: 建表之后又加的列：(表, 列, 类型)。老库靠它补齐。
#:
#: ⚠️ CREATE TABLE IF NOT EXISTS 对**已存在**的表完全不生效，
#:    所以加了新列之后老库不会自动多出来。表现是 insert 报
#:    "table runs has no column named case_yaml_path" —— 而我们的
#:    save_run 是吞异常的，于是变成"跑完了但没落库"这种难查的问题。
_ADDED_COLUMNS = (
    ("runs", "case_yaml_path", "TEXT"),
    ("runs", "case_yaml", "TEXT"),
)


def _migrate(con: sqlite3.Connection) -> None:
    """把老库缺的列补上。幂等，每次 connect 都跑（一次 PRAGMA 很便宜）。"""
    for table, col, typ in _ADDED_COLUMNS:
        try:
            have = {r["name"] for r in
                    con.execute(f"PRAGMA table_info({table})").fetchall()}
        except sqlite3.Error:
            continue
        if not have or col in have:
            continue
        try:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
            con.commit()
        except sqlite3.Error:
            # 加列失败不该让整个库打不开 —— 老数据还能看。
            # 但要让调用方知道，所以不做静默 pass，由上层报。
            pass


# ---------- 写入 -----------------------------------------------------------

def _target_parts(t: dict[str, Any] | None) -> tuple[str, str, int | None, int | None]:
    """把账本的 target（形状不固定）拆成 rid / text / x / y。

    target 有三种形态（都是历史原因，别指望统一）：
        {"rid": "save_view", "text": "完成", "c": [2913, 202]}
        {"c": [552, 648]}                     只有坐标
        {"key": "back"}                       按键
    拆出来存，是为了页面上能直接按 rid 搜 —— 存成一坨 JSON 就搜不了。
    """
    t = t or {}
    rid = str(t.get("rid") or t.get("key") or "")
    text = str(t.get("text") or t.get("key") or "")
    c = t.get("c") or []
    x = int(c[0]) if len(c) >= 1 and isinstance(c[0], (int, float)) else None
    y = int(c[1]) if len(c) >= 2 and isinstance(c[1], (int, float)) else None
    return rid, text, x, y


def save_run(
    *,
    case_name: str,
    report: dict[str, Any],
    ledger_rows: list[dict[str, Any]],
    paths: dict[str, str] | None = None,
    case_yaml_path: str = "",
    case_yaml: str = "",
    db: str | Path | None = None,
) -> int:
    """把一次运行整体落库，返回 run_id。

    ## 为什么一次写完好，而不是边跑边写

    边跑边写的好处是"跑到一半崩了也有记录"。但代价是并发（WebUI 正在读）
    和事务边界都得管。而我们的 runner 本身有 try/finally，
    崩了也会走到收尾 —— 所以一次性写入更简单，也不会看到半截数据。

    真要"实时看进度"，那是另一个功能（tail 日志），不该靠半截库凑。

    ## case_yaml_path / case_yaml

    记下这次跑的是**哪个文件**、**当时那份内容**。
    只存路径不够：YAML 会被改（168 的 a3 判据就改过），
    旧记录只存路径的话，点开看到的是"新判据 + 旧结论"，等于自欺。
    """
    con = connect(db)
    try:
        strong = report.get("strong_stats") or {}
        started_ts = None
        prog = report.get("progress") or []
        if prog:
            started_ts = prog[0].get("t")
        elapsed = report.get("elapsed_s")
        finished_ts = (float(started_ts) + float(elapsed)) if (
            started_ts and elapsed is not None) else None

        cur = con.execute(
            """INSERT INTO runs(
                case_name, case_id, case_title, status, summary,
                steps_done, steps_total, auto_rate, elapsed_s,
                by_model, by_code, engine,
                started_at, started_ts, finished_at,
                ledger_path, log_path, llm_path, report_path,
                case_yaml_path, case_yaml, report_json
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                case_name,
                str(report.get("case_id") or ""),
                case_name,
                report.get("status"),
                report.get("summary"),
                report.get("steps_done"),
                _steps_total(report),
                strong.get("auto_rate"),
                elapsed,
                strong.get("by_model"),
                strong.get("by_code"),
                report.get("engine"),
                _fmt_ts(started_ts),
                started_ts,
                _fmt_ts(finished_ts),
                (paths or {}).get("ledger"),
                (paths or {}).get("log"),
                (paths or {}).get("llm"),
                (paths or {}).get("report") or report.get("report_path"),
                case_yaml_path or None,
                case_yaml or None,
                json.dumps(report, ensure_ascii=False),
            ),
        )
        run_id = int(cur.lastrowid)

        for r in ledger_rows:
            rid, text, x, y = _target_parts(r.get("target"))
            con.execute(
                """INSERT INTO actions(
                    run_id, seq, step, action, rid, text, x, y,
                    before_act, after_act, changed, ms, ms_click, ms_observe,
                    why, perm, error, shot_before, shot_after,
                    xml_before, xml_after
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    run_id,
                    r.get("seq"),
                    str(r.get("step") or ""),
                    str(r.get("action") or "click"),
                    rid, text, x, y,
                    str(r.get("before") or ""),
                    str(r.get("after") or ""),
                    None if r.get("changed") is None else int(bool(r.get("changed"))),
                    r.get("ms"), r.get("ms_click"), r.get("ms_observe"),
                    str(r.get("why") or ""),
                    str(r.get("perm") or ""),
                    str(r.get("error") or ""),
                    r.get("shot_before"), r.get("shot_after"),
                    r.get("xml_before"), r.get("xml_after"),
                ),
            )

        for f in (report.get("findings") or []):
            con.execute(
                """INSERT INTO findings(
                    run_id, kind, fid, status, descr, expected, actual, note
                ) VALUES(?,?,?,?,?,?,?,?)""",
                (
                    run_id,
                    str(f.get("kind") or "assert"),
                    str(f.get("id") or ""),
                    str(f.get("status") or ""),
                    str(f.get("desc") or ""),
                    str(f.get("expected") or ""),
                    str(f.get("actual") or ""),
                    str(f.get("note") or ""),
                ),
            )
        con.commit()
        return run_id
    finally:
        con.close()


def _steps_total(report: dict[str, Any]) -> int | None:
    """用例总步数。

    报告里有 `steps_done: 7`，但"共几步"只有 summary 里那句
    「步骤 7/7」带着。从那里抠出来 —— 比再去读一遍 YAML 稳
    （YAML 可能刚改过，和这次运行对不上）。
    """
    import re
    m = re.search(r"步骤\s*(\d+)\s*/\s*(\d+)", str(report.get("summary") or ""))
    if m:
        return int(m.group(2))
    return None


def _fmt_ts(ts: Any) -> str:
    if not ts:
        return ""
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(ts)))
    except (TypeError, ValueError, OSError):
        return ""


# ---------- 读取（给 WebUI 用）--------------------------------------------

def list_runs(*, case_name: str = "", limit: int = 100,
              db: str | Path | None = None) -> list[dict[str, Any]]:
    """运行列表，新的在前。

    不返回 report_json / case_yaml —— 列表页用不上，还占带宽
    （case_yaml 一个就 5-8KB，100 条就是近 1MB）。
    列表里只带 case_yaml_path，够显示"这条跑的是哪个文件"了。
    """
    con = connect(db)
    try:
        sql = ("SELECT run_id, case_name, case_id, case_title, status, summary,"
               " steps_done, steps_total, auto_rate, elapsed_s, by_model, by_code,"
               " engine, started_at, finished_at, ledger_path, log_path, llm_path,"
               " case_yaml_path,"
               " (case_yaml IS NOT NULL) AS has_yaml"
               " FROM runs")
        args: list[Any] = []
        if case_name:
            sql += " WHERE case_name = ?"
            args.append(case_name)
        sql += " ORDER BY run_id DESC LIMIT ?"
        args.append(int(limit))
        return [dict(r) for r in con.execute(sql, args)]
    finally:
        con.close()


def get_run(run_id: int, *, db: str | Path | None = None) -> dict[str, Any] | None:
    """单次运行 + 它的动作和结论。详情页要的全部。"""
    con = connect(db)
    try:
        row = con.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if not row:
            return None
        out = dict(row)
        out["actions"] = [
            dict(r) for r in con.execute(
                "SELECT * FROM actions WHERE run_id = ? ORDER BY seq", (run_id,))
        ]
        out["findings"] = [
            dict(r) for r in con.execute(
                "SELECT * FROM findings WHERE run_id = ? ORDER BY finding_id",
                (run_id,))
        ]
        return out
    finally:
        con.close()


def compare_runs(a: int, b: int, *,
                 db: str | Path | None = None) -> dict[str, Any]:
    """对比两次运行 —— 这是变成数据库之后最有价值的一件事。

    回答的问题：「同一个用例，这次和上次差在哪」
      · 状态变了没（PASS -> BLOCKED 是回归）
      · 哪几条断言翻车了
      · 动作数差多少（动作突然变多通常意味着在乱试）
    """
    ra, rb = get_run(a, db=db), get_run(b, db=db)
    if not ra or not rb:
        return {"error": "run 不存在"}

    def _fmap(r: dict[str, Any]) -> dict[str, str]:
        return {f["fid"] or f"#{f['finding_id']}": f["status"]
                for f in r["findings"] if f.get("kind") == "assert"}

    fa, fb = _fmap(ra), _fmap(rb)
    flipped = [
        {"fid": k, "from": fa.get(k), "to": fb.get(k)}
        for k in sorted(set(fa) | set(fb))
        if fa.get(k) != fb.get(k)
    ]
    return {
        "a": {k: ra[k] for k in ("run_id", "status", "started_at",
                                 "elapsed_s", "auto_rate")},
        "b": {k: rb[k] for k in ("run_id", "status", "started_at",
                                 "elapsed_s", "auto_rate")},
        "status_changed": ra["status"] != rb["status"],
        "actions_a": len(ra["actions"]),
        "actions_b": len(rb["actions"]),
        "flipped": flipped,
    }


def stats(*, db: str | Path | None = None) -> dict[str, Any]:
    """仪表盘要的汇总：各状态数量、最近趋势、平均耗时。

    `days` 是**最近三天**按天的成败分布（旧 skill 的仪表盘就这么画的）。
    缺失的日期要补 0 —— 否则"昨天没跑"和"昨天全过"在图上没区别。
    """
    con = connect(db)
    try:
        by_status = {r["status"]: r["n"] for r in con.execute(
            "SELECT status, COUNT(*) n FROM runs GROUP BY status")}
        total = sum(by_status.values())
        rows = [dict(r) for r in con.execute(
            "SELECT run_id, case_name, status, elapsed_s, auto_rate, started_at"
            " FROM runs ORDER BY run_id DESC LIMIT 30")]
        avg = con.execute("SELECT AVG(elapsed_s) FROM runs").fetchone()[0]
        cases = [dict(r) for r in con.execute(
            "SELECT case_name, MAX(run_id) last_run, COUNT(*) n,"
            " MAX(started_at) last_at"
            " FROM runs GROUP BY case_name ORDER BY last_run DESC")]
        # 每个用例的最近一次状态 —— 仪表盘上"这个用例现在是什么状况"
        for c in cases:
            r = con.execute("SELECT status FROM runs WHERE run_id = ?",
                            (c["last_run"],)).fetchone()
            c["last_status"] = r["status"] if r else ""

        # 最近三天，按 started_at 的日期前缀分组。
        # started_at 是 "YYYY-MM-DD HH:MM:SS"，取前 10 位就是日期。
        days: dict[str, dict[str, int]] = {}
        for r in con.execute(
                "SELECT substr(started_at,1,10) d, status, COUNT(*) n"
                " FROM runs WHERE started_at IS NOT NULL"
                " GROUP BY d, status"):
            d = r["d"] or ""
            if not d:
                continue
            slot = days.setdefault(d, {"total": 0})
            slot[r["status"] or "?"] = r["n"]
            slot["total"] += r["n"]
        trend = sorted(({"day": k, **v} for k, v in days.items()),
                       key=lambda x: x["day"], reverse=True)[:7]

        return {
            "total": total,
            "by_status": by_status,
            "avg_elapsed_s": round(avg, 1) if avg else None,
            "recent": rows,
            "cases": cases,
            "days": trend,
        }
    finally:
        con.close()
