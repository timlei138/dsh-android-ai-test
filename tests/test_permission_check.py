# -*- coding: utf-8 -*-
"""权限判据：只有两条规则，都来自用户原话。

用户原话（判归属）：
  「你要区分APP弹窗和系统权限弹窗。系统权限弹窗的包名是固定的。
    APP弹窗的包名就是当前APP自己。APP自己的弹窗不缺分是啥弹窗。
    都是功能的一部分。」

用户原话（判什么）：
  「我们测试是测试APP。这个流程是 点击拍照导入按钮后 日历需要相加权限。
    同意了才能打开相机。只要打开就行了。哪怕打开后相机APP权限。
    那不是我们这个case关心得。」

用户原话（App 弹窗怎么处理）：
  「我们得弹窗都是我们自己包名。是流程得一部分。你不必太在意我们的弹窗。
    只要不影响主流程那就不用特意管它。直接顺手点了就行」

所以：
  ① 归属**只看包名**，不看文案（文案法会把「相机正在尝试使用相机」误判）
  ② 判**授权结果**，不判"弹过框"
  ③ App 自己的弹窗 = 流程的一部分，顺手点掉，不需要特别判定
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "assets" / "tools"))
sys.stdout.reconfigure(encoding="utf-8")

PASS = FAIL = 0


def ck(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}" + (f" -> {extra}" if extra else ""))
    else:
        FAIL += 1
        print(f"  [FAIL] {name}" + (f" -> {extra}" if extra else ""))


import perm

print("=" * 78)
print("1) ★ 归属只看包名（用户规则）")
print("=" * 78)
for pkg, exp in (("com.android.permissioncontroller", True),
                 ("com.google.android.permissioncontroller", True),
                 ("com.android.packageinstaller", True),
                 ("com.zui.calendar", False),
                 ("com.zui.camera", False),
                 ("", False)):
    ck(f"系统权限框？{pkg or '(空)'} -> {exp}",
       perm.is_system_perm_package(pkg) is exp)

print()
print("=" * 78)
print("2) ★ 解析 dumpsys 拿授权状态（判结果，不判弹窗）")
print("=" * 78)
DUMPSYS = """\
  requested permissions:
    android.permission.CAMERA
    android.permission.RECORD_AUDIO
  runtime permissions:
    android.permission.CAMERA: granted=true, flags=[USER_SET]
    android.permission.READ_MEDIA_IMAGES: granted=true, flags=[USER_SET]
    android.permission.READ_MEDIA_VISUAL_USER_SELECTED: granted=false
