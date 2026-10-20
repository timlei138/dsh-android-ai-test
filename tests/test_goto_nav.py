"""goto 前提（type: goto）必须是"交给子 Agent 走的起点要求"，不能被丢弃。

背景（为什么有这个测试）：
    goto 前提说的是"要摆好的状态"，判断现在在不在、怎么走过去，
    **本来就是 Agent 的活** —— 开场三步：读前提 → 定位 → 决定起点。

    但本执行器曾写成 `if ptype == "goto": return None` —— 直接丢掉。
    后果（2026-10 实测 8 个用例）：
      · 没有任何步骤把 App 带到用例要求的起点
      · 起点断言必然不符 → 模型现场乱找 → 连点无效 → 卡死终止
      · 8 条里 6 条 BLOCKED，而报告只写"暂未实现分类处理"，
        看起来像功能缺失，实际根因是**起点从未被建立**

    所以这里锁三件事：
      1. goto 要变成 kind="nav" 的前提（不是消失）
      2. 它的 target 要能转成**代码可判定**的起点判据
      3. 已在起点时 skip_if 成立 -> 不白走一遍
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.stdout.reconfigure(encoding="utf-8")

import android_agent.cases.loader as case_yaml  # noqa: E402
import android_agent.agent.nodes as ca  # noqa: E402

CASES = Path(__file__).resolve().parents[1] / "assets" / "storage" / "cases"
ok = fail = 0


def check(name: str, cond: bool, got: object = "") -> None:
    global ok, fail
    if cond:
        ok += 1
        print(f"  [PASS] {name}")
    else:
        fail += 1
        print(f"  [FAIL] {name}  -> {got}")


print("=== 1) 带 goto 的用例真的产出了 nav 前提 ===")
# ⚠️ 早先遍历 assets/storage/cases/*.yaml 统计"至少 8 个 goto"。
# 但用例 YAML 会被**主动清理**（含旧坐标/locate_seq 的会删掉重生成），
# 目录随时可能为空 —— 那样这个测试就变成"看运气过"，
# 而且它其实想测的是**解析逻辑**，不是"仓库里有几个用例"。
# 改成喂一份内置样例：逻辑对不对与仓库现状无关，才不会假过也不会假挂。
_SAMPLE = """
meta:
  case_id: sample_goto
  title: goto 解析样例
  package: com.demo
preconditions:
  - type: goto
    desc: 进课程表
    do: 点 iv_more 再点 课程表
    target:
      activity: .timetable.display.TimetableActivity
  - type: goto
    desc: 无需导航（已在首页）
    do: 无
    target:
      activity: .AllInOneActivity
steps:
  - id: s1
    desc: 点导入
    target: rid=btnImport
    landing: activity=.Picker
assertions:
  - id: a1
    desc: 到选择器
    check: activity=.Picker
"""

tmp_sample = Path(__file__).resolve().parents[1] / "tmp" / "_goto_sample.yaml"
tmp_sample.parent.mkdir(parents=True, exist_ok=True)
tmp_sample.write_text(_SAMPLE, encoding="utf-8")

spec, _ = case_yaml.load_case(tmp_sample)
navs = [pc for pc in spec.preconditions if pc.kind == "nav"]
total_nav = len(navs)
check("goto 被接住（不是消失）", total_nav == 2, [pc.kind for pc in spec.preconditions])
check("两条 goto 都变成 nav",
      all(pc.kind == "nav" for pc in spec.preconditions), total_nav)

print()
print("=== 2) nav 的起点判据必须是代码能判的（不能用自然语言猜） ===")
for pc in navs:
    if not pc.assert_start:
        continue
    det, why = ca.is_deterministic_landing(pc.assert_start)
    check(f"判据可判定 {pc.assert_start}", det, why)

print()
print("=== 3) nav 必须带上前人记的路径提示（不是空壳） ===")
for pc in navs:
    check("有 hint", bool(pc.hint), (pc.hint or "")[:40])
    check("有 target", bool(pc.target), pc.target)

print()
print("=== 4) 已在起点 -> skip_if 成立（不白走）；不在 -> 该走 ===")
sk = {"activity": ".timetable.display.TimetableActivity"}
r1 = ca._skip_if_met(sk, activity=".timetable.display.TimetableActivity", obs={})
r2 = ca._skip_if_met(sk, activity=".AllInOneActivity", obs={})
check("已在课程表 -> 跳过", r1 is True, r1)
check("在首页 -> 不跳过", r2 is False, r2)

print()
print("=== 5) 中文说明文字不能被当 shell 命令执行 ===")
# 实测 case 182：prepare.do 是一段操作描述，被扔进 shell 后报 "shell 失败(127)"
prose = [
    "① 点 `btnCreateManually`（c=[1520,1717]）进 `EditTimetableActivity`；",
    "本身就依赖课表是空的。若课表已排满，需先腾出空位。",
]
cmds = [
    "pm clear com.zui.calendar",
    "settings put global airplane_mode_on 1",
    "am force-stop com.zui.calendar",
]
for s in prose:
    check(f"说明文字识别正确：{s[:20]}", ca._looks_like_prose(s), s[:30])
for s in cmds:
    check(f"真命令不被误判：{s[:24]}", not ca._looks_like_prose(s), s)

print()
print("=" * 52)
if fail:
    print(f"  {fail} 项失败（通过 {ok}）")
    sys.exit(1)
print(f"  全部通过（{ok} 项）")
print("=" * 52)
