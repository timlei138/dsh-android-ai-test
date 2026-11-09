"""测试会话 SQLite 持久化（v3：单库 + run_id 隔离）。

三张表：
  sessions  一次测试会话（用户口述用例 + 结论）
  events    统一事件流：observe/act/read 每次工具调用自动落一条（append-only）
  findings  断言/结论（从事件流独立出来，方便聚合统计）

零依赖（标准库 sqlite3）。

## 库位置：`storage/test.db` —— **所有运行共用一个库**

### 运行之间怎么隔离

  靠 `sessions.run_id` 字段隔离，不靠文件隔离：

    sessions 表  run_id  <- 这次运行的 uuid（带 `-` 的标准 UUID v4）
    events 表    通过 session_id 关联到 sessions，自然带上 run_id
    findings 表  同上

  跨运行查询就是一句 SQL：

    SELECT * FROM sessions WHERE run_id = '2b818755-fc18-454c-96c1-ea1ca0444902'
    SELECT s.run_id, s.status FROM sessions s WHERE s.title LIKE '%176%'

  单库之后 WebUI 刷新、用例详情、跨运行对比都只连一个库，不用逐库打开再合并。

### 运行目录

  `storage/run_history/<uuid>/` 只放证据和计时，不放 db：

    run_history/<uuid>/
        evidence/           <- 每次运行的截图
        timing/ calls/ recording_errors/

  「一次运行 = 一个目录」的直觉还在，删一次运行 = 删目录 + 删库里对应行。
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from runtime import CALL_ID, STORAGE_DIR, current_record_dir, recording_issues

ROOT = Path(__file__).resolve().parent.parent

# 全局唯一数据库
DB_FILE = STORAGE_DIR / "test.db"


def _resolve_db_path() -> Path:
    """库放哪？固定是 `storage/test.db`，与运行上下文无关。

    库永远只有一个，任何进程（工具、WebUI、导出脚本、统计）都能直接 get_db()。
    run_id 只用于**写入时归属**（session start 时确定），不参与定位库。
    """
    return DB_FILE


def _current_file() -> Path:
    """'当前会话号' 记在哪。

    单库后不再跟着 record 目录走 —— 固定放 storage/ 下。
    多个运行同时跑是不允许的（设备只有一台），所以一个全局指针够用。
    """
    return STORAGE_DIR / "current_session"


# ⚠️ 不能在模块级解析路径 —— `import db` 那一刻还没有 record 上下文，
# 会直接抛 RuntimeError，连报错信息都看不到。用到时才解析
# （get_db() / SessionDB 内部调 _resolve_db_path）。
# 这个名字只为兼容旧引用，值是 None 表示"还没解析"。
DB_PATH: Path | None = None
CURRENT_FILE = _current_file()   # 仅为兼容旧引用；内部一律用 _current_file()

# 结论只有四种：PASS / FAIL / BLOCKED / ERROR。
#
# 不用含糊的中间态 —— 实际用起来会变成"不敢判 FAIL 就写 XXX"。
# 用例预期是有明确对照的：符合就 PASS，不符合就 FAIL，
# 环境/前置问题就 BLOCKED，工具异常就 ERROR。
STATUSES = ("PASS", "FAIL", "BLOCKED", "ERROR")
# 会话中间态：不是结论，不计入通过率统计；必须以 finish 收尾。
INTERIM = ("running",)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT,
  title TEXT,
  user_input TEXT,
  package TEXT,
  related TEXT,
  device TEXT,
  case_id INTEGER,
  status TEXT DEFAULT 'running',
  kind TEXT DEFAULT 'test',
  terminated INTEGER DEFAULT 0,
  terminate_reason TEXT,
  perm_intent_action TEXT,
  perm_intent_perm TEXT,
  perm_intent_at TEXT,
  knowledge_shown INTEGER DEFAULT 0,
  summary TEXT,
  report_path TEXT,
  started_at TEXT,
  finished_at TEXT
);
CREATE TABLE IF NOT EXISTS cases(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT NOT NULL,
  input TEXT NOT NULL,
  package TEXT,
  kind TEXT DEFAULT 'test',
  last_session_id INTEGER,
  last_run_at TEXT,
  last_status TEXT,
  run_count INTEGER DEFAULT 0,
  created_at TEXT,
  updated_at TEXT
);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id INTEGER NOT NULL REFERENCES sessions(id),
  seq INTEGER,
  kind TEXT,
  tool TEXT,
  detail TEXT,
  data_json TEXT,
  evidence TEXT,
  duration_ms INTEGER,
  started_at TEXT,
  created_at TEXT
);
CREATE TABLE IF NOT EXISTS findings(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id INTEGER NOT NULL REFERENCES sessions(id),
  event_id INTEGER,
  status TEXT,
  expect TEXT,
  actual TEXT,
  note TEXT,
  created_at TEXT
);
CREATE TABLE IF NOT EXISTS finding_events(
  finding_id INTEGER NOT NULL REFERENCES findings(id),
  event_id INTEGER NOT NULL REFERENCES events(id),
  PRIMARY KEY(finding_id, event_id)
);
CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_id, seq);
CREATE INDEX IF NOT EXISTS idx_findings_session ON findings(session_id);
CREATE INDEX IF NOT EXISTS idx_sessions_kind ON sessions(kind);
"""

# 会话类型：
#   test = 正式测试（进「测试记录」，计入仪表盘成功率）
#   diag = 验证/调试（工具自检、探针；默认不显示，不计入）
#   mock = 占位/构造数据（冒烟样例、手工造的演示数据；**永不入库**）
SESSION_KINDS = ("test", "diag", "mock")
# 只有这些类型算正式记录（进列表与统计）
FORMAL_KINDS = ("test",)