"""
g = perm.parse_granted_permissions(DUMPSYS)
ck("CAMERA 已授予", g.get("android.permission.CAMERA") is True)
ck("READ_MEDIA_IMAGES 已授予",
   g.get("android.permission.READ_MEDIA_IMAGES") is True)
ck("部分照片 未授予",
   g.get("android.permission.READ_MEDIA_VISUAL_USER_SELECTED") is False)
ck("★ 只声明未授予的音视频不进结果（不把声明当授予）",
   "android.permission.RECORD_AUDIO" not in g, str(g))

print()
print("=" * 78)
print("3) ★ 断言用授权状态判（169 的 a1/a2）")
print("=" * 78)
import android_agent.agent.nodes as ca


def st(granted=None):
    s = {"app_package": "com.zui.calendar",
         "last_activity": "", "obs_clickable": [], "obs_nodes_text": "",
         "last_package": "", "seen_rids": [], "runtime_permission_prompts": [],
         "runtime_permissions": []}
    if granted is not None:
        s["granted_permissions"] = granted
    return s


_s = st({"android.permission.CAMERA": True,
         "android.permission.READ_MEDIA_IMAGES": True})
ck("a1: CAMERA 已授予 -> ✔",
   ca.evaluate_check({"permission": {"CAMERA": True}}, state=_s)[0] is True)
ck("a2: READ_MEDIA_IMAGES 已授予 -> ✔",
   ca.evaluate_check({"permission": {"READ_MEDIA_IMAGES": True}},
                     state=_s)[0] is True)

_s2 = st({"android.permission.CAMERA": False})
ck("CAMERA 未授予 -> 要求 true 时 ✘",
   ca.evaluate_check({"permission": {"CAMERA": True}}, state=_s2)[0] is False)
ck("CAMERA 未授予 -> 要求 false 时 ✔",
   ca.evaluate_check({"permission": {"CAMERA": False}}, state=_s2)[0] is True)

print()
print("=" * 78)
print("4) ★ 查不到就判不了，绝不猜成立")
print("=" * 78)
ok, why = ca.evaluate_check({"permission": {"CAMERA": True}}, state=st())
ck("没查过 -> None（交模型）", ok is None, why)
ck("说明了原因", "没有查到" in why, why)

print()
print("=" * 78)
print("5) ★ 相机 App 自己的权限框不参与判定")
print("=" * 78)
# 即使留痕里出现相机 App 的权限框，判据也不该看它 —— 只看被测 App 的授予状态
_s3 = st({"android.permission.CAMERA": False})
_s3["runtime_permission_prompts"] = [{"step": 0, "prompt": "相机正在尝试使用相机"}]
_s3["runtime_permissions"] = [{"owner_package": "com.zui.camera"}]
ck("★ 被测 App 的 CAMERA 仍未授予 -> 要求 true 时 ✘（不被相机框带偏）",
   ca.evaluate_check({"permission": {"CAMERA": True}}, state=_s3)[0] is False)

print()
print("=" * 78)
print("6) ★ 不再有文案映射表（那是错的判法）")
print("=" * 78)
import io

src = io.open(str(Path(__file__).resolve().parents[1] / "src" / "android_agent" / "agent" / "nodes.py"), encoding="utf-8").read()
perm_src = io.open(str(Path(__file__).resolve().parents[1] / "assets" / "tools" / "perm.py"),
                   encoding="utf-8").read()
ck("★ _PERM_WORDS 已删除", "_PERM_WORDS" not in src and "_PERM_WORDS" not in perm_src)
ck("改成用包名判", "SYSTEM_PERM_PACKAGES" in perm_src)
ck("is_system_perm_package 在工具层", "def is_system_perm_package" in perm_src)
# ⚠️ 反向断言：child_agent **不能** import perm。
# perm.py 在 assets/tools 下、不在 sys.path 里，那样 import 会让
# 全部 32 个测试崩掉（真踩过一次）。归属判断复用 tools_bridge 的。
_code_lines = [ln for ln in src.splitlines() if not ln.lstrip().startswith("#")]
ck("★ child_agent 不 import perm（否则全线崩）",
   not any("from perm import" in ln or ln.strip().startswith("import perm")
           for ln in _code_lines))
ck("child_agent 用 tools_bridge 的判归属",
   "is_system_permission_pkg" in src)

print()
print("=" * 78)
print("7) App 自己的弹窗 = 流程的一部分（技能卡要这么说）")
print("=" * 78)
base = Path.home() / ".agents" / "skills"
runner = io.open(base / "android-case-runner" / "SKILL.md", encoding="utf-8").read()
planner = io.open(base / "android-case-planner" / "SKILL.md", encoding="utf-8").read()

ck("runner 说 App 弹窗是流程的一部分",
   "流程的一部分" in runner)
ck("runner 说顺手点掉/不必特别在意",
   "顺手" in runner or "不必" in runner)
ck("★ runner 归属按包名（不是文案）",
   "包名" in runner and "固定" in runner)
ck("★ planner 归属按包名", "包名" in planner)

print()
print("=" * 78)
print(f"  {FAIL} 失败 / {PASS + FAIL} 项")
print("=" * 78)
sys.exit(1 if FAIL else 0)
