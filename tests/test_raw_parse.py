# -*- coding: utf-8 -*-
"""原始用例解析：把用户手写的 txt 拆成「前提 / 步骤 / 预期」。

## 为什么要专门测这个

用户的原话：「记录详情。显示原始的步骤和期待，不是 YAML。显示是反的。」

拆解器一坏，详情页就会退化成显示一大段 txt —— 而且**不会报错**，
只是不好看。所以必须用小样本把每一类真实格式钉住。

样本全部取自真实的 63 份用例，覆盖踩过的坑：
  · 168 整份是一行、没有换行
  · 15 的编号后面**没有分隔符**（`1点击…`）
  · 185 的段落标题是「操作」「验证」两个字的短标题
  · 169 的中文和标题之间没有空格
  · 176 的预期条目跨多行、带 `·` 子项
"""

import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from android_agent.cases.raw_parse import parse_raw_case  # noqa: E402

FAIL = 0
PASS = 0


def ck(name, cond, extra=""):
    global FAIL, PASS
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}  {extra}")


# ── 1. 标准格式（176）────────────────────────────────────────────────
print("\n=== 1) 标准格式：步骤和预期分开读 ===")
T176 = """测试用例 联想日历_176
前提：
确保设备没有课程表。可以清空APP数据
操作步骤
1. 日历点击页面右上角更多按钮，选择课程表
2. 点击 "手动创建课程表" 按钮
3.点击"完成"应用信息到空课程表，查看显示
预期结果
1. 打开课程表页面
2. 进入课程表基本信息编辑页面
3.标题右侧显示当前周数是第几周
"""
d = parse_raw_case(T176)
ck("读到 title", d["title"] == "联想日历_176", d["title"])
ck("读到前提", "确保设备没有课程表" in d["precondition"], d["precondition"])
ck("步骤 3 条", len(d["steps"]) == 3, str(len(d["steps"])))
ck("预期 3 条", len(d["expects"]) == 3, str(len(d["expects"])))
ck("步骤编号从 1 起", [s["n"] for s in d["steps"]] == [1, 2, 3])
ck("步骤内容对", d["steps"][2]["text"].startswith("点击"), d["steps"][2]["text"])
ck("parsed=True", d["parsed"] is True)

# ── 2. 多行子项必须并进上一条（· 开头的）──────────────────────────────
print("\n=== 2) 子项并进上一条，不丢 ===")
T_SUB = """预期结果
1. 打开页面
2. 可以调整字段：
·课程名称（必填）
·备注（非必填）
"""
d = parse_raw_case(T_SUB)
ck("预期 2 条（不是 4 条）", len(d["expects"]) == 2, str(len(d["expects"])))
ck("子项并进第 2 条", "课程名称" in d["expects"][1]["text"]
   and "备注" in d["expects"][1]["text"], d["expects"][1]["text"])

# ── 3. 整份挤在一行（168）────────────────────────────────────────────
print("\n=== 3) 单行原文也能拆 ===")
T168 = ("测试用例 联想日历_168 前提：确保设备没有课程表 "
        "操作步骤 1.日历点击更多，选择课程表 2. 点击导入按钮 3. 验证可进入选择入口 "
        "预期结果 1. 可打开课程表页面 2. 图库导入入口可点击 3. 可正常进入入口")
d = parse_raw_case(T168)
ck("单行也能读到 title", d["title"].startswith("联想日历_168"), d["title"])
ck("单行拆出 3 步", len(d["steps"]) == 3,
   str([s["text"] for s in d["steps"]]))
ck("单行拆出 3 条预期", len(d["expects"]) == 3, str(len(d["expects"])))
ck("步骤 2 内容对", "导入按钮" in d["steps"][1]["text"], d["steps"][1]["text"])

# ── 4. 编号后面没有分隔符（15）────────────────────────────────────────
print("\n=== 4) 编号后没分隔符（1点击…）===")
T15 = """测试步骤：
1点击查看新建事件页面可调起的弹框
2再点一次

预期结果：
1调起、收起无异常
2再次调起正常
"""
d = parse_raw_case(T15)
ck("认出无分隔符编号", len(d["steps"]) == 2,
   str([s["text"] for s in d["steps"]]))
ck("步骤 1 内容对", "点击查看" in d["steps"][0]["text"], d["steps"][0]["text"])

# ── 5. 短标题「操作」「验证」（185）──────────────────────────────────
print("\n=== 5) 两个字的短标题也能认 ===")
T185 = """测试用例 联想日历_185
前提
1.日历已允许各项权限
操作
1.课程表页面点击右上角菜单按钮
2.点击右上角加号按钮
验证
1.点击后打开全部课程表页面
2.点击后进入新建课程表页面
"""
d = parse_raw_case(T185)
ck("短标题拆出 2 步", len(d["steps"]) == 2,
   str([s["text"] for s in d["steps"]]))
ck("短标题拆出 2 条预期", len(d["expects"]) == 2,
   str([e["text"] for e in d["expects"]]))

# ── 6. 中文和标题之间没空格（169）────────────────────────────────────
print("\n=== 6) 中文紧贴标题也能切开 ===")
T169 = ("测试用例 联想日历_169 前提： 1.日历未授予权限 （清理APP数据 ）"
        "操作步骤 1. 点击拍照导入 2. 点击图库导入 "
        "预期结果 1.弹出权限弹窗 2.弹出权限弹窗")
d = parse_raw_case(T169)
ck("无空格也能拆出 2 步", len(d["steps"]) == 2,
   str([s["text"] for s in d["steps"]]))
ck("无空格也能拆出 2 条预期", len(d["expects"]) == 2, str(len(d["expects"])))

# ── 7. 兜底：读不懂就老实说 ──────────────────────────────────────────
print("\n=== 7) 读不懂时 parsed=False（前端退化成显示原文）===")
d = parse_raw_case("这是一段完全不成形的东西，没有任何编号。")
ck("parsed=False", d["parsed"] is False)
ck("原文照给", "不成形" in d["raw"])
d = parse_raw_case("")
ck("空串不崩", d["parsed"] is False and d["raw"] == "")

# ── 8. 编号乱写时按出现顺序重编号 ────────────────────────────────────
print("\n=== 8) 原文编号漏号时按顺序重编（否则步骤和预期对不上）===")
T_GAP = """操作步骤
1. 甲
3. 丙
预期结果
1. 一
3. 三
"""
d = parse_raw_case(T_GAP)
ck("步骤重编号为 1,2", [s["n"] for s in d["steps"]] == [1, 2],
   str([s["n"] for s in d["steps"]]))
ck("预期重编号为 1,2", [e["n"] for e in d["expects"]] == [1, 2],
   str([e["n"] for e in d["expects"]]))

# ── 9. 真实文件全量冒烟（存在目录才跑）───────────────────────────────
print("\n=== 9) 真实用例全量（63 份，都在就必须全拆出来）===")
dirs = [Path(r"D:\dsh\cal_cases"), Path(r"D:\dsh\cases")]
files = []
for dd in dirs:
    if dd.is_dir():
        files.extend(sorted(dd.glob("*.txt")))
if not files:
    print("  （跳过：这台机器上没找到真实用例目录）")
else:
    bad = []
    for f in files:
        try:
            txt = io.open(f, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        r = parse_raw_case(txt)
        if not r["parsed"] or not r["steps"]:
            bad.append(f.name)
    ck(f"真实用例全部拆出步骤（{len(files)} 份）", not bad, str(bad))

print()
print("=" * 70)
print(f"  {FAIL} 失败 / {PASS + FAIL} 项")
print("=" * 70)
sys.exit(1 if FAIL else 0)
