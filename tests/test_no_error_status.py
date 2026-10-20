# -*- coding: utf-8 -*-
"""不许再有 ERROR 这个状态。

用户定的：只有三档。
    PASS / FAIL / BLOCKED

ERROR 曾经存在，语义和 BLOCKED 重叠（都是"没执行完"），
保留它只会让报告里出现第四种说法，让人不知道该怎么看。
已经整体并入 BLOCKED：崩溃、拿不到屏幕、参数错误、进程中断 —— 全是 BLOCKED。

这个测试盯的是"别让它悄悄回来"：
  · 数据库的合法状态表里不许有 ERROR
  · 路由/结论里不许出现 "BLOCKED", "ERROR" 这种并列表述
  · 插件 schema 的 status 说明里不许出现 ERROR
  · 会话收尾（僵尸清理）的结果必须是 BLOCKED

⚠️ 不误伤：sqlite3.Error / urllib.error / {"error": ...} 这些是别的东西。
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.stdout.reconfigure(encoding="utf-8")

OK = 0
BAD = 0


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


# 只挑"状态值"意义的 ERROR：字符串字面量、并且不是 error 字段/异常类
STATUS_ERROR = re.compile(r"""(?<![.\w'"])["']ERROR["']""")


def status_errors(txt: str) -> list[str]:
    out = []
    for i, ln in enumerate(txt.splitlines(), 1):
        s = ln.strip()
        if s.startswith("#") or s.startswith("//"):
            continue
        # 排除明显的非状态用法
        if re.search(r"sqlite3?\.Error|urllib\.error|child\.on\(", ln):
            continue
        if STATUS_ERROR.search(ln):
            out.append(f"L{i}: {s[:70]}")
    return out


print("=" * 70)
print("1) ★ 数据库合法状态表里没有 ERROR")
print("=" * 70)
db = read("assets/tools/db.py")
m = re.search(r"STATUSES = \(([^)]*)\)", db)
ck("找到 STATUSES 定义", bool(m), m.group(0) if m else "")
statuses = re.findall(r'"([A-Z]+)"', m.group(1)) if m else []
ck("★ 没有 ERROR", "ERROR" not in statuses, statuses)
ck("三档齐全", all(s in statuses for s in ("PASS", "FAIL", "BLOCKED")), statuses)

print()
print("=" * 70)
print("2) ★ 僵尸会话收尾成 BLOCKED（不是 ERROR）")
print("=" * 70)
ck("UPDATE 写的是 status='BLOCKED'",
   "SET status='BLOCKED'" in db)
ck("不再写 status='ERROR'", "SET status='ERROR'" not in db)

print()
print("=" * 70)
print("3) ★ 代码里没有并列表述 BLOCKED/ERROR")
print("=" * 70)
for rel in ("src/android_agent/agent/nodes.py",
            "src/android_agent/agent/runner.py",
            "assets/tools/db.py",
            "assets/tools/common.py",
            "runner-tool/lib/index.js"):
    t = read(rel)
    ck(f"{rel} 没有 BLOCKED\"/\"ERROR 并列",
       '"BLOCKED", "ERROR"' not in t and '"ERROR", "BLOCKED"' not in t
       and "BLOCKED/ERROR" not in t)

print()
print("=" * 70)
print("4) ★ 状态值意义上不再出现 ERROR")
print("=" * 70)
for rel in ("src/android_agent/agent/nodes.py",
            "src/android_agent/agent/runner.py",
            "src/android_agent/agent/graph.py",
            "assets/tools/db.py",
            "run_case_json.py",
            "runner-tool/lib/index.js"):
    hits = status_errors(read(rel))
    ck(f"{rel} 无状态型 ERROR", not hits, hits)

print()
print("=" * 70)
print("5) ★ 插件 schema 的 status 说明只提三档")
print("=" * 70)
js = read("runner-tool/lib/index.js")
# 定位 status 块里的 description（不是 expects 那个）
status_block = js[js.find("status: {\n"):]
status_block = status_block[:status_block.find("}")] if "status: {\n" in js else ""
desc = re.search(r'description:\s*"([^"]*)"', status_block)
ck("找到 status 的说明", bool(desc), desc.group(1) if desc else status_block[:60])
ck("★ 说明里没有 ERROR", desc and "ERROR" not in desc.group(1),
   desc.group(1) if desc else "")
ck("★ 说明里点明三档", desc and all(s in desc.group(1)
                              for s in ("PASS", "FAIL", "BLOCKED")),
   desc.group(1) if desc else "")

print()
print("=" * 70)
print("6) 收尾：出异常也不许冒出 ERROR")
print("=" * 70)
# 插件里"报告缺失"时的兜底状态
ck("插件兜底状态是 BLOCKED", 'report.status ?? "BLOCKED"' in js)
ck("主 Agent 越权被拒 -> BLOCKED", 'status: "BLOCKED",\n            engine: ""' in js
   or 'status: "BLOCKED"' in js)
ck("runner 无报告兜底是 BLOCKED",
   'status="BLOCKED"' in read("src/android_agent/agent/runner.py"))

print()
print("=" * 70)
print(f"  {BAD} 失败 / {OK + BAD} 项")
print("=" * 70)
sys.exit(1 if BAD else 0)
