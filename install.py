#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""第一次安装：一条命令把工具层装进「本会话的工作区」。

    python3 install.py                          # 装到 <cwd>/dsh-ai-auto-test
    python3 install.py --to <目录>              # 指定落点
    python3 install.py --check                  # 只判定不写东西（先看这台机器该装哪）
    python3 install.py --wire                   # 顺手把新层里 3 处 config.target 改成落点
    python3 install.py --reuse-venv <旧 .venv>  # 离线：从旧 venv 拷包，不跑 pip
    python3 install.py -- --skip-device-check   # -- 之后的参数原样转给 toolkit/scripts/setup.sh

为什么是一份 python，而不是 install.sh + install.ps1 两份
--------------------------------------------------------
`安装说明.md` §2.5 ⑧ 那句话原来写的是「不存在两份副本要同步的问题」，
2026-11-09 被打脸了：仓里一份、已装层一份、Windows/Linux 又各一份 shell，
`runtime.py` 就在其中一次漂移里落后了。环境判定（工作区在哪、能不能真写进去）
这件事本来只有一条逻辑，写两遍就必然对不齐 —— 而 install.ps1 这台机器上
根本没有 pwsh，**从来没被执行过**，语法都没验证过。
系统 python3 是前置条件（装依赖要用），所以让它当唯一入口，shell 那两个
退化成两行转发。

四步，每步都是一条能照抄的结论
------------------------------
0  真写探针 + 工作区判定（**不猜 cwd**，判法见 tools/harness.py）
1  铺代码：排除 .venv（跨路径即废）与 storage（那是别人的历史）
2  装依赖：委托 toolkit/scripts/setup.sh|.ps1；或 --reuse-venv 离线拷包
3  自检：tools/doctor.py 当硬闸门，不过就别开始
4  --wire：改新层里的 3 处 target；profile 的 link: 在工作区外，只打印不碰
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
IS_WIN = os.name == "nt"
DEFAULT_NAME = "dsh-ai-auto-test"
DOC_FILES = ("安装说明.md", "README.md", "分享介绍.html", "install.py")
TOOLKIT_SKIP = {".venv", "storage", "__pycache__", ".git", "node_modules",
                "webui", "docs"}          # webui/docs 是源码树里的附属，装机不需要
TOP_DIRS = ("plugins", "skills", "scripts")

if not IS_WIN and sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:      # noqa: BLE001
        pass

# ★ 必须行缓冲：后面要 spawn setup / doctor，它们的输出直写同一个 fd。
#   stdout 是管道时我这边的 print 是全缓冲的，日志会变成"自检结果排在横幅前面"。
try:
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)
except (AttributeError, OSError):
    pass

_USE_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def _c(code: str, s: str) -> str:
    return f"\033[{code}m{s}\033[0m" if _USE_COLOR else s


def say(t: str) -> None:
    print("\n" + _c("36", "▶ " + t))


def ok(t: str) -> None:
    print("  " + _c("32", "✓") + " " + t)


def warn(t: str) -> None:
    print("  " + _c("33", "!") + " " + t)


def die(t: str) -> "NoReturn":                      # type: ignore[valid-type]
    # stdout 重定向到文件时是全缓冲的，不 flush 就会出现「✗ 那行跑到 ▶ 之前」，
    # 事后看日志像少了尾巴。
    sys.stdout.flush()
    print("  " + _c("31", "✗") + " " + t, file=sys.stderr)
    sys.stderr.flush()
    raise SystemExit(1)


def ts_suffix() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


# ── 环境判定 ────────────────────────────────────────────────────────────────
def load_harness(from_root: Path):
    """把 toolkit/tools 挂上 sys.path 拿 harness（纯标准库，装依赖前就能 import）。"""
    tools = from_root / "toolkit" / "tools"
    if not (tools / "harness.py").is_file():
        return None
    sys.path.insert(0, str(tools))
    try:
        import harness                      # noqa: PLC0415
        return harness
    except Exception as exc:                # noqa: BLE001
        warn(f"harness.py 导入失败：{exc} —— 工作区判定降级为「只看真写探针」")
        return None


