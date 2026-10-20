"""设备工具层桥接。

PoC 阶段使用 MockDevice：不碰真机、不写 storage/evidence、不产生真实会话记录。
阶段二只需把 MockDevice 换成 RealDevice（子进程调用现有 tools/*.py），
图本身一行都不用改 —— 这就是这层存在的意义。

对应关系（真机层 -> 本层）：
    observe.py   -> observe()
    act.py       -> act()
    session.py   -> finding() / finish()
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol


# --------------------------------------------------------------------------
# 数据契约：字段名刻意对齐现有 tools 的返回，方便阶段二平滑替换
# --------------------------------------------------------------------------

@dataclass
class ObserveResult:
    """对应 observe.py 的返回。"""
    activity: str
    nodes: list[dict[str, Any]]      # nav 模式：短键名 i/t/r/c/s/off
    screenshot: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"activity": self.activity, "nodes": self.nodes,
                "screenshot": self.screenshot}


# --------------------------------------------------------------------------
# 弹窗分类：按**包名**区分系统权限弹窗 vs App 弹窗
# --------------------------------------------------------------------------

# Android 运行时权限 UI 的固定包名 —— 与具体 App 无关，所以可靠。
SYSTEM_PERM_PACKAGES = (
    "com.android.permissioncontroller",          # AOSP / 多数国产 ROM
    "com.google.android.permissioncontroller",   # GMS 设备
    "com.android.packageinstaller",              # 老版本 ROM
)


def is_system_permission_pkg(pkg: str | None) -> bool:
    """前台包名是不是**系统权限框**。

    ⚠️ 为什么必须按包名、不能按 activity：
    权限框是**浮层**，浮在 App 上时被测 App 的 activity 根本没变
    （真机实测：点了「从图库导入」后 activity 仍是 TimetableActivity，
    而系统框已经弹出来了）。所以"按 activity 认权限框"必然漏判。

    ⚠️ 为什么不能按 rid 认：
    App 自绘的权限说明页用的是 `button1`/`button2` 这种通用 rid，
    真机上和系统框的 `permission_allow_button_two` 完全不是一回事，
    早先我把 button1 当成系统框按钮，直接导致用例判错。

    判据来自用户：**系统权限弹窗的包名是固定的，App 弹窗的包名就是 App 自己。**
    """
    p = (pkg or "").strip().lower()
    if not p:
        return False
    return any(p == m or p.startswith(m + "/") or m in p
               for m in SYSTEM_PERM_PACKAGES)


def classify_dialog(pkg: str | None, app_pkg: str) -> str:
    """前台包名归类：system_perm / app / other。

    ⚠️ 「App 自己的弹窗」不做进一步区分 —— 用户明确说
    "APP自己的弹窗不区分是啥弹窗，都是功能的一部分"。
    所以它们和普通页面同类处理，代码不给它们开小灶。
    """
    if is_system_permission_pkg(pkg):
        return "system_perm"
    if (pkg or "").strip().lower() == (app_pkg or "").strip().lower():
        return "app"
    return "other"


@dataclass
class ActResult:
    """对应 act.py 的返回。

    next  : 精简可点清单，供 AI 从里面挑下一个目标
    event_ids : 证据事件 ID，finding 必须绑定它
    permission : **运行时权限弹窗的自动处理结果**（act.py 内置，默认 grant）

    ⚠️ permission 必须接出来，不能丢。
    工具层每次动作后都会自动调 perm.handle()：系统权限框约 6 秒不点就自动消失，
    所以"点不点、点多快"必须由工具层负责，不能等模型判断（慢）。
    但**结果要留痕** —— 自动点"允许"是不可逆的现场变更，
    事后必须能回答"这条用例到底授予了哪几个权限"。
    实测一次动作就授予了 5 个（通知/读日历/读联系人/设备信息/写删日历）。
    """
    ok: bool
    activity: str
    next: list[dict[str, Any]] = field(default_factory=list)
    event_ids: list[str] = field(default_factory=list)
    checkpoint: dict[str, Any] | None = None
    executed: bool = True
    error: str | None = None
    permission: dict[str, Any] | None = None
    package: str = ""          # 动作后的前台包名 —— 用来区分系统框/App
    # 动作后重新观察并落盘的 XML 路径（不是内容）。强证据判据要从它查。
    xml_path: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "activity": self.activity, "next": self.next,
                "event_ids": self.event_ids, "checkpoint": self.checkpoint,
                "executed": self.executed, "error": self.error,
                "permission": self.permission, "package": self.package}


class Device(Protocol):
    """设备层协议。Mock 与 Real 都实现它。"""
    def observe(self) -> ObserveResult: ...
    def act(self, x: int, y: int, why: str, expect: str | None = None,
            perm_action: str | None = None,
            perm: str | None = None) -> ActResult: ...
    def finding(self, status: str, event_id: str,
                expect: str, actual: str) -> dict[str, Any]: ...
    def finding_no_evidence(self, status: str,
                            expect: str, actual: str) -> dict[str, Any]: ...
    def finish(self, status: str, summary: str) -> dict[str, Any]: ...
    def perm_intent(self, action: str, perm: str = "") -> dict[str, Any]: ...
    def perm_intent_clear(self) -> dict[str, Any]: ...
    def state_perm(self, action: str, package: str,
                   perm: str) -> dict[str, Any]: ...


# --------------------------------------------------------------------------
# Mock 设备：一条「日历 -> 课程表 -> 空态」的最小场景
# --------------------------------------------------------------------------

# 场景节点表：activity + 锚点。用来模拟"认页面"。
_SCENES: dict[str, dict[str, Any]] = {
    "home": {
        "activity": "com.zui.calendar/.MainActivity",
        "nodes": [
            {"i": 0, "t": "菜单", "r": "btnMenu", "c": [2697, 371]},
            {"i": 1, "t": "今天", "r": "btnToday", "c": [540, 2100]},
        ],
    },
    "menu": {
        "activity": "com.zui.calendar/.MainActivity",
        "nodes": [
            {"i": 0, "t": "课程表", "r": "menuTimetable", "c": [2300, 900]},
            {"i": 1, "t": "设置", "r": "menuSettings", "c": [2300, 1100]},
        ],
    },
    "timetable_empty": {
        "activity": "com.zui.calendar/.timetable.display.TimetableActivity",
        "nodes": [
            {"i": 0, "t": "从图库导入", "r": "btnImportFromGallery", "c": [1520, 1521]},
            {"i": 1, "t": "手动创建", "r": "btnCreateManually", "c": [1520, 1717]},
        ],
    },
    "edit_schedule": {
        "activity": "com.zui.calendar/.timetable.edit.EditTimetableActivity",
        "nodes": [
            {"i": 0, "t": "课程表名称", "r": "et_schedule_name", "c": [1520, 375]},
            {"i": 1, "t": "完成", "r": "btnDone", "c": [2600, 371]},
        ],
    },
}

# 边的定义：(当前节点, 被点击的 rid) -> 目标节点
_EDGES: dict[tuple[str, str], str] = {
    ("home", "btnMenu"): "menu",
    ("menu", "menuTimetable"): "timetable_empty",
    ("timetable_empty", "btnCreateManually"): "edit_schedule",
}


class MockDevice:
    """可复现的假设备。

    关键：act() 会推进一个内部事件序号，因此 event_ids 与真实工具一样是
    递增且唯一的 —— 这样才能真正验证 finding 的证据绑定。
    """

    def __init__(self, serial: str = "MOCK-001") -> None:
        self.serial = serial
        self.scene = "home"
        self._seq = 0
        self.crashed = False
        self.acts: list[dict[str, Any]] = []   # 执行审计：验证"没有重复点击"
        self.findings: list[dict[str, Any]] = []
        self.finished: dict[str, Any] | None = None
        # 权限分支：声明与预置都记账，供测试断言"双分支真的按顺序跑了"
        self.perm_intents: list[dict[str, Any]] = []
        self.perm_states: list[dict[str, Any]] = []

    # -- 内部 ---------------------------------------------------------------
    def _next_event_id(self) -> str:
        self._seq += 1
        return f"evt-{self._seq:04d}"

    def _scene(self) -> dict[str, Any]:
        return _SCENES[self.scene]

    # -- Device 协议 --------------------------------------------------------
    def observe(self) -> ObserveResult:
        s = self._scene()
        return ObserveResult(activity=s["activity"],
                             nodes=[dict(n) for n in s["nodes"]],
                             screenshot=f"mock/{self.scene}.png")

    def act(self, x: int, y: int, why: str,
            expect: str | None = None,
            perm_action: str | None = None,
            perm: str | None = None) -> ActResult:
        cur = self._scene()
        # 按坐标找到被点的节点（真实层由 dump 现场派生坐标，这里反查）
        hit = None
        for n in cur["nodes"]:
            if n["c"] == [x, y]:
                hit = n
                break

        if hit is None:
            # 参数/定位类错误：确认没碰设备 -> 可以重试
            return ActResult(ok=False, activity=cur["activity"],
                             executed=False,
                             error=f"坐标 ({x},{y}) 未命中任何节点")

        self.acts.append({"scene": self.scene, "rid": hit["r"], "why": why})

        nxt = _EDGES.get((self.scene, hit["r"]))
        cp = None
        if expect:
            if nxt is not None:
                exp_activity = _SCENES[nxt]["activity"]
                ok = exp_activity.endswith(expect.split("=", 1)[-1])
                cp = {"checked": True, "ok": ok, "missed": [] if ok else [expect]}
            else:
                cp = {"checked": True, "ok": False, "missed": [expect]}

        if nxt is not None:
            self.scene = nxt

        return ActResult(ok=True, activity=self._scene()["activity"],
                         next=[dict(n) for n in self._scene()["nodes"]],
                         event_ids=[self._next_event_id()],
                         checkpoint=cp)

    def finding(self, status: str, event_id: str,
                expect: str, actual: str) -> dict[str, Any]:
        rec = {"status": status, "event_id": event_id,
               "expect": expect, "actual": actual}
        self.findings.append(rec)
        return {"ok": True, "finding": rec}

    def finding_no_evidence(self, status: str, expect: str,
                            actual: str) -> dict[str, Any]:
        return self.finding(status, "", expect, actual)

    # -- 权限分支控制（Mock：只记账，不碰真设备）----------------------------
    def perm_intent(self, action: str, perm: str = "") -> dict[str, Any]:
        self.perm_intents.append({"action": action, "perm": perm})
        return {"ok": True, "action": action, "perm": perm}

    def perm_intent_clear(self) -> dict[str, Any]:
        self.perm_intents.append({"action": "clear", "perm": ""})
        return {"ok": True, "cleared": True}

    def state_perm(self, action: str, package: str,
                   perm: str) -> dict[str, Any]:
        self.perm_states.append({"action": action, "package": package,
                                 "perm": perm})
        return {"ok": True, "cmd": action, "package": package, "perm": perm}

    def finish(self, status: str, summary: str) -> dict[str, Any]:
        self.finished = {"status": status, "summary": summary,
                         "findings": len(self.findings)}
        return {"ok": True, **self.finished}


# --------------------------------------------------------------------------
# 真机层：调用现有 tools/run.py，解析其结构化 JSON 输出
# --------------------------------------------------------------------------

class ToolError(RuntimeError):
    """工具层报错。区分"是否可能已碰设备"—— 这决定能不能重试。"""

    def __init__(self, message: str, *, executed: bool, returncode: int,
                 payload: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.executed = executed          # True = 可能已碰设备，禁止重放
        self.returncode = returncode
        self.payload = payload or {}


class RealDevice:
    """真实设备桥接：subprocess 调 run.py，解析 JSON。

    对接的是**现有 skill 的工具层**，不复制它的逻辑。
    注意 run.py 会写调用账本；observe/act 要求会话已存在（session start）。

    关键约束（来自 SKILL.md，硬性）：
      - 退出码 3 = 会话已因崩溃终止 -> 禁止重试，只能收尾
      - executed=False（device untouched）才允许重试参数
      - 坐标必须来自【本次】返回的 next，不复用旧坐标
    """

    def __init__(self, skill_root: str, serial: str | None = None,
                 python: str | None = None, timeout: int = 180) -> None:
        self.skill_root = Path(skill_root)
        # ★ 入口校验：skill_root 必须是一个**真实存在**的目录。
        #
        # 为什么必须在这里拦（实测踩过）：
        #   路径不存在时，错误会在**很深的 subprocess 层**才爆出来：
        #       NotADirectoryError: [WinError 267] 目录名称无效
        #   这个报错完全看不出"是 skill_root 传错了"—— 看起来像
        #   子进程/run.py 的问题。在这里拦，能把"层深处的诡异 OS 错误"
        #   变成"一句话说清哪错了"。
        if not self.skill_root.is_dir():
            hint = ""
            if str(self.skill_root) == "" or self.skill_root == Path("."):
                hint = ("\n  （传入的是空串 —— 传空不会报错，Path('') 会变成 '.'，"
                        "于是 cwd 落在当前目录、run.py 找不到，"
                        "表现为「找不到 run.py」而不是路径错误）")
            elif self.skill_root.name == ".venv":
                hint = ("\n  ★ 这是解释器目录，不是资产目录。"
                        "资产在 <repo>/assets，交给 paths.skill_root() 解析。")
            raise ValueError(
                f"skill_root 不是有效目录：{self.skill_root!r}\n"
                f"  它必须指向资产目录（含 tools/run.py、knowledge/、"
                f"storage/vision.json）。{hint}")
        self.serial = serial
        self.timeout = timeout
        # 解释器**不在**资产目录下（资产在 assets/，.venv 在工作区根），
        # 所以交给 assets_path 统一解析 —— 自己拼 skill_root/.venv 会找不到。
        if python:
            self.python = python
        else:
            try:
                from ..cases.paths import runner_python
                self.python = runner_python()
            except Exception:  # noqa: BLE001
                self.python = sys.executable
        self.run_py = self.skill_root / "tools" / "run.py"
        # 目录存在但 run.py 不在 —— 同样是"传错目录"，早点说清
        if not self.run_py.is_file():
            raise ValueError(
                f"skill_root 下没有 tools/run.py：{self.skill_root!r}\n"
                f"  这不像是资产目录。正确的那个含 tools/run.py、knowledge/、"
                f"storage/vision.json（通常是 langgraph-poc\\assets）。")

    # -- 底层调用 -----------------------------------------------------------
    def _run(self, tool: str, *args: str) -> dict[str, Any]:
        """调 run.py <tool> <args...>，返回解析后的 JSON。

        失败时抛 ToolError，并带上"是否可能已执行"这一关键信息。
        """
        cmd = [self.python, str(self.run_py), tool, *args]
        env = {**os.environ, "PYTHONIOENCODING": "utf-8",
               "ANDROID_TEST_VERBOSE": "1"}
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  encoding="utf-8", errors="replace",
                                  timeout=self.timeout, cwd=str(self.skill_root),
                                  env=env)
        except subprocess.TimeoutExpired as exc:
            # 超时最危险：动作可能已执行但没记账 -> 按"可能已执行"保守处理
            raise ToolError(f"{tool} 超时({self.timeout}s)：动作可能已执行，禁止重放",
                            executed=True, returncode=-1) from exc

        payload: dict[str, Any] | None = None
        try:
            payload = json.loads(proc.stdout) if proc.stdout.strip() else None
        except json.JSONDecodeError:
            payload = None

        rc = proc.returncode
        # 退出码 3：崩溃导致的会话终态，硬拦截，绝不能重试
        if rc == 3 or (payload or {}).get("terminated"):
            reason = (payload or {}).get("reason") or proc.stderr.strip()
            raise ToolError(f"会话已终止（崩溃硬拦截）：{reason}",
                            executed=True, returncode=3, payload=payload)

        # 判据是"有没有碰过设备"，不是"有没有报错"
        if isinstance(payload, dict) and payload.get("ok") is True:
            return payload

        state = (payload or {}).get("execution_state")
        untouched = state == "not_executed" or (payload or {}).get("executed") is False
        if rc == 2 and payload is None:
            untouched = True      # argparse 用法错误：入口即拒绝

        msg = ((payload or {}).get("error") or proc.stderr.strip()
               or f"{tool} 退出码 {rc}")
        raise ToolError(f"{tool} 失败：{msg}", executed=not untouched,
                        returncode=rc, payload=payload)

    def _serial(self) -> list[str]:
        return ["--serial", self.serial] if self.serial else []

    # -- Device 协议 --------------------------------------------------------
    def start_session(self, title: str, case: str | None = None,
                      package: str | None = None,
                      user_input: str | None = None,
                      diag: bool = False) -> int:
        """开一个真实会话，返回 session_id。

        ⚠️ 工具层要求 **同时** 给 title 与 user_input（或改用 --case）。
        只给 --title 会被拒绝（session.py L444 的校验），尽管 --help 没说清。
        这里默认把 title 复用作 user_input，避免踩这个坑。

        diag=True 标记为验证/调试会话：不进「测试记录」库、不计入仪表盘成功率。
        """
        args = ["start", "--title", title,
                "--input", user_input or title]
        if case:
            args += ["--case", str(case)]
        if package:
            args += ["--package", package]
        if diag:
            args += ["--diag"]
        out = self._run("session", *args)
        sid = out.get("session_id")
        if sid is None:
            raise ToolError(f"session start 未返回 session_id：{out}",
                            executed=False, returncode=0, payload=out)
        self.session_id = int(sid)
        return self.session_id

    def observe(self) -> ObserveResult:
        out = self._run("observe", *self._serial())
        return ObserveResult(
            activity=str(out.get("activity") or ""),
            nodes=out.get("next") or [],          # 用精简可点清单
            screenshot=(out.get("evidence") or {}).get("png"),
        )

    def observe_raw(self) -> dict[str, Any]:
        """给子 Agent 用：要完整字段（evidence 路径、event_ids、next）。

        observe() 是给图用的瘦接口；子 Agent 需要原始返回，
        但**只取当轮要用的字段**，不把整个 payload 塞进 state。
        """
        return self._run("observe", *self._serial())

    def act(self, x: int, y: int, why: str,
            expect: str | None = None,
            perm_action: str | None = None,
            perm: str | None = None) -> ActResult:
        # act 只按坐标点击；坐标必须来自本次 observe/act 的 next[].c
        args = ["tap", "--x", str(x), "--y", str(y), "--why", why]
        if expect:
            args += ["--expect", expect]
        # 权限意图必须在**动作之前**声明，否则本次动作触发的弹窗接不住
        # （act.py 的 --perm-action 就是干这个的）
        if perm_action:
            args += ["--perm-action", perm_action]
            if perm:
                args += ["--perm", perm]
        out = self._run("act", *self._serial(), *args)
        return ActResult(
            ok=bool(out.get("ok")),
            activity=str(out.get("activity") or ""),
            next=out.get("next") or [],
            event_ids=[str(e) for e in (out.get("event_ids") or [])],
            checkpoint=out.get("checkpoint"),
            executed=out.get("execution_state") != "not_executed",
            error=out.get("error"),
            # 运行时权限弹窗的自动处理结果 —— 工具层已经点完了，这里只是取回来留痕
            permission=out.get("permission"),
            # 动作后的前台包名：区分"系统权限框"和"App 自己的界面/弹窗"
            package=str(out.get("package") or ""),
        )

    def key(self, name: str,
            why: str = "",
            expect: str | None = None,
            perm_action: str | None = None,
            perm: str | None = None) -> ActResult:
        """系统键动作（back / home）。

        为什么需要单独一个方法（会话 #84 实测的坑）：
        用例 169 的 s3 第一动作是「从相机按返回」，YAML 写的是 `back: true`。
        但回放层只会 tap —— `back` 被解析层静默丢掉，执行器于是从相机页
        直接去找 btnImportFromGallery。找不到 → 落回模型 → 模型在相机里
        反复点 intent_back_button（那是「查看」不是返回）→ 白烧 4 分钟。

        act.py 本来就支持 `--action back/home`，这里只是把它接出来。
        """
        allowed = {"back", "home"}
        name = (name or "").strip().lower()
        if name not in allowed:
            raise ToolError(
                f"不支持的系统键动作：{name!r}（仅支持 {sorted(allowed)}）",
                executed=False, returncode=2)
        args = ["--action", name, "--why", why or f"按 {name}"]
        if expect:
            args += ["--expect", expect]
        if perm_action:
            args += ["--perm-action", perm_action]
            if perm:
                args += ["--perm", perm]
        out = self._run("act", *self._serial(), *args)
        return ActResult(
            ok=bool(out.get("ok")),
            activity=str(out.get("activity") or ""),
            next=out.get("next") or [],
            event_ids=[str(e) for e in (out.get("event_ids") or [])],
            checkpoint=out.get("checkpoint"),
            executed=out.get("execution_state") != "not_executed",
            error=out.get("error"),
            permission=out.get("permission"),
            package=str(out.get("package") or ""),
        )

    def finding(self, status: str, event_id: str,
                expect: str, actual: str) -> dict[str, Any]:
        return self._run("session", "finding", "--status", status,
                         "--event-id", event_id,
                         "--expect", expect, "--actual", actual)

    def finding_no_evidence(self, status: str, expect: str,
                            actual: str) -> dict[str, Any]:
        """不带证据 ID 的 finding。

        session.py 的 --event-id 是 append 且默认 []，所以**整个参数省略是合法的**。
        早先传 "(none)" 会被 argparse 按 int 解析而报错，导致 finding 写不进去、
        finish 也跟着失败（"FAIL requires a FAIL finding"）。
        """
        return self._run("session", "finding", "--status", status,
                         "--expect", expect, "--actual", actual)

    def finish(self, status: str, summary: str) -> dict[str, Any]:
        return self._run("session", "finish", "--status", status,
                         "--summary", summary)

    # -- 权限分支控制 -------------------------------------------------------
    # 默认是 grant（工具层自动点「允许」，见 act.py 的 perm.handle）。
    # 要测**拒绝分支**才需要显式声明 deny —— 对应 SKILL
    # 「默认同意，需要拒绝时显式声明」。

    def perm_intent(self, action: str, perm: str = "") -> dict[str, Any]:
        """声明权限意图（会话级持续有效，直到 clear）。

        action: "grant" / "deny"；perm 可留空（对整个弹窗生效）。
        """
        args = ["perm-intent", "--action", action]
        if perm:
            args += ["--perm", perm]
        return self._run("session", *args)

    def perm_intent_clear(self) -> dict[str, Any]:
        """清掉 deny 意图，回到默认同意。

        ⚠️ 双分支第二轮必须清 —— 否则默认 grant 会被残留的 deny 覆盖，
        第二轮的"允许"分支实际点的是拒绝（SKILL 明确警告过）。
        """
        return self._run("session", "perm-intent", "--clear")

    def state_perm(self, action: str, package: str, perm: str) -> dict[str, Any]:
        """预置权限状态：action="grant"/"revoke"。

        ⚠️ 双分支第二轮必须先 revoke：权限已授予时系统**不会再弹窗**，
        不撤销就会卡住（等一个永远不出现的框）。

        state.py 会回读 dumpsys 校验，不轻信空输出 —— 这里直接透传结果。
        """
        return self._run("state", action, "--package", package, "--perm", perm)

    # -- 辅助 ---------------------------------------------------------------
    def shell(self, command: str, timeout: int = 60) -> str:
        """直接在设备上跑一条 shell 命令（不经 run.py）。

        ⚠️ 用途仅限**前提准备与恢复**（pm clear / svc wifi disable 等），
        这类操作不属于"测试动作"，不进会话证据台账，所以不写 act 事件。
        测试动作（点击/滑动）必须走 act，不能走这里。

        前提分两类：
          data 类（pm clear）——清了没问题，测试数据是执行者自己造的
          env  类（关 WiFi）——**用完必须恢复**，否则污染下一个 case

        ★ 走 uiautomator2 的 shell，**不自己起 adb 进程**。
          用户定的规矩：所有设备操作都经 uiautomator2，只有 adb logcat 例外。
          自己拼 `["adb", "shell", ...]` 等于维护第二条通道：
          行为不一致、没有统一超时、出问题要查两处。
        """
        return self._u2().shell(command)

    def _u2(self):
        """lazily 拿一个 uiautomator2 客户端（前提准备/恢复专用）。

        ## 为什么要单独有一个，而不是复用别的

        `RealDevice` 的主链路是 subprocess 调 `run.py`（那是工具层的事）。
        但前提准备（pm clear 之类）不该再起一个 subprocess —— 那样就是
        **第二条 adb 通道**，正是用户明确禁止的。

        这里直接持有 u2 对象，前提准备走 u2 的 shell。
        只在真正用到时才连（lazily）—— 绝大多数用例不需要它，
        不该为"可能用不到的前提"先付一次连接代价。
        """
        d = getattr(self, "_u2_client", None)
        if d is not None:
            return d
        try:
            import uiautomator2 as u2
        except ImportError as exc:  # pragma: no cover
            raise ToolError(
                f"前提准备需要 uiautomator2，但没装：{exc}。"
                f"设备操作一律走 u2（用户定的规矩），装到 assets 那个 venv 里。",
                executed=False) from exc
        try:
            d = u2.connect(self.serial) if self.serial else u2.connect()
        except Exception as exc:  # noqa: BLE001
            raise ToolError(f"前提准备连不上设备：{exc}", executed=False) from exc
        self._u2_client = d
        return d

    def ocr(self, region: str | None = None) -> list[dict[str, Any]]:
        """OCR 读屏幕文字，返回 [{"text", "center", "bounds", "score"}, …]。

        ## 什么时候该用

        当 `observe` 的 XML dump **看不到**需要的信息时：
          · WebView / Flutter / 自绘控件 —— dump 里只有个大壳子，没有文字
          · 图片、图标里的文字
          · 被 Toast / 浮层挡住的内容

        这时 dump 是"空的"，但屏幕上明明有字。OCR 就是补这个缺口。

        ## 什么时候不该用

        能用 dump 就用 dump —— XML 有 rid，能精确点；OCR 只有像素坐标，
        点下去要按坐标，比按 rid 脆弱。所以这是**兜底手段**，不是首选。

        ⚠️ 只读，不改变设备状态。但它会**现场截图**（走 read 事件），
        所以别在同一轮里反复刷 —— 一次截图 + 一次推理约 10 秒。
        """
        args = list(self._serial())
        if region:
            # 形如 x1,y1,x2,y2（截图坐标系，即设备像素）
            args += ["--region", region]
        payload = self._run("read", *args)

        out: list[dict[str, Any]] = []
        for it in (payload.get("lines") or []):
            if not isinstance(it, dict):
                continue
            text = str(it.get("text") or "").strip()
            if not text:
                continue
            out.append({
                "text": text,
                # center 是像素中心点，可直接用于按坐标点击
                "center": it.get("center") or [],
                "bounds": it.get("bounds") or [],
                "norm": it.get("norm") or [],       # 归一化坐标（换分辨率也能用）
                "score": it.get("score"),
            })
        return out

    def connected(self) -> tuple[bool, str]:
        """检查设备是否连上。返回 (是否可用, 说明)。

        ★ 用 u2 探活，**不再 `adb devices`**。

        为什么不跑 `adb devices`：
          ① 规矩：设备操作一律走 uiautomator2，只有 adb logcat 例外。
          ② `adb devices` 能列出设备**不代表 u2 能用** —— 设备可能已授权
             但 uiautomator 服务没起来。真要用设备的是 u2，
             那就直接用 u2 探活，验的是"我接下来要用的那条路"通不通。
        """
        try:
            d = self._u2()
            w, h = d.window_size()
            return True, f"已连接：{w}x{h}"
        except ToolError as exc:
            return False, str(exc)
        except Exception as exc:  # noqa: BLE001
            return False, f"设备不可用：{exc}"


if __name__ == "__main__":
    # 自检：Mock 设备能推进场景，且 event_ids 唯一
    d = MockDevice()
    print(json.dumps(d.observe().to_dict(), ensure_ascii=False, indent=2))
    r = d.act(2697, 371, why="打开菜单", expect="activity=MainActivity")
    print(json.dumps(r.to_dict(), ensure_ascii=False, indent=2))
