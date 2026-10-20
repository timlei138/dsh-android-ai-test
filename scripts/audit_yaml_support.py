"""
用例 YAML → 执行器：字段支持度盘点。

YAML 比 CaseSpec 丰富得多，**不能假装全支持** ——
不认识的字段必须**显式报错**，绝不能静默忽略
（两者对不上时不会报错，只会静默跑偏）。

这个脚本把真实 YAML 拆开，逐字段标：支持 / 部分 / 不支持。
"""
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
import yaml

# 走统一入口，不写死路径 —— 写死的路径会静默变成空目录
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from android_agent.cases.paths import cases_dir  # noqa: E402

CASES = cases_dir()

# 我们现在真正能执行的
SUPPORTED = {
    "meta.case_id", "meta.title", "meta.package",
    "steps[].id", "steps[].desc",
    "steps[].expect.activity", "steps[].expect.activity_changed_to",
    "steps[].do[].tap.rid", "steps[].do[].tap.text",
    "assertions[].id", "assertions[].desc", "assertions[].check.activity",
}
# 部分支持（能读但语义不完全一致）
PARTIAL = {
    "steps[].on_fail": "只当字符串记日志，不做分支",
    "steps[].expect.dialog_present": "读得到，但判据没实现",
    "assertions[].check.node_present": "读得到，但判据没实现",
    "assertions[].check.package": "读得到，但判据没实现",
}
# 完全不支持（必须报错，不能静默丢）
UNSUPPORTED = {
    "preconditions[].type": "goto/clean/prepare/assert 四类未实现",
    "preconditions[].skip_if": "跳过条件未实现（会导致重复 pm clear）",
    "preconditions[].verified_by": "证据链未接",
    "steps[].know": "知识卡内联未接",
    "steps[].pitfalls[].from/quote_hash": "引用锚未接（kb_check 用）",
    "assertions[].check.node_text.equals": "文本相等判据未实现",
    "assertions[].check.after_tap": "动作后判据未实现",
    "confidence": "置信度未接",
    "playbook": "执行须知未渲染给模型",
}


def walk(o, prefix=""):
    """列出所有叶子路径。"""
    if isinstance(o, dict):
        for k, v in o.items():
            yield from walk(v, f"{prefix}.{k}" if prefix else k)
    elif isinstance(o, list):
        for item in o:
            yield from walk(item, f"{prefix}[]")
    else:
        yield prefix


print("=" * 66)
print("  用例 YAML：我们的执行器支持到哪一步")
print("=" * 66)

files = sorted(CASES.glob("*.yaml"))
print(f"\n共 {len(files)} 个用例文件\n")

allkeys = {}
for f in files:
    d = yaml.safe_load(f.read_text(encoding="utf-8"))
    ks = set(walk(d))
    for k in ks:
        allkeys.setdefault(k, []).append(f.stem)

print(f"字段总数（去重后）: {len(allkeys)}\n")

sup = [k for k in allkeys if k in SUPPORTED]
par = [k for k in allkeys if k in PARTIAL]
uns = [k for k in allkeys if k not in SUPPORTED and k not in PARTIAL]

print(f"── 完全支持 ({len(sup)}) ──")
for k in sorted(sup):
    print(f"   ✔ {k}")

print(f"\n── 部分支持 ({len(par)}) ──")
for k in sorted(par):
    print(f"   ~ {k}")
    print(f"       {PARTIAL[k]}")

print(f"\n── 不支持 ({len(uns)}) ──")
# 按顶层归类，避免刷屏
groups = {}
for k in uns:
    top = k.split(".")[0].split("[")[0]
    groups.setdefault(top, 0)
    groups[top] += 1
for top, n in sorted(groups.items(), key=lambda x: -x[1]):
    hint = UNSUPPORTED.get(top, "")
    print(f"   �’ {top:22} {n:>3} 个字段   {hint}")

print("\n" + "=" * 66)
print(f"  支持 {len(sup)} / 部分 {len(par)} / 不支持 {len(uns)}")
print("=" * 66)