def probe_writable(p: Path) -> tuple[bool, str]:
    """真写一个文件再删。权限位 / os.access 都不算数 —— 沙箱是内核层拦的。"""
    try:
        p.mkdir(parents=True, exist_ok=True)
        f = p / ".install_probe"
        f.write_text("x", encoding="utf-8")
        f.unlink()
        return True, "可写"
    except OSError as exc:
        return False, f"{type(exc).__name__}: {exc}"


# ── 1 铺代码 ────────────────────────────────────────────────────────────────
def copy_tree(src: Path, dst: Path, skip: set[str]) -> int:
    n = 0
    dst.mkdir(parents=True, exist_ok=True)
    for item in src.iterdir():
        if item.name in skip or item.name.endswith(".pyc"):
            continue
        target = dst / item.name
        if item.is_symlink() or item.is_file():
            shutil.copy2(item, target)
        else:
            shutil.copytree(item, target, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns(*sorted(skip), "*.pyc", "__pycache__"))
        n += 1
    return n


def lay_code(from_root: Path, to: Path) -> None:
    say("1/4 铺代码（不带 .venv / storage —— 前者跨路径即废，后者是你的历史）")
    (to / "toolkit").mkdir(parents=True, exist_ok=True)
    copy_tree(from_root / "toolkit", to / "toolkit", TOOLKIT_SKIP)
    for d in TOP_DIRS:
        if (from_root / d).is_dir():
            copy_tree(from_root / d, to / d, {"node_modules", ".git", "__pycache__"})
    for f in DOC_FILES:
        if (from_root / f).is_file():
            shutil.copy2(from_root / f, to / f)
    for sub in ("run_history", "tmp"):
        (to / "toolkit" / "storage" / sub).mkdir(parents=True, exist_ok=True)
    if not (to / "toolkit" / "assets" / "cases").is_dir():
        warn("源里没有 toolkit/assets/cases（用例库）—— 新机器上是空的，"
             "跑用例前先把 assets/cases/<包名>/ 放进来")
    size = sum(f.stat().st_size for f in (to / "toolkit").rglob("*") if f.is_file())
    ok(f"代码就位 {size / 1048576:.1f} MB → {to / 'toolkit'}")


# ── 2 依赖 ──────────────────────────────────────────────────────────────────
def site_packages(venv: Path) -> Path | None:
    for pat in ("lib/python*/site-packages", "Lib/site-packages"):
        for p in sorted(venv.glob(pat)):
            if p.is_dir():
                return p
    return None


def venv_python(venv: Path) -> Path:
    return venv / ("Scripts/python.exe" if IS_WIN else "bin/python")


def reuse_venv(src_venv: Path, to: Path) -> None:
    if not venv_python(src_venv).exists():
        die(f"--reuse-venv 指过去不是一个能用的 venv：{src_venv}"
            "（venv 不能跨系统、跨路径复用；Windows 建的在 Linux 上是死的，反之也一样）")
    src_sp = site_packages(src_venv)
    if src_sp is None:
        die(f"在 {src_venv} 里找不到 site-packages")
    src_is_win = src_sp.name == "site-packages" and (src_venv / "Lib").is_dir()
    if src_is_win != IS_WIN:
        die(f"--reuse-venv 是 {'Windows' if src_is_win else 'POSIX'} 建的 venv，"
            f"搬不到 {'Windows' if IS_WIN else 'POSIX'} 上；请直连镜像源装："
            "python3 install.py（不带 --reuse-venv）")
    warn(f"离线模式：从 {src_venv} 拷包，不跑 pip install —— 拷过来是什么版本就是什么版本")
    dst_venv = to / "toolkit" / ".venv"
    if dst_venv.exists():
        shutil.rmtree(dst_venv)
    r = subprocess.run([sys.executable, "-m", "venv", "--copies", str(dst_venv)])
    if r.returncode:
        die("python -m venv 失败（Debian/Ubuntu 可能要 apt install python3-venv）")
    dst_sp = site_packages(dst_venv)
    if dst_sp is None:
        die("新建的 venv 里没有 site-packages，不敢继续")
    skip = {"pip", "setuptools", "wheel", "pkg_resources", "distutils", "__pycache__"}
    n = 0
    for item in src_sp.iterdir():
        if item.name in skip or item.name.endswith((".dist-info", ".egg-info")):
            continue
        target = dst_sp / item.name
        if item.is_dir():
            shutil.copytree(item, target, dirs_exist_ok=True)
        else:
            shutil.copy2(item, target)
        n += 1
    # .dist-info 必须一起拷，否则 importlib.metadata / pip list 认不出这些包
    for pat in ("*.dist-info", "*.egg-info"):
        for item in src_sp.glob(pat):
            target = dst_sp / item.name
            if item.is_dir():
                shutil.copytree(item, target, dirs_exist_ok=True)
            else:
                shutil.copy2(item, target)
    ok(f"复制了 {n} 个包目录（离线）")
    check = subprocess.run([str(venv_python(dst_venv)), "-c",
                            "import uiautomator2, adbutils, yaml, PIL, rapidocr"],
                           capture_output=True, text=True)
    if check.returncode:
        die("拷完 import 不起来：" + (check.stderr.strip() or check.stdout.strip()))


