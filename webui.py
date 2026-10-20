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
import json
import os
import sys
import urllib.parse
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

from android_agent.reporting import store as st  # noqa: E402

STATIC = _HERE / "webui"

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

    db = st.db_path()
    print(f"数据库: {db}")
    if not db.exists():
        print("  ⚠️ 还没有任何运行记录（先跑一个用例："
              "echo '{\"case_name\":\"168\"}' | python run_case_json.py）")

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}/"
    print(f"打开: {url}")
    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n停止。")
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
