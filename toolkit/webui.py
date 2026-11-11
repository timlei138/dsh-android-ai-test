#!/usr/bin/env python3
"""本地 Web 测试台（v2）：测试会话时间线 + 知识库编辑。
零依赖：Python 标准库 http.server。

启动:  python webui.py [--port 8900]
访问:  http://127.0.0.1:8900
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import urllib.parse
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "tools"))
from common import resolve_case_stem  # noqa: E402
from db import SessionDB, get_db  # noqa: E402
from runtime import KNOWLEDGE_DIR, STORAGE_DIR  # noqa: E402

# ★ UTF-8 兜底：Windows 控制台默认 GBK，启动日志里 print 一个 ⚠️ 就崩。
#   实测踩过：webui 起不来，AI 以为是端口问题，换了 5 种启动方式都没想明白 ——
#   实际是一个 print 里的 emoji 编码失败。
try:
    from common import ensure_utf8_stdio
    ensure_utf8_stdio()
except Exception:  # noqa: BLE001
    pass

WEBUI_DIR = ROOT / "webui"
# 目录一律从 runtime 导入（那里只有一处定义，随工作目录变化）。
MD_NAME_RE = re.compile(r"^[\w\u4e00-\u9fff.\-]+\.md$")

# 用例：assets/cases/<应用>/，按应用分子目录，所以查文件一律 rglob。
# assets 是**资产**（进 git），storage 是**运行产物**（不进 git）。
CASES_DIR = ROOT / "assets" / "cases"
SPEC_NAME_RE = re.compile(r"^[\w\u4e00-\u9fff.\-（）()]+$")


# ══════════════════════════════════════════════════════════════════════
# 数据库：单库直查
# ══════════════════════════════════════════════════════════════════════
#
# 所有运行共用 `storage/test.db`，一次 SELECT 就能拿到全部历史和统计。
# 运行之间靠 `sessions.run_id` 字段隔离（见 tools/db.py 顶部说明），
# 不再按目录分库。
#
# 会话 id 是全局自增数字（"12"）。前端和旧链接还可能传
# `<run_id>:<local_id>` 复合形式，`_split_sid` 兼容两种。


def _split_sid(sid: str) -> tuple[str, int | None]:
    """把 `<run_id>:<local_id>` 拆开。纯数字 -> (run_id 为空, id)。

    返回 (run_id, local_id)：
      · "6ea8445e-...:1"  -> ("6ea8445e-...", 1)
      · "12"             -> ("", 12)         直接是全局 session id
      · 解析不了          -> ("", None)
    """
    sid = str(sid or "").strip()
    if ":" in sid:
        head, _, tail = sid.rpartition(":")
        if tail.isdigit():
            return head, int(tail)
        return "", None
    return ("", int(sid)) if sid.isdigit() else ("", None)


def _open_db() -> SessionDB | None:
    """拿数据库连接。打不开返回 None（不该让整页崩）。"""
    try:
        return get_db()
    except Exception:  # noqa: BLE001
        return None


def all_sessions(include: tuple[str, ...] | None = None,
                 kind: str | None = "test") -> list[dict]:
    """全部会话列表（单库直查，不再跨库合并）。

    每个 session 的 `id` 保持全局数字 id；`run_id` 从库里读，
    前端仍可用 `<run_id>:<id>` 复合形式（`_split_sid` 兼容）。
    """
    db = _open_db()
    if db is None:
        return []
    try:
        rows = db.list_sessions(include=include, kind=kind, limit=2000)
    except Exception:  # noqa: BLE001
        return []
    out = []
    for r in rows:
        r = dict(r)
        r["local_id"] = r.get("id")
        r["run_id"] = r.get("run_id") or ""
        out.append(r)
    out.sort(key=lambda r: str(r.get("started_at") or ""), reverse=True)
    return out


def session_detail(sid: str, include_events: bool = True) -> dict | None:
    """取单个会话详情（含 events / findings）。sid 可以是复合 id 或纯数字。"""
    _rid, local_id = _split_sid(sid)
    if local_id is None:
        return None
    db = _open_db()
    if db is None:
        return None
    try:
        s = db.get_session(local_id)
    except Exception:  # noqa: BLE001
        return None
    if not s:
        return None
    s = dict(s)
    s["local_id"] = s.get("id")
    s["run_id"] = s.get("run_id") or _rid or ""
    return s


def session_stats_all() -> dict:
    """全部运行的结论统计（测试记录页顶部那几个数字）。

    `db.session_stats()` 用 `pass_n` / `fail_n` / `blocked_n` / `running_n`
    这种带 `_n` 后缀的字段名。
    """
    # 结论只有 PASS / FAIL / BLOCKED / ERROR 四种，RUNNING 是还没收尾的中间态。
    agg = {"PASS": 0, "FAIL": 0, "BLOCKED": 0,
           "ERROR": 0, "RUNNING": 0, "total": 0}
    KEYMAP = {
        "pass_n": "PASS", "fail_n": "FAIL", "blocked_n": "BLOCKED",
        "running_n": "RUNNING", "error_n": "ERROR",
    }
    db = _open_db()
    if db is None:
        return agg
    try:
        st = db.session_stats() or {}
    except Exception:  # noqa: BLE001
        return agg
    agg["total"] += int(st.get("total") or 0)
    for src, dst in KEYMAP.items():
        agg[dst] += int(st.get(src) or 0)
    return agg


def session_kind_counts_all() -> dict:
    db = _open_db()
    if db is None:
        return {}
    try:
        k = db.session_kind_counts()
    except Exception:  # noqa: BLE001
        return {}
    return {key: int(n or 0) for key, n in (k or {}).items()}


def run_stats_all(days: int = 3) -> list[dict]:
    """最近 N 天每天的执行情况（仪表盘柱状图）。"""
    db = _open_db()
    if db is None:
        return []
    try:
        return db.run_stats(days) or []
    except Exception:  # noqa: BLE001
        return []


def _normalize_crlf(text: str) -> str:
    """统一换行成 CRLF（并去掉 BOM）。

    全库 70 个用例 md 都是 CRLF、无 BOM。浏览器 textarea 里是 LF，
    直接落盘会让每次编辑都变成「整文件重写」的 git 噪音。
    """
    text = text.lstrip("\ufeff")
    return text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r\n")


def _find_spec(title: str, suffix: str) -> Path | None:
    """按名字在 assets/cases/**/ 下找文件。

    为什么不能直接 `CASES_DIR / f"{title}{suffix}"`：
    分子目录后，文件在 `cases/<应用>/<title>.yaml`，
    直接拼根目录会永远找不到（而且报"还没渲染"这种误导性错误）。
    """
    if not CASES_DIR.is_dir():
        return None
    for fp in CASES_DIR.rglob(f"{title}{suffix}"):
        if fp.is_file():
            return fp
    return None


# 用例编号：`com.zui.calendar_168` -> "168"，`联想日历_168 课程表` -> "168"
#
# 前后各加一个约束，两个方向都有实际反例：
#   前面 `[_\-]`   防止把 `com.zui.calendar` 里没有的数字算进来；
#                  也防止 `abc1234` 这种没有分隔符的串被误当编号
#   后面 `(?!\d)`  防止 `1680` 被切出 `168`（那样 168 和 1680 会串）
#
# ⚠️ 这个正则和 tools/session.py 里"提示用 --case 复用"的那个必须一致。
CASE_NUM_RE = re.compile(r"[_\-](\d{2,4})(?!\d)")


def _case_number(text: str) -> str | None:
    """从用例文件名或会话标题里抽用例编号。

    用例在文件里叫 `com.zui.calendar_168`，会话标题叫
    `联想日历_168 课程表-从图库导入` —— 两边唯一稳定的共同点就是 168。
    dashboard 要靠它把"跑过的记录"对到"用例"上。
    """
    m = CASE_NUM_RE.search(str(text or ""))
    return m.group(1) if m else None


def _scan_case_files() -> list[dict]:
    """把 `assets/cases/<应用>/` 下的用例文件列成「用例库」的条目。

    用例的**唯一真相是文件**，数据库只是"跑过的痕迹"：
      · 加一条用例 —— 就是放一个 `.md` 文件
      · 数据库的 cases 表只有"跑过且回写过"的才有记录

    ## 同一条用例的 .md 和 _steps.yaml 怎么合并

    扩展名不同、**文件名同源** = 同一条用例：
      · `.md`         人写的原文（一定有）
      · `_steps.yaml` 跑过之后沉淀的跳转步骤（可能有，与 md 同名）
    一条用例只出一行，`has_steps` 标记有没有跳转步骤。

    ## 运行统计从哪来：按**用例 md 文件名**关联

    用例在文件里（`com.zui.calendar_新建课程表_176`），会话在数据库里
    （`联想日历_176 课程表-从图库导入`），两边名字对不上 —— 用
    `common.resolve_case_stem()` 把会话标题解回 md 文件名再配对。

    ⚠️ 不能用**编号**配对。编号会撞：`com.zui.calendar_启动应用_03` 和
       `com.zui.calendar_重要日倒数_03` 都是 03，按编号汇总时两条用例
       共用同一份次数和最近结论，两条都是错的。
       解不出来的会话（诊断会话、标题对不上任何文件）直接不计入。
    """
    if not CASES_DIR.is_dir():
        return []

    # (应用目录, 标题) -> 该标题下的文件
    found: dict[tuple[str, str], dict[str, Path]] = {}
    # `other/` 是**归档目录**，里面的用例不属于当前用例集。
    # 不排除的话它会当成一个「应用名=other」的包混进用例库。
    SKIP_DIRS = {"other"}
    for fp in sorted(CASES_DIR.rglob("*")):
        if not fp.is_file() or fp.suffix.lower() not in (".md", ".yaml"):
            continue
        # 应用目录名 = 相对 CASES_DIR 的第一段
        try:
            rel = fp.relative_to(CASES_DIR)
        except ValueError:
            continue
        app = rel.parts[0] if len(rel.parts) > 1 else ""
        if app in SKIP_DIRS:
            continue
        # 跳转步骤副产物 `xxx_steps.yaml` 不是用例本身 —— 它是 finish 时
        # 从**同名用例 md** 导出的，所以文件名去掉 `_steps` 就回到主用例上。
        stem = fp.stem
        is_steps = stem.endswith("_steps")
        if is_steps:
            stem = stem[: -len("_steps")]
        slot = found.setdefault((app, stem), {})
        if is_steps:
            slot["steps"] = fp
        else:
            slot[fp.suffix.lower()] = fp

    # ── 扫库，按用例 md 文件名汇总 ────────────────────────
    #
    # 一次遍历拿全：stem -> {run_count, last_status, last_run_at,
    #                        last_session_id, last_run_id}
    # 不逐条用例查库。
    #
    # ⚠️ 关联键是 **stem（md 文件名，含模块名）**，不是编号。
    #    编号会撞：com.zui.calendar_启动应用_03 和 com.zui.calendar_重要日倒数_03
    #    都是 03，按编号汇总时两条用例共用一份统计，次数和最近结论都会串。
    by_stem: dict[str, dict] = {}
    db = _open_db()
    if db is not None:
        try:
            rows = db._conn().execute(
                "SELECT id, run_id, title, user_input, package, status,"
                " started_at FROM sessions WHERE title IS NOT NULL"
                " ORDER BY started_at").fetchall()
        except Exception:  # noqa: BLE001
            rows = []
        for r in rows:
            pkg = r["package"] or "com.zui.calendar"
            stem = resolve_case_stem(pkg, r["title"], r["user_input"])
            # 认不出是哪条用例的会话（诊断会话、标题对不上文件）不计入统计
            if not stem:
                continue
            slot = by_stem.setdefault(stem, {"run_count": 0})
            slot["run_count"] += 1
            # rows 按 started_at 升序遍历，最后写入的就是最近一次
            slot["last_status"] = r["status"]
            slot["last_run_at"] = r["started_at"]
            slot["last_session_id"] = r["id"]
            slot["last_run_id"] = r["run_id"] or ""

    items: list[dict] = []
    for (app, stem), files in sorted(
            found.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        # 只有 `_steps.yaml` 而没有 `.md`/`.yaml` 的，是**孤立的导产物** ——
        # 用例原文都没有，列出来只会在用例库里多几个点进去空白的条目。
        if not (".md" in files or ".yaml" in files):
            continue
        num = _case_number(stem)
        st = by_stem.get(stem, {})
        # id 用标题（前端只用它做 key / 路由），不用数据库自增 id
        items.append({
            "id": stem,
            "title": stem,
            "package": app,
            "case_number": num,
            "has_md": ".md" in files,
            "has_yaml": ".yaml" in files,
            # `_steps.yaml` = 跑过之后导出的跳转步骤，算附属产物，不是独立用例
            "has_steps": "steps" in files,
            "run_count": st.get("run_count") or 0,
            "last_status": st.get("last_status"),
            "last_run_at": st.get("last_run_at"),
            "last_session_id": st.get("last_session_id"),
        })
    return items


def _case_detail(item: dict) -> dict:
    """单条用例详情：列表字段 + 用例原文。

    原文优先读 `.md`（人写的），没有 `.md` 就渲 `.yaml` 的 text。
    前端「用例原文」那个页签直接显示 `input` 字段。
    """
    out = dict(item)
    text = ""
    md = _find_spec(item["title"], ".md")
    if md is not None:
        try:
            text = md.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
    if not text:
        yml = _find_spec(item["title"], ".yaml")
        if yml is not None:
            try:
                text = yml.read_text(encoding="utf-8", errors="replace")
            except OSError:
                text = ""
    out["input"] = text
    return out


def _module_stats() -> list[dict]:
    """按应用（包名）聚合用例数与结论分布 —— 供 `/api/modules` 和仪表盘用。

    和 `_scan_case_files()` 同源：从 assets/cases 的文件算，不从 cases 表。
    """
    items = _scan_case_files()
    agg: dict[str, dict] = {}
    for it in items:
        pkg = it.get("package") or "未识别"
        a = agg.setdefault(pkg, {
            "package": pkg, "total": 0, "pass": 0, "fail": 0,
            "blocked": 0, "error": 0, "never_run": 0,
        })
        a["total"] += 1
        st = it.get("last_status")
        if not st:
            a["never_run"] += 1
        elif st in ("PASS", "FAIL", "BLOCKED", "ERROR"):
            a[st.lower()] += 1
        else:
            a["never_run"] += 1
    return sorted(agg.values(), key=lambda x: x["package"])


def _case_overview() -> dict:
    """仪表盘顶部的用例总数 + 结论分布 —— 也从文件算。

    用户反馈：「仪表盘用例显示 0」。

    原因和用例库一样：`/api/dashboard` 用的是 `db.case_overview()`，
    它 `COUNT(*) FROM cases` —— 表空就是 0，而 assets/cases 下有 15 个文件。
    """
    items = _scan_case_files()
    ov = {
        "total": len(items),
        "pass": 0, "fail": 0, "blocked": 0, "error": 0,
        "never_run": 0,
    }
    for it in items:
        st = it.get("last_status")
        key = (st or "").lower()
        if key in ("pass", "fail", "blocked", "error"):
            ov[key] += 1
        else:
            ov["never_run"] += 1
    return ov

# ── 视觉模型配置（OpenAI 兼容多模态通道，vision.py 消费）────────────
VISION_KEYS = ("base_url", "model", "api_key")


def _load_vision() -> dict:
    conf = {k: "" for k in VISION_KEYS}
    fp = STORAGE_DIR / "vision.json"
    try:
        data = json.loads(fp.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            for k in VISION_KEYS:
                conf[k] = str(data.get(k) or "")
    except (OSError, ValueError):
        pass
    return conf


def _save_vision(conf: dict) -> None:
    STORAGE_DIR.mkdir(exist_ok=True)
    (STORAGE_DIR / "vision.json").write_text(
        json.dumps(conf, ensure_ascii=False, indent=2), encoding="utf-8")


def _mask(s: str) -> str:
    """api_key 脱敏回显：只留首 4 与末 4 位。GET 绝不能返回明文。"""
    if not s:
        return ""
    if len(s) <= 8:
        return "*" * len(s)
    return s[:4] + "*" * (len(s) - 8) + s[-4:]


# 64x64 小图（画了个对勾）：视觉连通性测试用，不依赖本地证据文件。
#
# 尺寸不能太小 —— 服务端要求宽高都 >10，否则直接拒：
#     HTTP 400 "The image length and width do not meet the model restrictions."
# 这种失败看着像"配置错了"，其实是占位图太小，很容易被误读。
_TINY_PNG = base64.b64decode(
    b"iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAIAAAAlC+aJAAABEElEQVR42u2aMR"
    b"KEMAhF45+9Efc/Qo6T3sImo7O7BoEE51Nomf8CKIFsrbWS2VCS2+d4iUg66bXW"
    b"F3mgZ1rf+nhJ7wECEIAABCAAAQhAAAIQwKFOzgdwqB9lwIJ7P8SABSNn6GCI1O"
    b"rnAzxUv1YS61oKWGT71Q0RpFY/DcCwEYiMiTsTwFa9HkAXA+bqlQC6osVD/TCA"
    b"iOiKFif1wwC6hf3Ua0LotPxfJ7iqV+bAfQZv9Waf0TvJ4DQ9gVXhdWWwKha8PP"
    b"CbIUb90xD6pixy5gnbIv70owgYHML1IBIw9sRqJ6w5AFetYSNn2O53/8zXWoxX"
    b"z+YuAQhAAAIQgAAEIEB5z73RjPd303tg4+31ybYDbuB13HUQTMgAAAAASUVORK"
    b"5CYII="
)


def _test_vision() -> dict:
    """用已保存的配置做一次真实连通性验证（不发真图，只验鉴权+模型可用）。"""
    import base64
    import time
    import urllib.error
    import urllib.request

    conf = _load_vision()
    base = (conf.get("base_url") or "").rstrip("/")
    model = conf.get("model") or ""
    key = conf.get("api_key") or ""
    if not (base and model and key):
        return {"ok": False, "error": "配置不完整（base_url / model / api_key 均必填）"}

    b64 = base64.b64encode(_TINY_PNG).decode()
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": "回复两个字：可用"},
            {"type": "image_url",
             "image_url": {"url": f"data:image/png;base64,{b64}"}},
        ]}],
        "max_tokens": 20,
    }).encode()
    req = urllib.request.Request(
        base + "/chat/completions", data=body,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {key}"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.load(resp)
        ms = round((time.time() - t0) * 1000)
        text = data["choices"][0]["message"]["content"]
        return {"ok": True, "latency_ms": ms, "model": model,
                "reply": str(text)[:200],
                "usage": data.get("usage"),
                "hint": "连通成功：鉴权与模型均可用"}
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:300]
        except Exception:  # noqa: BLE001
            pass
        # 400 单独说 —— 它不是"配置错"，是"请求内容被拒"，
        # 说成 api_key 问题会把人带偏。
        if e.code == 400:
            hint = "400 多为请求内容问题（如测试图尺寸不合规）；配置本身可能没填错"
        elif e.code in (401, 403):
            hint = "401/403 多为 api_key 错误"
        elif e.code == 404:
            hint = "404 多为 base_url 少了或多了 /v1"
        else:
            hint = "按 detail 里的 message 定位；鉴权类看 api_key，路径类看 base_url"
        return {"ok": False, "error": f"HTTP {e.code} {e.reason}", "detail": detail,
                "hint": hint}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}",
                "hint": "网络不可达或 base_url 写错（需含 http(s):// 与 /v1 前缀，视服务而定）"}

MIME = {".html": "text/html", ".js": "application/javascript",
        ".css": "text/css", ".png": "image/png", ".xml": "text/xml",
        ".json": "application/json", ".md": "text/markdown", ".log": "text/plain"}


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", f"{ctype}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200):
        # ⚠️ 容忍客户端断连（2026-11-03）：
        #    删除接口耗时几秒（虽已改为先删库后台清文件），用户在此期间
        #    刷新/关闭页面时，浏览器 abort 连接 → 写响应抛 ConnectionAbortedError
        #    → 之前整段堆栈打到控制台，看起来像服务端坏了，实际操作已成功。
        #    此时无话可说，静默即可。其余异常照常抛出（那才是真 bug）。
        try:
            self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                       "application/json")
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            pass

    def _static(self, relpath: str):
        fp = (WEBUI_DIR / relpath).resolve()
        # is_relative_to 而非 startswith：字符串前缀会放过同前缀兄弟目录
        if not fp.is_relative_to(WEBUI_DIR) or not fp.is_file():
            return self._json({"error": "not found"}, 404)
        self._send(200, fp.read_bytes(), MIME.get(fp.suffix, "application/octet-stream"))

    def _replay(self, sid):
        """回放数据：把会话里有截图的事件串成"帧"。

        每帧 = {evidence(图片), x/y(点击坐标，仅动作有), size(原图尺寸),
                kind, action, via, why, detail, ts}

        ⚠️ **坐标换算交给前端按百分比做**：这里给的是**原图坐标系**的 x/y 和
        size，前端用 `left = x/size_w*100%` 定位 —— 这样图片无论被 CSS
        缩放成多大，圆点都不会偏。
        """
        s = session_detail(sid)
        if not s:
            return self._json({"error": "not found"}, 404)

        frames = []
        for e in s.get("events") or []:
            ev = e.get("evidence")
            if not ev:
                continue
            try:
                j = json.loads(e.get("data_json") or "{}")
            except ValueError:
                j = {}
            act = j.get("action") or {}
            size = j.get("size") or []
            # action 字段可能没存（历史数据），从 detail 首段回退（如"点击 (x,y) via ..."）
            action = act.get("action") or ""
            if not action and e.get("detail"):
                action = str(e["detail"]).split(" ")[0]
            frames.append({
                "seq": e.get("seq"),
                "kind": e.get("kind"),
                "evidence": ev,
                "x": act.get("x"),
                "y": act.get("y"),
                "size": size if len(size) == 2 else None,
                "action": action,
                "via": act.get("via") or "",
                "why": act.get("why") or "",
                "detail": e.get("detail") or "",
                # 图还在不在（被 cleanup 轮转/误删后为 False）
                "ok": (ROOT / ev).is_file(),
            })

        missing = sum(1 for f in frames if not f["ok"])
        return self._json({
            "session_id": sid,
            "title": s.get("title") or f"会话 #{sid}",
            "status": s.get("status"),
            "frames": frames,
            "total": len(frames),
            "missing": missing,
            # 一张可用的图都没有 → 前端把回放入口置灰
            "playable": any(f["ok"] for f in frames),
        })

    def _storage_file(self, relpath: str):
        """伺服 storage 下的证据文件。

        ⚠️ 事件流里的 evidence 字段来自 common.rel()，是**项目根相对**路径
        （形如 `storage/evidence/.../shot.png`），而本函数历史上按
        **storage 相对**解析 → 拼成 storage/storage/... 恒 404（真实事故：
        记录里所有截图全空白）。故先按 storage 相对试，未命中再按项目根相对试。
        """
        candidates = [STORAGE_DIR / relpath]
        p = Path(relpath)
        if p.parts and p.parts[0] == STORAGE_DIR.name:
            candidates.append(ROOT / p)
        for fp in candidates:
            fp = fp.resolve()
            # is_relative_to 而非 startswith：字符串前缀会放过同前缀兄弟目录
            if fp.is_relative_to(STORAGE_DIR) and fp.is_file():
                return self._send(200, fp.read_bytes(),
                                  MIME.get(fp.suffix, "application/octet-stream"))
        return self._json({"error": "not found"}, 404)

    # ── GET ─────────────────────────────────────────────────────
    def do_GET(self):  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        if path in ("/", "/index.html"):
            return self._static("index.html")
        if path.startswith("/static/"):
            return self._static(path[len("/static/"):])
        if path.startswith("/files/"):
            return self._storage_file(urllib.parse.unquote(path[len("/files/"):]))

        if path == "/api/sessions":
            # 默认只看正式测试记录；?all=1 追加**验证/调试**会话。
            # ⚠️ mock（占位/构造数据）**永不出现**——与 session_kind_counts()
            # 口径一致，否则"显示全部"会漏出 mock 而计数里又没有它。
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            show_all = qs.get("all", ["0"])[0] in ("1", "true")
            sessions = all_sessions(
                include=("test", "diag") if show_all else None,
                kind=None if show_all else "test")
            return self._json({"sessions": sessions,
                               "stats": session_stats_all(),
                               "kinds": session_kind_counts_all()})

        if path == "/api/vision":
            conf = _load_vision()
            conf["api_key_masked"] = _mask(conf.pop("api_key"))
            return self._json(conf)

        if path == "/api/modules":
            # 模块分组从文件算（和用例库同源）
            return self._json({"modules": _module_stats()})

        if path == "/api/dashboard":
            # overview/modules 从文件算；recent/sessions 查库
            return self._json({"overview": _case_overview(),
                               "modules": _module_stats(),
                               "recent": run_stats_all(3),
                               "sessions": session_stats_all()})

        if path == "/api/cases":
            # 用例库 = `assets/cases/` 下的文件（文件才是用例本身，
            # 数据库只是"跑过的痕迹"）
            return self._json({"items": _scan_case_files()})

        m = re.match(r"^/api/cases/([^/]+)$", path)
        if m:
            # id 现在是标题（文件名 stem），不再是数据库自增 id。
            # 先在文件里找；找不到再退回数据库（兼容旧的数字 id 链接）。
            key = urllib.parse.unquote(m.group(1))
            for item in _scan_case_files():
                if str(item["id"]) == key or item["title"] == key:
                    return self._json(_case_detail(item))
            if key.isdigit():
                c = get_db().get_case(int(key))
                if c:
                    return self._json(c)
            return self._json({"error": "not found"}, 404)

        # session id 现在是 `<uuid>:<local_id>`（也接受旧的纯数字）
        m = re.match(r"^/api/sessions/([^/]+)/replay$", path)
        if m:
            return self._replay(urllib.parse.unquote(m.group(1)))

        m = re.match(r"^/api/sessions/([^/]+)$", path)
        if m:
            s = session_detail(urllib.parse.unquote(m.group(1)))
            return self._json(s if s else {"error": "not found"},
                              200 if s else 404)

        if path == "/api/knowledge":
            items = []
            if KNOWLEDGE_DIR.is_dir():
                for fp in sorted(KNOWLEDGE_DIR.glob("*.md")):
                    st = fp.stat()
                    items.append({"name": fp.name, "size": st.st_size,
                                  "mtime": datetime.fromtimestamp(st.st_mtime)
                                  .isoformat(timespec="seconds")})
            return self._json({"items": items})

        m = re.match(r"^/api/knowledge/(.+\.md)$", path)
        if m:
            name = urllib.parse.unquote(m.group(1))
            if not MD_NAME_RE.match(name):
                return self._json({"error": "bad name"}, 400)
            fp = KNOWLEDGE_DIR / name
            if not fp.is_file():
                return self._json({"error": "not found"}, 404)
            return self._json({"name": name, "content": fp.read_text(encoding="utf-8")})

        # ── 用例程序化描述（assets/cases/<包名>/*_steps.yaml）──
        if path == "/api/case-specs":
            items = []
            for fp in sorted(CASES_DIR.rglob("*.yaml")):
                st = fp.stat()
                title = fp.stem
                cid = None
                try:
                    import yaml as _y
                    cid = ((_y.safe_load(fp.read_text(encoding="utf-8")) or {})
                           .get("meta") or {}).get("case_id")
                except Exception:  # noqa: BLE001
                    pass
                md = fp.with_suffix(".md")
                # `xxx_steps.yaml` 对应的原文是 `xxx.md`，不是 `xxx_steps.md`
                if fp.stem.endswith("_steps"):
                    md = fp.with_name(fp.stem[: -len("_steps")] + ".md")
                # 没有原文的 `_steps.yaml` 是**孤立的导产物**（用例原文已删掉），
                # 列出来点进去是空白页，直接不列。
                if not md.is_file():
                    continue
                items.append({
                    "name": fp.name, "title": title, "case_id": cid,
                    "size": st.st_size,
                    "has_md": True,
                    "mtime": datetime.fromtimestamp(st.st_mtime)
                    .isoformat(timespec="seconds"),
                })
            return self._json({"items": items})

        # 渲染后的 MD（只读视图）
        m = re.match(r"^/api/case-specs/(.+)/rendered$", path)
        if m:
            title = urllib.parse.unquote(m.group(1))
            if not SPEC_NAME_RE.match(title):
                return self._json({"error": "bad name"}, 400)
            md = _find_spec(title, ".md") or (CASES_DIR / f"{title}.md")
            if not md.is_file():
                return self._json({"error": "not rendered yet"}, 404)
            return self._json({"name": md.name, "content": md.read_text(encoding="utf-8")})

        # YAML 源（可编辑）
        m = re.match(r"^/api/case-specs/(.+)$", path)
        if m:
            title = urllib.parse.unquote(m.group(1))
            if not SPEC_NAME_RE.match(title):
                return self._json({"error": "bad name"}, 400)
            fp = _find_spec(title, ".yaml") or (CASES_DIR / f"{title}.yaml")
            if not fp.is_file():
                return self._json({"error": "not found"}, 404)
            return self._json({"name": fp.name,
                               "content": fp.read_text(encoding="utf-8")})

        return self._json({"error": "not found"}, 404)

    # ── POST ────────────────────────────────────────────────────
    def do_POST(self):  # noqa: N802
        path = urllib.parse.urlparse(self.path).path

        if path == "/api/vision":
            length = int(self.headers.get("Content-Length") or 0)
            try:
                data = json.loads(self.rfile.read(length).decode("utf-8"))
            except ValueError:
                return self._json({"error": "bad body"}, 400)
            conf = _load_vision()
            for k in VISION_KEYS:
                v = str(data.get(k) or "").strip()
                # api_key 留空 = 保留原值（避免只想改 model 却清掉密钥）
                if v or not (k == "api_key" and conf["api_key"]):
                    conf[k] = v
            _save_vision(conf)
            return self._json({"ok": True})

        if path == "/api/vision/test":
            return self._json(_test_vision())

        m = re.match(r"^/api/cases/(\d+)/kind$", path)
        if m:
            length = int(self.headers.get("Content-Length") or 0)
            try:
                data = json.loads(self.rfile.read(length).decode("utf-8"))
            except ValueError:
                return self._json({"error": "bad body"}, 400)
            ok = get_db().set_case_kind(int(m.group(1)), str(data.get("kind") or ""))
            return self._json({"ok": ok}, 200 if ok else 400)

        # session id 可能是 `<run_id>:<local_id>` 复合串（含冒号），
        # 路由要按 `([^/]+)` 匹配，`(\d+)` 会 404。
        m = re.match(r"^/api/sessions/([^/]+)/kind$", path)
        if m:
            _rid, sid = _split_sid(urllib.parse.unquote(m.group(1)))
            _db = get_db()
            length = int(self.headers.get("Content-Length") or 0)
            try:
                data = json.loads(self.rfile.read(length).decode("utf-8"))
            except ValueError:
                return self._json({"error": "bad body"}, 400)
            ok = _db.set_session_kind(sid, str(data.get("kind") or ""))
            return self._json({"ok": ok}, 200 if ok else 400)

        if path == "/api/cases":
            length = int(self.headers.get("Content-Length") or 0)
            try:
                data = json.loads(self.rfile.read(length).decode("utf-8"))
            except ValueError:
                return self._json({"error": "bad body"}, 400)
            title = str(data.get("title") or "").strip()
            inp = str(data.get("input") or "").strip()
            if not (title and inp):
                return self._json({"ok": False,
                                   "error": "标题与用例原文均必填"}, 400)
            cid = get_db().add_case(title, inp,
                                    str(data.get("package") or "").strip() or None)
            return self._json({"ok": True, "case_id": cid})

        # ── 用例原文可编辑 ────────────────────────────────────
        #
        # 写的是 `assets/cases/<应用>/<stem>.md` —— 用例的**唯一真相是文件**，
        # 数据库只是"跑过的痕迹"。所以这里不写 sessions.user_input（那是历史
        # 快照，改了等于篡改记录），只写文件；若 `cases` 表里恰有同名记录，
        # 顺手同步它的 input，避免「文件改了、库还是旧的」两处不一致。
        #
        # ⚠️ 程序化描述（`_steps.yaml`）仍**只读**：它由"已验证成功的会话"沉淀，
        #    手工改动会破坏它与证据/知识卡的对应关系。改原文后重跑再沉淀即可。
        m = re.match(r"^/api/cases/([^/]+)/content$", path)
        if m:
            stem = urllib.parse.unquote(m.group(1))
            if not SPEC_NAME_RE.match(stem):
                return self._json({"error": "bad name"}, 400)
            fp = _find_spec(stem, ".md")
            if fp is None:
                # 只有 `_steps.yaml` 没有 md 的孤立导产物 —— 没有可编辑的原文
                return self._json({
                    "error": "这条用例没有 .md 原文文件，无法编辑。"
                             "（只有程序化描述 YAML 的用例不能在此改）",
                }, 404)
            length = int(self.headers.get("Content-Length") or 0)
            try:
                data = json.loads(self.rfile.read(length).decode("utf-8"))
                content = data["content"]
                if not isinstance(content, str):
                    raise ValueError
            except (ValueError, KeyError):
                return self._json({"error": "bad body"}, 400)
            if not content.strip():
                return self._json({"error": "用例原文不能为空"}, 400)
            try:
                # 全库 70 个用例 md 都是 CRLF、无 BOM —— 按同一格式落盘，
                # 否则每次编辑都会在 git 里变成整文件重写。
                fp.write_text(_normalize_crlf(content), encoding="utf-8",
                              newline="")
            except OSError as e:
                return self._json({"error": f"写入失败：{e}"}, 500)
            # 同名库记录同步（有才同步，没有就只动文件）
            synced = False
            db = _open_db()
            if db is not None:
                try:
                    for c in db.list_cases(limit=100000, include_mock=True):
                        if str(c.get("title") or "") == stem:
                            synced = bool(db.update_case(int(c["id"]),
                                                         user_input=content))
                            break
                except Exception:  # noqa: BLE001
                    synced = False
            st = fp.stat()
            return self._json({
                "ok": True,
                "path": str(fp.relative_to(ROOT)).replace("\\", "/"),
                "bytes": st.st_size,
                "mtime": datetime.fromtimestamp(st.st_mtime)
                .isoformat(timespec="seconds"),
                "db_synced": synced,
            })

        m = re.match(r"^/api/cases/(\d+)$", path)
        if m:
            # 数据库自增 id 的旧链接：用例**原文**请用
            # POST /api/cases/<stem>/content（按文件名定位文件）。
            # 这里保留拒绝是有意的 —— 数字 id 在文件库里没有对应文件，
            # 猜一个改掉就是改错用例。
            return self._json({
                "error": "请按文件名编辑（POST /api/cases/<用例文件名>/content），"
                         "不要用数据库自增 id。",
                "readonly": True,
            }, 403)

        m = re.match(r"^/api/case-specs/(.+)$", path)
        if m:
            title = urllib.parse.unquote(m.group(1))
            if not SPEC_NAME_RE.match(title):
                return self._json({"error": "bad name"}, 400)
            # 用例程序化描述在本阶段**只读**：YAML 由"已验证成功的会话"沉淀生成，
            # 手工改动会破坏它与证据/知识卡的对应关系。
            # 前端不提供编辑入口；服务端这里也一并拒绝，避免绕过前端直接改。
            return self._json({
                "error": "用例程序化描述为只读，不支持通过 WebUI 修改。"
                         "如需调整请直接编辑文件并重新渲染。",
                "readonly": True,
            }, 403)

        m = re.match(r"^/api/knowledge/(.+\.md)$", path)
        if not m:
            return self._json({"error": "not found"}, 404)
        name = urllib.parse.unquote(m.group(1))
        if not MD_NAME_RE.match(name):
            return self._json({"error": "bad name"}, 400)
        length = int(self.headers.get("Content-Length") or 0)
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
            content = data["content"]
            if not isinstance(content, str):
                raise ValueError
        except (ValueError, KeyError):
            return self._json({"error": "bad body"}, 400)
        KNOWLEDGE_DIR.mkdir(exist_ok=True)
        (KNOWLEDGE_DIR / name).write_text(content, encoding="utf-8")
        return self._json({"ok": True})

    def do_DELETE(self):  # noqa: N802
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        path = urllib.parse.urlparse(self.path).path
        m = re.match(r"^/api/cases/(\d+)$", path)
        if m:
            # 级联删除：用例 + 全部历史测试记录（数据库与磁盘证据）+ 程序化描述文件。
            # 不可恢复，前端已做二次确认。
            return self._json(get_db().delete_case_cascade(int(m.group(1))))
        m = re.match(r"^/api/sessions/([^/]+)$", path)
        if m:
            # 默认连带删除磁盘证据；?evidence=0 只删库记录
            keep_ev = qs.get("evidence", ["1"])[0] in ("0", "false")
            _rid, sid = _split_sid(urllib.parse.unquote(m.group(1)))
            _db = get_db()
            r = _db.delete_session(sid, remove_evidence=not keep_ev)
            return self._json(r, 200 if r.get("ok") else 404)
        return self._json({"error": "not found"}, 404)

    def log_message(self, *a):  # 静默访问日志
        pass


def main():
    ap = argparse.ArgumentParser(description="Android AI 测试台")
    ap.add_argument("--port", type=int, default=8900)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print("⚠️ 警告：绑定非回环地址——Web UI 无鉴权，知识库 POST 接口可被"
              "同网段任意改写，仅限可信内网短时使用！")
    print(f"测试台: http://{args.host}:{args.port}")
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
