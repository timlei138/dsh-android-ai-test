# -*- coding: utf-8 -*-
"""验证 package= 判据（本次新增）与「判据未生效要进报告」。

背景：169 的 s2/s3 写了
    expect:
      package: com.zui.camera
意思是"这一刻前台该是相机"。但加载器把它当"无法表达"丢弃了 ——
那条预期从来没验过，而且没人知道（warning 只打 stdout）。

按用户原则「能完全确定的让代码判断」，包名**是唯一确定的**，必须交给代码。

注：原第 4/5 段（加载器转 package= / yaml_unused_fields 进报告）随 YAML 整体删除。
"""
import sys
from pathlib import Path

# 仓库根（tests/ 的上一级）与 src/ 都要进 path：
#   src/  给 android_agent 包
#   根    给 run_case_json 入口脚本（它不是包，就在根目录）
_REPO = Path(__file__).resolve().parents[1]
for _p in (str(_REPO / "src"), str(_REPO)):
    if _p not in sys.path:
        sys.path.insert(0, _p)
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


import android_agent.agent.nodes as ca

print("=" * 74)
print("1) package= 是确定判据")
print("=" * 74)
ok, why = ca.is_deterministic_landing("package=com.zui.camera")
ck("package 归为确定判据", ok, why)
ok, why = ca.is_deterministic_landing("package=com.x,activity=.Y")
ck("package+activity -> 确定", ok, why)
ok, why = ca.is_deterministic_landing("package=com.x,text=课程")
ck("package+text -> 交 AI", not ok, why)

print()
print("=" * 74)
print("2) ★ 用相等匹配，不是子串（否则 com.zui 会误中 com.zui.calendar）")
print("=" * 74)
hit, _ = ca.match_landing("package=com.zui.camera",
                          state_pkg="com.zui.camera")
ck("包名完全一致 -> 命中", hit)

hit, _ = ca.match_landing("package=com.zui", state_pkg="com.zui.calendar")
ck("★ 短名前缀不算命中（相等语义）", not hit)

hit, _ = ca.match_landing("package=com.zui.camera",
                          state_pkg="com.zui.calendar")
ck("不同包 -> 不命中", not hit)

hit, _ = ca.match_landing("package=com.zui.camera", state_pkg="")
ck("前台包名为空 -> 不命中（不猜）", not hit)

print()
print("=" * 74)
print("3) package 与 activity 组合")
print("=" * 74)
hit, _ = ca.match_landing("package=com.zui.camera,activity=.CaptureActivity",
                          state_pkg="com.zui.camera",
                          activity="com.zui.camera/.CaptureActivity")
ck("AND 都满足 -> 命中", hit)

hit, _ = ca.match_landing("package=com.zui.camera,activity=.CaptureActivity",
                          state_pkg="com.zui.calendar",
                          activity="com.zui.camera/.CaptureActivity")
ck("AND 其一不满足 -> 不命中", not hit)


print()
print("=" * 74)
print(f"  {FAIL} 失败 / {PASS + FAIL} 项")
print("=" * 74)
sys.exit(1 if FAIL else 0)