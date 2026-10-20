# -*- coding: utf-8 -*-
"""扫描存量用例 YAML：按「代码能不能唯一确定」分类判据。

用户定的原则（原话）：
  「能完全确定的可以让代码判断。不能完全确定的让AI来判断。
    完全确定的比如点击这个按钮跳转到一个Activity这种。就直接走代码了。
    其他有异议的还是靠AI来处理。」

所以这里不再笼统地说"缺判据"，而是分四级：

  ✓ 确定     activity= / rid=        代码判，快且准（零 token）
  ○ 仅到页面  只写了 activity，没写具体控件 —— 能判"到没到这一屏"
  ? 交 AI     text= / dialog_present / input_text 等 —— 代码判得出但判不准
  ✗ 无判据    完全没写 —— 代码无从判断，必须交 AI

同时报出加载器丢弃的字段（case_yaml 的 warning），那是**看不见的损失**。
"""
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
sys.stdout.reconfigure(encoding="utf-8")

from android_agent.cases.loader import load_case, cases_dir  # noqa: E402
import android_agent.agent.nodes as ca  # noqa: E402

d = Path(cases_dir())
files = sorted(d.glob("*.yaml"))

tot = Counter()
rows_all: list[dict] = []
warn_all: Counter = Counter()

for f in files:
    try:
        spec, warns = load_case(f)
    except Exception as exc:  # noqa: BLE001
        print(f"[{f.stem}] ⚠️ 加载失败：{exc}")
        continue

    rows = []
    for st in spec.steps:
        landing = (getattr(st, "landing", None) or "").strip()
        seq = list(getattr(st, "locate_seq", None) or [])
        one = (getattr(st, "locate", None) or "").strip()

        if not landing:
            level = "none"
            det_why = "无判据"
        else:
            det, det_why = ca.is_deterministic_landing(landing)
            if det:
                has_rid = "rid=" in landing or landing.startswith("r=")
                level = "strong" if has_rid else "activity_only"
            else:
                level = "ai"

        tot[level] += 1
        rows.append({"id": st.id, "desc": (st.desc or "")[:30],
                     "landing": landing, "level": level,
                     "why": det_why, "acts": len(seq or ([one] if one else []))})
    rows_all.append({"file": f.name, "title": spec.title, "steps": rows})

    for w in warns:
        if "expect." in w:
            import re
            m = re.search(r"expect\.(\w+)", w)
            if m:
                warn_all[m.group(1)] += 1

ICON = {"strong": "✓", "activity_only": "○", "ai": "?", "none": "✗"}
NAME = {"strong": "确定·代码判", "activity_only": "仅到页面·代码判",
        "ai": "有异议·交 AI", "none": "无判据·交 AI"}

print("=" * 80)
print("存量用例判据扫描（按「代码能否唯一确定」分类）")
print("=" * 80)

for r in rows_all:
    cnt = Counter(x["level"] for x in r["steps"])
    parts = "  ".join(f"{NAME[k]} {cnt[k]}" for k in
                      ("strong", "activity_only", "ai", "none") if cnt[k])
    print(f"\n{r['file']}")
    print(f"   {len(r['steps'])} 步：{parts}")
    for x in r["steps"]:
        if x["level"] in ("ai", "none"):
            print(f"   {ICON[x['level']]} {x['id']}: {x['desc']}")
            print(f"       判据 = {x['landing'] or '(空)'}")
            print(f"       原因 = {x['why'][:60]}")

total = sum(tot.values())
print()
print("=" * 80)
print(f"合计 {len(rows_all)} 个用例 / {total} 步")
for k in ("strong", "activity_only", "ai", "none"):
    if tot[k]:
        print(f"  {ICON[k]} {NAME[k]:<16} {tot[k]:2d} 步")
code = tot["strong"] + tot["activity_only"]
print(f"\n  → 代码能判：{code} 步（{code*100//max(total,1)}%）")
print(f"  → 需要 AI ：{tot['ai'] + tot['none']} 步"
      f"（{(tot['ai']+tot['none'])*100//max(total,1)}%）")
print("=" * 80)

if warn_all:
    print("\n加载器丢弃的判据字段（这些字写了等于没写）：")
    for k, n in warn_all.most_common():
        print(f"  {n:2d}  expect.{k}")

# 产物进 tmp/，不写仓库根 —— 根目录只放长期文件（见 README 的目录约定）。
out = Path(__file__).resolve().parents[1] / "tmp" / "_landing_scan.json"
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps({"cases": rows_all,
                           "totals": dict(tot),
                           "dropped_fields": dict(warn_all)},
                          ensure_ascii=False, indent=2), encoding="utf-8")
print(f"\n明细：{out}")
