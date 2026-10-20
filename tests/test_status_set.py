# -*- coding: utf-8 -*-
"""状态只有三档：PASS / FAIL / BLOCKED。

这个测试守住"别多出第四种状态"。
它不针对某个具体名字，而是校验**整个状态集合**就是这三档 ——
以后谁再往报告里加一个状态，这里直接红。

⚠️ 不误伤：小写 `error` 作为字段（`{"error": ...}`、
`sqlite3.Error`、`urllib.error`）是错误信息本身，与用例状态无关。
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.stdout.reconfigure(encoding="utf-8")

OK = 0
BAD = 0

# 用例状态的合法全集。只许这三个。
ALLOWED = {"PASS", "FAIL", "BLOCKED"}


def ck(name, cond, extra=""):
    global OK, BAD
    if cond:
        OK += 1
        print(f"  [PASS] {name}" + (f" -> {extra}" if extra else ""))
    else:
        BAD += 1
        print(f"  [FAIL] {name}" + (f" -> {extra}" if extra else ""))


def read(rel):
    return (ROOT / rel).read_text(encoding="utf-8-sig")


print("=" * 70)
print("1) ★ 数据库的合法状态集合就是三档")
print("=" * 70)
db = read("assets/tools/db.py")
m = re.search(r"STATUSES = \(([^)]*)\)", db)
ck("找到 STATUSES 定义", bool(m))
found = set(re.findall(r'"([A-Z_]+)"', m.group(1))) if m else set()
ck("★ 只有三档（WARN 是给 finding 用的，不算用例状态）",
   found - {"WARN"} == ALLOWED, sorted(found))
ck("没有第四种", len(found - {"WARN"}) == 3, sorted(found - {"WARN"}))

print()
print("=" * 70)
print("2) ★ 插件 schema 的 status 说明只提三档")
print("=" * 70)
js = read("runner-tool/lib/index.js")
blk = js[js.find("status: {\n"):]
blk = blk[:blk.find("}")] if "status: {\n" in js else ""
desc = re.search(r'description:\s*"([^"]*)"', blk)
d = desc.group(1) if desc else ""
ck("找到 status 的说明", bool(d), d)
mentioned = set(re.findall(r"\b(PASS|FAIL|BLOCKED|ERROR|WARN)\b", d))
ck("★ 说明里提到的状态 ⊆ 三档", mentioned <= ALLOWED, sorted(mentioned))
ck("三档都提到了", ALLOWED <= mentioned, sorted(mentioned))

print()
print("=" * 70)
print("3) ★ 代码里不出现三档之外的状态字面量")
print("=" * 70)
# 找形如 "XXXX" 的全大写状态字面量，且出现在 status/结论上下文里
SUSPECT = re.compile(r'"([A-Z]{4,})"')
for rel in ("src/android_agent/agent/nodes.py",
            "src/android_agent/agent/runner.py",
            "src/android_agent/agent/graph.py",
            "assets/tools/db.py",
            "run_case_json.py",
            "runner-tool/lib/index.js"):
    bad = []
    for i, ln in enumerate(read(rel).splitlines(), 1):
        s = ln.strip()
        if s.startswith(("#", "//")):
            continue
        if "sqlite" in ln or "urllib" in ln or ".Error" in ln or "Error(" in ln:
            continue
        for word in SUSPECT.findall(ln):
            if word in ALLOWED or word in {"WARN", "PASS", "FAIL"}:
                continue
            # 只关心像状态的值：出现在 status / report 上下文
            if re.search(r"status|report\.|finish\(", ln, re.I):
                bad.append(f"L{i}: {word}")
    ck(f"{rel} 无三档之外的状态", not bad, bad)

print()
print("=" * 70)
print("4) ★ 僵尸会话收尾成 BLOCKED")
print("=" * 70)
ck("UPDATE 写 status='BLOCKED'", "SET status='BLOCKED'" in db)

print()
print("=" * 70)
print("5) ★ 没有并列的状态表述（三档之外的两两组合）")
print("=" * 70)
# WARN 是 finding 级的评级（某条断言"大致成立但有问题"），不是用例结论，
# 所以 PASS/WARN 这种组合是正常的，不在此列。
others = sorted({"ERROR", "SKIP", "UNKNOWN", "TIMEOUT", "ABORT"} - ALLOWED)
for rel in ("src/android_agent/agent/nodes.py",
            "src/android_agent/agent/runner.py",
            "assets/tools/db.py",
            "assets/tools/common.py",
            "runner-tool/lib/index.js"):
    t = read(rel)
    hits = [f"{o} 与三档并列" for o in others
            if f'"BLOCKED", "{o}"' in t or f'"{o}", "BLOCKED"' in t
            or f"BLOCKED/{o}" in t or f"{o}/BLOCKED" in t]
    ck(f"{rel} 无并列表述", not hits, hits)

print()
print("=" * 70)
print("6) 收尾兜底状态也是三档之一")
print("=" * 70)
ck("插件兜底是 BLOCKED", 'report.status ?? "BLOCKED"' in js)
ck("runner 无报告兜底是 BLOCKED", 'status="BLOCKED"' in read(
    "src/android_agent/agent/runner.py"))

print()
print("=" * 70)
print(f"  {BAD} 失败 / {OK + BAD} 项")
print("=" * 70)
sys.exit(1 if BAD else 0)
