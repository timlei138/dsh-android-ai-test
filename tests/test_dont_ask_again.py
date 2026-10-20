"""验证：第二次申请权限时多了「拒绝并不再询问」，deny 绝不能点到它。

用户指出的场景（真机）：
  1. 第一次申请「访问您设备上的照片」（相机/图册）权限
  2. 第二次**再申请**时，Android 的弹窗多一个按钮：**「拒绝并不再询问」**
  3. 一旦点了它 → 设了 don't-ask-again → **后续"授予"分支永远不再弹窗**

这就是 SKILL 里那条硬规矩的由来：
  "deny **只点「拒绝」**，绝不点「拒绝并不再询问」"

危险在哪：deny 模式下要选「拒绝」，而屏幕上同时有
「拒绝」和「拒绝并不再询问」——**前者是需求的，后者是毒药**，
按文案"包含拒绝"来匹配就会点错。

perm.py 的做法是白名单（_DENY_TEXTS 精确相等）+ 黑名单（_EXCLUDE_*）。
这里把这条契约钉成回归测试，因为**一旦破了，是静默污染设备状态**——
测试看起来还在跑，但后续所有授予分支都失效了。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))  # noqa: E402
from android_agent.cases.paths import skill_root  # noqa: E402
SKILL = skill_root()
sys.path.insert(0, str(Path(SKILL) / "tools"))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except AttributeError:
    pass

import perm

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  -> {detail}" if detail else ""))


def btn(text, rid=""):
    return {"text": text, "rid": rid, "center": [100, 200]}


PKG = "com.android.permissioncontroller:id/"

print("=== 1. 第二次申请的弹窗：两个「拒绝」并存 ===")
# 真实形态：拒绝 / 拒绝并不再询问
second_time = [
    btn("不允许", PKG + "permission_deny_button"),
    btn("拒绝并不再询问", PKG + "permission_deny_and_dont_ask_again_button"),
]
picked = perm.match_button(second_time, "deny")
check("deny 选中了「不允许」", picked is not None and picked["text"] == "不允许",
      str(picked and picked["text"]))
check("绝不选中「拒绝并不再询问」",
      picked is not None and "不再询问" not in picked["text"],
      str(picked and picked["rid"]))

print("\n=== 2. 三个按钮的完整形态 ===")
full = [
    btn("允许", PKG + "permission_allow_button"),
    btn("拒绝", PKG + "permission_deny_button"),
    btn("拒绝并不再询问", PKG + "permission_deny_and_dont_ask_again_button"),
]
for action in ("deny", "grant"):
    p = perm.match_button(full, action)
    ok = p is not None and "不再询问" not in p["text"]
    check(f"{action} 模式下不碰「不再询问」", ok, str(p and p["text"]))

print("\n=== 3. 即使文案变体也不点 ===")
variants = [
    btn("拒绝且不再询问", PKG + "permission_deny_and_dont_ask_again_button"),
    btn("拒绝并不再询问"),
]
for v in variants:
    p = perm.match_button([btn("拒绝", PKG + "permission_deny_button"), v], "deny")
    check(f"变体「{v['text']}」被排除",
          p is not None and p["text"] == "拒绝", str(p and p["text"]))

print("\n=== 4. 只有「不再询问」时 -> 返回 None（交回 AI，不瞎点）===")
only_dont_ask = [btn("拒绝并不再询问",
                     PKG + "permission_deny_and_dont_ask_again_button")]
p = perm.match_button(only_dont_ask, "deny")
check("无安全候选时返回 None", p is None, str(p))

print("\n=== 5. 「允许访问所选照片」也不能被 grant 点到 ===")
# 这是"只给部分照片"，语义不同于"允许全部"，SKILL 明令不自动点
sel = [btn("允许访问所选照片", PKG + "permission_allow_selected_button")]
p = perm.match_button(sel, "grant")
check("不自动点「允许访问所选照片」", p is None, str(p))

print("\n=== 6. deny 不会误点 grant 按钮 ===")
p = perm.match_button(full, "deny")
check("deny 拿到的是拒绝类", p is not None and p["text"] != "允许",
      str(p and p["text"]))

print("\n" + "=" * 46)
print(f"  全部通过（{len(PASS)} 项）" if not FAIL
      else f"  {len(FAIL)} 项失败: {FAIL}")
print("=" * 46)
raise SystemExit(0 if not FAIL else 1)