class SessionDB:
    """Thread-local connections; trust-critical writes raise on failure."""

    def __init__(self, path: Path | None = None):
        # ★ 必须**调用时**才解析路径，不能用 `path: Path = DB_PATH`。
        #
        #   默认参数在**函数定义时**求值 —— 也就是模块 import 那一刻。
        #   而 `session.py start` 的流程是先 import、再设环境变量、最后才
        #   get_db()。如果路径在 import 时就定死，库会建到当时那个位置去，
        #   而环境变量指定的位置反而是空的。
        #
        #   所以：None 默认值 + 调用时解析，环境变量才能生效。
        self.path = str(path if path is not None else _resolve_db_path())
        self._local = threading.local()

    def _conn(self) -> sqlite3.Connection:
        if getattr(self._local, "conn", None) is None:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self.path, check_same_thread=False, timeout=10)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.row_factory = sqlite3.Row
            conn.executescript(_SCHEMA)
            # 幂等迁移：早期建的表补列
            for stmt in ("ALTER TABLE events ADD COLUMN duration_ms INTEGER",
                         "ALTER TABLE events ADD COLUMN started_at TEXT",
                         "ALTER TABLE sessions ADD COLUMN related TEXT",
                         "ALTER TABLE sessions ADD COLUMN case_id INTEGER",
                         "ALTER TABLE sessions ADD COLUMN kind TEXT DEFAULT 'test'",
                         "ALTER TABLE cases ADD COLUMN kind TEXT DEFAULT 'test'",
                         "ALTER TABLE sessions ADD COLUMN terminated INTEGER DEFAULT 0",
                         "ALTER TABLE sessions ADD COLUMN terminate_reason TEXT",
                         "ALTER TABLE sessions ADD COLUMN perm_intent_action TEXT",
                         "ALTER TABLE sessions ADD COLUMN perm_intent_perm TEXT",
                         "ALTER TABLE sessions ADD COLUMN perm_intent_at TEXT",
                         "ALTER TABLE sessions ADD COLUMN knowledge_shown INTEGER DEFAULT 0",
                         "ALTER TABLE events ADD COLUMN call_id TEXT",
                         # 单库化的关键列：这次运行属于哪个 uuid
                         "ALTER TABLE sessions ADD COLUMN run_id TEXT"):
                try:
                    conn.execute(stmt)
                except sqlite3.OperationalError:
                    pass
            conn.commit()
            conn.execute("CREATE INDEX IF NOT EXISTS idx_events_call ON events(call_id)")
            # run_id 是单库的主查询维度（前端按 run 筛、按 run 删）
            conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_run ON sessions(run_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_started ON sessions(started_at)")
            conn.commit()
            self._local.conn = conn
        return self._local.conn

    @staticmethod
    def _now() -> str:
        # 毫秒精度：相邻事件差 = AI 决策耗时（时间轴展示依赖它）
        return datetime.now().isoformat(timespec="milliseconds")

    # ── 写 ──────────────────────────────────────────────────────
    def start_session(self, title: str, user_input: str, device: str | None = None,
                      case_id: int | None = None, kind: str = "test",
                      run_id: str | None = None) -> int:
        """开一个会话。run_id 不传时自动取当前运行目录名。

        run_id 是单库的主隔离维度：同一个 uuid 下的会话（正常只有一个，
        但重复 start 会产生多个）都能靠它聚起来。
        """
        if kind not in SESSION_KINDS:
            kind = "test"
        if run_id is None:
            rec = current_record_dir()
            run_id = rec.name if rec is not None else None
        cur = self._conn().execute(
            "INSERT INTO sessions (run_id, title, user_input, device, case_id,"
            " kind, started_at) VALUES (?,?,?,?,?,?,?)",
            (run_id, title, user_input, device, case_id, kind, self._now()))
        self._conn().commit()
        return cur.lastrowid

    def log_event(self, session_id: int, kind: str, tool: str, detail: str = "",
                  data: dict | None = None, evidence: str | None = None,
                  duration_ms: int | None = None,
                  started_at: str | None = None) -> int | None:
        conn = self._conn()
        with conn:
            if not conn.execute("SELECT 1 FROM sessions WHERE id=?", (session_id,)).fetchone():
                raise ValueError(f"Session {session_id} does not exist")
            seq = self._conn().execute(
                "SELECT COALESCE(MAX(seq),0)+1 FROM events WHERE session_id=?",
                (session_id,)).fetchone()[0]
            cur = self._conn().execute(
                "INSERT INTO events (session_id, seq, kind, tool, detail, data_json,"
                " evidence, duration_ms, started_at, created_at, call_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (session_id, seq, kind, tool, detail,
                 json.dumps(data, ensure_ascii=False) if data else None,
                 evidence, duration_ms, started_at, self._now(), CALL_ID))
            return cur.lastrowid

    def add_finding(self, session_id: int, status: str, expect: str = "",
                    actual: str = "", note: str = "", event_id: int | None = None,
                    event_ids: list[int] | None = None) -> int:
        if status not in STATUSES:
            raise ValueError(f"Invalid finding status: {status}")
        ids = list(dict.fromkeys(([event_id] if event_id is not None else []) + (event_ids or [])))
        if not actual.strip():
            raise ValueError("Finding actual observation/reason is required")
        if status in ("PASS", "FAIL") and (not ids or not expect.strip()):
            raise ValueError("Verified findings require an expectation and explicit evidence events")
        conn = self._conn()
        with conn:
            session = conn.execute("SELECT status FROM sessions WHERE id=?", (session_id,)).fetchone()
            if not session or session["status"] not in INTERIM:
                raise ValueError("Findings require an existing unfinished session")
            for eid in ids:
                self._validate_evidence(conn, session_id, eid)
            cur = self._conn().execute(
                "INSERT INTO findings (session_id, event_id, status, expect, actual,"
                " note, created_at) VALUES (?,?,?,?,?,?,?)",
                (session_id, ids[0] if ids else None, status, expect, actual, note, self._now()))
            fid = cur.lastrowid
            conn.executemany("INSERT INTO finding_events VALUES (?,?)", [(fid, eid) for eid in ids])
            return fid

    @staticmethod
    def _validate_evidence(conn: sqlite3.Connection, session_id: int, event_id: int) -> None:
        event = conn.execute(
            "SELECT session_id, evidence FROM events WHERE id=?", (event_id,)).fetchone()
        if not event or event["session_id"] != session_id or not event["evidence"]:
            raise ValueError(f"Event {event_id} must contain evidence from session {session_id}")
        path = Path(event["evidence"])
        if not path.is_absolute():
            path = ROOT / path
        if not path.is_file():
            raise ValueError(f"Event {event_id} evidence is missing: {path}")

    # summary 里出现的统计数字，必须和库里的断言统计一致。
    #
    # ⚠️ 为什么需要（2026-10-29 实测）：
    #   176 那轮 summary 写着「12 条预期中 8 PASS / 4 FAIL」，
    #   而 findings 表里实际是 **9 PASS / 3 FAIL**。
    #
    #   本项目的分工是「代码管纪律，AI 管语义」。数一数 findings 表
    #   是**确定且唯一**的结果，却让 AI 手写 —— 必然出错，而且错了没人发现。
    #   结论数字是读报告的人看到的第一条信息，错了会带偏后续所有判断。
    #
    # 只校验形如「8 PASS」「4 FAIL」这种显式计数；不碰自然语言描述。
    _COUNT_STATUSES = ("PASS", "FAIL", "BLOCKED")

    def _check_summary_counts(self, summary: str, findings: list) -> None:
        import re as _re
        if not summary.strip():
            return
        actual: dict[str, int] = {}
        for finding in findings:
            st = finding["status"]
            actual[st] = actual.get(st, 0) + 1
        for word in self._COUNT_STATUSES:
            for match in _re.finditer(rf"(\d+)\s*{word}\b", summary):
                claimed = int(match.group(1))
                real = actual.get(word, 0)
                if claimed != real:
                    raise ValueError(
                        f"summary 里写的是「{claimed} {word}」，"
                        f"但断言实际是 {real} 条 {word}"
                        f"（共 {len(findings)} 条断言）。"
                        f"结论里的统计数字必须和断言一致。")

    def finish_session(self, session_id: int, status: str, summary: str = "",
                       package: str | None = None) -> bool:
        if status not in STATUSES:
            raise ValueError(f"Invalid session status: {status}")
        conn = self._conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
            if not row or row["status"] not in INTERIM:
                raise ValueError("Finish requires an existing unfinished session")
            findings = conn.execute("SELECT * FROM findings WHERE session_id=?", (session_id,)).fetchall()
            statuses = {finding["status"] for finding in findings}
            if row["terminated"] and status != "BLOCKED":
                raise ValueError("Terminated sessions must finish BLOCKED")
            if status == "PASS":
                # PASS 只允许 PASS findings —— 一条 FAIL 都不能有
                if not findings or not statuses <= {"PASS"}:
                    raise ValueError(
                        "PASS requires all findings PASS (no FAIL, no BLOCKED)")
            if status == "FAIL" and "FAIL" not in statuses:
                raise ValueError("FAIL requires a FAIL finding")
            if status in ("BLOCKED", "ERROR") and not summary.strip():
                raise ValueError("BLOCKED/ERROR requires a reason in summary")
            self._check_summary_counts(summary, findings)
            if status not in ("ERROR", "BLOCKED") and recording_issues(Path(self.path).parent, session_id):
                raise ValueError("Recording/ledger is incomplete; finish ERROR/BLOCKED with a reason")
            for finding in findings if status in ("PASS", "FAIL") else []:
                if finding["status"] not in ("PASS", "FAIL"):
                    continue
                ids = [r[0] for r in conn.execute(
                    "SELECT event_id FROM finding_events WHERE finding_id=?", (finding["id"],))]
                if finding["event_id"] is not None and finding["event_id"] not in ids:
                    ids.append(finding["event_id"])
                if not ids or not finding["expect"].strip() or not finding["actual"].strip():
                    raise ValueError(f"Finding {finding['id']} is missing evidence or concrete observations")
                for eid in ids:
                    self._validate_evidence(conn, session_id, eid)
            conn.execute(
                "UPDATE sessions SET status=?, summary=?, finished_at=?,"
                " package=COALESCE(?, package) WHERE id=?",
                (status, summary, self._now(), package, session_id))
            if row["case_id"]:
                updated = conn.execute(
                    "UPDATE cases SET package=COALESCE(package, ?), last_session_id=?,"
                    " last_run_at=?, last_status=?, run_count=COALESCE(run_count,0)+1 WHERE id=?",
                    (package or row["package"], session_id, self._now(), status, row["case_id"]))
                # ⚠️ 关联的用例不存在时**不能让整条会话收尾失败**（2026-11-02 修）。
                #
                #    真实事故：`start --case 176` 从 assets/cases/ 读文件开会话，
                #    cases 表里没有 id=176 的行，finish 时这里抛
                #    「Associated case is missing」→ 会话卡在 running 收不了尾，
                #    AI 只能手动 case add 补建再改 id 才绕过。
                #
                #    会话结论是主要产物，用例库统计是附属的 —— 附属失败不该毁掉主产物。
                #    这里只把 case_id 解绑（置空），会话照常收尾。
                if updated.rowcount != 1:
                    conn.execute("UPDATE sessions SET case_id=NULL WHERE id=?",
                                 (session_id,))
            conn.commit()
            return True
        except (sqlite3.Error, ValueError, OSError):
            conn.rollback()
            raise

    def set_package(self, session_id: int, package: str) -> None:
        """首个 observe 回填被测包名（已有值不覆盖）。"""
        try:
            self._conn().execute(
                "UPDATE sessions SET package=? WHERE id=? AND (package IS NULL OR package='')",
                (package, session_id))
            self._conn().commit()
        except sqlite3.Error:
            pass

    def get_session_package(self, session_id: int) -> str | None:
        """会话的被测包名（崩溃分级的 target 依据）。

        用会话声明而非"当前前台包"：被测 App 崩溃后前台回落到桌面，
        拿前台包分类会把被测包的崩溃误判成 other 而放过。
        """
        try:
            row = self._conn().execute(
                "SELECT package FROM sessions WHERE id=?", (session_id,)).fetchone()
            return (row["package"] or None) if row else None
        except (sqlite3.Error, TypeError):
            return None

    def terminate_session(self, session_id: int, reason: str) -> None:
        """标记会话为**终态**：被测/关联包崩溃后，后续动作一律拒绝执行。

        这是工具层硬约束（不只靠 AI 自觉读 `terminate` 信号）：
        崩溃一旦发生，继续观测/点击只会产生不可信的数据和多余动作。
        """
        try:
            self._conn().execute(
                "UPDATE sessions SET terminated=1, terminate_reason=? WHERE id=?",
                (reason, session_id))
            self._conn().commit()
        except sqlite3.Error:
            pass

    def get_termination(self, session_id: int) -> str | None:
        """会话是否已终止；返回终止原因（None=未终止，可继续执行）。"""
        row = self._conn().execute(
            "SELECT terminated, terminate_reason FROM sessions WHERE id=?",
            (session_id,)).fetchone()
        if row is None:
            raise ValueError(f"Session {session_id} does not exist")
        if row["terminated"]:
            return row["terminate_reason"] or "会话已终止"
        return None

    def clear_termination(self, session_id: int) -> bool:
        """清除终态（resume 时用：人工确认后允许继续）。"""
        try:
            self._conn().execute(
                "UPDATE sessions SET terminated=0, terminate_reason=NULL WHERE id=?",
                (session_id,))
            self._conn().commit()
            return True
        except sqlite3.Error:
            return False

    # ── 权限测试意图（声明式，跨命令持久）──────────────────────────
    # 设计参考 AiAgentTest 的 set_permission_intent：AI 在分支开头声明
    # "接下来遇到权限弹窗该点同意还是拒绝"，之后每次 act/observe 自动按此响应。
    # 关键：决策**提前做完**，动作时只执行——弹窗约 6s 自动消失，来不及现场想。
    PERM_INTENT_TTL_S = 600.0   # 10 分钟过期（与 AiAgentTest 一致）

    def set_perm_intent(self, session_id: int, action: str,
                        permission: str = "") -> bool:
        """action ∈ {"grant","deny"}；permission 仅作标记（如 media_images）。"""
        if action not in ("grant", "deny"):
            return False
        try:
            self._conn().execute(
                "UPDATE sessions SET perm_intent_action=?, perm_intent_perm=?,"
                " perm_intent_at=? WHERE id=?",
                (action, (permission or "").strip(), self._now(), session_id))
            self._conn().commit()
            return True
        except sqlite3.Error:
            return False

    def clear_perm_intent(self, session_id: int) -> bool:
        try:
            self._conn().execute(
                "UPDATE sessions SET perm_intent_action=NULL, perm_intent_perm=NULL,"
                " perm_intent_at=NULL WHERE id=?", (session_id,))
            self._conn().commit()
            return True
        except sqlite3.Error:
            return False

    def get_perm_intent(self, session_id: int) -> dict | None:
        """返回 {"action","permission"}；无意图或已过期返回 None。"""
        try:
            row = self._conn().execute(
                "SELECT perm_intent_action, perm_intent_perm, perm_intent_at"
                " FROM sessions WHERE id=?", (session_id,)).fetchone()
        except sqlite3.Error:
            return None
        if not row or not row["perm_intent_action"]:
            return None
        at = row["perm_intent_at"] or ""
        try:
            set_at = datetime.fromisoformat(at)
            if (datetime.now() - set_at).total_seconds() > self.PERM_INTENT_TTL_S:
                return None      # 过期即视为未声明
        except (ValueError, TypeError):
            return None
        return {"action": row["perm_intent_action"],
                "permission": row["perm_intent_perm"] or ""}

    def mark_knowledge_shown(self, session_id: int) -> None:
        """记录"知识卡提示已给过"——避免每步重复带索引浪费 token。"""
        try:
            self._conn().execute(
                "UPDATE sessions SET knowledge_shown=1 WHERE id=?", (session_id,))
            self._conn().commit()
        except sqlite3.Error:
            pass

    def knowledge_shown(self, session_id: int) -> bool:
        try:
            row = self._conn().execute(
                "SELECT knowledge_shown FROM sessions WHERE id=?",
                (session_id,)).fetchone()
            return bool(row and row["knowledge_shown"])
        except (sqlite3.Error, TypeError, IndexError):
            return False

    def set_related(self, session_id: int, related: list[str]) -> None:
        try:
            self._conn().execute("UPDATE sessions SET related=? WHERE id=?",
                                 (",".join(related), session_id))
            self._conn().commit()
        except sqlite3.Error:
            pass

    def get_related(self, session_id: int) -> list[str]:
        """关联包列表（崩溃分级的"关联"判定域）。"""
        try:
            row = self._conn().execute(
                "SELECT related FROM sessions WHERE id=?", (session_id,)).fetchone()
            return [x.strip() for x in (row["related"] or "").split(",") if x.strip()]
        except (sqlite3.Error, TypeError):
            return []

    def set_session_status(self, session_id: int, status: str) -> bool:
        """中间态切换（现在只剩 running）。结论态必须走 finish_session。"""
        if status not in INTERIM:
            return False
        try:
            self._conn().execute("UPDATE sessions SET status=? WHERE id=?",
                                 (status, session_id))
            self._conn().commit()
            return True
        except sqlite3.Error:
            return False

    def finding_stats(self, session_id: int) -> dict:
        """各结论的断言计数（finish 自检用：有 FAIL 时结论不得 PASS）。"""
        try:
            rows = self._conn().execute(
                "SELECT status, COUNT(*) FROM findings WHERE session_id=?"
                " GROUP BY status", (session_id,)).fetchall()
            return {r[0]: r[1] for r in rows}
        except sqlite3.Error:
            return {}

    def count_crash_events(self, session_id: int) -> int:
        try:
            return self._conn().execute(
                "SELECT COUNT(*) FROM events WHERE session_id=?"
                " AND data_json LIKE '%crash_detected%'",
                (session_id,)).fetchone()[0]
        except sqlite3.Error:
            return 0

    def list_events(self, session_id: int) -> list[dict]:
        """某会话的全部事件（按 seq 升序）。给导出台账/步骤序列用。"""
        try:
            rows = self._conn().execute(
                "SELECT seq, kind, tool, detail, data_json"
                " FROM events WHERE session_id=? ORDER BY seq",
                (session_id,)).fetchall()
        except sqlite3.Error:
            return []
        return [{"seq": r[0], "kind": r[1], "tool": r[2],
                 "detail": r[3], "data_json": r[4]} for r in rows]

    def efficiency_stats(self, session_id: int) -> dict:
        """操作效率自查（finish 时提示，防止"跑得很脏"却无人察觉）。

        判据来自 SKILL.md「执行节奏 → 操作循环」：
          · **observe 次数应明显少于 act 次数**（0 也正常）——
            第一反应是读上一步的 `next`，不是再 observe 一次。
          · **swipe 用于拨滚轮是反模式** —— 有惯性会过冲，
            重复手势应用 `act tap --repeat N`。

        这里只**如实统计并提示**，不阻断 finish（效率差不等于用例结论错）。
        """
        out = {"act": 0, "observe": 0, "swipe": 0, "cp_miss": 0, "ratio": None}
        try:
            rows = self._conn().execute(
                "SELECT kind, data_json FROM events WHERE session_id=?",
                (session_id,)).fetchall()
        except sqlite3.Error:
            return out
        for kind, dj in rows:
            if kind == "observe":
                out["observe"] += 1
            elif kind == "act":
                out["act"] += 1
                if not dj:
                    continue
                if '"type": "swipe"' in dj or '"type":"swipe"' in dj:
                    out["swipe"] += 1
                if '"ok": false' in dj or '"ok":false' in dj:
                    out["cp_miss"] += 1
        if out["act"]:
            out["ratio"] = round(out["observe"] / out["act"], 2)
        return out

    # ── 用例库（正式用例资产：口述文本的沉淀与复用）─────────────────
    def add_case(self, title: str, user_input: str, package: str | None = None,
                 kind: str = "test") -> int:
        if kind not in SESSION_KINDS:
            kind = "test"
        now = self._now()
        cur = self._conn().execute(
            "INSERT INTO cases (title, input, package, kind, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?)", (title, user_input, package, kind, now, now))
        self._conn().commit()
        return cur.lastrowid

    def set_case_kind(self, case_id: int, kind: str) -> bool:
        """切换用例类型（test 正式 / mock 占位，不入库；diag 预留）。"""
        if kind not in SESSION_KINDS:
            return False
        try:
            self._conn().execute(
                "UPDATE cases SET kind=?, updated_at=? WHERE id=?",
                (kind, self._now(), case_id))
            self._conn().commit()
            return True
        except sqlite3.Error:
            return False

    def update_case(self, case_id: int, title: str | None = None,
                    user_input: str | None = None, package: str | None = None) -> bool:
        try:
            self._conn().execute(
                "UPDATE cases SET"
                " title=COALESCE(?, title), input=COALESCE(?, input),"
                " package=COALESCE(?, package), updated_at=? WHERE id=?",
                (title, user_input, package, self._now(), case_id))
            self._conn().commit()
            return True
        except sqlite3.Error:
            return False

    def get_case(self, case_id: int) -> dict | None:
        row = self._conn().execute(
            "SELECT * FROM cases WHERE id=?", (case_id,)).fetchone()
        return dict(row) if row else None

    def list_cases(self, limit: int = 100, include_mock: bool = False) -> list[dict]:
        """用例列表。默认排除 mock（占位/构造数据，不该出现在用例库）。"""
        sql = ("SELECT c.*,"
               " (SELECT COUNT(*) FROM sessions s WHERE s.case_id=c.id) AS session_count"
               " FROM cases c")
        if not include_mock:
            sql += " WHERE COALESCE(c.kind,'test') != 'mock'"
        sql += " ORDER BY c.id DESC LIMIT ?"
        rows = self._conn().execute(sql, (limit,)).fetchall()
        return [dict(r) for r in rows]

    def module_stats(self) -> list[dict]:
        """按模块（包名）聚合用例数与结论分布（仪表盘/用例库分组）。

        package 为空时**不再一律归成「未识别」**：优先从该用例最近一次
        关联会话的 package 兜底，仍为空才记「未识别」。否则历史用例
        （建卡时没写包名）会全挤进一个桶，模块分类失去意义（真实 bug）。
        """
        rows = self._conn().execute(
            "SELECT COALESCE(NULLIF(c.package,''),"
            "                NULLIF((SELECT s.package FROM sessions s"
            "                        WHERE s.id=c.last_session_id),''),"
            "                '未识别') AS package,"
            " COUNT(*) AS total,"
            " SUM(CASE WHEN c.last_status='PASS' THEN 1 ELSE 0 END) AS pass,"
            " SUM(CASE WHEN c.last_status='FAIL' THEN 1 ELSE 0 END) AS fail,"
            " SUM(CASE WHEN c.last_status='BLOCKED' THEN 1 ELSE 0 END) AS blocked,"
            " SUM(CASE WHEN c.last_status='ERROR' THEN 1 ELSE 0 END) AS error,"
            " SUM(CASE WHEN c.last_status IS NULL THEN 1 ELSE 0 END) AS never_run"
            " FROM cases c"
            " WHERE COALESCE(c.kind,'test') != 'mock'"
            " GROUP BY 1 ORDER BY total DESC").fetchall()
        return [dict(r) for r in rows]

    def delete_case(self, case_id: int) -> bool:
        """删除用例（**不牵连**历史会话：sessions.case_id 置空而非级联删）。

        ⚠️ WebUI 的「删除用例」走的是 `delete_case_cascade`（连记录一起删），
        不是本方法。本方法保留给"只想去掉用例、留住记录"的场景（如 CLI 整理用例库）。
        """
        try:
            cur = self._conn().execute("DELETE FROM cases WHERE id=?", (case_id,))
            self._conn().execute(
                "UPDATE sessions SET case_id=NULL WHERE case_id=?", (case_id,))
            self._conn().commit()
            return cur.rowcount > 0
        except sqlite3.Error:
            return False

    def delete_case_cascade(self, case_id: int) -> dict:
        """删除用例，**并连带删除其全部历史测试记录**（数据库 + 磁盘证据）。

        与 delete_case（只解关联、保留会话）的区别：
          本方法用于 WebUI「删除用例」——语义是"这条用例连同它的记录一起不要了"。

        连带删除的内容：
          ① 该用例关联的每个会话 → 复用 delete_session（含 events/findings/
             finding_events 及磁盘截图、dump、日志）；
          ② 用例自身的 YAML / 渲染 MD（assets/cases/<应用>/<标题>.yaml|.md）。

        ⚠️ 不可恢复。调用方负责二次确认。
        """
        out = {"ok": False, "case": False, "sessions": 0,
               "files": 0, "dirs": 0, "spec_files": 0}
        try:
            row = self._conn().execute(
                "SELECT id, title FROM cases WHERE id=?", (case_id,)).fetchone()
            if row is None:
                return out
            title = row["title"]

            # ① 逐会话级联删（删一个提交一次，中途失败不会留下半删状态）
            sids = [r["id"] for r in self._conn().execute(
                "SELECT id FROM sessions WHERE case_id=?", (case_id,))]
            for sid in sids:
                r = self.delete_session(sid, remove_evidence=True)
                out["files"] += r.get("files", 0)
                out["dirs"] += r.get("dirs", 0)
            out["sessions"] = len(sids)

            # ② 用例的程序化描述文件
            if title:
                # 用例按应用分目录放在 assets/cases/<包名>/ 下，要**递归**找同名文件。
                from runtime import CASES_DIR as _CASES
                found: list[Path] = []
                if _CASES.is_dir():
                    for suf in (".yaml", ".md"):
                        found.extend(_CASES.rglob(f"{title}{suf}"))
                for fp in found:
                    try:
                        if fp.is_file():
                            fp.unlink()
                            out["spec_files"] += 1
                    except OSError:
                        pass

            # ③ 最后删用例本身。此时已无会话引用它，无需再解关联。
            cur = self._conn().execute("DELETE FROM cases WHERE id=?", (case_id,))
            self._conn().commit()
            out["case"] = cur.rowcount > 0
            out["ok"] = out["case"]
            return out
        except sqlite3.Error:
            return out

    def delete_session(self, session_id: int, remove_evidence: bool = True) -> dict:
        """删除一次测试记录：数据库行 + **连带磁盘证据**（截图/日志）。

        返回 {"ok","files","dirs"}。证据定位走三条线索（缺一会漏删）：
          ① events.evidence 字段记的相对路径；
          ② storage/{evidence,logs}/<会话id>/ 自动命名目录；
          ③ ①中每个文件**所在的步骤目录**（shot.png 与 dump.xml/meta.json 同目录，
             只删 events 里那一个文件会把 dump/meta 留成孤儿）。

        ## ⚠️ 性能：**先删数据库行立刻返回，文件删除放后台**（2026-11-03）

        真实事故：WebUI 删一个 40 证据的会话耗时 **6.8 秒**（120 文件/40 目录）。
        期间 HTTP 连接挂着，浏览器/系统回收空闲连接后，响应写不回去 ——
        ConnectionAbortedError [WinError 10053]，前端 fetch 也失败，
        用户看到「没删掉」，实际库已删 → 界面与事实不一致。

        顺序反过来就好：**数据库行毫秒级删掉 → 响应立刻回 → 文件后台慢慢删**。
        证据文件只是"录像"，晚几秒删掉不影响数据一致性；
        即使 webui 进程退出没删完，也只是残留几个孤儿文件，无害。

        后台线程用 daemon=True：webui 关停时不阻塞（没删完就当残留）。
        """
        import shutil
        import threading

        conn = self._conn()

        # ① 先收集证据路径（删行之前，还能查到）
        ev_paths = [r["evidence"] for r in conn.execute(
            "SELECT evidence FROM events WHERE session_id=?",
            (session_id,)).fetchall() if r["evidence"]]

        # ② 删数据库行（毫秒级）
        try:
            cur = conn.execute("DELETE FROM sessions WHERE id=?", (session_id,))
            conn.execute("DELETE FROM events WHERE session_id=?", (session_id,))
            conn.execute(
                "DELETE FROM finding_events WHERE finding_id IN "
                "(SELECT id FROM findings WHERE session_id=?)", (session_id,))
            conn.execute("DELETE FROM findings WHERE session_id=?", (session_id,))
            conn.commit()
        except sqlite3.Error:
            return {"ok": False, "files": 0, "dirs": 0}
        ok = cur.rowcount > 0
        if not ok or not remove_evidence:
            return {"ok": ok, "files": 0, "dirs": 0}

        # ③ 文件删除放后台 daemon 线程（秒级，不阻塞 HTTP 响应）
        db_path = self.path
        storage_root = Path(db_path).parent.resolve()

        def _bg_cleanup() -> None:
            files_n, dirs_n = 0, 0
            try:
                step_dirs: set[Path] = set()
                for rel_p in ev_paths:
                    try:
                        fp = (ROOT / rel_p).resolve()
                        if not fp.is_relative_to(storage_root):
                            continue
                        if fp.is_file():
                            fp.unlink()
                            files_n += 1
                        if fp.parent != storage_root and fp.parent.is_dir():
                            step_dirs.add(fp.parent)
                    except OSError:
                        pass
                # 步骤目录整棵删（含同目录 dump.xml / meta.json / crash.log）
                for d in step_dirs:
                    n = sum(1 for _ in d.rglob("*") if _.is_file())
                    if shutil.rmtree(d, ignore_errors=True) or not d.exists():
                        files_n += n
                        dirs_n += 1
                # 自动命名目录 <会话id>/ 整棵删
                for base in ("evidence", "logs"):
                    d = Path(db_path).parent / base / str(session_id)
                    if d.is_dir():
                        n = sum(1 for _ in d.rglob("*") if _.is_file())
                        if shutil.rmtree(d, ignore_errors=True) or not d.exists():
                            files_n += n
                            dirs_n += 1
                # 清掉遗留的空目录（只清空的，不动有内容的）
                ev_root = Path(db_path).parent / "evidence"
                if ev_root.is_dir():
                    for sub in sorted(ev_root.rglob("*"), reverse=True):
                        if sub.is_dir() and not any(sub.iterdir()):
                            try:
                                sub.rmdir()
                                dirs_n += 1
                            except OSError:
                                pass
            except Exception:  # noqa: BLE001
                pass      # 后台清理失败无害：顶多残留孤儿文件

        threading.Thread(target=_bg_cleanup, daemon=True,
                         name=f"evidence-cleanup-{session_id}").start()
        return {"ok": True, "files": 0, "dirs": 0}  # 文件后台删，数字未知记 0

    def set_session_kind(self, session_id: int, kind: str) -> bool:
        """切换会话类型（test=进测试记录 / diag=验证调试，默认不展示）。"""
        if kind not in SESSION_KINDS:
            return False
        try:
            self._conn().execute("UPDATE sessions SET kind=? WHERE id=?",
                                 (kind, session_id))
            self._conn().commit()
            return True
        except sqlite3.Error:
            return False

    def run_stats(self, days: int = 3) -> list[dict]:
        """最近 N 天每天的执行情况（仪表盘柱状图：成功率/失败率）。

        只统计正式测试（kind='test'），验证/调试会话不计入，
        否则自测噪音会污染成功率曲线。
        """
        try:
            rows = self._conn().execute(
                "SELECT substr(started_at,1,10) AS day, COUNT(*) AS total,"
                " SUM(CASE WHEN status='PASS' THEN 1 ELSE 0 END) AS pass,"
                " SUM(CASE WHEN status='FAIL' THEN 1 ELSE 0 END) AS fail,"
                " SUM(CASE WHEN status='BLOCKED' THEN 1 ELSE 0 END) AS blocked,"
                " SUM(CASE WHEN status NOT IN ('PASS','FAIL','BLOCKED')"
                "     THEN 1 ELSE 0 END) AS other"
                " FROM sessions WHERE kind='test'"
                # date('now') 是 UTC，而 started_at 是本地时间——
                # 用 'localtime' 对齐，否则跨天边界（本地 00:00~08:00）会算错一天。
                "   AND substr(started_at,1,10) >= date('now','localtime', ?)"
                " GROUP BY day ORDER BY day DESC", (f"-{max(0, days - 1)} day",)).fetchall()
            return [dict(r) for r in rows]
        except sqlite3.Error:
            return []

    def case_overview(self) -> dict:
        """仪表盘汇总：用例总数 + 最近结论分布。"""
        try:
            row = self._conn().execute(
                "SELECT COUNT(*) AS total,"
                " SUM(CASE WHEN last_status='PASS' THEN 1 ELSE 0 END) AS pass,"
                " SUM(CASE WHEN last_status='FAIL' THEN 1 ELSE 0 END) AS fail,"
                " SUM(CASE WHEN last_status='BLOCKED' THEN 1 ELSE 0 END) AS blocked,"
                " SUM(CASE WHEN last_status='ERROR' THEN 1 ELSE 0 END) AS error,"
                " SUM(CASE WHEN last_status IS NULL THEN 1 ELSE 0 END) AS never_run"
                " FROM cases WHERE COALESCE(kind,'test') != 'mock'").fetchone()
            return dict(row) if row else {}
        except sqlite3.Error:
            return {}

    def touch_case(self, case_id: int, session_id: int, status: str) -> None:
        """会话 finish 时回写用例的运行统计。"""
        try:
            self._conn().execute(
                "UPDATE cases SET last_session_id=?, last_run_at=?, last_status=?,"
                " run_count=COALESCE(run_count,0)+1 WHERE id=?",
                (session_id, self._now(), status, case_id))
            self._conn().commit()
        except sqlite3.Error:
            pass

    # ── 读（Web UI 用）───────────────────────────────────────────
    def list_sessions(self, limit: int = 100, kind: str | None = "test",
                      include: tuple[str, ...] | None = None) -> list[dict]:
        """会话列表。

        kind='test'（默认）= 只看正式测试记录；
        include=None 且 kind=None → 全部（含 diag/mock，仅调试用）；
        include=("test","diag") → 正式 + 验证调试（mock 仍排除）。
        mock（占位/构造数据）**永不进正式列表**。
        """
        # ⚠️ 时间基准坑（踩过两次，务必看清）：
        #   · julianday('now') 是 **UTC**，而 started_at/finished_at 由
        #     datetime.now() 写成**本地时间**——直接相减差一个时区（UTC+8 得 -28800s）。
        #   · 但 'localtime' 修饰符只能加在 **'now'** 上，**不能加在 finished_at 上**：
        #     julianday(finished_at,'localtime') 的语义是"把 finished_at 当 UTC 再换算"，
        #     而它本来就是本地串 → 对已收尾会话二次偏移（150s 变成 28950s）。
        #   正解：对 'now' 就地取本地时间；finished_at 已是本地串，原样使用。
        sql = ("SELECT s.*, MAX(0, ROUND((julianday(COALESCE(s.finished_at,"
               " datetime('now','localtime'))) - julianday(s.started_at)) * 86400, 1))"
               " AS duration_seconds,"
               " (SELECT COUNT(*) FROM events e WHERE e.session_id=s.id) AS event_count,"
               " (SELECT COUNT(*) FROM findings f WHERE f.session_id=s.id"
               "   AND f.status='FAIL') AS fail_count,"
               " (SELECT COUNT(*) FROM findings f WHERE f.session_id=s.id) AS finding_count"
               " FROM sessions s")
        params: tuple = ()
        if include:
            ph = ",".join("?" for _ in include)
            sql += f" WHERE COALESCE(s.kind,'test') IN ({ph})"
            params = tuple(include)
        elif kind:
            sql += " WHERE COALESCE(s.kind,'test')=?"
            params = (kind,)
        sql += " ORDER BY s.id DESC LIMIT ?"
        rows = self._conn().execute(sql, params + (limit,)).fetchall()
        out = [dict(r) for r in rows]
        # 「回放」入口是否可用：该会话至少有一帧的截图还在磁盘上。
        # ⚠️ 证据可能被 cleanup 轮转或误删 —— 那时按钮要置灰，而不是点进去才发现播不了。
        # 放在这里统一算（列表页要按行显示），避免前端为每条记录各发一次请求。
        for r in out:
            evs = self._conn().execute(
                "SELECT evidence FROM events WHERE session_id=? AND evidence IS NOT NULL"
                " AND evidence != '' LIMIT 50", (r["id"],)).fetchall()
            r["has_evidence"] = any((ROOT / e["evidence"]).is_file() for e in evs)
        return out

    def session_kind_counts(self) -> dict:
        """各类型会话数（测试记录页「显示验证记录」开关用）。

        mock 不计入（它对用户不可见，不该出现在任何计数里）。
        """
        try:
            rows = self._conn().execute(
                "SELECT COALESCE(kind,'test') AS k, COUNT(*) AS n"
                " FROM sessions WHERE COALESCE(kind,'test') != 'mock'"
                " GROUP BY k").fetchall()
            return {r["k"]: r["n"] for r in rows}
        except sqlite3.Error:
            return {}

    def get_session(self, session_id: int) -> dict | None:
        row = self._conn().execute(
            "SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        if not row:
            return None
        s = dict(row)
        s["events"] = [dict(r) for r in self._conn().execute(
            "SELECT * FROM events WHERE session_id=? ORDER BY seq", (session_id,))]
        s["findings"] = [dict(r) for r in self._conn().execute(
            "SELECT * FROM findings WHERE session_id=? ORDER BY id", (session_id,))]
        for finding in s["findings"]:
            finding["event_ids"] = [r[0] for r in self._conn().execute(
                "SELECT event_id FROM finding_events WHERE finding_id=? ORDER BY event_id",
                (finding["id"],))]
            if finding["event_id"] is not None and finding["event_id"] not in finding["event_ids"]:
                finding["event_ids"].insert(0, finding["event_id"])
        return s

    # ── 单库化后新增：按 run_id 查询 ─────────────────────────────
    def sessions_of_run(self, run_id: str) -> list[dict]:
        """某次运行（uuid）下的全部会话。

        正常只有一个；重复 start 会产生多个（2026-10-31 跑 46 就撞见过）。
        """
        return [dict(r) for r in self._conn().execute(
            "SELECT * FROM sessions WHERE run_id=? ORDER BY id", (run_id,))]

    def list_runs(self, limit: int = 200) -> list[dict]:
        """运行列表（按 run_id 聚合），新的在前。

        单库化让这件事变成一句 GROUP BY —— 以前要开 138 个库才能算出来。
        """
        rows = self._conn().execute(
            "SELECT run_id,"
            " COUNT(*) AS session_count,"
            " MIN(started_at) AS started_at,"
            " MAX(COALESCE(finished_at, started_at)) AS ended_at,"
            " GROUP_CONCAT(DISTINCT title) AS titles,"
            " GROUP_CONCAT(DISTINCT status) AS statuses"
            " FROM sessions WHERE run_id IS NOT NULL AND run_id != ''"
            " GROUP BY run_id ORDER BY MIN(started_at) DESC LIMIT ?",
            (limit,)).fetchall()
        out = [dict(r) for r in rows]
        for r in out:
            # 库里有多个会话时取「有结论的那个」当代表状态
            sts = (r.get("statuses") or "").split(",")
            for pref in ("PASS", "FAIL", "BLOCKED", "ERROR"):
                if pref in sts:
                    r["status"] = pref
                    break
            else:
                r["status"] = sts[0] if sts else "running"
            r["title"] = (r.get("titles") or "").split(",")[0]
        return out

    def stats_by_run(self) -> dict:
        """按 run 维度的总量（供仪表盘）。"""
        row = self._conn().execute(
            "SELECT COUNT(DISTINCT run_id) AS runs, COUNT(*) AS sessions"
            " FROM sessions WHERE run_id IS NOT NULL AND run_id != ''").fetchone()
        return {"runs": row[0], "sessions": row[1]}

    def session_stats(self) -> dict:
        """正式测试记录的结论统计（kind='test'，验证/调试会话不计入）。"""
        row = self._conn().execute(
            "SELECT COUNT(*) AS total,"
            " SUM(CASE WHEN status='PASS' THEN 1 ELSE 0 END) AS pass_n,"
            " SUM(CASE WHEN status='FAIL' THEN 1 ELSE 0 END) AS fail_n,"
            " SUM(CASE WHEN status='BLOCKED' THEN 1 ELSE 0 END) AS blocked_n,"
            " SUM(CASE WHEN status='running' THEN 1 ELSE 0 END) AS running_n,"
            " SUM(CASE WHEN status='ERROR' THEN 1 ELSE 0 END) AS error_n"
            " FROM sessions WHERE COALESCE(kind,'test')='test'").fetchone()
        return dict(row) if row else {}


_DB: SessionDB | None = None


def get_db() -> SessionDB:
    global _DB
    if _DB is None:
        _DB = SessionDB()
    return _DB


# ── 当前会话（工具层自动挂接）────────────────────────────────────
def set_current(session_id: int) -> None:
    # 用 _current_file() 而不是模块级 CURRENT_FILE —— 它要跟着 record 目录走
    cf = _current_file()
    cf.parent.mkdir(parents=True, exist_ok=True)
    cf.write_text(str(session_id), encoding="utf-8")


def clear_current(expected_id: int | None = None) -> None:
    if expected_id is not None:
        try:
            if int(_current_file().read_text(encoding="utf-8").strip()) != expected_id:
                return
        except FileNotFoundError:
            return
    _current_file().unlink(missing_ok=True)


def current_session() -> int | None:
    explicit = os.environ.get("ANDROID_TEST_SESSION_ID")
    if explicit is not None:
        return int(explicit)
    try:
        return int(_current_file().read_text(encoding="utf-8").strip())
    except FileNotFoundError:
        return None