def make_venv(to: Path, reuse: str | None, setup_args: list[str]) -> None:
    say("2/4 Python 环境 + 依赖")
    dst_venv = to / "toolkit" / ".venv"
    if reuse:
        reuse_venv(Path(reuse).expanduser().resolve(), to)
        return
    if dst_venv.exists():
        die(f"{dst_venv} 已存在。要重建请先删掉它，或加 --recreate 转给 setup（"
            f"手工：cd {to / 'toolkit'} && bash toolkit/scripts/setup.sh --recreate）")
    script = to / "toolkit" / "scripts" / ("setup.ps1" if IS_WIN else "setup.sh")
    if not script.is_file():
        die(f"没有 {script} —— 源不完整，检查一下 --from")
    cmd = (["powershell", "-ExecutionPolicy", "Bypass", "-File", str(script)] if IS_WIN
           else ["bash", str(script)])
    print("  " + " ".join(cmd + setup_args))
    r = subprocess.run(cmd + setup_args)
    if r.returncode:
        die("scripts/setup 失败（上面是它的原文输出，别绕过去手改）")


# ── 3 自检 ──────────────────────────────────────────────────────────────────
def run_doctor(to: Path) -> int:
    say("3/4 自检（装完不自证 = 把问题留给第一条用例）")
    py = venv_python(to / "toolkit" / ".venv")
    if not py.exists():
        py = Path(sys.executable)
        warn(f"venv 里没有 python，退回 {py}")
    r = subprocess.run([str(py), "tools/doctor.py"], cwd=str(to / "toolkit"))
    if r.returncode >= 2:
        die("自检有硬失败：按上面每一条的修法改完再跑一次，别硬开用例")
    if r.returncode == 1:
        warn("自检有警告（能跑但产物可能不全）—— 上面每条 ! 都给了修法")
    return r.returncode


# ── 4 挂接 ──────────────────────────────────────────────────────────────────
TARGET_RE = re.compile(r"^(\s*target:\s*).*$")


