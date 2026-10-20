# -*- coding: utf-8 -*-
"""YAML 回写测试。

用户要求：「用我们的工具完成后要生成或者把旧的YAML不合理的修改。」

必须锁住的行为：
  1. 实际路径 == YAML 声明 -> **一个字都不改**（否则 diff 全是噪音）
  2. 有差异 -> 只改 `do:` 块，**注释/坑/pitfalls 全部保留**
  3. 坐标绝不写进 YAML（换设备必失效）
  4. 判定标准（expect / assertions）绝不碰 —— 那是用户写的需求
  5. 回写失败不能影响用例结论
"""
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
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


import android_agent.report.writeback as ywb
from android_agent.cases.loader import load_case

ORIG = """\
# 顶部注释必须保留
meta:
  case_id: 7
  title: 测试用例
  package: com.zui.calendar

preconditions:
  - desc: 清理数据
    type: clean
    do: pm clear com.zui.calendar

steps:
  - id: s1
    desc: 进课程表页
    # 这一步的行内注释
    target: rid=iv_more
    landing: activity=.timetable.display.TimetableActivity
    know:
      where: 首页 -> 课程表
    pitfalls:
      - "iv_more 没有 text/desc，必须用 rid"

  - id: s2
    desc: 拍照导入
    target: rid=btnImportFromPhoto
    pitfalls:
      - "点完直接进相机，没有权限弹窗"

assertions:
  - id: a1
    desc: 应有权限弹窗
    check:
      dialog_present: {permission_dialog: "CAMERA"}
"""


def make_file(text: str) -> Path:
    d = Path(tempfile.mkdtemp())
    p = d / "case.yaml"
    p.write_text(text, encoding="utf-8")
    return p


print("=" * 72)
print("1) 实际路径与 YAML 一致 -> 不改")
print("=" * 72)
p = make_file(ORIG)
spec, _ = load_case(p)
# ★ 新机制：一步一个 target。s1 只有 iv_more，
#   所以"一致"就是实际也只点了 iv_more。
#   早先这里写 [{"rid": "iv_more"}, {"text": "课程表"}] 两个动作 ——
#   那是 locate_seq 时代的"一步连点两下"，现在该拆成两步。
same_trace = [
    {"step": "s1", "by": "code", "actions": [{"rid": "iv_more"}]},
    {"step": "s2", "by": "code", "actions": [{"rid": "btnImportFromPhoto"}]},
]
ch = ywb.diff_steps(spec, same_trace)
ck("一致时无差异", ch == [], str(ch))
before = p.read_text(encoding="utf-8")
res = ywb.writeback(p, ch)
ck("一致时文件不变", p.read_text(encoding="utf-8") == before)
ck("changed 为空", res["changed"] == [], str(res["changed"]))

print()
print("=" * 72)
print("2) 有差异 -> 改 target，其余原文保留")
print("=" * 72)
p = make_file(ORIG)
spec, _ = load_case(p)
diff_trace = [
    # s1: 实际不是按 iv_more 点的，改用了文案「更多」
    {"step": "s1", "by": "code", "actions": [{"text": "更多"}]},
    # s2: 实际是按键动作
    {"step": "s2", "by": "code", "actions": [{"key": "back"}]},
]
ch = ywb.diff_steps(spec, diff_trace)
ck("检出 2 处差异", len(ch) == 2, str([c["step"] for c in ch]))
ck("s1 差异的 actual 正确",
   [c for c in ch if c["step"] == "s1"][0]["actual"] == ["text=更多"])

res = ywb.writeback(p, ch)
after = p.read_text(encoding="utf-8")
ck("写了 2 步", len(res["changed"]) == 2, str(res["changed"]))

ck("★ 顶部注释保留", "# 顶部注释必须保留" in after)
ck("★ pitfalls 保留", "iv_more 没有 text/desc，必须用 rid" in after)
ck("★ 行内注释保留", "# 这一步的行内注释" in after)
ck("★ landing 未被改动", "activity=.timetable.display.TimetableActivity" in after)
ck("★ assertions 未被改动", "dialog_present" in after)
ck("s1 的新 target 写入", "target: text=更多" in after)
ck("s2 写成 target: key=back", "target: key=back" in after)
ck("旧的 btnImportFromPhoto 已被替换",
   "btnImportFromPhoto" not in after.split("assertions")[0])

# 改完还得能被重新解析（不能改出坏 YAML）
spec2, w2 = load_case(p)
ck("改后仍可解析", spec2 is not None)
ck("改后 s2 的 target 是 key=back",
   spec2.steps[1].target == "key=back", str(spec2.steps[1].target))

print()
print("=" * 72)
print("3) 坐标绝不写进 YAML")
print("=" * 72)
ck("纯坐标 -> 无从表达",
   ywb._actual_locate({"x": 100, "y": 200}) is None)
p = make_file(ORIG)
spec, _ = load_case(p)
ch = ywb.diff_steps(spec, [{"step": "s1", "by": "code", "actions": [{}, {"x": 1, "y": 2}]}])
ck("全是坐标时不算差异（没法回写）", ch == [], str(ch))

print()
print("=" * 72)
print("4) 没跑过的步骤不动")
print("=" * 72)
p = make_file(ORIG)
spec, _ = load_case(p)
ch = ywb.diff_steps(spec, [{"step": "s1", "by": "code", "actions": [{"rid": "iv_more"},
                                                     {"text": "课程表"}]}])
ck("只跑 s1 时 s2 不参与", all(c["step"] == "s1" for c in ch), str(ch))

print()
print("=" * 72)
print("5) ★★ 模型的点击不回写（真机 168 踩到的）")
print("=" * 72)
# 现象：168 跑完 PASS，写回却把 s1 的 iv_more 改成了 rid=button1 ——
#      那个 button1 是 s0 点「同意」的按钮，模型在 s1 里又点了一次。
# 规则：只有代码强证据直点的动作才配回写。模型的是猜测，不是经验。
p = make_file(ORIG)
spec, _ = load_case(p)
ch = ywb.diff_steps(spec, [{"step": "s1", "by": "model",
                            "actions": [{"rid": "button1"}]}])
ck("★ 模型点的 -> 不回写", ch == [], str(ch))
ch = ywb.diff_steps(spec, [{"step": "s1", "actions": [{"rid": "button1"}]}])
ck("★ 缺 by 字段 -> 按模型处理，不回写", ch == [], str(ch))
ch = ywb.diff_steps(spec, [{"step": "s1", "by": "code",
                            "actions": [{"rid": "button1"}]}])
ck("代码直点的才回写", len(ch) == 1, str(ch))

print()
print("=" * 72)
print("6) 渲染给人看的变更行")
print("=" * 72)
lines = ywb.render_change_lines({"step": "s3",
                                 "declared": ["rid=old"],
                                 "actual": ["key=back"]})
ck("渲染出两行", len(lines) == 2, str(lines))
ck("含原写", "rid=old" in lines[0], lines[0])
ck("含实际", "key=back" in lines[1], lines[1])

print()
print("=" * 72)
print(f"  {FAIL} 失败 / {PASS + FAIL} 项")
print("=" * 72)
sys.exit(1 if FAIL else 0)
