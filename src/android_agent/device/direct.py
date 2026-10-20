"""设备直连层：in-process 调库，不起 subprocess。

## 一次观察产出什么

    storage/evidence/<case>/step01/
        dump.xml     ← XML 全量落盘（**不进上下文**）
        shot.png     ← 截图（只在需要视觉判断时才看）
        meta.json    ← activity / package / 尺寸 / rotation

返回给上层 / 模型的只有一份**可点击控件清单**和一个路径：

    Screen(
        xml_path  = "…/step01/dump.xml",   # 路径，不是内容
        shot_path = "…/step01/shot.png",
        clickable = [{i, t, r, c}, …],     # 只有可点的，十几个
    )

## 为什么 XML 不放进上下文

一份日历首页的 dump 约 34000 字符 ≈ 1.7 万 token。每轮都带，几十轮就是百万 token，
而且其中 95% 是布局容器 —— 模型根本用不上。

但 XML **又不能丢**：
  · 判据要查"这个 rid 在不在"、"这段文字在不在"
  · XPath 判据要算命中数（必须恰好 1 个才算强证据）
  · 事后复核要能翻原始界面

所以：**落盘 + 按需从文件查**。判据用的是同一份文件，
既不用重新 dump，也不用把内容塞进 prompt。

## 边界（不能破）

    ⚠️ 这一层只给子 Agent 用。主 Agent 负责拆解，绝不碰设备。
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .bridge import ActResult
from .ledger import ClickLedger, target_from_screen


_ADB_CACHE: list[str] = []

# ── 设备层调试日志 ────────────────────────────────────────────────
#
# 为什么要有（用户明确要求过："要增加 log 机制，我们得知道每个子 Agent 的执行情况"）：
#
#   踩坑现场：日志停在 "判定本步完成" 之后再没增长，设备不动、进程还活着。
#   人只能看到"最后一次点击的效果"，于是一口咬定"它在一直点这个按钮"。
#   实际是卡在点击**之后**的 observe 里，一次都没再点。
#
#   ⇏ 结论：**动作和观察都必须有日志**。只记"点了什么"不够，
#     必须记"点完之后到拿回画面花了多久、卡在哪条 adb 上"。
#
# 用环境变量 `ANDROID_DEVICE_LOG=1` 打开（默认静默，不污染正常输出）。
_DEBUG_SINK: list[Any] = []


def _debug_log():
    """返回调试日志函数；没开就返回 None。"""
    import os

    if _DEBUG_SINK:
        return _DEBUG_SINK[0]
    if os.environ.get("ANDROID_DEVICE_LOG", "").strip() in ("1", "true", "yes"):
        import time as _t

        t0 = _t.time()

        def _emit(msg: str) -> None:
            print(f"  [dev {_t.time() - t0:6.1f}s] {msg}", flush=True)

        _DEBUG_SINK.append(_emit)
        return _emit
    _DEBUG_SINK.append(None)  # type: ignore[arg-type]
    return None


def _adb_exe() -> str:
    """adb 可执行文件路径。

    ⚠️ 设备操作**一律走 uiautomator2**，不再自己拼 adb 命令（用户定的规矩）——
    唯一的例外是 `adb logcat`：它是查崩溃/日志的旁路手段，
    而且 u2 没有等价能力，所以保留。这个函数现在只服务于那条路径。
    """
    import os
    import shutil as _sh

    env = os.environ.get("ANDROID_ADB")
    if env and Path(env).exists():
        return env
    found = _sh.which("adb")
    if found:
        return found
    fallback = Path(r"D:\AndroidSdk\platform-tools\adb.exe")
    return str(fallback) if fallback.exists() else "adb"


def _parse_top_activity(dumpsys_text: str) -> dict:
    """从 `dumpsys activity activities` 的输出里取当前前台。

    返回 {"package": ..., "activity": ...}，取不到返回 {}。

    ## 三级回退（和 AiAgentTest 的做法一致）

      1. `topResumedActivity=ActivityRecord{... u0 pkg/.Act ...}`
         最精准 —— 就是栈顶正在 resume 的那个。
      2. `mResumedActivity: ActivityRecord{... u0 pkg/.Act ...}`
         老 ROM 只有这个字段。
      3. `ResumedActivity: ActivityRecord{...}` 后面再出现包名
         某些定制 ROM 的写法。

    ## 为什么 activity 要补全成全名

    dumpsys 里常写成 `.timetable.display.TimetableActivity`（省略包名前缀），
    而落点判据里写的是全名。这里统一拼成 `包名 + 短名`，
    免得下游拿 `endswith` 去猜。

    ## 为什么不用 u2 的 app_current

    见 `_app_current` 的 docstring：它在页面过渡期会返回**旧页面**，
    这正是 176 s5 连点 15 格全部"看不到变化"的根因。
    """
    import re as _re

    if not dumpsys_text:
        return {}

    # 真实格式（设备实测）：
    #   topResumedActivity=ActivityRecord{205796433 u0 com.zui.calendar/
    #       .timetable.display.TimetableActivity t455, isEmbedded= false}
    # 注意 activity 常以 `.` 开头（省略包名前缀）。
    _REC = r"ActivityRecord\{[^}]*\s+u\d+\s+([\w.]+)/([\w.$]+)[^}]*\}"
    for pat in (r"topResumedActivity=" + _REC,
                r"mResumedActivity:\s*" + _REC,
                r"ResumedActivity:\s*" + _REC):
        m = _re.search(pat, dumpsys_text)
        if not m:
            continue
        pkg, act = m.group(1), m.group(2)
        if act.startswith("."):
            act = pkg + act
        elif "." not in act:
            act = f"{pkg}.{act}"
        return {"package": pkg, "activity": act}
    return {}


def _clickable_sig(scr: Any) -> str:

    """把一屏的**可点控件**压成一个短指纹，用来判断"点了有没有进展"。

    为什么不用整个 XML 比对：XML 里常有时间戳、滚动位置这类噪声，
    一变就判"有变化"，反而看不出真·无反应。

    只取可点控件的 `rid|文字`，排序后拼起来：
        `iv_more|,save_view|完成,toolbar_title|测试课程表`
    这个串变了，说明界面上"能点的东西"确实变了 —— 那才算进展。
    """
    if scr is None:
        return ""
    try:
        items = []
        for n in (getattr(scr, "clickable", None) or []):
            r = str(n.get("r") or n.get("rid") or "")
            t = str(n.get("t") or n.get("text") or "")
            if r or t:
                items.append(f"{r}|{t}")
        return ",".join(sorted(items))
    except Exception:  # noqa: BLE001  指纹算不出来不能影响执行
        return ""


def _load_perm():
    """加载系统权限弹窗处理模块（assets/tools/perm.py）。

    ## 为什么是"按路径动态加载"而不是普通 import

    `perm.py` 是**工具层**的资产（assets/tools/，和 act.py 一套），
    它自己 `import subprocess` 直接调 adb，**不依赖本项目的任何包**。
    保持这个边界很重要：它要能独立离线单测（喂一段假 dumpsys 输出即可）。

    所以这里按文件路径加载，不往 package 里塞一份副本 ——
    两份代码迟早会分叉，而权限判定错一次的代价是"授权状态被悄悄改掉"。

    加载失败时返回 None：权限处理是**增强**，不该让整个执行器起不来。
    """
    import importlib.util
    import os

    root = os.environ.get("ANDROID_SKILL_ROOT", "").strip()
    cands = []
    if root:
        cands.append(Path(root) / "tools" / "perm.py")
    # 兜底：从本文件往上找到项目根下的 assets/tools/perm.py
    #   src/android_agent/device/direct.py -> parents[3] = 项目根
    try:
        cands.append(Path(__file__).resolve().parents[3] / "assets" / "tools" / "perm.py")
    except IndexError:
        pass
    for p in cands:
        if p.is_file():
            try:
                spec = importlib.util.spec_from_file_location("_aagent_perm", p)
                if spec and spec.loader:
                    mod = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(mod)
                    return mod
            except Exception:  # noqa: BLE001
                continue
    return None

_BOUNDS = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")

# 系统 UI 的包名 —— 从可点清单里剔掉，模型不该去点状态栏/导航栏
_DECOR_PKGS = ("com.android.systemui",)


@dataclass
class Screen:
    """一次观察的结果：**路径 + 可点清单**，XML 内容不进来。"""

    xml_path: str
    activity: str
    package: str
    width: int
    height: int
    rotation: int = 0
    shot_path: str | None = None
    clickable: list[dict[str, Any]] = field(default_factory=list)
    ts: float = 0.0
    _root: Any = field(default=None, repr=False)     # lxml 根，惰性解析

    # -- 从落盘 XML 查询（不重新 dump、不进上下文）-------------------------
    def _doc(self):
        """惰性加载落盘的 XML。同一个 Screen 上多次查询只解析一次。"""
        if self._root is None:
            from lxml import etree
            with open(self.xml_path, "rb") as f:
                self._root = etree.parse(f).getroot()
        return self._root

    def find_rid(self, rid: str) -> dict[str, Any] | None:
        """按 rid 末段找**可点**控件。找不到返回 None。"""
        for n in self.clickable:
            if n.get("r") == rid:
                return n
        return None

    def xpath_count(self, expr: str) -> int:
        """XPath 命中数 —— 强证据判定要用它（必须恰好 1 个）。"""
        try:
            return len(self._doc().xpath(expr))
        except Exception:  # noqa: BLE001  非法 xpath / 解析失败
            return 0

    def xpath_center(self, expr: str) -> list[int] | None:
        """XPath 命中**唯一**节点时返回其中心坐标，否则 None。

        为什么"唯一"才返回：命中 0 个没得点；命中多个不知道该点哪个 ——
        实测同一界面有 4 个 bounds 不同、rid/desc/text 全空的节点，
        只凭单一属性分不出来，点错了整条用例就废了。
        """
        try:
            hits = self._doc().xpath(expr)
        except Exception:  # noqa: BLE001
            return None
        if len(hits) != 1:
            return None
        return _center_of(hits[0].get("bounds", ""))

    def has_text(self, text: str) -> bool:
        """可点清单里有没有这段文字。"""
        t = (text or "").strip()
        return bool(t) and any(t in str(n.get("t") or "") for n in self.clickable)


def _center_of(bounds: str) -> list[int] | None:
    m = _BOUNDS.search(bounds or "")
    if not m:
        return None
    x1, y1, x2, y2 = map(int, m.groups())
    if x2 <= x1 or y2 <= y1:
        return None
    return [(x1 + x2) // 2, (y1 + y2) // 2]


def parse_clickable(xml_path: str | Path, *, fg_package: str = "") -> tuple[list[dict[str, Any]], int]:
    """从落盘的 XML 提取**可点击控件**清单。

    只取 clickable=true 的 —— 可滑动的容器不进来。
    理由：点击时只需要"能点的"，滚动目标是少数，交给 XPath 按需找更准。
    一屏通常十几个，模型看着轻松。

    返回 (清单, rotation)。清单形状：{i, t, r, c, off}
      i   序号
      t   文字（text 优先，没有就用 content-desc）
      r   rid 末段
      c   中心坐标
      off 置灰（enabled=false）
    """
    from lxml import etree

    with open(xml_path, "rb") as f:
        root = etree.parse(f).getroot()

    try:
        rotation = int(root.get("rotation") or 0)
    except ValueError:
        rotation = 0

    out: list[dict[str, Any]] = []
    for el in root.xpath('//*[local-name()="node"]'):
        if el.get("clickable") != "true":
            continue
        rid_full = el.get("resource-id") or ""
        pkg = el.get("package") or ""

        # 剔掉系统 UI：状态栏时间、电量这些点不到也没意义
        if any(d in pkg for d in _DECOR_PKGS):
            continue
        if any(d in rid_full for d in _DECOR_PKGS):
            continue

        c = _center_of(el.get("bounds", ""))
        if not c:
            continue

        text = el.get("text") or ""
        desc = el.get("content-desc") or ""
        if not text:
            text = desc
        elif desc and desc not in text:
            text = f"{text} {desc}"

        node: dict[str, Any] = {"i": len(out), "c": c}
        if text:
            node["t"] = text.strip()
        if rid_full:
            node["r"] = rid_full.split("/")[-1]
        if el.get("enabled") == "false":
            node["off"] = True
        out.append(node)

    return out, rotation


class DirectDevice:
    """直接操作设备：全程 in-process。

    与老 RealDevice 的关系：
      · 接口形状一致（observe / act / key / ocr / shell）
      · 不再有会话、事件台账 —— 那些是 subprocess 时代为跨进程追踪而存在的
      · 每次观察都**落盘**，判据从落盘文件查，不重新 dump
    """

    # ocr 模型只加载一次：RapidOCR() 每次构造要读三个 onnx，约 13s
    _ocr_engine: Any = None

    def __init__(self, serial: str | None = None,
                 evidence_dir: str | Path | None = None,
                 ledger_path: str | Path | None = None) -> None:
        import uiautomator2 as u2
        self.serial = serial
        self._d = u2.connect(serial) if serial else u2.connect()
        self.evidence_dir = Path(evidence_dir) if evidence_dir else Path("tmp/evidence")
        self._n = 0
        # 系统权限弹窗处理模块。加载失败是 None（权限处理降级，不影响主流程）——
        # 但要**显式暴露**出来，别让"权限框没被点"变成静默行为。
        self._perm = _load_perm()
        # 上一轮看到的"非权限框"前台包 = 权限申请方。
        # 多权限申请时同一个框里连点几次，申请方不变，靠它记住。
        self._last_perm_owner = ""
        # ★ 点击账本：每次点击一条完整记录（点了哪个控件、前后页面、耗时）。
        #   排查"一直点某个按钮"这类问题的唯一可靠依据 —— 见 ledger.py。
        self._ledger = ClickLedger(ledger_path) if ledger_path else None
        # 上一次观察到的屏 —— act 里用它反查坐标落在哪个控件上。
        self._last_screen: Screen | None = None

    @property
    def ledger(self) -> ClickLedger | None:
        return self._ledger

    # -- 观察 ---------------------------------------------------------------
    def _call_with_timeout(self, fn: Any, *, timeout: float,
                           label: str) -> Any:
        """用线程包住一次 u2 调用，超时就抛 TimeoutError。

        ## 为什么需要这层（用户定的规矩：设备操作一律走 uiautomator2）

        规矩很清爽：**不许绕开 u2 直接调 adb**。理由也站得住 ——
        u2 已经把"点、滑、输入、取界面、取前台"都做全了，
        自己再拼 adb 命令等于维护两套通道，两边行为还不一致。

        但 u2 有个老毛病：底层 adbutils 的 `shell()` 挂着 `@retry`
        （`retry.api`，BrokenPipeError/IndexError 无限重试）。socket 一旦僵住，
        调用就永远不返回 —— 实测卡死 20 分钟，日志一个字没有。

        `try/except` 挡不住它（**根本没有异常抛出**，它只是在重试）。
        所以这里起一个 daemon 线程去调，主线程只等 timeout：
        超时就抛，让上层按"这次没拿到"降级，而不是整条用例停摆。

        为什么线程可以被丢掉不管：u2 的 socket 调用一旦僵住，
        重开连接也救不回来；daemon 线程不会阻止进程退出。
        """
        box: list[Any] = []
        err: list[BaseException] = []

        def _run() -> None:
            try:
                box.append(fn())
            except BaseException as exc:  # noqa: BLE001
                err.append(exc)

        th = threading.Thread(target=_run, daemon=True,
                              name=f"u2-{label}")
        th.start()
        th.join(timeout)
        if th.is_alive():
            raise TimeoutError(
                f"{label} 超过 {timeout:.0f}s 未返回（u2 在无限重试）")
        if err:
            raise err[0]
        return box[0] if box else None

    def _app_current(self) -> dict:
        """当前前台应用。**走 dumpsys，不用 u2.app_current。**

        ## 为什么不用 `d.app_current()`（这是 u2 的已知 bug）

        `app_current` 在界面上还有动画/过渡时会**返回上一个页面**。
        176 的 s5 就是被它坑的：点周视图空格子，界面 1.1 秒后才跳到
        EditCourseActivity，而点击后立刻读 app_current 拿到的还是
        TimetableActivity —— 记账 changed=False、模型以为没反应、换格子再点，
        连点 15 格全判无效，最后 BLOCKED。
        同一个坐标手动点能进，所以不是控件点不动，是**读前台读早了**。

        `dumpsys activity activities` 的 `topResumedActivity`
        是系统解析栈里真实的栈顶，比 u2 的实现稳。
        （参考 D:\\Project\\python\\AiAgentTest\\device\\controller.py：
          它也是绕过 app_current 直接读 dumpsys，并优先匹配 topResumedActivity。）

        ## 仍然要兜底

        拿不到就返回空 dict，让上层按"不知道当前在哪"处理
        （落点判不了 -> 交给模型），而不是让执行器停摆。
        """
        try:
            out = self._call_with_timeout(
                lambda: self._d.shell("dumpsys activity activities"),
                timeout=8.0, label="dumpsys activity")
        except Exception:  # noqa: BLE001
            return {}
        # ⚠️ u2 的 shell() 返回 ShellResponse 对象，不是 str！
        #    直接 isinstance(out, str) 判断会永远 False，于是这里静默返回 {}，
        #    表现为"读不到前台"——比报错更难查。所以两种形态都收。
        text = getattr(out, "output", None)
        if text is None and isinstance(out, str):
            text = out
        if not isinstance(text, str):
            return {}
        return _parse_top_activity(text)

    def _window_size(self) -> tuple[int, int]:
        """屏幕宽高 (width, height)。拿不到返回 (0, 0)，绝不允许卡住。

        ★⚠️ `u2.window_size()` 返回的是 **(height, width)**，不是 (width, height)！
             实测：设备 1904x3040，它返回 (3040, 1904)。
             早先直接拿它当宽高用，于是"屏幕宽度"变成 3040 —— 落在屏幕外侧的
             x=2913 被判成合法坐标，点下去毫无反应，落点判据永远不满足，
             表现就是"一直点同一个按钮"。这里以**短的当宽**。
        """
        try:
            size = self._call_with_timeout(
                self._d.window_size, timeout=6.0, label="window_size")
        except Exception:  # noqa: BLE001
            return 0, 0
        try:
            a, b = int(size[0]), int(size[1])
        except Exception:  # noqa: BLE001
            return 0, 0
        # 取短的作宽：手机竖屏时宽一定小于高，横屏时同理
        return (a, b)

    def _dump_hierarchy(self, timeout: float = 20.0) -> str:
        """dump 界面 XML。**必须有上限**，不许无限重试。

        ⚠️ 与 _app_current / _window_size 同一个坑，这是第三处：

            _do_dump_hierarchy (uiautomator2/__init__.py:136)
            __retry_internal (retry/api.py:35)      ← 无限重试
            dump_hierarchy (uiautomator2/__init__.py:120)
            observe (direct.py:349)

        实测卡死：用例跑到 s3 的第一次观察就停住，进程活着、
        设备停在原页不动、日志不再增长 —— 看起来像"一直在点完成"，
        实际是**一次都没点**，全卡在观察里。

        做法：放到有超时的线程里跑。超时就抛，由 observe 的调用方处理。
        uiautomator2 的 jsonrpc 有自己的 HTTP 超时，但 retry 装饰器会把
        超时当"可重试"继续打，所以只能在**外面**再加一道硬墙。
        """
        result: dict[str, Any] = {}

        def work() -> None:
            try:
                result["xml"] = self._d.dump_hierarchy()
            except BaseException as exc:  # noqa: BLE001
                result["err"] = exc

        th = threading.Thread(target=work, daemon=True, name="dump_hierarchy")
        th.start()
        th.join(timeout)
        if th.is_alive():
            raise TimeoutError(f"dump_hierarchy 超过 {timeout:.0f}s 未返回（uiautomator2 在无限重试）")
        if "err" in result:
            raise result["err"]
        return str(result.get("xml") or "")

    def _screenshot(self, path: str, timeout: float = 15.0) -> bool:
        """截图。同样加硬超时 —— 截图失败不该中断执行，但也不该卡死。"""
        done: dict[str, Any] = {}

        def work() -> None:
            try:
                self._d.screenshot(path)
                done["ok"] = True
            except BaseException:  # noqa: BLE001
                done["ok"] = False

        th = threading.Thread(target=work, daemon=True, name="screenshot")
        th.start()
        th.join(timeout)
        return bool(done.get("ok")) and not th.is_alive()

    def observe(self, *, name: str | None = None, with_shot: bool = True) -> Screen:
        """看一眼：XML + 截图落盘，返回可点清单。

        ⚠️ 尺寸与 rotation **每次现取**：设备一转宽高就换，
           缓存了就会拿竖屏坐标去点横屏界面（实测踩过）。
        """
        self._n += 1
        tag = name or f"step{self._n:02d}"
        outdir = self.evidence_dir / tag
        outdir.mkdir(parents=True, exist_ok=True)

        xml_path = outdir / "dump.xml"
        shot_path = outdir / "shot.png"
        meta_path = outdir / "meta.json"

        t0 = time.time()
        _dbg = _debug_log()
        if _dbg:
            _dbg(f"observe[{tag}] 开始")
        # ★ dump 超时 -> 重试一次再放弃。
        #   为什么重试：u2 卡住多半是那条 jsonrpc 连接僵了，重开通常就好了。
        #   为什么只重试一次：两次都拿不到，说明设备/uiautomator 服务真有问题，
        #   再多试只是把"卡死"换成"慢慢卡死"。
        last: Exception | None = None
        xml = ""
        for attempt in (1, 2):
            try:
                t_dump = time.time()
                xml = self._dump_hierarchy(timeout=20.0)
                if _dbg:
                    _dbg(f"observe[{tag}] dump 成功 "
                         f"{(time.time() - t_dump) * 1000:.0f}ms")
                last = None
                break
            except TimeoutError as exc:
                last = exc
                if _dbg:
                    _dbg(f"observe[{tag}] dump 超时（第 {attempt} 次）：{exc}")
                if attempt == 1:
                    time.sleep(1.0)
        if last is not None:
            # 抛给调用方（runner 会把它变成 BLOCKED 并写明原因），
            # 绝不能返回一个空界面让上层误以为"屏幕上什么都没有"。
            raise TimeoutError(
                f"observe 拿不到界面（dump_hierarchy 连续两次超时）：{last}"
            ) from last
        xml_path.write_text(xml, encoding="utf-8")

        t_cur = time.time()
        cur = self._app_current() or {}
        if _dbg:
            _dbg(f"observe[{tag}] app_current {(time.time() - t_cur) * 1000:.0f}ms "
                 f"-> {cur.get('activity')}")
        try:
            t_ws = time.time()
            w, h = self._window_size()
            if _dbg:
                _dbg(f"observe[{tag}] window_size {(time.time() - t_ws) * 1000:.0f}ms "
                     f"-> {w}x{h}")
        except Exception:  # noqa: BLE001
            w = h = 0

        shot: str | None = None
        if with_shot:
            # ★ 只有真截到了才把路径记下来。
            #   之前无条件写 shot=路径，截图失败时下游会去读一个不存在的文件。
            if self._screenshot(str(shot_path)):
                shot = str(shot_path)

        clickable, rotation = parse_clickable(xml_path, fg_package=cur.get("package", ""))

        meta = {
            "activity": cur.get("activity"), "package": cur.get("package"),
            "width": w, "height": h, "rotation": rotation,
            "clickable_count": len(clickable),
            "elapsed_ms": round((time.time() - t0) * 1000),
        }
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                             encoding="utf-8")

        scr = Screen(xml_path=str(xml_path), activity=cur.get("activity", ""),
                     package=cur.get("package", ""), width=w, height=h,
                     rotation=rotation, shot_path=shot, clickable=clickable,
                     ts=time.time())
        # 记住这一屏：act 里要用它把坐标反查成控件（账本的可读性靠它）
        self._last_screen = scr
        return scr

    # -- 权限 ---------------------------------------------------------------
    def _perm_owner(self) -> str:
        """这个权限框是谁申请的 —— 返回申请方包名。

        ## 为什么不能每次都直接读前台

        多权限申请时 Android **不关框**，就在同一个框里点一次换下一个权限。
        第二轮再去读前台，拿到的已经是 com.android.permissioncontroller
        （权限框自己），不是申请方。实测 168：同一个框里连点 5 个权限，
        第 2~5 条的 owner 全记成了 permissioncontroller —— 记账等于废了。

        申请方在**整条链上不变**，所以框已经在前台时沿用上一次记下的。
        """
        try:
            pkg = str((self._app_current() or {}).get("package") or "")
        except Exception:  # noqa: BLE001
            return ""
        if self._perm is not None and self._perm.is_system_perm_package(pkg):
            # 框已在前台 -> 沿用上一轮记下的申请方
            return str(self._last_perm_owner or "")
        self._last_perm_owner = pkg
        return pkg

    # -- 动作 ---------------------------------------------------------------
    def act(self, x: int, y: int, why: str = "",
            perm_action: str | None = None, perm: str | None = None,
            step: str = "", **_: Any) -> ActResult:
        """按坐标点击，处理掉点击引发的系统权限框，然后观察一次。

        ## 为什么要在这里接权限处理（不是可选优化，是硬约束）

        系统权限框约 **6 秒**没人点就自动消失，而"该同意还是拒绝"要读用例语义。
        决策慢、窗口短 -> 必须在动作返回的这一刻就地把它点掉，不能等上层下一轮。
        没接这段的后果是真机实测过的：权限框自己消失了，后续所有点击
        都落在不存在的框上，整条用例空转到轮次上限。

        ## 归属：先取前台包，再处理权限框

        `owner_package` 必须在**权限框弹出之前**取 —— 那是发起申请的 App（被测 App）。
        放到权限框弹出之后取，拿到的是 com.android.permissioncontroller，归属就错了。

        ## 两类权限的处置差异（用户明确要求区分）

        这是本模块最容易做错的地方，单独说明：

        **① 普通运行时权限**（通知、联系人、日历读写…）
           不授予**也能继续往下走**，只是某个功能不可用。
           -> 默认自动同意即可，失败不阻断流程，如实记账。

        **② 内容型权限**（相机 / 照片视频 / 麦克风 / 定位）
           不授予就**根本进不了下一步** —— 点"从图库导入"没照片权限，
           图片选择器永远不出现。
           -> 必须确认"真的授予了"，而不只是"点过允许按钮"。
              授权结果用 `dumpsys` 复查，这是判据；点没点过按钮只是过程。

        所以这里做两件事：自动点 + 把 clicks/prompts 带回去留痕，
        由上层按"授权结果"判断言（见 nodes.py 的 perm 分支）。
        """
        # ★ 归属包名：此刻前台还是**发起申请的那个 App**（权限框还没弹）
        owner = self._perm_owner()

        _dbg = _debug_log()
        _t_all = time.time()

        # 点之前先把"要点的控件"查出来 —— 坐标对人是无意义的，
        # 账本里必须写清那是「同意」还是「完成」（见 ledger.py 的说明）。
        _pre = None
        try:
            _pre = self._last_screen
        except Exception:  # noqa: BLE001
            _pre = None
        _tgt = (target_from_screen(_pre, x, y) if _pre is not None
                else {"c": [int(x), int(y)]})
        _act_before = getattr(_pre, "activity", "") if _pre else ""
        # ★ 没有上一屏时（会话第一次动作）用 dumpsys 补一个"点击前".
        #
        # 不补的话 `_act_before` 是空串，账本里 changed 就是 None，
        # 于是"这次点击有没有进展"完全没法判断 —— 后面所有基于
        # changed 的机制（强证据熔断、死循环检测）在这一条上都失灵。
        # 而"会话第一次动作"恰恰是最容易失败的（冷启动、首屏还没稳）。
        if not _act_before:
            _act_before = self._app_current().get("activity", "")
        # _pre 为空时，控件的语义（这是「完成」还是「允许」）也没法从旧屏拿，
        # 账本里就只能记坐标 —— 由 _tgt 已经处理（只有 c）。

        if _dbg:
            _dbg(f"act 点击 ({x},{y}) "
                 f"{_tgt.get('text') or _tgt.get('rid') or '?'} {why[:50]}")

        t_click = time.time()
        try:
            self._d.click(int(x), int(y))
        except Exception as exc:  # noqa: BLE001
            if self._ledger is not None:
                self._ledger.record(
                    step=str(step or ""), action="click", target=_tgt,
                    before=_act_before, after="", ms=(time.time() - t_click) * 1000,
                    changed=None, why=why, extra={"error": str(exc)[:200]})
            raise
        _ms_click = (time.time() - t_click) * 1000
        if _dbg:
            _dbg(f"act 点击完成 {_ms_click:.0f}ms")

        # ★ 抢在 6 秒窗口内处理权限框
        perm_result = None
        if self._perm is not None:
            try:
                perm_result = self._perm.handle(
                    self._d, perm_action, owner_package=owner)
            except Exception as exc:  # noqa: BLE001
                # 权限处理失败**不影响点击本身** —— 动作已经执行了。
                # 记下来让上层知道"这次没看清权限框"，而不是假装没发生。
                perm_result = {"error": str(exc)[:200]}

        t_obs = time.time()
        # ★ 点击后**等 1 秒**再截图/dump，否则会把有效点击误判成"没反应"。
        #
        # 用户原话：「每个操作等 1s 再截图和 dump xml 就比较稳定了」。
        # 这是踩出来的经验，不是拍脑袋的缓冲：
        #
        #   176 s5：点周视图空格子 -> **1.1 秒后**才跳到 EditCourseActivity。
        #   原来 click 完立刻 observe，dump 到的是旧页面，记账 changed=False，
        #   模型以为没反应就换个格子再点，连点 15 格全判无效 -> BLOCKED。
        #   而同一个坐标手动点**能进** —— 所以不是控件点不动，是**看得太早**。
        #
        # 我也试过"轮询 activity 一变就返回"（更省时间），实测不可靠：
        # 界面在过渡期时 activity 的读数本身就跳，轮询拿到的值不足以判断
        # "已经稳定"。固等一秒简单、可预测，代价是每次点击 +1s ——
        # 一条二三十次点击的用例多 20 秒，但换来的是不再误判，
        # 这个交换是划算的（比 BLOCKED 后人工重跑便宜得多）。
        time.sleep(1.0)

        scr = self.observe()
        _ms_obs = (time.time() - t_obs) * 1000
        if _dbg:
            _dbg(f"act 点击后 等1s+observe {_ms_obs:.0f}ms -> {scr.activity}")

        # ★ 记账：一次点击一条，含前后页面和"有没有进展"
        #
        # `changed` 的语义是**有没有进展**，不只是"跳没跳页"：
        #   · activity 变了              -> 有进展
        #   · 可点控件清单变了            -> 有进展（弹了对话框、键盘收起、多出个按钮）
        #   · 可点控件的文字变了          -> 有进展（按钮从「保存」变「已完成」）
        #
        # 为什么不能只看 activity：176 的 `save_view` 点了 37 次，
        # activity 一次都没变 —— 但中间也不能排除"多了个错误提示"这种情况。
        # 只看 activity 会把它当成"完全没反应"，可能会误判成因。
        # 反过来，只有**全都没变**才是真·无效动作，那才是该叫模型的时候。
        if self._ledger is not None:
            _sig_before = _clickable_sig(_pre)
            _sig_after = _clickable_sig(scr)
            self._ledger.record(
                step=str(step or ""),
                action="click",
                target=_tgt,
                before=_act_before,
                after=scr.activity,
                ms=(time.time() - _t_all) * 1000,
                changed=(
                    None if not _act_before
                    else (_act_before != scr.activity or _sig_before != _sig_after)
                ),
                why=why,
                extra={"ms_click": round(_ms_click, 1),
                       "ms_observe": round(_ms_obs, 1),
                       "sig": _sig_after,
                       "perm": (perm_result or {}).get("result")
                       if isinstance(perm_result, dict) else None,
                       # ★ 证据路径：点**前**那屏 + 点**后**那屏。
                       #
                       # 为什么要记：截图和 XML 一直都在落盘（observe 每次
                       # 都写 stepNNNN/dump.xml + shot.png），但账本里没有指针，
                       # 于是"某次点击当时长什么样"永远关联不上 ——
                       # WebUI 想展示证据也没法展示。
                       #
                       # 记 before 和 after 两个，是因为排查时最常问的是
                       # "点之前是这样、点之后变那样了吗"，
                       # 只有 after 一张看不出变化。
                       "shot_before": getattr(_pre, "shot_path", None),
                       "shot_after": getattr(scr, "shot_path", None),
                       "xml_before": getattr(_pre, "xml_path", None),
                       "xml_after": getattr(scr, "xml_path", None)},
            )

        return ActResult(ok=True, activity=scr.activity, next=scr.clickable,
                         event_ids=[], checkpoint=None, executed=True,
                         error=None, permission=perm_result,
                         package=scr.package, xml_path=scr.xml_path)

    def click_rid(self, rid: str, *, scr: Screen | None = None) -> ActResult | None:
        """按 rid 点。找不到返回 None（让调用方决定退回到模型）。"""
        s = scr or self.observe()
        n = s.find_rid(rid)
        if not n:
            return None
        return self.act(n["c"][0], n["c"][1], why=f"rid={rid}")

    def input_text(self, rid: str, text: str, *, scr: Screen | None = None,
                   step: str = "", why: str = "") -> ActResult | None:
        """在 rid 那个输入框里填入 text。找不到控件返回 None。

        为什么要这个（实测踩到）：
            日历「新建课程表」的名称是必填项。执行器只有"点"没有"输入"，
            于是点了「完成」被校验拦住、页面不动，落点判据永远不满足，
            看起来就是"一直在点完成"。根子不是执行器笨，是它**没有输入这个动作**。

        ⚠️ 中文怎么填 —— 这里踩过两次坑，别再改回去：

          ① `adb shell input text 中文` **无效**。Android 的 input text 只送
             ASCII，中文会被静默丢弃（实测：英文 HelloTest 进去了，中文没有，
             不报错）。所以不能靠 shell 灌字。

          ② uiautomator2 的 `setText`（jsonrpc）**可以**。它走 uiautomator
             服务的 AccessibilityNodeInfo.setText，不经过 ADB 的文本输入通道，
             所以 Unicode 没问题。

        做法：先点进输入框拿焦点，再用 u2 的 setText 覆盖写入。
        """
        s = scr or self.observe()
        n = s.find_rid(rid)
        if not n:
            # 找不到也记账 —— 否则账本里会**整段缺失**这件事，
            # 排查时看到的是"什么都没发生"，而不是"想输入但控件不在"。
            if self._ledger is not None:
                self._ledger.record(
                    step=step, action="input",
                    target={"rid": rid, "text": str(text)[:40]},
                    before=s.activity, after=s.activity, ms=0.0,
                    changed=None, why=why,
                    extra={"error": f"找不到输入框 rid={rid}"})
            return None

        cx, cy = int(n["c"][0]), int(n["c"][1])
        _t_in = time.time()

        # 1) 点进输入框（有些输入框不获得焦点就不接受 setText）
        self._call_with_timeout(
            lambda: self._d.click(cx, cy), timeout=10.0, label="input 点击")
        time.sleep(0.3)

        # 2) 用 uiautomator 服务写入（支持中文）。
        #    找不到元素时退回"当前聚焦的输入框" —— 点了之后通常就是它。
        full = n.get("rid_full") or ""
        if not full and "." in rid:
            full = rid
        sel = {"resourceId": full} if full else {"focused": True}
        _err = ""
        try:
            self._call_with_timeout(
                lambda: self._d(**sel).set_text(str(text)),
                timeout=15.0, label="input set_text")
        except Exception as exc:  # noqa: BLE001  退回聚焦控件再试一次
            try:
                self._call_with_timeout(
                    lambda: self._d(focused=True).set_text(str(text)),
                    timeout=15.0, label="input set_text(focused)")
            except Exception as exc2:  # noqa: BLE001
                _err = f"{exc} / {exc2}"[:200]
        # ★ 输入完同样等 1 秒再看 —— 和点击一个道理：
        #   键盘收起、正文上屏都有动画，立刻 dump 拿到的可能还是旧内容，
        #   后续判断就会以为"没填进去"。
        time.sleep(1.0)

        scr2 = self.observe()
        # ★ 输入也要记账 —— 用户的要求是"每一步点击了啥都要留痕方便排查"，
        #   而"填了什么字"比"点了哪"更容易出问题（中文、覆盖、焦点）。
        if self._ledger is not None:
            self._ledger.record(
                step=step, action="input",
                target={"rid": rid, "text": str(text)[:60],
                        "c": [cx, cy]},
                before=s.activity, after=scr2.activity,
                ms=(time.time() - _t_in) * 1000,
                changed=(s.activity != scr2.activity
                         or _clickable_sig(s) != _clickable_sig(scr2)),
                why=why,
                extra={"error": _err or None,
                       "shot_before": getattr(s, "shot_path", None),
                       "shot_after": getattr(scr2, "shot_path", None),
                       "xml_before": getattr(s, "xml_path", None),
                       "xml_after": getattr(scr2, "xml_path", None)})
        return ActResult(ok=True, activity=scr2.activity, next=scr2.clickable,
                         event_ids=[], checkpoint=None, executed=True,
                         error=_err or None, permission=None,
                         package=scr2.package, xml_path=scr2.xml_path)


    def click_xpath(self, expr: str, *, scr: Screen | None = None) -> ActResult | None:
        """按 XPath 点。**必须命中唯一一个**，否则返回 None。

        命中多个时宁可退回模型，也不赌 —— 点错元素整条用例就废了。
        """
        s = scr or self.observe()
        c = s.xpath_center(expr)
        if not c:
            return None
        return self.act(c[0], c[1], why=f"xpath={expr[:60]}")

    def key(self, name: str, why: str = "", perm_action: str | None = None,
            perm: str | None = None, step: str = "", **_: Any) -> ActResult:
        """系统键（back / home）。

        为什么也要接权限处理：按返回键时系统可能弹「确定要退出吗」，
        某些 OEM 的权限框按返回会变成"仅本次允许"选择页。
        和点击一样，处置窗口只有几秒。
        """
        _pre = self._last_screen
        _before = getattr(_pre, "activity", "") if _pre else ""
        _t_key = time.time()
        self._d.press(name)
        perm_result = None
        if self._perm is not None:
            try:
                perm_result = self._perm.handle(
                    self._d, perm_action, owner_package=self._perm_owner())
            except Exception as exc:  # noqa: BLE001
                perm_result = {"error": str(exc)[:200]}
        # ★ 按键后同样等 1 秒：返回/回桌面都有退出动画，
        #   立刻 dump 会拿到上一屏（和点击同一个坑）。
        time.sleep(1.0)
        scr = self.observe()
        # ★ 记账。按键原来完全不进账本 —— 于是"模型按了返回"
        #   在账本里看起来像"什么都没发生"，排查时最容易被误导。
        if self._ledger is not None:
            if not _before:
                _before = self._app_current().get("activity", "")
            self._ledger.record(
                step=step, action=f"key:{name}",
                target={"key": name},
                before=_before, after=scr.activity,
                ms=(time.time() - _t_key) * 1000,
                changed=(
                    None if not _before
                    else (_before != scr.activity
                          or _clickable_sig(_pre) != _clickable_sig(scr))
                ),
                why=why,
                extra={"perm": (perm_result or {}).get("result")
                       if isinstance(perm_result, dict) else None,
                       "shot_before": getattr(_pre, "shot_path", None),
                       "shot_after": getattr(scr, "shot_path", None),
                       "xml_before": getattr(_pre, "xml_path", None),
                       "xml_after": getattr(scr, "xml_path", None)})
        return ActResult(ok=True, activity=scr.activity, next=scr.clickable,
                         event_ids=[], checkpoint=None, executed=True,
                         error=None, permission=perm_result,
                         package=scr.package, xml_path=scr.xml_path)

    def swipe(self, x1: int, y1: int, x2: int, y2: int,
              duration: float = 0.3) -> ActResult:
        self._d.swipe(int(x1), int(y1), int(x2), int(y2), duration=duration)
        scr = self.observe()
        return ActResult(ok=True, activity=scr.activity, next=scr.clickable,
                         event_ids=[], checkpoint=None, executed=True,
                         error=None, permission=None, package=scr.package,
                         xml_path=scr.xml_path)

    # -- OCR（只在 XML 看不到文字时才用）-----------------------------------
    def _engine(self):
        if DirectDevice._ocr_engine is None:
            import logging
            logging.getLogger("RapidOCR").setLevel(logging.WARNING)
            from rapidocr import RapidOCR
            DirectDevice._ocr_engine = RapidOCR()
        return DirectDevice._ocr_engine

    def ocr(self, image: str | None = None) -> list[dict[str, Any]]:
        """OCR 读屏幕文字。

        用于 XML dump 看不到文字的场景：WebView / 自绘控件 / 图片里的字。
        能用 XML 就别用它 —— XML 有 rid 能精确点，OCR 只有像素坐标。
        """
        img = image
        if not img:
            outdir = self.evidence_dir / f"ocr{self._n + 1:02d}"
            outdir.mkdir(parents=True, exist_ok=True)
            img = str(outdir / "shot.png")
            if not self._screenshot(img):
                return []   # 截不到图就没得 OCR —— 返回空，由上层按"没识别到字"处理

        out = self._engine()(str(img))
        res: list[dict[str, Any]] = []
        boxes = getattr(out, "boxes", None)
        txts = getattr(out, "txts", None)
        scores = getattr(out, "scores", None)
        if boxes is not None and txts is not None:
            for box, text, score in zip(boxes, txts,
                                        scores if scores is not None else []):
                pts = [[int(p[0]), int(p[1])] for p in box]
                xs = [p[0] for p in pts]
                ys = [p[1] for p in pts]
                res.append({
                    "text": str(text),
                    "center": [(min(xs) + max(xs)) // 2, (min(ys) + max(ys)) // 2],
                    "bounds": [min(xs), min(ys), max(xs), max(ys)],
                    "score": float(score) if score is not None else None,
                })
        return res

    # -- 兼容老接口 ---------------------------------------------------------
    def observe_raw(self) -> dict[str, Any]:
        scr = self.observe()
        return {"ok": True, "activity": scr.activity, "package": scr.package,
                "next": scr.clickable,
                "evidence": {"png": scr.shot_path, "xml": scr.xml_path},
                "rotation": scr.rotation}

    def finding(self, status: str, event_id: str, actual: str,
                expect: str = "") -> dict[str, Any]:
        return {"status": status, "event_id": event_id, "actual": actual,
                "expect": expect}

    def finding_no_evidence(self, status: str, expect: str = "",
                            actual: str = "") -> dict[str, Any]:
        return {"status": status, "event_id": "", "actual": actual,
                "expect": expect, "no_evidence": True}

    def finish(self, status: str, summary: str) -> dict[str, Any]:
        return {"status": status, "summary": summary}

    def start_session(self, **_: Any) -> int:
        """直连模式没有会话概念，这个序号只用来拼 thread_id。"""
        self._n += 1
        return self._n

    def shell(self, command: str, timeout: int = 60) -> str:
        """跑一条 shell 命令 —— **经 uiautomator2 转发**，不自己起 adb。

        仅用于前提准备/恢复（清数据、拉起 App、改设置），**不用于测试动作**。
        测试动作一律走 act / input_text / key / swipe，那些有坐标校验、
        有落点复核、有账本记录，走 shell 就把这些保障全绕过去了。

        ⚠️ 不许换成 `adbutils.adb.device().shell()`：
          那是另一条通道，且不带超时（底层 @retry 无限重试，实测能卡 20 分钟）。
          u2 的 `d.shell()` 是同一条通道但能包超时，用户定的规矩也是走 u2。

        ⚠️ 这里**不是** "允许调 adb" 的例外口子。真需要 adb logcat
          就单独说明，别拿 shell 塞任意命令进来。
        """
        return self._call_with_timeout(
            lambda: self._d.shell(str(command)),
            timeout=float(timeout), label=f"shell {str(command)[:40]}")

    def connected(self) -> tuple[bool, str]:
        try:
            cur = self._app_current()
            w, h = self._window_size()
            return True, f"已连接：{w}x{h} {cur.get('package')}/{cur.get('activity')}"
        except Exception as e:  # noqa: BLE001
            return False, f"设备不可用：{e}"