def wire(to: Path) -> None:
    say("4/4 挂到 DSH profile（只改新层里的 config.target；你的 profile 本脚本不碰）")
    hit = 0
    for rel in ("plugins/android-tools-plugin/cordis.patch.yml",
                "plugins/android-auto-test-preset/preset/cordis.patch.yml"):
        f = to / rel
        if not f.is_file():
            warn(f"没有 {rel}，跳过")
            continue
        lines = f.read_text(encoding="utf-8").splitlines(keepends=True)
        changed = []
        for i, line in enumerate(lines):
            m = TARGET_RE.match(line.rstrip("\n"))
            if m and m.group(1).strip() == "target:":
                new = f"{m.group(1)}'{to / 'toolkit'}'\n"
                if new != line:
                    lines[i] = new
                    changed.append(i + 1)
        if not changed:
            warn(f"{rel} 里没有 target: 行（格式变了？）—— 自己看一眼")
            continue
        bak = f.with_name(f.name + ".bak-" + ts_suffix())
        shutil.copy2(f, bak)
        f.write_text("".join(lines), encoding="utf-8")
        for ln in changed:
            print(f"    {rel}:{ln}  {lines[ln - 1].rstrip()}")
        ok(f"改了 {len(changed)} 处 target（原文件备份在 {bak.name}）")
        hit += len(changed)
    if not hit:
        warn("一处都没改成 —— 别让插件继续指旧层，否则产物又分家")
    # 实测 DSH_PROFILE = "web"（**是个名字不是路径**），DSH_PROFILE_DIR 才带目录；
    # 之前直接 Path(os.environ["DSH_PROFILE"]) 会拼出相对路径 web/package.json 找不到。
    prof = None
    for env_key in ("DSH_PROFILE_DIR", "DSH_WORKSPACE"):
        v = (os.environ.get(env_key) or "").strip()
        if v and (Path(v) / "package.json").is_file():
            prof = Path(v)
            break
    if prof is None:
        name = (os.environ.get("DSH_PROFILE") or "web").strip()
        home = Path(os.environ.get("DSH_HOME") or (Path.home() / ".dsh"))
        cand = [home / "profiles" / name, Path(name)]
        prof = next((c for c in cand if (c / "package.json").is_file()), cand[0])
    pj = prof / "package.json"
    if pj.is_file():
        warn(f"profile 里这两个 link: 要指到新层（这文件在工作区外，我不动）：{pj}")
        print(f'    "@local/dsh-android-tools": "link:{to / "plugins" / "android-tools-plugin"}"')
        print(f'    "@local/dsh-android-auto-test-preset": '
              f'"link:{to / "plugins" / "android-auto-test-preset"}"')
    else:
        warn(f"找不到 profile 的 package.json：{pj}（用 DSH_PROFILE 指一下）")
    warn(f"然后：cd {to / 'plugins' / 'android-tools-plugin'} && node link-dsh-deps.mjs "
         "（漏了插件根本起不来）；再重启 harness —— target 是进程起来时读一次的")
    warn("验证生效：重开会话看 android_env() 的 tool_root 是不是新路径")


