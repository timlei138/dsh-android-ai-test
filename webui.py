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
    """文件名 -> raw_cases 表里的 case_name（去掉 .yaml）。

    和 runs.case_name 保持一致 —— 那里存的就是 YAML 文件名去掉扩展名，
    两边不一致的话"这条用例的原始需求"就查不出来了。
    """
    return Path(name).stem


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


def _purge_evidence(paths: list[str]) -> dict:
    """删掉一次运行留下的所有证据文件，返回统计。

    ## 为什么删之前要这么小心

    路径来自数据库。库是可信来源，但这个服务会被手动改库调试（改坏过），
    所以**不假设库里一定是干净的**。规则：

      1. 只删**证据根目录内部**的文件（tmp/shots、tmp/evidence、
         storage/evidence），以及这几类明确的产物（log/llm/ledger/report）。
      2. 删完如果 stepNN 目录空了，顺手 rmdir —— 不删整个 shots 根。
      3. 有任何一个文件不满足规则就**跳过它**并记下来，不报错中断 ——
         删不掉一个文件，不该让整次删除失败。

    ## 为什么不直接 `rmtree(evidence_dir)`

    因为一次运行的截图是**按 stepNN 分目录**共存于同一个 evidence_dir 下的。
    176 和 177 的图都在 tmp/shots/ 里。rmtree 一下就把别人的记录也删了。
    """
    roots = [p.resolve() for p in _evidence_roots()]
    # 产物文件（log/llm/ledger/report）不在证据根里，单独放行 —— 但只放行
    # 我们自己的 tmp/ 下的这些后缀，别的一律不碰。
    tmp_root = (_HERE / "tmp").resolve()
    ok_suffix = {".png", ".xml", ".log", ".md", ".jsonl", ".json", ".txt"}

    done, skipped = [], []
    parents: set[Path] = set()
    for raw in paths:
        if not raw:
            continue
        p = Path(raw)
        if not p.is_absolute():
            p = _HERE / p
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

    # 空的 stepNN 目录顺手清掉；只清空目录，非空/根目录不动
    for d in sorted(parents):
        try:
            if d.is_dir() and not any(d.iterdir()):
                d.rmdir()
        except OSError:
            pass

    return {"files": len(done), "skipped": skipped}


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
            return self._json(run) if run else self._err("没有这次运行", 404)

        if path == "/api/compare":
            try:
                a = int((qs.get("a") or [""])[0])
                b = int((qs.get("b") or [""])[0])
            except ValueError:
                return self._err("需要 a 和 b 两个 run_id")
            return self._json(st.compare_runs(a, b))

        # --- 用例文件 ---
        #
        # 和旧 skill 一样：列出 cases 目录，点进去看 YAML 原文。
        # 我们比旧 skill 多一层 —— 运行记录会带 case_yaml 快照，
        # 所以详情页能显示"跑的时候那份"，而不只是"现在这份"。
        if path == "/api/cases":
            items = _case_files()
            # 标上"有没有录原始用例"，列表里才好一眼看出来 / 提供录入入口。
            # 逐条查库：条数是个位数，不值得为它搞批量接口。
            for it in items:
                it["has_raw"] = st.get_raw_case(_case_key(it["name"])) is not None
            return self._json({"items": items, "dir": str(_cases_dir())})

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

        # 原始用例（用户写的「测试步骤 + 预期结果」原文）
        #
        # ⚠️ 和 /api/case/<name> 是**两样东西**，别混：
        #   /api/case/         YAML 原文（判据、landing、pitfalls —— 改写过的执行结构）
        #   /api/rawcase/      用户当初写的需求原话（"打开日历，点更多→课程表"）
        # 前端两个都显示，因为用户要对着自己的原话看跑得对不对。
        if path.startswith("/api/rawcase/"):
            key = _case_key(path[len("/api/rawcase/"):])
            rec = st.get_raw_case(key)
            if not rec:
                # 200 + found:false，不是 404 —— "这条用例没录原文"是正常状态，
                # 前端要据此显示"还没录"，而不是弹个错误。
                return self._json({"case_name": key, "found": False,
                                   "raw_text": "", "updated_at": ""})
            return self._json({"case_name": key, "found": True, **rec})

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

        # 保存原始用例（用户写的需求原话）
        #
        # 为什么要留这个写入口：这条东西**只能人来录**（它是"用户当初
        # 说了什么"，任何自动推导都只是猜）。不录的话前端就只能显示
        # YAML 里改写过的 desc，那正是这次要避免的。
        if path.startswith("/api/rawcase/"):
            key = _case_key(path[len("/api/rawcase/"):])
            if not key:
                return self._err("要带上用例名")
            data = self._body()
            if data is None:
                return self._err("请求体不是合法 JSON")
            text = str(data.get("raw_text") or "")
            if not text.strip():
                return self._err("原始用例不能为空")
            try:
                created = st.save_raw_case(key, text)
            except Exception as e:  # noqa: BLE001
                return self._err(f"存不进去：{e}", 500)
            rec = st.get_raw_case(key) or {}
            return self._json({"ok": True, "case_name": key,
                               "created": created,
                               "updated_at": rec.get("updated_at") or ""})

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
            ev = _purge_evidence(res.get("paths") or []) if with_ev else {
                "files": 0, "skipped": []}
            return self._json({"ok": True, "run_id": rid,
                               "case_name": res.get("case_name") or "",
                               "evidence": ev,
                               "deleted_files": ev["files"],
                               "skipped": ev["skipped"]})

        # --- 删一条原始用例（单独删，保留 YAML）---
        if path.startswith("/api/rawcase/"):
            key = _case_key(path[len("/api/rawcase/"):])
            n = st.delete_raw_case(key)
            return self._json({"ok": True, "deleted": n, "case_name": key})

        # --- 删用例（YAML 文件 + 原始需求）---
        if path.startswith("/api/case/"):
            name = path[len("/api/case/"):]
            fp = _find_case_file(name)
            if not fp:
                return self._err(f"没有这个用例文件：{name}", 404)
            # 校验文件名没被编码绕过（_find_case_file 已经限定在 cases 目录内，
            # 这里再确认一次它不是个目录、也不是软链出去的）
            if not fp.is_file():
                return self._err("不是文件", 400)
            try:
                fp.unlink()
            except OSError as e:
                return self._err(f"删不掉：{e.strerror or e}", 500)

            key = _case_key(name)
            raw_n = 0
            if str(qs.get("raw", ["1"])[0]) not in ("0", "false"):
                raw_n = st.delete_raw_case(key)

            # 历史记录保留（见上面 docstring）。但要告诉前端还有几条，
            # 免得用户以为删干净了。
            left = len(st.list_runs(case_name=key, limit=1000))
            return self._json({"ok": True, "case_name": key,
                               "deleted_file": fp.name,
                               "raw_deleted": raw_n,
                               "runs_kept": left})

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
