# -*- coding: utf-8 -*-
"""本地 WebUI：看用例执行结果、账本、截图证据、自动化率。

## 为什么自己写，而不是把旧 skill 的 webui.py 搬过来

看过那套代码（webui.py 20KB + 前端 1.1MB），它的 API 全部架在它自己的
5 张表上（sessions/events/findings/cases/modules）。搬过来会有三张永远空的表，
而我们真正的字段（ms_click、shot_before、changed）得挤进 data_json 里，查询就废了。

所以我们只借它的**形状**（左侧导航 + 时间轴 + 证据灯箱），数据层用
`android_agent.reporting.store`（贴合我们真实账本字段）。

## 只用标准库

`http.server` 起服务，零依赖。这个工具是给人排查用的，
不该为了看一眼结果去装一套 web 框架。

## 启动

    python webui.py            # 默认 127.0.0.1:8770，自动开浏览器
    python webui.py --port 9000 --no-browser
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_SRC = _HERE / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except AttributeError:
    pass

# 库的位置必须和 run_case_json.py 写库时完全一致，否则会出现
# "跑完了但页面是空的"。这个 bug 真的发生过：启动脚本把
# ANDROID_SKILL_ROOT 设成了仓库根，库就落到 storage/runs.db，
# 而写库那边用的是 assets/storage/runs.db。
#
# 兜底规则：没设就补成 assets；设了但明显指错（仓库根而不是 assets）
# 也纠正过来，并说一句 —— 静默用错路径比报错更难查。
_REPO = _HERE
if not os.environ.get("ANDROID_SKILL_ROOT"):
    os.environ["ANDROID_SKILL_ROOT"] = str(_REPO / "assets")
else:
    _root = Path(os.environ["ANDROID_SKILL_ROOT"])
    if not (_root / "storage").is_dir() and (_root / "assets" / "storage").is_dir():
        os.environ["ANDROID_SKILL_ROOT"] = str(_root / "assets")
        print(f"⚠️ ANDROID_SKILL_ROOT 指向了仓库根（{_root}），"
              f"已自动改用 {_root / 'assets'}", file=sys.stderr)

from android_agent.reporting import store as st  # noqa: E402
from android_agent.cases.paths import cases_dir as _cases_dir  # noqa: E402
from android_agent.cases.paths import raw_case_dirs  # noqa: E402
from android_agent.cases import importer  # noqa: E402

STATIC = _HERE / "webui"


# ── 视觉模型配置 ──────────────────────────────────────────────────────
#
# 和 decide.py / vision.py 读的是**同一个文件**（storage/vision.json）。
# 不改配置来源，只加一个网页界面 —— 两边各存一份迟早对不上。
#
# ⚠️ api_key 绝不明文回显。GET 只给脱敏值，改的时候留空表示"不动密钥"。
VISION_KEYS = ("base_url", "model", "api_key")


def _vision_file() -> Path:
    root = os.environ.get("ANDROID_SKILL_ROOT")
    base = Path(root) if root else (_HERE / "assets")
    return base / "storage" / "vision.json"


def _mask(s: str) -> str:
    """只留首 4 末 4。短到分不出来就整串打码。"""
    if not s:
        return ""
    if len(s) <= 8:
        return "*" * len(s)
    return s[:4] + "*" * (len(s) - 8) + s[-4:]


def _load_vision() -> dict[str, str]:
    conf = {k: "" for k in VISION_KEYS}
    fp = _vision_file()
    try:
        data = json.loads(fp.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            for k in VISION_KEYS:
                conf[k] = str(data.get(k) or "")
    except (OSError, ValueError):
        pass          # 没有配置文件是正常状态（还没配过）
    return conf


def _save_vision(conf: dict[str, str]) -> None:
    fp = _vision_file()
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_text(json.dumps(conf, ensure_ascii=False, indent=2),
                  encoding="utf-8")


# ── 用例文件 ──────────────────────────────────────────────────────────

def _case_files() -> list[dict]:
    """assets/storage/cases/*.yaml 的清单。

    ⚠️ 故意**不解析**每个文件来取标题：解析要跑 YAML + pydantic 校验，
    10 个文件还行，以后多了就慢。文件名本身就带用例名和编号。
    case_id 从文件名前缀抠（"联想日历_168_图库导入入口" -> "168"）。
    """
    out = []
    d = Path(_cases_dir())
    if not d.is_dir():
        return out
    for fp in sorted(d.glob("*.yaml")):
        name = fp.stem
        cid = ""
        for seg in name.split("_"):
            if seg.isdigit():
                cid = seg
                break
        out.append({
            "name": fp.name,
            "title": name,
            "case_id": cid,
            "size": fp.stat().st_size,
            "mtime": _fmt_mtime(fp.stat().st_mtime),
            "valid": "invalid" not in name.lower(),
        })
    return out


def _fmt_mtime(ts: float) -> str:
    import datetime
    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def _find_case_file(name: str) -> Path | None:
    """按文件名找用例。**只允许 cases 目录内**，防路径穿越。"""
    root = Path(_cases_dir()).resolve()
    cand = (root / name).resolve()
    if not str(cand).startswith(str(root)) or not cand.is_file():
        return None
    return cand


def _case_key(name: str) -> str:
    """文件名 -> 用例编号（"167"）。见 store.case_number 的说明。"""
    return st.case_number(name)


def _run_stats_by_case() -> dict[str, dict]:
    """编号 -> 跑过几次 / 最近一次结论。

    ★ runs.case_name 存的就是**编号**（'167'），不是 YAML 文件名。
      别拿 YAML 的 stem 去对 —— 那必然对不上，而且不报错，
      表现为"跑过 16 次"的用例在列表里显示"未跑过"。
    """
    out: dict[str, dict] = {}
    for row in st.stats().get("cases", []):
        out[str(row["case_name"])] = row
    return out


def _cases_payload() -> dict:
    """用例库列表：库里全部用例 + 关联状态 + 跑过几次。"""
    rows = st.list_cases()
    by_case = _run_stats_by_case()
    cdir = Path(_cases_dir())

    items = []
    for c in rows:
        yname = c.get("yaml_name") or ""
        run = by_case.get(str(c["case_key"]), {})
        ypath = cdir / yname if yname else None
        items.append({
            "case_key": c["case_key"],
            "title": c.get("title") or "",
            "priority": c.get("priority") or "",
            "raw_chars": len(c.get("raw_text") or ""),
            "yaml_name": yname,
            "yaml_exists": bool(ypath and ypath.is_file()),
            "yaml_size": (ypath.stat().st_size if ypath and ypath.is_file() else 0),
            "runs": run.get("n") or 0,
            "last_status": run.get("last_status") or "",
            "last_at": run.get("last_at") or "",
            "source_path": c.get("source_path") or "",
            "updated_at": c.get("updated_at") or "",
        })

    # 目录里有、但库里没关联上的 YAML（比如 _test_invalid 那种夹具，
    # 或者编号没对上的）。**列出来而不是藏起来** —— 否则页面上
    # "看不到这个文件"和"这个文件不存在"分不清。
    known = {i["yaml_name"] for i in items if i["yaml_name"]}
    orphans = []
    if cdir.is_dir():
        for f in sorted(cdir.glob("*.yaml")):
            if f.name in known:
                continue
            orphans.append({"name": f.name,
                            "size": f.stat().st_size,
                            "mtime": _fmt_mtime(f.stat().st_mtime)})

    return {
        "items": items,
        "dir": str(cdir),
        "raw_dirs": [str(p) for p in raw_case_dirs()],
        "orphan_yaml": orphans,
        "total": len(items),
        "with_yaml": sum(1 for i in items if i["yaml_exists"]),
    }


def _case_detail(key: str) -> dict | None:
    """单条用例：原文 + 关联的 YAML 全文 + 这条用例的历史运行。"""
    c = st.get_case(key)
    if not c:
        return None
    cdir = Path(_cases_dir())
    yname = c.get("yaml_name") or ""
    ypath = cdir / yname if yname else None
    yaml = {"name": "", "path": "", "content": "", "size": 0, "exists": False}
    if ypath and ypath.is_file():
        try:
            content = ypath.read_text(encoding="utf-8")
        except OSError as e:
            content = f"(读不出来：{e})"
        yaml = {"name": ypath.name, "path": str(ypath), "content": content,
                "size": ypath.stat().st_size, "exists": True}

    runs = st.list_runs(case_name=str(c["case_key"])) if c["case_key"] else []
    return {
        "case_key": c["case_key"],
        "title": c.get("title") or "",
        "priority": c.get("priority") or "",
        "raw_text": c.get("raw_text") or "",
        "source_path": c.get("source_path") or "",
        "updated_at": c.get("updated_at") or "",
        "yaml": yaml,
        "runs": runs,
    }


def _safe_under(path: Path, roots: list[Path]) -> bool:
    """path 解析后是否真的落在 roots 之一的**内部**。

    ⚠️ 只判 startswith 是不够的：`/a/bc` 也 startswith `/a/b`。
       要么比到分隔符，要么用 relative_to。这里用后者 —— 它天然认边界。
    """
    try:
        rp = path.resolve()
    except OSError:
        return False
    for r in roots:
        try:
            rp.relative_to(r.resolve())
            return rp != r.resolve()   # 根目录本身不能删
        except (ValueError, OSError):
            continue
    return False


def _run_dirs_of(paths: list[str], roots: list[Path]) -> dict[Path, int]:
    """从证据路径里认出"属于单次运行的整目录"，返回 {目录: 里面有几个文件}。

    认的是 `…/shots/run_YYYYmmdd_HHMMSS/` 这一层（evidence 同名）。

    ## 为什么要有这条

    一次运行除了 shot.png / dump.xml，还会写 meta.json、ocrNN 之类的
    中间文件 —— 这些**不在 actions 表里**，只删"记录引用过的文件"
    会让目录剩一堆孤儿文件（实测：删完还剩 18 个）。而按运行隔离的目录
    是可以整个丢的。

    ## 为什么只认 run_ 前缀

    老的 evidence 布局是 tmp/shots/stepNN/ —— **各次运行共用同名目录**，
    176 和 177 的图都在 tmp/shots/step04/ 里。那种目录 rmtree 一下就会
    删掉别人的证据。所以判据是**目录名**（只有新布局带 run_ 前缀），
    不是"它在证据根内"。
    """
    out: dict[Path, int] = {}
    for raw in paths:
        if not raw:
            continue
        p = Path(raw)
        if not p.is_absolute():
            p = _HERE / p
        for anc in list(p.parents)[:3]:
            if not anc.name.startswith("run_"):
                continue
            if anc.parent.name not in ("shots", "evidence"):
                break
            if not _safe_under(anc, roots):
                break
            if anc.is_dir():
                out[anc] = sum(1 for f in anc.rglob("*") if f.is_file())
            break
    return out


def _purge_evidence(paths: list[str]) -> dict:
    """删掉一次运行留下的证据文件，返回统计。

    ## 顺序很重要

      1. 先认出"按运行隔离的整目录"（run_xxx/）—— 这些整个丢，最彻底
      2. 再逐个删剩下的文件（老布局的 stepNN/xxx.png、以及 tmp/ 下的
         log / ledger / report 等产物）
      3. 最后把清空的 stepNN 目录 rmdir，但不动证据根本身

    ## 为什么删之前要这么小心

    路径来自数据库。库是可信来源，但这服务会被手动改库调试（改坏过），
    所以**不假设库里一定是干净的**。规则：
      · 只删证据根内部的，或 tmp 下明确后缀的产物
      · 边界用 relative_to 判，不用 startswith（/a/bc 也 startswith /a/b）
      · 任何一个不满足规则就**跳过并记下**，不报错中断 ——
        删不掉一个文件不该让整次删除失败
    """
    import shutil as _sh

    roots = [p.resolve() for p in _evidence_roots()]
    tmp_root = (_HERE / "tmp").resolve()
    ok_suffix = {".png", ".xml", ".log", ".md", ".jsonl", ".json", ".txt"}

    # ── 1. 整目录（按运行隔离的）──
    run_dirs = _run_dirs_of(paths, roots)
    freed = 0
    purged: list[str] = []
    for d, n_in in run_dirs.items():
        _sh.rmtree(d, ignore_errors=True)
        if not d.exists():
            purged.append(d.name)
            freed += n_in
    # 整目录已经连里面的文件一起删了，逐个删的时候要跳过
    gone = {str(d.resolve()) for d in run_dirs}

    # ── 2. 剩余文件 ──
    done: list[str] = []
    skipped: list[str] = []
    parents: set[Path] = set()
    for raw in paths:
        if not raw:
            continue
        p = Path(raw)
        if not p.is_absolute():
            p = _HERE / p
        try:
            rp = str(p.resolve())
        except OSError:
            skipped.append(f"{raw}（路径解析失败）")
            continue
        if any(rp == g or rp.startswith(g + os.sep) for g in gone):
            continue      # 已经在整目录里删掉了
        allowed = _safe_under(p, roots) or (
            _safe_under(p, [tmp_root]) and p.suffix.lower() in ok_suffix)
        if not allowed:
            skipped.append(f"{raw}（不在证据目录内）")
            continue
        try:
            if p.is_file():
                p.unlink()
                done.append(raw)
                parents.add(p.parent)
        except OSError as e:
            skipped.append(f"{raw}（{e.strerror or e}）")

    # ── 3. 清空的 stepNN 目录顺手删掉（只删空的，不碰根）──
    for d in sorted(parents):
        try:
            if d.is_dir() and not any(d.iterdir()):
                d.rmdir()
        except OSError:
            pass

    return {"files": len(done) + freed, "skipped": skipped, "dirs": purged}

def _test_vision() -> dict:

    """拿 storage/vision.json 发一次**真实**请求，看视觉通道通不通。

    为什么不做成"检查字段填了没"：填了不等于能用 —— key 错了、
    模型名写错、base_url 少个 /v1，都只有真发一次才知道。
    旧 skill 也是这么做的（发一张 1x1 的图）。

    用 1x1 图而不是本机截图：不依赖证据文件，任何机器上都能测。
    """
    conf = _load_vision()
    if not (conf["base_url"] and conf["model"] and conf["api_key"]):
        missing = [k for k in ("base_url", "model", "api_key") if not conf[k]]
        return {"ok": False, "error": f"配置不完整，缺：{', '.join(missing)}"}

    import base64
    import time

    png_1x1 = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
        "YPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")
    b64 = base64.b64encode(png_1x1).decode("ascii")

    payload = {
        "model": conf["model"],
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": "这张图是什么颜色？只回答一个词。"},
            {"type": "image_url",
             "image_url": {"url": f"data:image/png;base64,{b64}"}},
        ]}],
        "max_tokens": 2000,     # 推理模型给太小会空返回，见 vision.py 注释
    }
    url = conf["base_url"].rstrip("/") + "/chat/completions"
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {conf['api_key']}"},
        method="POST")

    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=40) as resp:
            body = json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", errors="replace")[:400]
        except OSError:
            pass
        return {"ok": False, "latency_ms": int((time.time() - t0) * 1000),
                "error": f"HTTP {e.code}", "detail": detail,
                "hint": "401/403 多半是 key 不对；404 检查 base_url 是否要带 /v1"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "latency_ms": int((time.time() - t0) * 1000),
                "error": f"{type(e).__name__}: {e}",
                "hint": "网络不通或 base_url 写错；公司内网可能要代理"}

    ms = int((time.time() - t0) * 1000)
    try:
        text = (body["choices"][0]["message"]["content"] or "").strip()
    except (KeyError, IndexError, TypeError):
        return {"ok": False, "latency_ms": ms,
                "error": "返回结构不认识", "detail": json.dumps(
                    body, ensure_ascii=False)[:400]}

    usage = body.get("usage") or {}
    if not text:
        # ok=true 但没文本 = 额度被 reasoning 吃光。别当成功。
        return {"ok": False, "latency_ms": ms, "model": conf["model"],
                "usage": usage,
                "error": "模型返回空文本（通常是 max_tokens 被 reasoning 吃光）",
                "hint": f"usage: {usage}"}
    return {"ok": True, "latency_ms": ms, "model": conf["model"],
            "reply": text[:200], "usage": usage}

_MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".png": "image/png",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
}


class Handler(BaseHTTPRequestHandler):
    server_version = "AndroidTestUI/1.0"

    def log_message(self, fmt, *args):  # noqa: A003
        # 默认会把每个请求打到 stderr，刷屏且没价值 —— 静音。
        pass

    # -- 工具 ---------------------------------------------------------------
    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _err(self, msg: str, code: int = 400) -> None:
        self._json({"error": msg}, code)

    # -- GET ----------------------------------------------------------------
    def do_HEAD(self):  # noqa: N802
        """只回应"这个文件在不在"，不返回正文。

        前端用它决定要不要显示"模型对话"链接 —— 没开 LLM_TRACE 时
        那个文件根本不存在，给一个点开就 404 的链接不如不给。
        """
        path = urllib.parse.unquote(urllib.parse.urlparse(self.path).path)
        ok = False
        if path.startswith("/api/file/"):
            parts = path.split("/")
            if len(parts) == 5:
                run = st.get_run(int(parts[3])) if parts[3].isdigit() else None
                key = {"log": "log_path", "llm": "llm_path",
                       "ledger": "ledger_path"}.get(parts[4])
                if run and key and run.get(key):
                    fp = Path(run[key])
                    if not fp.is_absolute():
                        fp = _HERE / fp
                    ok = fp.exists()
        else:
            rel = path.lstrip("/") or "index.html"
            ok = (STATIC / rel).is_file()
        self.send_response(200 if ok else 404)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def do_GET(self):  # noqa: N802
        u = urllib.parse.urlparse(self.path)
        path = urllib.parse.unquote(u.path)
        qs = urllib.parse.parse_qs(u.query)

        # --- API ---
        if path == "/api/stats":
            return self._json(st.stats())

        if path == "/api/runs":
            return self._json(st.list_runs(
                case_name=(qs.get("case") or [""])[0],
                limit=int((qs.get("limit") or ["100"])[0])))

        if path.startswith("/api/run/"):
            try:
                rid = int(path.rsplit("/", 1)[-1])
            except ValueError:
                return self._err("run_id 不是数字")
            run = st.get_run(rid)
            if not run:
                return self._err("没有这次运行", 404)
            # ★ 用例可能已经被删掉了（用户允许"删用例但保留记录"）。
            #   记录本身完全自足 —— 判据在 case_yaml 快照里、证据在磁盘上，
            #   都不依赖用例还活着。但页面上那个"看当前版本"的链接会变成死链，
            #   所以先把"还在不在"告诉前端，让它决定是给链接还是给一句话。
            run["case_exists"] = st.get_case(str(run.get("case_name") or "")) is not None
            return self._json(run)

        if path == "/api/compare":
            try:
                a = int((qs.get("a") or [""])[0])
                b = int((qs.get("b") or [""])[0])
            except ValueError:
                return self._err("需要 a 和 b 两个 run_id")
            return self._json(st.compare_runs(a, b))

        # --- 用例文件 ---
        #
        # 用例库：**原始 case 和 YAML 是一体的**（用户要求）。
        #
        # 数据来源变了：以前是"扫 cases 目录下有几个 YAML 就列几个"，
        # 现在是"库里有几条用例就列几条" —— 因为原始 case 已经导入库，
        # 而其中绝大多数（63 里的 56 条）还没有 YAML。
        # 只扫目录的话，那 56 条永远不会出现在页面上。
        if path == "/api/cases":
            return self._json(_cases_payload())

        # 单条用例：原文 + 关联的 YAML 全文 + 跑过几次
        if path.startswith("/api/cases/"):
            key = path[len("/api/cases/"):]
            one = _case_detail(key)
            if not one:
                return self._err(f"没有这个用例：{key}", 404)
            return self._json(one)

        # 单个 YAML 原文（用例详情里已经并进去了，这个留着给外部/测试用）
        if path.startswith("/api/case/"):
            name = path[len("/api/case/"):]
            fp = _find_case_file(name)
            if not fp:
                return self._err(f"没有这个用例文件：{name}", 404)
            try:
                content = fp.read_text(encoding="utf-8")
            except OSError as e:
                return self._err(f"读不出来：{e}", 500)
            return self._json({"name": fp.name, "path": str(fp),
                               "content": content,
                               "size": fp.stat().st_size})

        # --- 视觉模型配置 ---
        if path == "/api/vision":
            conf = _load_vision()
            key = conf.pop("api_key", "")
            conf["api_key_masked"] = _mask(key)
            conf["configured"] = bool(key)
            conf["file"] = str(_vision_file())
            return self._json(conf)

        # 运行日志 / 模型对话：原样回文本，前端直接渲染
        if path.startswith("/api/file/"):
            # /api/file/<run_id>/<log|llm|ledger>
            parts = path.split("/")
            if len(parts) != 5:
                return self._err("路径要 /api/file/<run_id>/<log|llm|ledger>")
            try:
                rid = int(parts[3])
            except ValueError:
                return self._err("run_id 不是数字")
            run = st.get_run(rid)
            if not run:
                return self._err("没有这次运行", 404)
            key = parts[4]
            fpath = {"log": run.get("log_path"),
                     "llm": run.get("llm_path"),
                     "ledger": run.get("ledger_path")}.get(key)
            if not fpath:
                return self._err(f"这次运行没记 {key} 路径", 404)
            p = Path(fpath)
            if not p.is_absolute():
                # 库里可能是相对路径（跑用例时 cwd 不同就会这样），
                # 相对仓库根解析。
                p = _HERE / p
            if not p.exists():
                return self._err(f"文件不存在：{fpath}", 404)
            # 大文件只读尾部 —— 模型对话动辄几百 KB，
            # 整个读进来再塞给浏览器会卡。尾部正是最想看的（最后在干嘛）。
            raw = p.read_bytes()
            limit = 400_000
            truncated = len(raw) > limit
            if truncated:
                raw = raw[-limit:]
            text = raw.decode("utf-8", errors="replace")
            if truncated:
                text = f"……（只显示最后 {limit // 1000} KB）……\n" + text
            return self._send(200, text.encode("utf-8"),
                              "text/plain; charset=utf-8")

        # --- 证据图片：/shot/<run_id>/<seq>/<before|after> ---
        #
        # ## 为什么用 run_id+seq 而不是直接传路径
        #
        # 一开始是 `/evidence/<路径尾巴>`，把 shot.png 的相对路径塞进 URL。
        # 实测立刻出问题：截图会落在两个根下（tmp/shots 和
        # assets/storage/evidence），而两者的尾巴都可能长成 `step05/shot.png` ——
        # 于是拼出来的 URL 分不清该读哪个，只能 403。
        #
        # 改成"按 run_id + 动作序号去库里查真实路径"，
        # 路径就只有一个来源（数据库），不会拼错，也不用在 URL 里
        # 暴露服务器文件结构。
        if path.startswith("/shot/"):
            return self._serve_shot(path[len("/shot/"):])

        # --- 静态 ---
        return self._serve_static(path)

    def _serve_shot(self, rest: str) -> None:
        """按 run_id + 动作序号取截图：/shot/<run_id>/<seq>/<before|after>。

        路径**只从数据库取**，不信 URL 里的任何路径片段 ——
        既不会拼错，也没有路径穿越的口子（能不能读某文件由库里的记录决定）。
        """
        parts = rest.split("/")
        if len(parts) != 3:
            return self._err("用法：/shot/<run_id>/<seq>/<before|after>")
        try:
            rid, seq = int(parts[0]), int(parts[1])
        except ValueError:
            return self._err("run_id / seq 要是数字")
        which = parts[2]
        if which not in ("before", "after"):
            return self._err("最后一段只能是 before 或 after")
        run = st.get_run(rid)
        if not run:
            return self._err("没有这次运行", 404)
        row = next((a for a in run["actions"] if a.get("seq") == seq), None)
        if not row:
            return self._err("这次运行里没有这个动作序号", 404)
        fpath = row.get("shot_before" if which == "before" else "shot_after")
        if not fpath:
            return self._err("这个动作没记截图", 404)
        # 再兜一层：只允许读证据目录里的东西。
        # 库里的路径是执行器写进来的，理论上可信；但这个服务会被人
        # 手动改库调试（改坏过），所以不假设库里一定是干净的。
        roots = [p.resolve() for p in _evidence_roots()]
        p = Path(fpath)
        if not p.is_absolute():
            p = _HERE / p
        p = p.resolve()
        if not any(str(p).startswith(str(r)) for r in roots):
            return self._err("截图不在证据目录内，拒绝读取", 403)
        if not p.is_file():
            return self._err(f"截图文件不在了：{fpath}", 404)
        self._send(200, p.read_bytes(), _MIME.get(p.suffix.lower(),
                                                  "application/octet-stream"))

    def _serve_static(self, path: str) -> None:
        if path in ("/", ""):
            path = "/index.html"
        rel = path.lstrip("/")
        p = (STATIC / rel).resolve()
        if not str(p).startswith(str(STATIC.resolve())) or not p.is_file():
            return self._err("没有这个页面", 404)
        self._send(200, p.read_bytes(),
                   _MIME.get(p.suffix.lower(), "application/octet-stream"))

    # ── POST ──────────────────────────────────────────────────────
    def _body(self) -> dict | None:
        """读请求体并解析成 dict。解析失败返回 None（调用方回 400）。"""
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0:
                return {}
            data = json.loads(self.rfile.read(n).decode("utf-8"))
        except (ValueError, OSError):
            return None
        return data if isinstance(data, dict) else None

    def do_POST(self):  # noqa: N802
        path = urllib.parse.unquote(urllib.parse.urlparse(self.path).path)

        if path == "/api/vision":
            data = self._body()
            if data is None:
                return self._err("请求体不是合法 JSON")
            conf = _load_vision()
            for k in VISION_KEYS:
                v = str(data.get(k) or "").strip()
                # ★ api_key 留空 = 保留原值。
                #   否则"只想改 model"会把密钥清掉，下一个用例直接跑不动，
                #   而且报错会是"LLM 配置不完整"，看不出是这个页面干的。
                if k == "api_key" and not v and conf["api_key"]:
                    continue
                conf[k] = v
            try:
                _save_vision(conf)
            except OSError as e:
                return self._err(f"写不进去：{e}", 500)
            return self._json({"ok": True, "file": str(_vision_file())})

        if path == "/api/vision/test":
            return self._json(_test_vision())

        # 保存原始 case 原文（网页里编辑）
        #
        # 为什么留这个写入口：原文是"用户当初写了什么"，任何自动推导都只是猜。
        # 现在原文归库管了，这里改的就是正本。
        if path.startswith("/api/cases/") and path.endswith("/raw"):
            key = path[len("/api/cases/"):-len("/raw")]
            if not key:
                return self._err("要带上用例编号")
            data = self._body()
            if data is None:
                return self._err("请求体不是合法 JSON")
            text = str(data.get("raw_text") or "")
            if not text.strip():
                return self._err("原文不能为空")
            if not st.get_case(key):
                return self._err(f"没有这个用例：{key}", 404)
            try:
                st.save_case_raw(key, text)
            except Exception as e:  # noqa: BLE001
                return self._err(f"存不进去：{e}", 500)
            rec = st.get_case(key) or {}
            return self._json({"ok": True, "case_key": key,
                               "updated_at": rec.get("updated_at") or ""})

        # 从文件夹重新同步原始 case（可指定 overwrite 覆盖已有原文）
        if path == "/api/cases/import":
            data = self._body() or {}
            ow = bool(data.get("overwrite"))
            try:
                res = importer.import_all(overwrite=ow)
            except Exception as e:  # noqa: BLE001
                return self._err(f"导入失败：{e}", 500)
            return self._json({"ok": True, **res})

        return self._err("没有这个接口", 404)

    # -- DELETE -------------------------------------------------------------
    def do_DELETE(self):  # noqa: N802
        """删记录 / 删用例 / 删原始用例。

        ## 两件事分开删，是刻意的

          DELETE /api/run/<id>?evidence=1   只删"这一次运行"
          DELETE /api/case/<name>?raw=1     只删"用例文件（和它的原始需求）"

        **删用例不连坐历史记录**。旧 skill 是连坐的（删用例顺手把该用例
        全部记录带走），这里不学那条 —— 历史记录里存了 case_yaml 快照，
        是"当时那份判据"的唯一副本；用例文件删了，快照还在，
        那条记录就仍然可复核。连坐删掉等于把证据一起销毁了。

        真要清历史，逐条删记录即可（那是显式的、看得见删了几条）。
        """
        u = urllib.parse.urlparse(self.path)
        path = urllib.parse.unquote(u.path)
        qs = urllib.parse.parse_qs(u.query)

        # --- 删一次运行 ---
        if path.startswith("/api/run/"):
            tail = path[len("/api/run/"):]
            if not tail.isdigit():
                return self._err("run_id 要是数字")
            rid = int(tail)
            if not st.get_run(rid):
                return self._err("没有这次运行", 404)

            # evidence 默认 1（跟着记录一起清）。传 0 表示只删库里的行。
            with_ev = str(qs.get("evidence", ["1"])[0]) not in ("0", "false")
            res = st.delete_run(rid)
            if not res.get("deleted"):
                return self._err("删除失败：记录不在了", 404)

            # ★ 共享文件不能删。
            #   run_167.log / clicks_167.jsonl 是按用例名命名、每次追加的，
            #   同用例的其它记录全都指向同一个文件。删了就等于把那些记录的
            #   执行证据一起销毁 —— 而它们看着还好好的，只是点开日志是空的。
            #   store 已经把这类路径单独挑出来了，这里只删"独享"的那批。
            shared = res.get("shared") or []
            stale = res.get("stale") or []
            ev = _purge_evidence(res.get("paths") or []) if with_ev else {
                "files": 0, "skipped": []}
            if with_ev:
                ev["skipped"].extend(
                    f"{p}（还被其它记录引用，保留）" for p in shared)
                ev["skipped"].extend(
                    f"{p}（已被后续运行覆盖，不是这次的证据）" for p in stale)
            return self._json({"ok": True, "run_id": rid,
                               "case_name": res.get("case_name") or "",
                               "evidence": ev,
                               "deleted_files": ev["files"],
                               "kept_shared": len(shared),
                               "kept_stale": len(stale),
                               "skipped": ev["skipped"]})


        # --- 删一条用例（原始 case + 关联的 YAML，一起走）---
        #
        # ★ 用户要求的就是这个：case 和 YAML 关联起来，删除时"简单点"。
        #   以前是"删 YAML"和"删原始 case"两个接口，删完一半还可能失败；
        #   现在一条用例就是一行 + 一个文件，一次删干净。
        #
        # ★★★ 用户后来补了一条硬要求，这个接口必须守住 ★★★
        #   「删除 case 的时候要确保已经跑过的记录没有影响」
        #
        #   所以这里只做两件事：删 cases 那一行、unlink 那个 YAML 文件。
        #   **绝不**碰 runs / actions / findings，也**绝不**碰任何证据文件
        #   （log / clicks.jsonl / 截图 / dump.xml）。
        #
        #   记录为什么能不受影响 —— 它本来就不依赖这个用例：
        #     · 判据：跑的时候已经把 YAML 全文快照进 runs.case_yaml
        #     · 证据：路径存在 runs/actions 里，文件在 tmp/ 下，与用例无关
        #   下面 runs_kept 报的是"这些记录一条没动"，不是"我帮你保住了"。
        if path.startswith("/api/cases/"):
            key = path[len("/api/cases/"):]
            c = st.get_case(key)
            if not c:
                return self._err(f"没有这个用例：{key}", 404)

            # 删之前先把受影响的记录数记下来。删完再数就是 0 了 ——
            # 那等于"事后编一个数字"，没法用它证明记录没被动过。
            runs_before = st.list_runs(case_name=str(key), limit=1000)
            ids_before = [r["run_id"] for r in runs_before]

            deleted_file = ""
            yname = c.get("yaml_name") or ""
            if yname and str(qs.get("yaml", ["1"])[0]) not in ("0", "false"):
                fp = _find_case_file(yname)
                if fp and fp.is_file():
                    try:
                        fp.unlink()
                        deleted_file = fp.name
                    except OSError as e:
                        # ★ 文件删不掉就**不删库里的行**。
                        #   反过来做的话，页面上用例没了、磁盘上文件还在，
                        #   而库里再没有任何线索指向它 —— 成了孤儿文件。
                        return self._err(
                            f"YAML 删不掉（{e.strerror or e}），"
                            "为免留下孤儿文件，这次没有删任何东西", 500)

            n = st.delete_case(key)

            # ★ 自检：删完之后把同样的记录再查一遍，逐条核对还在、内容没变。
            #   这不是"相信 delete_case 只删了一张表"——是**验**它只删了一张表。
            #   万一哪天有人在 store.delete_case 里加了级联，这里会当场发现。
            after = {r["run_id"]: r for r in st.list_runs(case_name=str(key), limit=1000)}
            lost = [i for i in ids_before if i not in after]
            changed = [i for i in ids_before
                       if i in after and after[i].get("status") != next(
                           r.get("status") for r in runs_before if r["run_id"] == i)]
            return self._json({
                "ok": True, "case_key": key,
                "deleted": n, "deleted_yaml": deleted_file,
                "runs_kept": len(ids_before),
                "runs_lost": lost,
                "runs_changed": changed,
            })

        # --- 只删 YAML、保留用例行（拆掉关联）---
        if path.startswith("/api/case/"):
            name = path[len("/api/case/"):]
            fp = _find_case_file(name)
            if not fp or not fp.is_file():
                return self._err(f"没有这个用例文件：{name}", 404)
            try:
                fp.unlink()
            except OSError as e:
                return self._err(f"删不掉：{e.strerror or e}", 500)
            key = st.case_number(name)
            st.set_case_yaml(key, None)
            return self._json({"ok": True, "case_key": key,
                               "deleted_yaml": fp.name})

        return self._err("没有这个接口", 404)


def _already_running(host: str, port: int) -> bool:
    """这个端口上是不是已经有我们自己的服务在跑。

    为什么需要：用户会连着发好几次"打开 webui"。第二次启动如果直接
    报 "address already in use" 退出，体验就很差 —— 明明已经开着了。
    这里先探一下，是旧实例就直接打开浏览器，不再起第二个。
    """
    import socket
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/api/stats",
                                    timeout=1.5) as r:
            body = r.read(200)
            return b'"total"' in body
    except Exception:  # noqa: BLE001
        # 探不通不代表端口空着 —— 可能是别的程序占着。
        # 用一条 TCP 连接判"有没有人监听"。
        try:
            with socket.create_connection((host, port), timeout=1.0):
                return False   # 有监听，但不是我们的服务
        except OSError:
            return False       # 没人监听，端口是空的


def _evidence_roots() -> list[Path]:
    """截图可能落的地方。见 Handler._serve_shot 的说明。

    三处都见过：
      · tmp/shots            runner.py 显式传的 evidence_dir（当前主路径）
      · tmp/evidence         DirectDevice 不传 evidence_dir 时的默认值
      · storage/evidence     旧 tools 链路
    """
    root = os.environ.get("ANDROID_SKILL_ROOT")
    base = Path(root) if root else (_HERE / "assets")
    return [
        _HERE / "tmp" / "shots",
        _HERE / "tmp" / "evidence",
        base / "storage" / "evidence",
    ]


def main() -> int:
    ap = argparse.ArgumentParser(description="用例执行结果 WebUI")
    ap.add_argument("--port", type=int, default=8770)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    # ★ 启动信息必须立刻可见。
    #
    # 输出被重定向时（启动脚本、管道、任务管理器）python 默认是块缓冲，
    # 那几行提示会一直压在缓冲区里 —— 用户只看到"什么都没打印"，
    # 会误以为服务没起来，然后重复启动。
    def say(msg: str = "") -> None:
        print(msg, flush=True)

    db = st.db_path()
    # 直接说"库里有多少条"比只报路径有用 —— 页面空的时候
    # 一眼就能分清是"没跑过"还是"跑过没落库"。
    try:
        _n = st.stats().get("total", 0)
    except Exception:  # noqa: BLE001
        _n = None
    say(f"数据库: {db}")
    if _n is None:
        say("  ⚠️ 库打不开（文件坏了或没权限）")
    elif not _n:
        say("  库里还没有运行记录。先跑一个用例："
            "  echo '{\"case_name\":\"168\"}' | python run_case_json.py")
    else:
        say(f"  已有 {_n} 条运行记录")

    # 用例库为空时自动从文件夹导一次。
    #
    # ★ 只在**空库**时导，不在每次启动时导：原文导入后归库管，
    #   每次启动都扫一遍会把人改过的内容冲掉。
    #   要重新同步走页面上的「从文件夹同步」或 POST /api/cases/import。
    try:
        if not st.list_cases():
            _imp = importer.import_all()
            say(f"用例库: 已从文件夹导入 {_imp['files']} 条"
                f"（关联 YAML {_imp['linked']} 个）")
            for _d in _imp["scanned_dirs"]:
                say(f"  扫描: {_d}")
        else:
            say(f"用例库: 已有 {len(st.list_cases())} 条")
    except Exception as exc:  # noqa: BLE001
        # 导入失败不该让服务起不来 —— 页面上照样能看到运行记录。
        say(f"  ⚠️ 用例导入失败：{exc}")

    url = f"http://{args.host}:{args.port}/"

    # 已经开着了就不再起第二个 —— 直接把浏览器指过去。
    # 用户会连着发命令，第二次报 "address already in use" 是最糟的回应。
    if _already_running(args.host, args.port):
        say(f"服务已经在跑：{url}")
        if not args.no_browser:
            try:
                webbrowser.open(url)
            except Exception:  # noqa: BLE001
                pass
        return 0

    try:
        srv = ThreadingHTTPServer((args.host, args.port), Handler)
    except OSError as exc:
        say(f"⚠️ 端口 {args.port} 起不来：{exc}")
        say(f"   可能是别的程序占着。换个端口：python webui.py --port 8771")
        return 1
    say(f"打开: {url}")
    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        say("\n停止。")
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