# ── 主流程 ──────────────────────────────────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser(add_help=True, description="第一次安装：装进会话工作区")
    ap.add_argument("--to", default=None, help=f"落点，默认 <cwd>/{DEFAULT_NAME}")
    ap.add_argument("--from", dest="frm", default=None, help="源（仓库或 Share 包根），默认脚本所在目录")
    ap.add_argument("--wire", action="store_true", help="改新层里 3 处 config.target")
    ap.add_argument("--reuse-venv", default=None, metavar="VENV", help="离线：从已有 POSIX venv 拷包")
    ap.add_argument("--check", action="store_true", help="只判定落点/工作区/可写性，不写任何东西")
    ap.add_argument("--setup-args", nargs="*", default=None,
                    help=argparse.SUPPRESS)     # 由 -- 后面的裸参数填
    args, extra = ap.parse_known_args()
    setup_args = list(args.setup_args or []) + list(extra)

    from_root = Path(args.frm or HERE).expanduser().resolve()
    if not (from_root / "toolkit" / "tools" / "session.py").is_file():
        die(f"源里没有 toolkit/tools/session.py：{from_root}（--from 指到仓库或 Share 包根）")
    if not (from_root / "toolkit" / "scripts" / ("setup.ps1" if IS_WIN else "setup.sh")).is_file():
        die(f"源里没有 toolkit/scripts/setup：{from_root}")

    if not args.to:
        args.to = str(Path.cwd() / DEFAULT_NAME)
        print(f"  未指定 --to，默认装到当前目录下：{args.to}")
    want = Path(args.to).expanduser()
    try:
        to = (want if want.is_absolute() else Path.cwd() / want).resolve()
    except OSError as exc:
        die(f"落点路径解析不了：{want}（{exc}）")
    if (to / "toolkit" / "tools" / "session.py").is_file():
        die(f"落点已经装过了：{to / 'toolkit'}（要重装请先自己挪走旧目录，我不覆盖你的产物）")
    try:
        to.relative_to(from_root)
        die(f"落点 {to} 在源码树 {from_root} 里面 —— 别把装出来的层埋进仓里，"
            f"--to 指到工作区的一个新目录（默认 {Path.cwd() / DEFAULT_NAME} 就挺好，"
            "前提是你不在仓里跑）")
    except ValueError:
        pass
    if to == from_root:
        die(f"落点和源是同一个目录。就地安装请直接跑 "
            f"{'powershell -File ' if IS_WIN else 'bash '}{from_root / 'toolkit/scripts' / ('setup.ps1' if IS_WIN else 'setup.sh')}，否则换个 --to")
    try:
        Path.cwd().resolve().relative_to(to)
        die(f"落点不能包住当前工作区（{Path.cwd()} 在 {to} 里面），会自指")
    except (ValueError, OSError):
        pass

    print("============================================")
    print("  Android 真机测试环境 · 第一次安装")
    print(f"  源    {from_root}")
    print(f"  落点  {to}")
    print("============================================")

    say("0/4 落点能不能写（这一条决定你后面会不会撞 [Errno 13]）")
    # --check 是"先看这台机器该装哪"，不该留下任何目录：探针落在 to 本身，
    # 结束时把新建出来的 to 收回。
    probe_dir = to if args.check else to / "toolkit"
    existed = to.exists()
    good, why = probe_writable(probe_dir)
    if not good:
        die(f"落点写不进去：{to} —— {why}\n"
            f"    请 --to 指到本会话工作区之内；判法：python3 {from_root / 'toolkit/tools/harness.py'}")
    ok(f"落点可写：{to}")
    h = load_harness(from_root)
    if h:
        ws = h.workspace()
        inside = h.is_inside(to, ws)
        line = f"{to} 在内 工作区 {ws['path']}  [{ws['confidence']}]  依据：{ws['source']}"
        if ws["confidence"] == "low" or inside is None:
            warn(f"工作区判不了：{line}；以真写探针为准（已可写）")
        elif inside:
            ok(line)
        else:
            warn(f"{to} 在外 工作区 {ws['path']} —— 文件策略是 workspace-write 时 "
                 "AI 侧写不进产物；挪进工作区，或本会话放宽策略")
    else:
        warn("没找到 toolkit/tools/harness.py —— 跳过工作区判定，只信真写探针")
    if args.check:
        if not existed:
            try:
                to.rmdir()
            except OSError:
                pass
        print("\n" + _c("32", "--check 完成，什么都没写。"))
        return 0

    lay_code(from_root, to)
    make_venv(to, args.reuse_venv, setup_args)
    rc = run_doctor(to)
    if args.wire:
        wire(to)
    else:
        say("4/4 未挂接 DSH（没给 --wire）—— 插件/skills 仍指你原来那份工具层")

    manifest = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "from": str(from_root),
        "to": str(to),
        "target_toolkit": str(to / "toolkit"),
        "python": sys.version.split()[0],
        "platform": sys.platform,
        "harness_env": h.available_vars() if h else {},
        "workspace": (h.workspace() if h else {}),
        "reuse_venv": args.reuse_venv,
        "doctor_rc": rc,
        "wired": bool(args.wire),
    }
    try:
        (to / "install.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1),
                                         encoding="utf-8")
    except OSError as exc:
        warn(f"install.json 没写成（不影响使用）：{exc}")

    print()
    ok(f"装完了。落点 {to}")
    print(f"   开会话：cd {to / 'toolkit'} && "
          f"{'./.venv/bin/python' if not IS_WIN else '.venv\\Scripts\\python.exe'} "
          "tools/session.py start --package <包名> ...")
    print(f"   产物：{to / 'toolkit' / 'storage'}（在工作区内，沙箱写得住；用例历史与证据都在这）")
    print(f"   还没挂 DSH 的话：python3 install.py --to {to} --wire，然后重启 harness")
    print("   整套搬家：venv 搬完要修 bin/ 里的 shebang，插件 node_modules 的 link 要重跑 "
          "link-dsh-deps.mjs；会话文件策略是 workspace-write 时脚本删/搬不了工作区外的旧层")
    return 0


if __name__ == "__main__":
    sys.exit(main())
