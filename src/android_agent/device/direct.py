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
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .bridge import ActResult


def _adb_exe() -> str:
    """adb 可执行文件路径。

    优先环境变量（换机器/CI 不用改代码），其次 PATH，最后退回 Windows 常见安装位置。
    """
    import os

    env = os.environ.get("ANDROID_ADB")
    if env and Path(env).exists():
        return env
    found = shutil.which("adb")
    if found:
        return found
    fallback = Path(r"D:\AndroidSdk\platform-tools\adb.exe")
    return str(fallback) if fallback.exists() else "adb"


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
                 evidence_dir: str | Path | None = None) -> None:
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

    # -- 观察 ---------------------------------------------------------------
    def _app_current(self) -> dict:
        """当前前台应用。**绝不许把整条用例卡死在这里。**

        ⚠️ 为什么不能直接用 `u2.app_current()`：

        uiautomator2 的 app_current 走 adbutils 的 `shell()`，而那个函数
        挂着 `@retry`（`retry.api`，IndexError/BrokenPipeError 无限重试）。
        实测踩到：一次 adb socket 异常后，`close (adbutils/_adb.py:98)`
        → `shell (adbutils/_device_base.py:262)` → `app_current` 无限转圈，
        进程 CPU 一直涨、日志一个字没有、用例卡死 20 分钟无从判断。

        这里改成**直接发一条带超时的 adb 命令**：拿不到就返回空 dict，
        让上层按"不知道当前在哪"处理（落点判不了 → 交给模型），
        而不是让整个执行器停摆。
        """
        # 两条命令都试：不同 Android 版本、不同 ROM 给出的字段不一样。
        # 实测这台（TB323FU / Android 15）`dumpsys window windows` 里**没有**
        # mCurrentFocus，只有 `dumpsys activity activities` 有 topResumedActivity。
        for cmd, pat in (
            (["dumpsys", "activity", "activities"],
             r"(?:topResumedActivity|mResumedActivity)\s*=\s*ActivityRecord\{\S+\s+\S+\s+"
             r"(?P<pkg>[^\s/]+)/(?P<act>[^\s,}]+)"),
            (["dumpsys", "window", "windows"],
             r"mCurrentFocus=Window\{\S+\s+\S+\s+(?P<pkg>[^\s/]+)/(?P<act>[^\s}]+)\}"),
        ):
            try:
                out = self._shell_text(cmd, timeout=8)
            except Exception:  # noqa: BLE001  拿不到就换下一条，不许升级成卡死
                continue
            m = re.search(pat, out)
            if m:
                return {"package": m.group("pkg").strip(),
                        "activity": m.group("act").strip()}
        return {}

    def _shell_text(self, cmd: list[str], *, timeout: float = 8.0) -> str:
        """执行一条 adb shell 命令，**硬超时**。超时就抛，由调用方决定怎么降级。

        直接走 `adb -s <serial> shell ...`，不经过 adbutils/uiautomator2 ——
        那两层要么无限重试、要么没有超时参数，都不适合"必须能在有限时间内返回"的场景。
        """
        exe = _adb_exe()
        args = [exe, "-s", self.serial, "shell", *cmd] if self.serial else \
               [exe, "shell", *cmd]
        proc = subprocess.run(args, capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=timeout)
        return proc.stdout or ""

    def _window_size(self) -> tuple[int, int]:
        """屏幕宽高。拿不到返回 (0, 0)，绝不允许卡住。

        与 _app_current 同样的理由：`u2.window_size()` 底层是 adbutils 的
        `shell()`，带无限重试装饰器。包一层 try/except 挡不住它 ——
        因为异常永远不会被抛出，它只是不停地重试。
        """
        try:
            out = self._shell_text(["wm", "size"], timeout=6)
        except Exception:  # noqa: BLE001
            return 0, 0
        m = re.search(r"Override size:\s*(\d+)x(\d+)", out) or \
            re.search(r"Physical size:\s*(\d+)x(\d+)", out)
        if not m:
            return 0, 0
        return int(m.group(1)), int(m.group(2))

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
        xml = self._d.dump_hierarchy()
        xml_path.write_text(xml, encoding="utf-8")

        cur = self._app_current() or {}
        try:
            w, h = self._window_size()
        except Exception:  # noqa: BLE001
            w = h = 0

        shot: str | None = None
        if with_shot:
            try:
                self._d.screenshot(str(shot_path))
                shot = str(shot_path)
            except Exception:  # noqa: BLE001  截图失败不该中断执行
                shot = None

        clickable, rotation = parse_clickable(xml_path, fg_package=cur.get("package", ""))

        meta = {
            "activity": cur.get("activity"), "package": cur.get("package"),
            "width": w, "height": h, "rotation": rotation,
            "clickable_count": len(clickable),
            "elapsed_ms": round((time.time() - t0) * 1000),
        }
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                             encoding="utf-8")

        return Screen(xml_path=str(xml_path), activity=cur.get("activity", ""),
                      package=cur.get("package", ""), width=w, height=h,
                      rotation=rotation, shot_path=shot, clickable=clickable,
                      ts=time.time())

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
            **_: Any) -> ActResult:
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

        self._d.click(int(x), int(y))

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

        scr = self.observe()
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
            perm: str | None = None, **_: Any) -> ActResult:
        """系统键（back / home）。

        为什么也要接权限处理：按返回键时系统可能弹「确定要退出吗」，
        某些 OEM 的权限框按返回会变成"仅本次允许"选择页。
        和点击一样，处置窗口只有几秒。
        """
        self._d.press(name)
        perm_result = None
        if self._perm is not None:
            try:
                perm_result = self._perm.handle(
                    self._d, perm_action, owner_package=self._perm_owner())
            except Exception as exc:  # noqa: BLE001
                perm_result = {"error": str(exc)[:200]}
        scr = self.observe()
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
            self._d.screenshot(img)

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
        """跑一条 adb shell 命令（仅用于前提准备/恢复，不用于测试动作）。"""
        import adbutils
        return adbutils.adb.device(self.serial).shell(command)

    def connected(self) -> tuple[bool, str]:
        try:
            cur = self._app_current()
            w, h = self._window_size()
            return True, f"已连接：{w}x{h} {cur.get('package')}/{cur.get('activity')}"
        except Exception as e:  # noqa: BLE001
            return False, f"设备不可用：{e}"
