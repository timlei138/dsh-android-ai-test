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
import re
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
    -- 模型耗时（秒）。和 elapsed_s 的差就是设备/截图花掉的时间。
    -- 分开存是为了回答"这条用例慢在哪"：模型一次 5~20s，点击才 200ms。
    model_s     REAL,
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

-- ★ 原始用例（用户写的那份「测试步骤 + 预期结果」）
--
-- 为什么要单独一张表、而不是跟 runs 挤在一起：
--   原始用例是**用例的属性**，不是"某一次运行"的属性。
--   同一条用例跑 20 次，原始用例还是那一份 —— 存进 runs 就是 20 份重复。
--
-- 为什么之前没有：
--   系统里一直只有 YAML，而 YAML 里的 steps[].desc / assertions[].desc
--   是**改写过的判据描述**（"点掉 pm clear 后的「同意」权限说明框…"），
--   跟用户当初写的"首次打开日历，关掉权限弹窗"不是一回事。
--   前端要展示"原始 case"时才发现根本没存过 —— 这次补上。
--
-- 为什么进库不进文件：
--   文件要额外管目录、编码、命名冲突、跟 YAML 改名同步；
--   而它天然是"一条用例一份"，正好是表。
--   用户本来就是散在两个文件夹里的 txt，导入一次之后由库管，
--   网页里改的就是库里的那份，不会出现"文件夹一份、库里一份"两个真相。
--
-- 一个 case_key 可能有多条记录吗？
--   不会 —— case_key 是主键。改了就 REPLACE，永远保留最新那份。
--   历史版本不需要留：YAML 已经存了每次运行的快照，那才是判据的真相；
--   原始用例是"需求原话"，需求变了就以新的为准。
--
-- ⚠️ 主键是**编号**（"167" / "03"），不是 YAML 文件名。
--   原始用例里绝大多数还没有 YAML，用 YAML 名当键就装不进来。
--   编号是两边都有的唯一线索：文件名 xx_日历_<编号>.txt、
--   YAML 名 联想日历_<编号>_<题目>.yaml。见 cases/importer.py。
CREATE TABLE IF NOT EXISTS cases (
    case_key    TEXT PRIMARY KEY,  -- 编号，如 "167" / "03"
    title       TEXT,              -- 展示名（原文里没有题目时可由 YAML 补）
    priority    TEXT,              -- P0/P1/P2/P3，原文里带的就存
    raw_text    TEXT NOT NULL,     -- 用户原文，原样存，不加工
    source_path TEXT,              -- 从哪个 txt 导进来的（留痕，不参与逻辑）
    yaml_name   TEXT,              -- 关联的 YAML 文件名（不含目录），空=还没拆解
    updated_at  TEXT
);

CREATE INDEX IF NOT EXISTS idx_runs_case   ON runs(case_name, run_id DESC);
CREATE INDEX IF NOT EXISTS idx_actions_run ON actions(run_id, seq);
CREATE INDEX IF NOT EXISTS idx_findings_run ON findings(run_id);
CREATE INDEX IF NOT EXISTS idx_cases_yaml  ON cases(yaml_name);
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
    ("runs", "model_s", "REAL"),
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

    _migrate_raw_cases(con)


