# -*- coding: utf-8 -*-
"""系统权限弹窗：自动处置 + 两类权限分开判。

用户指出的核心差异：
    「普通运行权限不给可能某个功能会有影响。
      相机、图片这种，同意了才能进行下一步。」

所以这里分两层验：
  1. perm.py 的匹配逻辑（哪类按钮能点、哪类**绝不能**点）
  2. DirectDevice 真的把它接上了（曾经换成 in-process 后整段丢了）
  3. 内容型 / 普通型的分流判法
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.stdout.reconfigure(encoding="utf-8")

PASS = 0
FAIL = 0


def ck(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}" + (f" -> {extra}" if extra else ""))
    else:
        FAIL += 1
        print(f"  [FAIL] {name}" + (f" -> {extra}" if extra else ""))


from android_agent.device.direct import _load_perm  # noqa: E402
from android_agent.agent.nodes import _perm_kind, _perm_is_granted  # noqa: E402

print("=" * 68)
print("1) perm 模块能被加载（接线的前提）")
print("=" * 68)
pm = _load_perm()
ck("加载成功", pm is not None, getattr(pm, "__file__", "None"))
if pm is None:
    print("\n无法继续：perm.py 没找到")
    sys.exit(1)

print()
print("=" * 68)
print("2) 按钮匹配：能点谁、绝不能点谁")
print("=" * 68)
R = "com.android.permissioncontroller:id/"


def b(text, rid):
    return {"text": text, "rid": R + rid, "center": [1, 1]}


# grant 的优先级：全量 > 前台 > 基本
btns = [b("仅在使用中允许", "permission_allow_foreground_only_button"),
        b("全部允许", "permission_allow_all_button"),
        b("拒绝", "permission_deny_button")]
ck("grant 优先选「全部允许」",
   (pm.match_button(btns, "grant") or {}).get("text") == "全部允许")

btns2 = [b("仅在使用中允许", "permission_allow_foreground_only_button"),
         b("允许", "permission_allow_button")]
ck("没有全量时选前台",
   (pm.match_button(btns2, "grant") or {}).get("text") == "仅在使用中允许")

# ★ 绝不能点：设了 don't-ask-again，后续"授予"分支永远不再弹窗
btns3 = [b("拒绝并不再询问", "permission_deny_and_dont_ask_again_button"),
         b("全部允许", "permission_allow_all_button")]
got = (pm.match_button(btns3, "grant") or {}).get("text")
ck("★ grant 不点「拒绝并不再询问」", got == "全部允许", str(got))

btns4 = [b("拒绝并不再询问", "permission_deny_and_dont_ask_again_button")]
ck("★ 只剩 don't-ask-again 时交回 AI（返回 None）",
   pm.match_button(btns4, "deny") is None)

# 部分授权不算 grant
btns5 = [b("允许访问所选照片", "permission_allow_selected_button"),
         b("全部允许", "permission_allow_all_button")]
ck("★ grant 不选「允许访问所选照片」，选全部允许",
   (pm.match_button(btns5, "grant") or {}).get("text") == "全部允许")

# 前往设置：会把 App 带离当前流程
btns6 = [b("前往设置", ""), b("取消", "")]
ck("不自动点「前往设置」", pm.match_button(btns6, "grant") is None)

print()
print("=" * 68)
print("3) 归属：只看包名，不看文案")
print("=" * 68)
ck("permissioncontroller 是系统框",
   pm.is_system_perm_package("com.android.permissioncontroller"))
ck("被测 App 不是系统框",
   not pm.is_system_perm_package("com.zui.calendar"))
ck("★ 名字里带 permission 的普通包也不算系统框",
   not pm.is_system_perm_package("com.foo.permissiondemo"))
ck("GrantPermissionsActivity 能识别为权限页",
   pm.is_permission_activity(
       "com.android.permissioncontroller.permission.ui.GrantPermissionsActivity"))

print()
print("=" * 68)
print("4) ★★ 两类权限分流（用户明确要求的区别）")
print("=" * 68)
# 内容型：不授予 = 流程走不下去
for p in ("CAMERA", "android.permission.READ_MEDIA_IMAGES",
          "RECORD_AUDIO", "ACCESS_FINE_LOCATION"):
    ck(f"{p} -> content", _perm_kind(p) == "content", _perm_kind(p))
# 普通型：不授予只是功能受限
for p in ("POST_NOTIFICATIONS", "READ_CALENDAR", "READ_PHONE_STATE"):
    ck(f"{p} -> normal", _perm_kind(p) == "normal", _perm_kind(p))

print()
print("=" * 68)
print("5) 授权核实：判不了 ≠ 已授予")
print("=" * 68)
g = {"android.permission.CAMERA": True,
     "android.permission.POST_NOTIFICATIONS": False}
ck("精确命中已授予", _perm_is_granted(g, "android.permission.CAMERA") is True)
ck("精确命中未授予",
   _perm_is_granted(g, "android.permission.POST_NOTIFICATIONS") is False)
ck("★ 模糊命中", _perm_is_granted(g, "CAMERA") is True)
ck("★ 对不上 -> None（不许当成已授予）",
   _perm_is_granted(g, "照片/媒体（相册）") is None)
ck("★ dumpsys 为空 -> None", _perm_is_granted({}, "CAMERA") is None)

print()
print("=" * 68)
print("6) DirectDevice 真的接上了权限处理")
print("=" * 68)
src = (Path(__file__).resolve().parents[1] / "src" / "android_agent"
       / "device" / "direct.py").read_text(encoding="utf-8")
ck("★ act 里调用了 perm.handle", "_perm.handle(" in src)
ck("★ key 里也调用了（返回键也可能触发权限框）",
   src.count("_perm.handle(") >= 2, f"{src.count('_perm.handle(')} 处")
ck("★ 有 __init__ 初始化 _perm", "self._perm = _load_perm()" in src)
ck("★ 归属包名在点击前取（权限框还没弹）",
   src.index("owner = self._perm_owner()") < src.index("self._d.click(int(x), int(y))"))
ck("★ 权限框已在前台时沿用上一次的申请方（多权限链）",
   "self._last_perm_owner" in src and "is_system_perm_package(pkg)" in src)
ck("★ permission 不再硬编码 None", "permission=perm_result" in src)


# 原第 7 段（权限动作不写进回写/YAML）随 YAML 回写模块整体删除。
print()
print("=" * 68)
print(f"  {FAIL} 失败 / {PASS + FAIL} 项")
print("=" * 68)
sys.exit(1 if FAIL else 0)