def _migrate_raw_cases(con: sqlite3.Connection) -> None:
    """raw_cases(按 YAML 名) -> cases(按编号)。

    为什么要搬：raw_cases 用 YAML 文件名当主键，而**绝大多数原始用例还没有
    YAML** —— 那 56 条根本装不进去。改成按编号当键才能全都收进来。

    搬完把老表删掉。留着的坏处是两份数据、两个真相，下次改的时候必然有人
    用错那个 —— 与其留个坑，不如一次清干净（老表只有测试期录的几条）。
    """
    have = {r["name"] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    if "raw_cases" not in have:
        return
    try:
        for r in con.execute(
                "SELECT case_name, raw_text, updated_at FROM raw_cases").fetchall():
            key = case_number(r["case_name"] or "")
            if not key or not (r["raw_text"] or "").strip():
                continue
            exists = con.execute("SELECT 1 FROM cases WHERE case_key = ?",
                                 (key,)).fetchone()
            if exists:
                continue      # 新表已有（导过一遍了），不覆盖
            con.execute(
                "INSERT INTO cases(case_key, title, priority, raw_text,"
                " source_path, updated_at) VALUES(?,?,?,?,?,?)",
                (key, "", "", r["raw_text"], "(迁移自 raw_cases)",
                 r["updated_at"]))
        con.execute("DROP TABLE raw_cases")
        con.commit()
    except sqlite3.Error:
        # 迁移失败就留着老表，别把库弄坏 —— 下次 connect 会再试一次。
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
        elapsed = report.get("elapsed_s")

        # started_ts 取 progress[0].t —— nodes._tick 写的是 time.time()，
        # 也就是 epoch 秒，可以直接用。
        #
        # ⚠️ 但要防一手：假如 progress 里的 t 是个小数字（< 1e9 —— 相对耗时、
        #    或者调用方手搓的测试数据），拿它当 epoch 会渲染成 1970-01-01，
        #    连带"最近三天"统计、对比页、删记录的时间判断全错。
        #    这种明显不可能是时间戳的值一律不认，退回落库时刻减总耗时
        #    （也就是"现在往前推本次运行的全部用时"≈真实开始时刻）。
        raw_t = (report.get("progress") or [{}])[0].get("t")
        try:
            raw_t = float(raw_t)
        except (TypeError, ValueError):
            raw_t = 0.0
        # 1e9 ≈ 2001 年。真时间戳都远大于它，相对耗时都远小于它。
        started_ts = raw_t if raw_t > 1e9 else time.time()
        if raw_t <= 1e9 and elapsed is not None:
            try:
                started_ts -= float(elapsed)
            except (TypeError, ValueError):
                pass
        finished_ts = (started_ts + float(elapsed)) if elapsed is not None else None

        cur = con.execute(
            """INSERT INTO runs(
                case_name, case_id, case_title, status, summary,
                steps_done, steps_total, auto_rate, elapsed_s, model_s,
                by_model, by_code, engine,
                started_at, started_ts, finished_at,
                ledger_path, log_path, llm_path, report_path,
                case_yaml_path, case_yaml, report_json
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
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
                report.get("model_s"),
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
               " steps_done, steps_total, auto_rate, elapsed_s, model_s,"
               " by_model, by_code,"
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


# ── 用例（原始需求原话 + 关联的 YAML）────────────────────────────
#
# 为什么原始 case 和 YAML **绑在同一行**里（用户要求）：
#   它们是同一条用例的两面 —— 原文是"要什么"，YAML 是"怎么判"。
#   分两张表存，删除时就得先查关联再删两处，还要处理"只删了一半"；
#   绑在一起，删用例 = 删这一行 + 删那个文件，不会漏。

_NUM_RE = re.compile(r"(\d+)")


def case_number(name: str) -> str:
    """从文件名 / YAML 名里取用例编号。

    '联想日历_167_首次进入课程表引导与空态.yaml' -> '167'
    '13_日历_167.txt'                          -> '167'
    '03_日历_03.txt'                           -> '03'

    ★ 取**最后一个**数字段而不是第一个：
      '13_日历_167.txt' 里第一个数字是排序前缀 13，第二个才是编号 167。
      取错了会把 13 号和 167 号认成同一条 —— 而且是静默认错。
    """
    stem = Path(name).stem
    parts = stem.split("_")
    for seg in reversed(parts):
        m = _NUM_RE.fullmatch(seg.strip())
        if m:
            return m.group(1)
    m = _NUM_RE.search(stem)
    return m.group(1) if m else ""


def save_case(case_key: str, *, title: str = "", priority: str = "",
              raw_text: str = "", source_path: str = "",
              db: str | Path | None = None) -> str:
    """写入/更新一条用例。返回 "created" / "updated" / "skipped"。

    ## 为什么空文本不覆盖

    手滑清空会把原来那份删掉，而"清空"和"从来没有"在前端看起来一样。
    但**新建**时允许空文本（编号在、原文还没录），所以分两种情况：
      · 新行 + 空文本 -> 建成 (空壳)，backend 仍会写一行，raw_text 存空串
      · 已有行 + 空文本 -> 不动它，报 skipped

    ## 为什么 yaml_name 不在这个函数里

    YAML 关联是**扫描目录**得出的，跟"人改了原文"是两件事。
    混在一起写，人一保存原文就会把刚链好的 YAML 冲掉。
    单独 set_case_yaml。
    """
    text = (raw_text or "").strip()
    con = connect(db)
    try:
        row = con.execute("SELECT raw_text FROM cases WHERE case_key = ?",
                          (case_key,)).fetchone()
        if row is None:
            con.execute(
                "INSERT INTO cases(case_key, title, priority, raw_text,"
                " source_path, updated_at) VALUES(?,?,?,?,?,?)",
                (case_key, title or "", priority or "", text,
                 source_path or "", _fmt_ts(time.time())))
            con.commit()
            return "created"
        if not text:
            return "skipped"      # 已有原文，空文本不覆盖
        con.execute(
            "UPDATE cases SET raw_text = ?, title = ?, priority = ?,"
            " source_path = ?, updated_at = ? WHERE case_key = ?",
            (text, title or (row["title"] if "title" in row.keys() else ""),
             priority or "", source_path or "", _fmt_ts(time.time()), case_key))
        con.commit()
        return "updated"
    finally:
        con.close()


def get_case(case_key: str, *,
             db: str | Path | None = None) -> dict[str, Any] | None:
    """取一条用例；没有则 None（**不要返回空串冒充有**）。"""
    con = connect(db)
    try:
        row = con.execute("SELECT * FROM cases WHERE case_key = ?",
                          (case_key,)).fetchone()
        return dict(row) if row else None
    finally:
        con.close()


def list_cases(*, db: str | Path | None = None) -> list[dict[str, Any]]:
    """全部用例，按编号排序。数字键按数值排，不然 100 会排在 20 前面。"""
    con = connect(db)
    try:
        rows = [dict(r) for r in con.execute("SELECT * FROM cases")]
        rows.sort(key=lambda r: (len(r["case_key"]), r["case_key"]))
        return rows
    finally:
        con.close()


def set_case_yaml(case_key: str, yaml_name: str | None, *,
                  db: str | Path | None = None) -> int:
    """把用例关联到某个 YAML 文件（或解除关联，yaml_name=None）。返回影响行数。"""
    con = connect(db)
    try:
        cur = con.execute("UPDATE cases SET yaml_name = ? WHERE case_key = ?",
                          (yaml_name or None, case_key))
        con.commit()
        return cur.rowcount or 0
    finally:
        con.close()


def set_case_title(case_key: str, title: str, *,
                   db: str | Path | None = None) -> int:
    """只改展示名。返回影响行数。

    单独一个函数是因为"补标题"和"改原文"是两件事：
    原文没题目时（`测试用例 联想日历_168 前提：…`），题目由 YAML 名补出来，
    那时不该动 raw_text。
    """
    con = connect(db)
    try:
        cur = con.execute("UPDATE cases SET title = ? WHERE case_key = ?",
                          (title or "", case_key))
        con.commit()
        return cur.rowcount or 0
    finally:
        con.close()


def save_case_raw(case_key: str, raw_text: str, *,
                  db: str | Path | None = None) -> bool:
    """只改原始 case 原文（网页里编辑用）。返回 True 表示内容真的变了。

    空文本不写 —— 理由同 save_case。
    """
    text = (raw_text or "").strip()
    if not text:
        return False
    con = connect(db)
    try:
        cur = con.execute(
            "UPDATE cases SET raw_text = ?, updated_at = ? WHERE case_key = ?",
            (text, _fmt_ts(time.time()), case_key))
        con.commit()
        return (cur.rowcount or 0) > 0
    finally:
        con.close()


def delete_case(case_key: str, *, db: str | Path | None = None) -> int:
    """删掉一条用例，返回删了几行（0/1）。

    ⚠️ 只删库里的行，**不碰磁盘上的 YAML** ——
       删文件有"删错了不可恢复"的后果，得由调用方（webui）显式做，
       而且要在真正 unlink 之前先确认路径在用例目录内。
    """
    con = connect(db)
    try:
        cur = con.execute("DELETE FROM cases WHERE case_key = ?", (case_key,))
        con.commit()
        return cur.rowcount or 0
    finally:
        con.close()


def delete_run(run_id: int, *, db: str | Path | None = None) -> dict[str, Any]:
    """删掉一次运行（runs + actions + findings 三张表里的行）。

    ⚠️ **只删库里的行，不碰磁盘**。
       磁盘上的日志/截图/xml 由调用方决定删不删 —— 理由见 webui.py
       的 DELETE 处理：万一库里路径被改坏过，无脑递归删会删到别处去。
       所以"删文件"必须由一个知道证据根在哪的层来做，不在这里。

    ## 共享文件：只报不删

    `run_167.log` / `clicks_167.jsonl` / `report_167_subagent.json` 是
    **按用例名命名、每次运行追加**的，不是"一次运行一个文件"。实测库里
    7 条 167 记录全都指向同三个文件 —— 删任意一条就顺手 unlink，
    另外 6 条的日志和点击账本就一起没了，而那是它们唯一的执行证据。

    所以这里先查"还有没有别的记录引用这个路径"，有的就放进 `shared`，
    上层**不许删**，只能跳过。截图/xml 是按 stepNN 分目录的，通常不共享，
    但同样走这个判断 —— 不靠"我以为它不会共享"来赌。

    返回：
      paths   只被这次运行引用的文件（可以删）
      shared  还被别的记录引用着（不能删）
    """
    con = connect(db)
    try:
        row = con.execute("SELECT * FROM runs WHERE run_id = ?",
                          (run_id,)).fetchone()
        if not row:
            return {"deleted": 0, "paths": [], "shared": []}
        rec = dict(row)

        # 收集候选路径。用 dict 去重：同一次运行里同一个文件可能被记两遍
        # （比如 shot_before 和 xml_before 恰好同目录的同一个名字不该出现，
        # 但 report_path 之类确实可能与 ledger_path 重合）。
        cand: dict[str, None] = {}
        for key in ("ledger_path", "log_path", "llm_path", "report_path"):
            if rec.get(key):
                cand[str(rec[key])] = None
        for a in con.execute(
                "SELECT shot_before, shot_after, xml_before, xml_after "
                "FROM actions WHERE run_id = ?", (run_id,)):
            for v in a:
                if v:
                    cand[str(v)] = None

        # ★ 关键一步：把**别的记录**引用到的路径挑出来，判为共享。
        #   比对用规范化后的绝对路径 —— 库里存的可能是相对路径
        #   （跑用例时 cwd 不同就会这样），纯字符串比会漏掉。
        others: set[str] = set()
        for r2 in con.execute(
                "SELECT run_id, ledger_path, log_path, llm_path, report_path "
                "FROM runs WHERE run_id <> ?", (run_id,)):
            for k in ("ledger_path", "log_path", "llm_path", "report_path"):
                if r2[k]:
                    others.add(_norm(r2[k]))
        for a in con.execute(
                "SELECT shot_before, shot_after, xml_before, xml_after "
                "FROM actions WHERE run_id <> ?", (run_id,)):
            for v in a:
                if v:
                    others.add(_norm(v))

        shared, own = [], []
        for p in cand:
            (shared if _norm(p) in others else own).append(p)

        # ★ 第二道：**老布局**下被后来的运行覆盖过的文件，不是这次的证据。
        #
        #   老布局是 tmp/shots/stepNN/ —— 按步号复用，下一次运行直接覆盖
        #   上一次的 shot.png / dump.xml。所以库里 #10 和 #15 记着同一个
        #   tmp/shots/step04/shot.png，而磁盘上那份其实是后来那次的。
        #   引用计数判不出来（后来的记录可能已经被删了），只能看时间。
        #
        #   ⚠️ 两个前提，缺一个就会把正常删除变成"什么都不删"（踩过）：
        #
        #   1. **只对老布局生效**。新布局 tmp/shots/run_xxx/ 是按运行隔离的，
        #      那里的文件天生属于这次运行，不该被时间戳否决。
        #
        #   2. 比的是**运行结束时刻**，不是开始时刻。截图是在运行过程中
        #      写的，当然比 started_at 晚 —— 拿开始时刻当基准，等于把
        #      这次运行自己的截图全判成"别人的"（实测：20 个文件判掉 14 个）。
        #
        #   start_ts 取不到（老记录/字段空）就不启用这道判断 ——
        #   宁可多删一个，也不要因为解析失败把正常删除全挡掉。
        start_ts = _parse_ts(rec.get("started_at"))
        try:
            end_ts = start_ts + float(rec.get("elapsed_s") or 0)
        except (TypeError, ValueError):
            end_ts = start_ts
        stale = []
        if start_ts:
            keep2 = []
            for p in own:
                pp = Path(p)
                # 新布局（run_xxx/ 下）不参与时间判断
                if any(a.name.startswith("run_") for a in list(pp.parents)[:3]):
                    keep2.append(p)
                    continue
                try:
                    mt = pp.stat().st_mtime
                except OSError:
                    keep2.append(p)   # 文件不在了：交给上层当"已删"处理
                    continue
                # 留 2 秒余量：最后一张截图落盘和结束时刻很接近
                if mt > end_ts + 2.0:
                    stale.append(p)
                else:
                    keep2.append(p)
            own = keep2

        con.execute("DELETE FROM actions WHERE run_id = ?", (run_id,))
        con.execute("DELETE FROM findings WHERE run_id = ?", (run_id,))
        con.execute("DELETE FROM runs WHERE run_id = ?", (run_id,))
        con.commit()
        return {"deleted": 1, "paths": own, "shared": shared,
                "stale": stale, "case_name": rec.get("case_name")}
    finally:
        con.close()


def _parse_ts(s: Any) -> float:
    """把 started_at（'2026-10-20 20:35:05' 这类）转成 epoch 秒；失败返回 0。"""
    if not s:
        return 0.0
    text = str(s).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return time.mktime(time.strptime(text[:19], fmt))
        except (ValueError, OverflowError):
            continue
    return 0.0


def _norm(p: str | Path) -> str:
    """路径比对用的规范化形式（绝对 + 小写，Windows 不区分大小写）。"""
    try:
        return str(Path(p).resolve()).lower()
    except OSError:
        return str(p).lower()


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
